import html as html_lib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="DDX.cl Video Downloader API",
    description=(
        "Extrae y descarga únicamente las versiones reales alojadas en los servidores de origen "
        "(Facebook, Instagram, TikTok, X, Pinterest, Threads, LinkedIn, Reddit, YouTube, Twitch, Kick, Vimeo y Bluesky)."
    ),
    version="3.5.0",
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

FB_CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"


def get_ffmpeg_path() -> Optional[str]:
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    if imageio_ffmpeg is not None:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    return None


PLATFORM_PATTERNS = {
    "facebook": re.compile(r"(?:https?://)?(?:www\.|m\.|web\.)?(?:facebook\.com|fb\.watch|fb\.com)/.+", re.I),
    "instagram": re.compile(r"(?:https?://)?(?:www\.)?(?:instagram\.com|instagr\.am)/.+", re.I),
    "tiktok": re.compile(r"(?:https?://)?(?:www\.|vm\.|vt\.)?tiktok\.com/.+", re.I),
    "x": re.compile(r"(?:https?://)?(?:www\.|mobile\.)?(?:twitter\.com|x\.com|t\.co)/.+", re.I),
    "pinterest": re.compile(r"(?:https?://)?(?:[a-z]{2}\.|www\.)?(?:pinterest\.[a-z.]+|pin\.it)/.+", re.I),
    "threads": re.compile(r"(?:https?://)?(?:www\.)?threads\.(?:net|com)/.+", re.I),
    "linkedin": re.compile(r"(?:https?://)?(?:[a-z]{2}\.|www\.)?(?:linkedin\.com|lnkd\.in)/.+", re.I),
    "reddit": re.compile(r"(?:https?://)?(?:www\.|old\.|v\.)?(?:reddit\.com|redd\.it)/.+", re.I),
    "youtube": re.compile(r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/.+", re.I),
    "twitch": re.compile(r"(?:https?://)?(?:www\.|clips\.|m\.)?twitch\.tv/.+", re.I),
    "kick": re.compile(r"(?:https?://)?(?:www\.|clips\.)?kick\.com/.+", re.I),
    "vimeo": re.compile(r"(?:https?://)?(?:www\.|player\.)?vimeo\.com/.+", re.I),
    "bluesky": re.compile(r"(?:https?://)?(?:www\.)?bsky\.app/.+", re.I),
}

PLATFORM_LABELS = {
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "x": "X (Twitter)",
    "facebook": "Facebook",
    "pinterest": "Pinterest",
    "threads": "Threads",
    "linkedin": "LinkedIn",
    "reddit": "Reddit",
    "youtube": "YouTube / Shorts",
    "twitch": "Twitch",
    "kick": "Kick",
    "vimeo": "Vimeo",
    "bluesky": "Bluesky",
}


class UrlRequest(BaseModel):
    url: str


def detect_platform(url: str) -> Optional[str]:
    cleaned = (url or "").strip()
    if not cleaned:
        return None
    for name, pattern in PLATFORM_PATTERNS.items():
        if pattern.match(cleaned):
            return name
    try:
        with_proto = cleaned if cleaned.startswith(("http://", "https://")) else f"https://{cleaned}"
        parsed = urlparse(with_proto)
        host = (parsed.hostname or "").lower()
        if "facebook." in host or "fb." in host:
            return "facebook"
        if "instagram." in host or "instagr.am" in host:
            return "instagram"
        if "tiktok." in host:
            return "tiktok"
        if "twitter." in host or host == "x.com" or host.endswith(".x.com") or host == "t.co":
            return "x"
        if "pinterest." in host or host == "pin.it":
            return "pinterest"
        if "threads.net" in host or "threads.com" in host:
            return "threads"
        if "linkedin.com" in host or host == "lnkd.in":
            return "linkedin"
        if "reddit.com" in host or "redd.it" in host:
            return "reddit"
        if "youtube.com" in host or host == "youtu.be":
            return "youtube"
        if "twitch.tv" in host:
            return "twitch"
        if "kick.com" in host:
            return "kick"
        if "vimeo.com" in host:
            return "vimeo"
        if "bsky.app" in host:
            return "bluesky"
    except Exception:
        return None
    return None


def format_size_mb(size_bytes: Optional[float]) -> Optional[str]:
    if not size_bytes or size_bytes <= 0:
        return None
    mb = size_bytes / (1024 * 1024)
    if mb < 1:
        return f"{max(50, int(size_bytes / 1024))} KB"
    return f"~{mb:.1f} MB"


def build_exact_blocks_from_info(info: dict) -> tuple[list[dict], list[dict]]:
    """
    Inspects the actual formats returned by yt-dlp from the origin server and builds:
    1) video_options: ONLY the real resolutions actually hosted on the origin server,
       ordered from smallest resolution to largest resolution (first marked recommended & primary).
    2) audio_options: Audio extraction option (.mp3).
    """
    formats = info.get("formats") or []
    duration = info.get("duration") or 15

    by_resolution: dict[int, dict] = {}
    unlabelled_candidates: list[dict] = []

    for f in formats:
        vcodec = f.get("vcodec")
        if vcodec == "none":
            continue
        url = f.get("url")
        if not url:
            continue
        ext = (f.get("ext") or "mp4").lower()
        if ext in ("mhtml", "jpg", "png", "webp"):
            continue

        fid = str(f.get("format_id") or "best")
        w = f.get("width")
        h = f.get("height")
        if w and h:
            short_side = min(int(w), int(h))
            dim_label = f"{int(w)}x{int(h)}"
        elif h:
            short_side = int(h)
            dim_label = f"{int(h)}p"
        elif w:
            short_side = int(w)
            dim_label = f"{int(w)}p"
        elif "sd" in fid.lower():
            short_side = 480
            dim_label = "SD"
        elif "hd" in fid.lower():
            short_side = 720
            dim_label = "HD"
        else:
            short_side = 0
            dim_label = "Original"

        if 0 < short_side < 120:
            continue

        has_audio = f.get("acodec") not in (None, "none")
        is_direct_mp4 = ext == "mp4" and not str(f.get("protocol", "")).startswith("m3u8")
        tbr = float(f.get("tbr") or f.get("vbr") or 0)
        size = f.get("filesize") or f.get("filesize_approx")
        if not size and tbr > 0 and duration:
            size = (tbr * 1000 / 8) * float(duration)

        score = (
            1 if (is_direct_mp4 and has_audio) else 0,
            1 if has_audio else 0,
            1 if is_direct_mp4 else 0,
            tbr,
        )

        entry = {
            "short_side": short_side,
            "dim_label": dim_label,
            "format_id": fid,
            "has_audio": has_audio,
            "size": size,
            "score": score,
        }

        if short_side == 0:
            unlabelled_candidates.append(entry)
        else:
            existing = by_resolution.get(short_side)
            if not existing or score > existing["score"]:
                by_resolution[short_side] = entry

    sorted_keys = sorted(by_resolution.keys())

    video_options: list[dict] = []
    if sorted_keys:
        for idx, res_key in enumerate(sorted_keys):
            item = by_resolution[res_key]
            size_str = format_size_mb(item["size"])
            res_tag = f"{res_key}p" if res_key > 0 else "Original"

            meta_parts = [item["dim_label"]]
            if size_str:
                meta_parts.append(size_str)

            fid = item["format_id"]
            quality_token = f"fmt_{fid}" if item["has_audio"] else f"merge_{fid}"

            video_options.append({
                "id": quality_token,
                "label": f"MP4 · {res_tag}",
                "meta": " · ".join(meta_parts),
                "ext": "mp4",
                "primary": idx == 0,
                "recommended": idx == 0,
            })
    elif unlabelled_candidates:
        unlabelled_candidates.sort(key=lambda x: x["score"])
        best_u = unlabelled_candidates[-1]
        size_str = format_size_mb(best_u["size"])
        meta = f"Original · {size_str}" if size_str else "Resolución original"
        video_options.append({
            "id": f"fmt_{best_u['format_id']}",
            "label": "MP4 · Calidad original",
            "meta": meta,
            "ext": "mp4",
            "primary": True,
            "recommended": True,
        })
    else:
        w = info.get("width")
        h = info.get("height")
        dim_label = f"{w}x{h}" if (w and h) else "Resolución original"
        short_side = min(int(w), int(h)) if (w and h) else 0
        res_tag = f"{short_side}p" if short_side > 0 else "Original"
        video_options.append({
            "id": "best_single",
            "label": f"MP4 · {res_tag}",
            "meta": dim_label,
            "ext": "mp4",
            "primary": True,
            "recommended": True,
        })

    audio_options = [
        {
            "id": "audio",
            "label": "MP3 · Solo pista de audio",
            "meta": "128 kbps · Extraído del video",
            "ext": "mp3",
            "primary": False,
            "recommended": False,
        }
    ]
    return video_options, audio_options


def _decode_fb_escaped_url(raw: str) -> str:
    """Decodes JSON/JS escaped URLs from Facebook HTML payloads."""
    try:
        cleaned = raw.replace("\\/", "/")
        cleaned = cleaned.encode("utf-8").decode("unicode_escape")
        return html_lib.unescape(cleaned)
    except Exception:
        return raw.replace("\\/", "/")


def _extract_fb_video_id(url: str, html_text: str = "") -> Optional[str]:
    """Extracts the numeric Facebook video/reel ID from a URL or page HTML."""
    patterns = [
        r"(?:videos|reel|reels)/(\d{8,22})",
        r"[?&]v=(\d{8,22})",
        r"story_fbid=(\d{8,22})",
        r'"video_id"\s*:\s*"(\d{8,22})"',
        r'"videoID"\s*:\s*"(\d{8,22})"',
        r"fb://video/(\d{8,22})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    if html_text:
        for pat in patterns:
            m = re.search(pat, html_text)
            if m:
                return m.group(1)
    return None


async def resolve_and_extract_facebook(url: str) -> dict:
    """
    Resolves modern Facebook share links (/share/v/..., /share/r/..., /reel/..., fb.watch/...)
    using Facebook's externalhit crawler UA (avoiding 302 login redirect loops) and extracts:
    - canonical_url (https://www.facebook.com/watch/?v=<id>) so yt-dlp uses [facebook] instead of [generic]
    - SD and HD direct MP4 URLs from Facebook's page or embed plugin if present
    - OpenGraph title, description, uploader, and thumbnail
    """
    result = {
        "canonical_url": url,
        "video_id": _extract_fb_video_id(url),
        "title": "Video de Facebook",
        "description": "",
        "uploader": "",
        "thumbnail": None,
        "duration": 15,
        "sd_url": None,
        "hd_url": None,
    }

    headers_crawler = {
        "User-Agent": FB_CRAWLER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    }

    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, max_redirects=10) as client:
        # Step 1: Fetch with crawler UA to resolve /share/v/ or /share/r/ or fb.watch without login redirect loop
        html_body = ""
        try:
            resp = await client.get(url, headers=headers_crawler)
            final_url = str(resp.url)
            html_body = resp.text or ""

            # Check og:url in HTML
            og_url_m = re.search(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']', html_body, re.I)
            if og_url_m:
                final_url = html_lib.unescape(og_url_m.group(1))

            vid = _extract_fb_video_id(final_url, html_body)
            if vid:
                result["video_id"] = vid
                result["canonical_url"] = f"https://www.facebook.com/watch/?v={vid}"
            elif "facebook.com" in final_url and "/share/" not in final_url:
                result["canonical_url"] = final_url

            # Extract OpenGraph metadata
            og_title = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html_body, re.I)
            if og_title:
                result["title"] = html_lib.unescape(og_title.group(1)).strip()

            og_desc = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', html_body, re.I)
            if og_desc:
                result["description"] = html_lib.unescape(og_desc.group(1)).strip()

            og_img = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html_body, re.I)
            if og_img:
                result["thumbnail"] = html_lib.unescape(og_img.group(1)).strip()
        except Exception:
            pass

        # Step 2: Check for embedded SD/HD MP4 URLs in the fetched HTML or via Facebook's public video plugin
        pages_to_scan = [html_body]
        target_href = result["canonical_url"]
        plugin_url = f"https://www.facebook.com/plugins/video.php?href={quote(target_href, safe='')}&show_text=false"
        try:
            p_resp = await client.get(
                plugin_url,
                headers={
                    "User-Agent": DEFAULT_UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://www.facebook.com/",
                },
            )
            if p_resp.status_code == 200 and p_resp.text:
                pages_to_scan.append(p_resp.text)
        except Exception:
            pass

        # Also scan mobile watch page with crawler/desktop UA if video_id is known
        if result["video_id"] and not result["sd_url"]:
            try:
                w_resp = await client.get(
                    f"https://www.facebook.com/watch/?v={result['video_id']}",
                    headers={
                        "User-Agent": DEFAULT_UA,
                        "Accept": "text/html,application/xhtml+xml",
                        "Sec-Fetch-Mode": "navigate",
                    },
                )
                if w_resp.status_code == 200 and w_resp.text:
                    pages_to_scan.append(w_resp.text)
            except Exception:
                pass

        sd_patterns = [
            r'"browser_native_sd_url"\s*:\s*"([^"]+)"',
            r'"playable_url"\s*:\s*"([^"]+)"',
            r'"sd_src_no_ratelimit"\s*:\s*"([^"]+)"',
            r'"sd_src"\s*:\s*"([^"]+)"',
        ]
        hd_patterns = [
            r'"browser_native_hd_url"\s*:\s*"([^"]+)"',
            r'"playable_url_quality_hd"\s*:\s*"([^"]+)"',
            r'"hd_src_no_ratelimit"\s*:\s*"([^"]+)"',
            r'"hd_src"\s*:\s*"([^"]+)"',
        ]

        for page_html in pages_to_scan:
            if not page_html:
                continue
            if not result["sd_url"]:
                for pat in sd_patterns:
                    m = re.search(pat, page_html)
                    if m and m.group(1) != "null":
                        candidate = _decode_fb_escaped_url(m.group(1))
                        if candidate.startswith("http"):
                            result["sd_url"] = candidate
                            break
            if not result["hd_url"]:
                for pat in hd_patterns:
                    m = re.search(pat, page_html)
                    if m and m.group(1) != "null":
                        candidate = _decode_fb_escaped_url(m.group(1))
                        if candidate.startswith("http"):
                            result["hd_url"] = candidate
                            break

            if not result["thumbnail"]:
                og_img = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', page_html, re.I)
                if og_img:
                    result["thumbnail"] = html_lib.unescape(og_img.group(1)).strip()

    return result


async def resolve_instagram_share_url(url: str) -> str:
    """Resolves /share/reel/... or /share/p/... Instagram links to their canonical /reel/... URL."""
    if "/share/" not in url.lower():
        return url
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": FB_CRAWLER_UA})
            return str(resp.url)
    except Exception:
        return url


async def fetch_x_real_variants(url: str) -> Optional[dict]:
    """
    Queries X's syndication API and vxtwitter to obtain the exact MP4 variants
    actually hosted on video.twimg.com when yt-dlp is blocked on datacenter IPs.
    """
    m = re.search(r"(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)", url, re.I)
    if not m:
        return None
    user, status_id = m.group(1), m.group(2)

    variants_by_res: dict[int, dict] = {}
    title = f"Publicación de @{user} en X"
    description = ""
    uploader = f"@{user}"
    thumbnail = None
    duration_sec = 15

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        try:
            syn_url = f"https://cdn.syndication.twimg.com/tweet-result?id={status_id}&token=a"
            resp = await client.get(syn_url, headers={"User-Agent": DEFAULT_UA})
            if resp.status_code == 200:
                sdata = resp.json()
                if sdata.get("text"):
                    description = sdata["text"].strip()
                    title = description
                usr_obj = sdata.get("user") or {}
                if usr_obj.get("screen_name"):
                    uploader = f"@{usr_obj['screen_name']}"
                media_details = sdata.get("mediaDetails") or []
                for md in media_details:
                    if md.get("media_url_https") and not thumbnail:
                        thumbnail = md["media_url_https"]
                    vinfo = md.get("video_info") or {}
                    dur_ms = vinfo.get("duration_millis")
                    if dur_ms:
                        duration_sec = max(1, round(dur_ms / 1000))
                    for var in vinfo.get("variants") or []:
                        if var.get("content_type") == "video/mp4" and var.get("url"):
                            vurl = var["url"]
                            bitrate = int(var.get("bitrate") or 0)
                            rm = re.search(r"/(\d+)x(\d+)/", vurl)
                            if rm:
                                w, h = int(rm.group(1)), int(rm.group(2))
                                short_side = min(w, h)
                                dim_label = f"{w}x{h}"
                            else:
                                short_side = bitrate // 1000 if bitrate else 480
                                dim_label = "MP4"

                            size_est = (bitrate / 8) * duration_sec if bitrate and duration_sec else None
                            existing = variants_by_res.get(short_side)
                            if not existing or bitrate > existing["bitrate"]:
                                variants_by_res[short_side] = {
                                    "short_side": short_side,
                                    "dim_label": dim_label,
                                    "bitrate": bitrate,
                                    "size": size_est,
                                    "url": vurl,
                                }
        except Exception:
            pass

        if not variants_by_res:
            try:
                vx_url = f"https://api.vxtwitter.com/{user}/status/{status_id}"
                resp = await client.get(vx_url, headers={"User-Agent": DEFAULT_UA})
                if resp.status_code == 200:
                    vdata = resp.json()
                    if vdata.get("text"):
                        description = vdata["text"].strip()
                        title = description
                    if vdata.get("user_screen_name"):
                        uploader = f"@{vdata['user_screen_name']}"
                    for m_item in vdata.get("media_extended") or []:
                        if m_item.get("type") in ("video", "gif") and m_item.get("url"):
                            if m_item.get("thumbnail_url") and not thumbnail:
                                thumbnail = m_item["thumbnail_url"]
                            dur_ms = m_item.get("duration_millis") or 15000
                            duration_sec = max(1, round(dur_ms / 1000))
                            sz = m_item.get("size") or {}
                            w = int(sz.get("width") or 0)
                            h = int(sz.get("height") or 0)
                            short_side = min(w, h) if (w and h) else 720
                            dim_label = f"{w}x{h}" if (w and h) else "Original"
                            variants_by_res[short_side] = {
                                "short_side": short_side,
                                "dim_label": dim_label,
                                "bitrate": 0,
                                "size": None,
                                "url": m_item["url"],
                            }
                            break
            except Exception:
                pass

    if not variants_by_res:
        return None

    sorted_res = sorted(variants_by_res.keys())
    video_options = []
    variant_urls = {}

    for idx, r in enumerate(sorted_res):
        v = variants_by_res[r]
        opt_id = f"xvar_{r}"
        variant_urls[opt_id] = v["url"]
        size_str = format_size_mb(v["size"])
        meta_parts = [v["dim_label"]]
        if size_str:
            meta_parts.append(size_str)

        video_options.append({
            "id": opt_id,
            "label": f"MP4 · {r}p",
            "meta": " · ".join(meta_parts),
            "ext": "mp4",
            "primary": idx == 0,
            "recommended": idx == 0,
        })

    variant_urls["default"] = variants_by_res[sorted_res[0]]["url"]

    audio_options = [
        {
            "id": "audio",
            "label": "MP3 · Solo pista de audio",
            "meta": "128 kbps · Extraído del video",
            "ext": "mp3",
            "primary": False,
            "recommended": False,
        }
    ]

    return {
        "title": title,
        "description": description or title,
        "uploader": uploader,
        "thumbnail": thumbnail,
        "duration": duration_sec,
        "video_options": video_options,
        "audio_options": audio_options,
        "variant_urls": variant_urls,
    }


@app.get("/")
async def index():
    return FileResponse(
        str(STATIC_DIR / "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/thumbnail")
async def proxy_thumbnail(url: str = Query(...)):
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid thumbnail URL")
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": DEFAULT_UA})
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/jpeg")
            return Response(
                content=resp.content,
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=3600"},
            )
    except Exception:
        raise HTTPException(status_code=404, detail="Thumbnail unavailable")


@app.post("/api/info")
async def extract_video_info(payload: UrlRequest):
    raw_url = (payload.url or "").strip()
    platform = detect_platform(raw_url)
    if not platform:
        raise HTTPException(
            status_code=400,
            detail=(
                "Enlace no reconocido. Soportamos Facebook, Instagram, TikTok, X, "
                "Pinterest, Threads, LinkedIn, Reddit, YouTube, Twitch, Kick, Vimeo y Bluesky."
            ),
        )

    source_label = PLATFORM_LABELS.get(platform, platform.capitalize())

    # Pre-resolve Facebook and Instagram /share/ links so yt-dlp uses the native extractor instead of [generic]
    fb_resolved = None
    target_url = raw_url
    if platform == "facebook":
        fb_resolved = await resolve_and_extract_facebook(raw_url)
        target_url = fb_resolved["canonical_url"]
    elif platform == "instagram":
        target_url = await resolve_instagram_share_url(raw_url)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 12,
        "http_headers": {"User-Agent": DEFAULT_UA},
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=False)
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            video_options, audio_options = build_exact_blocks_from_info(info)
            raw_desc = (info.get("description") or "").strip()
            raw_title = (info.get("title") or "").strip()
            if raw_desc and len(raw_desc) > len(raw_title):
                full_description = raw_desc
            else:
                full_description = raw_title or raw_desc or (fb_resolved["description"] if fb_resolved else "") or f"Video de {source_label}"

            uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""
            if uploader and not str(uploader).startswith("@"):
                uploader = f"@{uploader}"

            thumb = info.get("thumbnail")
            if not thumb and info.get("thumbnails"):
                thumbs = [t.get("url") for t in info["thumbnails"] if t.get("url")]
                if thumbs:
                    thumb = thumbs[-1]
            if not thumb and fb_resolved:
                thumb = fb_resolved.get("thumbnail")

            return {
                "original_url": target_url,
                "platform": platform,
                "source_label": source_label,
                "title": full_description,
                "description": full_description,
                "uploader": uploader,
                "thumbnail": thumb,
                "duration": info.get("duration") or 15,
                "video_options": video_options,
                "audio_options": audio_options,
                "options": video_options + audio_options,
            }
    except Exception as exc:
        # Fallback for Facebook using direct SD/HD streams extracted via crawler/embed plugin
        if platform == "facebook" and fb_resolved and (fb_resolved.get("sd_url") or fb_resolved.get("hd_url")):
            video_options = []
            if fb_resolved.get("sd_url"):
                video_options.append({
                    "id": "fbvar_sd",
                    "label": "MP4 · Calidad SD",
                    "meta": "SD · Original alojado en Facebook",
                    "ext": "mp4",
                    "primary": True,
                    "recommended": True,
                })
            if fb_resolved.get("hd_url"):
                is_first = len(video_options) == 0
                video_options.append({
                    "id": "fbvar_hd",
                    "label": "MP4 · Calidad HD",
                    "meta": "HD · Original alojado en Facebook",
                    "ext": "mp4",
                    "primary": is_first,
                    "recommended": is_first,
                })
            audio_options = [
                {
                    "id": "audio",
                    "label": "MP3 · Solo pista de audio",
                    "meta": "128 kbps · Extraído del video",
                    "ext": "mp3",
                    "primary": False,
                    "recommended": False,
                }
            ]
            desc = fb_resolved.get("description") or fb_resolved.get("title") or "Video de Facebook"
            return {
                "original_url": target_url,
                "platform": "facebook",
                "source_label": source_label,
                "title": desc,
                "description": desc,
                "uploader": fb_resolved.get("uploader") or "",
                "thumbnail": fb_resolved.get("thumbnail"),
                "duration": fb_resolved.get("duration") or 15,
                "video_options": video_options,
                "audio_options": audio_options,
                "options": video_options + audio_options,
            }

        # Fallback for X/Twitter
        if platform == "x":
            x_data = await fetch_x_real_variants(raw_url)
            if x_data:
                return {
                    "original_url": raw_url,
                    "platform": "x",
                    "source_label": source_label,
                    "title": x_data["description"],
                    "description": x_data["description"],
                    "uploader": x_data["uploader"],
                    "thumbnail": x_data["thumbnail"],
                    "duration": x_data["duration"],
                    "video_options": x_data["video_options"],
                    "audio_options": x_data["audio_options"],
                    "options": x_data["video_options"] + x_data["audio_options"],
                }

        raise HTTPException(
            status_code=422,
            detail=f"No se pudo leer el video desde el servidor de origen. Verifica que el post sea público ({str(exc)[:120]}).",
        )


def cleanup_temp_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


@app.get("/robots.txt")
async def robots_txt():
    content = "User-agent: *\nAllow: /\nSitemap: https://ddx.cl/sitemap.xml\n"
    return PlainTextResponse(content)


@app.get("/sitemap.xml")
async def sitemap_xml():
    urls = [
        "https://ddx.cl/",
        "https://ddx.cl/bajar-video-instagram",
        "https://ddx.cl/bajar-video-tiktok",
        "https://ddx.cl/bajar-video-facebook",
        "https://ddx.cl/bajar-video-x",
        "https://ddx.cl/bajar-video-pinterest",
        "https://ddx.cl/bajar-video-threads",
        "https://ddx.cl/bajar-video-linkedin",
        "https://ddx.cl/bajar-video-reddit",
    ]
    items = "".join(f"<url><loc>{u}</loc><changefreq>weekly</changefreq><priority>0.9</priority></url>" for u in urls)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'
    return Response(content=xml, media_type="application/xml")


def build_ydl_format_selector(quality: str, has_ffmpeg: bool) -> tuple[str, bool]:
    if quality == "audio":
        return "bestaudio[ext=m4a]/bestaudio/worst[vcodec!=none]/best", True

    if quality.startswith("fmt_"):
        fid = quality[4:]
        return f"{fid}/best[ext=mp4]/best", False

    if quality.startswith("merge_"):
        fid = quality[6:]
        if has_ffmpeg:
            return f"{fid}+bestaudio[ext=m4a]/{fid}+bestaudio/{fid}/best[ext=mp4]/best", False
        return f"{fid}/best[ext=mp4]/best", False

    if quality == "best_single":
        return "best[ext=mp4]/best", False

    return "best[ext=mp4]/best", False


@app.get("/api/download")
async def download_video(
    url: str = Query(...),
    quality: str = Query("best_single"),
):
    platform = detect_platform(url) or "video"
    ffmpeg_path = get_ffmpeg_path()
    has_ffmpeg = ffmpeg_path is not None
    is_audio = quality == "audio"

    # Handle Facebook direct variant streams (fbvar_sd, fbvar_hd) or resolved Facebook URLs
    target_url = url
    if platform == "facebook":
        fb_resolved = await resolve_and_extract_facebook(url)
        target_url = fb_resolved["canonical_url"]
        if quality in ("fbvar_sd", "fbvar_hd") or (is_audio and (fb_resolved.get("sd_url") or fb_resolved.get("hd_url"))):
            direct_fb_url = (
                fb_resolved.get("hd_url")
                if quality == "fbvar_hd"
                else (fb_resolved.get("sd_url") or fb_resolved.get("hd_url"))
            )
            if direct_fb_url and not is_audio:
                filename = f"ddx_facebook_{quality.replace('fbvar_', '')}.mp4"
                async def stream_fb_variant():
                    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                        async with client.stream("GET", direct_fb_url, headers={"User-Agent": DEFAULT_UA}) as resp:
                            resp.raise_for_status()
                            async for chunk in resp.aiter_bytes(chunk_size=65536):
                                yield chunk

                return StreamingResponse(
                    stream_fb_variant(),
                    media_type="video/mp4",
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "Cache-Control": "no-store",
                    },
                )
            elif direct_fb_url and is_audio and has_ffmpeg:
                tmp_dir_fb = tempfile.mkdtemp(prefix="dl_fb_")
                out_mp3 = os.path.join(tmp_dir_fb, "audio.mp3")
                try:
                    subprocess.run(
                        [ffmpeg_path, "-y", "-i", direct_fb_url, "-vn", "-acodec", "libmp3lame", "-b:a", "128k", out_mp3],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=20,
                        check=True,
                    )
                    filename = "ddx_facebook_audio.mp3"
                    return FileResponse(
                        path=out_mp3,
                        filename=filename,
                        media_type="audio/mpeg",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
                        background=BackgroundTask(cleanup_temp_dir, tmp_dir_fb),
                    )
                except Exception:
                    cleanup_temp_dir(tmp_dir_fb)

    # Handle X (Twitter) syndication variants
    if platform == "x" and (quality.startswith("xvar_") or is_audio):
        x_data = await fetch_x_real_variants(url)
        if x_data and x_data.get("variant_urls"):
            vurls = x_data["variant_urls"]
            direct_x_url = vurls.get(quality) or vurls.get("default")
            if direct_x_url and not is_audio:
                filename = f"ddx_x_{quality.replace('xvar_', '')}p.mp4"
                async def stream_x_variant():
                    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                        async with client.stream("GET", direct_x_url, headers={"User-Agent": DEFAULT_UA}) as resp:
                            resp.raise_for_status()
                            async for chunk in resp.aiter_bytes(chunk_size=65536):
                                yield chunk

                return StreamingResponse(
                    stream_x_variant(),
                    media_type="video/mp4",
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "Cache-Control": "no-store",
                    },
                )
            elif direct_x_url and is_audio and has_ffmpeg:
                tmp_dir_x = tempfile.mkdtemp(prefix="dl_x_")
                out_mp3 = os.path.join(tmp_dir_x, "audio.mp3")
                try:
                    subprocess.run(
                        [ffmpeg_path, "-y", "-i", direct_x_url, "-vn", "-acodec", "libmp3lame", "-b:a", "128k", out_mp3],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=20,
                        check=True,
                    )
                    filename = "ddx_x_audio.mp3"
                    return FileResponse(
                        path=out_mp3,
                        filename=filename,
                        media_type="audio/mpeg",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
                        background=BackgroundTask(cleanup_temp_dir, tmp_dir_x),
                    )
                except Exception:
                    cleanup_temp_dir(tmp_dir_x)

    tmp_dir = tempfile.mkdtemp(prefix="dl_")
    output_template = os.path.join(tmp_dir, "media.%(ext)s")
    fmt_str, is_audio = build_ydl_format_selector(quality, has_ffmpeg)

    ydl_opts = {
        "format": fmt_str,
        "outtmpl": output_template,
        "noplaylist": True,
        "nopart": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
        "http_headers": {"User-Agent": DEFAULT_UA},
    }

    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = ffmpeg_path

    if is_audio and has_ffmpeg:
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ]
    elif has_ffmpeg and not is_audio:
        ydl_opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(target_url, download=True)

        files = [f for f in os.listdir(tmp_dir) if not f.endswith(".part")]
        if files:
            chosen_file = os.path.join(tmp_dir, files[0])
            ext = os.path.splitext(chosen_file)[1].lstrip(".") or ("mp3" if is_audio else "mp4")
            media_type = "audio/mpeg" if ext == "mp3" else ("audio/mp4" if ext == "m4a" else "video/mp4")
            clean_q = re.sub(r"[^a-zA-Z0-9_-]", "", quality)
            filename = f"ddx_{platform}_{clean_q}.{ext}"
            return FileResponse(
                path=chosen_file,
                filename=filename,
                media_type=media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Cache-Control": "no-store",
                },
                background=BackgroundTask(cleanup_temp_dir, tmp_dir),
            )
    except Exception as exc:
        cleanup_temp_dir(tmp_dir)
        raise HTTPException(
            status_code=422,
            detail=f"No se pudo descargar el archivo desde el servidor de origen ({str(exc)[:120]}).",
        )

    cleanup_temp_dir(tmp_dir)
    raise HTTPException(status_code=422, detail="No se pudo generar el archivo de descarga.")
