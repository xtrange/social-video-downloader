import html as html_lib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

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


def slugify_text(text: str, max_len: int = 28) -> str:
    """Converts text into a clean ASCII slug without @, accents, or special characters."""
    if not text:
        return ""
    clean = str(text).lstrip("@").strip()
    clean = unicodedata.normalize("NFKD", clean).encode("ascii", "ignore").decode("ascii")
    clean = re.sub(r"https?://\S+", "", clean)
    clean = re.sub(r"[^a-zA-Z0-9]+", "-", clean).strip("-").lower()
    if len(clean) > max_len:
        clean = clean[:max_len].rstrip("-")
    return clean


def build_ddx_filename(platform: str, quality: str, ext: str, author: str = "", title: str = "") -> str:
    """Builds a brand-reinforcing, OS-safe filename starting with ddx.cl- and without @."""
    parts = ["ddx.cl", slugify_text(platform, 12) or "video"]
    author_slug = slugify_text(author, 16)
    title_slug = slugify_text(title, 24)
    if author_slug:
        parts.append(author_slug)
    if title_slug and title_slug != author_slug:
        parts.append(title_slug)

    q_clean = quality.replace("xvar_", "").replace("fbvar_", "").replace("fmt_", "").replace("merge_", "").replace("res_", "")
    q_slug = slugify_text(q_clean, 10)
    if q_slug:
        parts.append(q_slug)

    safe_ext = re.sub(r"[^a-z0-9]", "", (ext or "mp4").lower()) or "mp4"
    return "-".join(parts) + f".{safe_ext}"


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
            "is_direct_mp4": is_direct_mp4,
            "direct_url": url if (is_direct_mp4 and has_audio) else None,
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

            opt_dict = {
                "id": quality_token,
                "label": f"MP4 · {res_tag}",
                "meta": " · ".join(meta_parts),
                "ext": "mp4",
                "primary": idx == 0,
                "recommended": idx == 0,
            }
            if item.get("direct_url"):
                opt_dict["direct_url"] = item["direct_url"]
            video_options.append(opt_dict)
    elif unlabelled_candidates:
        unlabelled_candidates.sort(key=lambda x: x["score"])
        best_u = unlabelled_candidates[-1]
        size_str = format_size_mb(best_u["size"])
        meta = f"Original · {size_str}" if size_str else "Resolución original"
        opt_dict = {
            "id": f"fmt_{best_u['format_id']}",
            "label": "MP4 · Calidad original",
            "meta": meta,
            "ext": "mp4",
            "primary": True,
            "recommended": True,
        }
        if best_u.get("direct_url"):
            opt_dict["direct_url"] = best_u["direct_url"]
        video_options.append(opt_dict)
    else:
        w = info.get("width")
        h = info.get("height")
        dim_label = f"{w}x{h}" if (w and h) else "Resolución original"
        short_side = min(int(w), int(h)) if (w and h) else 0
        res_tag = f"{short_side}p" if short_side > 0 else "Original"
        opt_dict = {
            "id": "best_single",
            "label": f"MP4 · {res_tag}",
            "meta": dim_label,
            "ext": "mp4",
            "primary": True,
            "recommended": True,
        }
        if info.get("url") and str(info.get("ext") or "mp4").lower() == "mp4":
            opt_dict["direct_url"] = info["url"]
        video_options.append(opt_dict)

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
    """Decodes JSON/JS single- or multi-escaped URLs from Facebook and Instagram HTML/JSON payloads."""
    try:
        cleaned = raw.strip().rstrip("\\")
        for _ in range(3):
            cleaned = re.sub(r"\\+/", "/", cleaned)
            cleaned = re.sub(r"\\+u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), cleaned)
            cleaned = re.sub(r"\\+x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), cleaned)
        return html_lib.unescape(cleaned)
    except Exception:
        return raw.replace("\\\\/", "/").replace("\\/", "/")


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


def _extract_ig_shortcode(url: str, html_text: str = "") -> Optional[str]:
    """
    Extracts the canonical Instagram shortcode from /reel/, /reels/, /p/, /tv/,
    /@user/reel/, or /accounts/login/?next=%2Freel%2F... URLs and HTML metadata.
    """
    candidates = [url]
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if "next" in qs and qs["next"]:
            nxt = unquote(unquote(qs["next"][0]))
            if nxt.startswith("/"):
                nxt = f"https://www.instagram.com{nxt}"
            candidates.insert(0, nxt)
        unquoted_url = unquote(url)
        if unquoted_url != url:
            candidates.append(unquoted_url)
    except Exception:
        pass

    ig_pat = re.compile(
        r"(?:instagram\.com|^)/(?:(?!share/|accounts/|explore/|stories/)[A-Za-z0-9_.-]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]{5,})",
        re.I,
    )
    for cand in candidates:
        m = ig_pat.search(cand)
        if m:
            sc = m.group(1)
            if sc.lower() not in ("embed", "captioned", "login", "share"):
                return sc

    if html_text:
        for pat in (
            r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']',
            r'\\*"shortcode\\*"\s*:\s*\\*"([A-Za-z0-9_-]{5,})\\*"',
            r'instagram://media\?id=([A-Za-z0-9_-]+)',
        ):
            hm = re.search(pat, html_text, re.I)
            if hm:
                val = hm.group(1)
                if "instagram.com" in val:
                    sub = ig_pat.search(val)
                    if sub:
                        return sub.group(1)
                elif len(val) >= 5 and not val.isdigit():
                    return val
    return None


async def resolve_instagram_share_url(url: str) -> str:
    """
    Resolves /share/reel/..., /share/p/..., or /accounts/login/?next=... Instagram links
    to their clean canonical https://www.instagram.com/reel/{shortcode}/ URL.
    """
    direct_sc = _extract_ig_shortcode(url)
    if direct_sc and "/share/" not in url.lower():
        kind = "p" if "/p/" in url.lower() else "reel"
        return f"https://www.instagram.com/{kind}/{direct_sc}/"

    for ua in (FB_CRAWLER_UA, DEFAULT_UA):
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, max_redirects=8) as client:
                resp = await client.get(url, headers={"User-Agent": ua})
                # Inspect all redirect hops + final URL + HTML body
                urls_to_check = [str(r.url) for r in resp.history] + [str(resp.url)]
                for hop_url in urls_to_check:
                    sc = _extract_ig_shortcode(hop_url)
                    if sc:
                        kind = "p" if "/p/" in hop_url.lower() else "reel"
                        return f"https://www.instagram.com/{kind}/{sc}/"
                sc_html = _extract_ig_shortcode(str(resp.url), resp.text or "")
                if sc_html:
                    return f"https://www.instagram.com/reel/{sc_html}/"
        except Exception:
            continue
    return url
async def fetch_x_real_variants(url: str) -> Optional[dict]:
    """
    Queries X's syndication API and vxtwitter to obtain the exact MP4 variants
    actually hosted on video.twimg.com when yt-dlp is blocked on datacenter IPs.
    Supports multi-video tweets (up to 4 videos in a single post).
    """
    m = re.search(r"(?:twitter\.com|x\.com)/([^/]+)/status/(\d+)", url, re.I)
    if not m:
        return None
    user, status_id = m.group(1), m.group(2)

    x_slides: list[dict] = []
    title = f"Publicación de @{user} en X"
    description = ""
    uploader = f"@{user}"
    thumbnail = None

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
                    slide_thumb = md.get("media_url_https")
                    if slide_thumb and not thumbnail:
                        thumbnail = slide_thumb
                    vinfo = md.get("video_info") or {}
                    dur_ms = vinfo.get("duration_millis")
                    slide_dur = max(1, round(dur_ms / 1000)) if dur_ms else 15
                    slide_vars: dict[int, dict] = {}
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

                            size_est = (bitrate / 8) * slide_dur if bitrate and slide_dur else None
                            existing = slide_vars.get(short_side)
                            if not existing or bitrate > existing["bitrate"]:
                                slide_vars[short_side] = {
                                    "short_side": short_side,
                                    "dim_label": dim_label,
                                    "bitrate": bitrate,
                                    "size": size_est,
                                    "url": vurl,
                                }
                    if slide_vars:
                        x_slides.append({
                            "variants_by_res": slide_vars,
                            "thumbnail": slide_thumb or thumbnail,
                            "duration": slide_dur,
                        })
        except Exception:
            pass

        if not x_slides:
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
                            slide_thumb = m_item.get("thumbnail_url")
                            if slide_thumb and not thumbnail:
                                thumbnail = slide_thumb
                            dur_ms = m_item.get("duration_millis") or 15000
                            slide_dur = max(1, round(dur_ms / 1000))
                            sz = m_item.get("size") or {}
                            w = int(sz.get("width") or 0)
                            h = int(sz.get("height") or 0)
                            short_side = min(w, h) if (w and h) else 720
                            dim_label = f"{w}x{h}" if (w and h) else "Original"
                            x_slides.append({
                                "variants_by_res": {
                                    short_side: {
                                        "short_side": short_side,
                                        "dim_label": dim_label,
                                        "bitrate": 0,
                                        "size": None,
                                        "url": m_item["url"],
                                    }
                                },
                                "thumbnail": slide_thumb or thumbnail,
                                "duration": slide_dur,
                            })
            except Exception:
                pass

    if not x_slides:
        return None

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

    carousel_items = []
    variant_urls = {}

    for s_idx, slide in enumerate(x_slides):
        v_by_res = slide["variants_by_res"]
        sorted_res = sorted(v_by_res.keys())
        slide_video_options = []
        for idx, r in enumerate(sorted_res):
            v = v_by_res[r]
            opt_id = f"xvar_{r}" if s_idx == 0 else f"xvar_s{s_idx + 1}_{r}"
            variant_urls[opt_id] = v["url"]
            size_str = format_size_mb(v["size"])
            meta_parts = [v["dim_label"]]
            if size_str:
                meta_parts.append(size_str)

            slide_video_options.append({
                "id": opt_id,
                "label": f"MP4 · {r}p",
                "meta": " · ".join(meta_parts),
                "ext": "mp4",
                "primary": idx == 0,
                "recommended": idx == 0,
                "direct_url": v["url"],
            })
        if s_idx == 0:
            variant_urls["default"] = v_by_res[sorted_res[0]]["url"]
        carousel_items.append({
            "index": s_idx + 1,
            "label": f"Video {s_idx + 1}",
            "thumbnail": slide["thumbnail"],
            "duration": slide["duration"],
            "video_options": slide_video_options,
            "audio_options": audio_options,
        })

    first_slide = carousel_items[0]
    return {
        "title": title,
        "description": description or title,
        "uploader": uploader,
        "thumbnail": first_slide["thumbnail"],
        "duration": first_slide["duration"],
        "video_options": first_slide["video_options"],
        "audio_options": audio_options,
        "variant_urls": variant_urls,
        "carousel_items": carousel_items if len(carousel_items) > 1 else [],
    }


_IG_ENCODING_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _ig_shortcode_to_media_id(shortcode: str) -> str:
    """Converts an Instagram base64url shortcode to its numeric media_id (pk) string."""
    sc = shortcode[:-28] if len(shortcode) > 28 else shortcode
    val = 0
    for ch in sc:
        idx = _IG_ENCODING_CHARS.find(ch)
        if idx != -1:
            val = val * 64 + idx
    return str(val)


async def fetch_instagram_embed_fallback(url: str) -> Optional[dict]:
    """
    Multi-layer fallback for public Instagram Reels, Posts, and Multi-Video Carousels on cloud IPs:
    1) Public embed endpoints (/p/{shortcode}/embed/captioned/ & /reel/{shortcode}/embed/captioned/)
       with multi-escaped contextJSON decoding.
    2) 2026 Instagram Logged-Out GraphQL handshake (/api/v1/web/get_ruling_for_content/ ->
       PolarisLoggedOutDesktopWWWPostRootContentQuery doc_id=27130156389949648 &
       PolarisPostRootQuery doc_id=27128499623469141) with v1 product & carousel schema parsing.
    3) OpenGraph crawler fallback for direct og:video extraction.
    """
    shortcode = _extract_ig_shortcode(url)
    if not shortcode:
        resolved = await resolve_instagram_share_url(url)
        shortcode = _extract_ig_shortcode(resolved)
    if not shortcode:
        return None

    media_id = _ig_shortcode_to_media_id(shortcode)
    ig_slides: list[dict] = []
    uploader = ""
    thumbnail: Optional[str] = None
    description = "Reel de Instagram"
    duration_sec = 15
    lsd_token = "AVqbxe3J_YA"

    v_patterns = [
        r'\\*"video_url\\*"\s*:\s*\\*"(https?:[^"\s]+?)\\*"',
        r'\\*"video_versions\\*"\s*:\s*\[\s*\{[^}]*?\\*"url\\*"\s*:\s*\\*"(https?:[^"\s]+?)\\*"',
        r'<meta[^>]+property=["\']og:video(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
    ]

    def _consume_v1_or_gql_media(media_obj: dict) -> None:
        nonlocal uploader, thumbnail, description, duration_sec
        if not isinstance(media_obj, dict) or not media_obj:
            return
        usr = media_obj.get("user") or media_obj.get("owner") or {}
        if isinstance(usr, dict) and usr.get("username") and not uploader:
            uploader = f"@{usr['username']}"

        cap_obj = media_obj.get("caption")
        if isinstance(cap_obj, dict) and cap_obj.get("text"):
            description = str(cap_obj["text"]).strip()
        else:
            cap_edges = ((media_obj.get("edge_media_to_caption") or {}).get("edges")) or []
            if cap_edges and isinstance(cap_edges[0], dict):
                txt = (cap_edges[0].get("node") or {}).get("text")
                if txt:
                    description = str(txt).strip()

        if not thumbnail:
            cands = ((media_obj.get("image_versions2") or {}).get("candidates")) or []
            if cands and isinstance(cands[0], dict) and cands[0].get("url"):
                thumbnail = cands[0]["url"]
            elif media_obj.get("display_url"):
                thumbnail = media_obj["display_url"]

        if media_obj.get("video_duration"):
            try:
                duration_sec = max(1, round(float(media_obj["video_duration"])))
            except Exception:
                pass

        items_to_scan = []
        if isinstance(media_obj.get("carousel_media"), list) and media_obj["carousel_media"]:
            items_to_scan.extend(media_obj["carousel_media"])
        else:
            sidecar_edges = ((media_obj.get("edge_sidecar_to_children") or {}).get("edges")) or []
            if sidecar_edges:
                for edge in sidecar_edges:
                    if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
                        items_to_scan.append(edge["node"])
            else:
                items_to_scan.append(media_obj)

        for item in items_to_scan:
            if not isinstance(item, dict):
                continue
            slide_vurl = item.get("video_url")
            slide_vars: dict[int, dict] = {}
            for vver in item.get("video_versions") or []:
                if isinstance(vver, dict) and vver.get("url"):
                    w = int(vver.get("width") or 0)
                    h = int(vver.get("height") or 0)
                    short_side = min(w, h) if (w and h) else 720
                    if short_side not in slide_vars:
                        slide_vars[short_side] = {
                            "short_side": short_side,
                            "dim_label": f"{w}x{h}" if (w and h) else "Original",
                            "url": vver["url"],
                        }
            if slide_vurl or slide_vars:
                item_thumb = thumbnail
                icands = ((item.get("image_versions2") or {}).get("candidates")) or []
                if icands and isinstance(icands[0], dict) and icands[0].get("url"):
                    item_thumb = icands[0]["url"]
                elif item.get("display_url"):
                    item_thumb = item["display_url"]
                item_dur = duration_sec
                if item.get("video_duration"):
                    try:
                        item_dur = max(1, round(float(item["video_duration"])))
                    except Exception:
                        pass
                ig_slides.append({
                    "video_url": slide_vurl,
                    "variants_by_res": slide_vars,
                    "thumbnail": item_thumb,
                    "duration": item_dur,
                })

    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        # Layer 1: Public embed endpoints (/p/{shortcode}/embed/captioned/ & /reel/{shortcode}/embed/captioned/)
        for embed_path in (f"/p/{shortcode}/embed/captioned/", f"/reel/{shortcode}/embed/captioned/"):
            if ig_slides:
                break
            try:
                embed_url = f"https://www.instagram.com{embed_path}"
                resp = await client.get(
                    embed_url,
                    headers={
                        "User-Agent": DEFAULT_UA,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
                        "Referer": "https://www.instagram.com/",
                    },
                )
                html_text = resp.text or ""
                if not html_text:
                    continue

                lsd_m = (
                    re.search(r'\["LSD",\s*\[\],\s*\{"token"\s*:\s*"([^"]+)"\}', html_text)
                    or re.search(r'"l"\s*:\s*"([A-Za-z0-9_-]{8,})"', html_text)
                )
                if lsd_m:
                    lsd_token = lsd_m.group(1)

                if not uploader:
                    um = re.search(r'\\*"username\\*"\s*:\s*\\*"([^"\\]+)\\*"', html_text)
                    if not um:
                        um = re.search(r'class="[^"]*UsernameText[^"]*"[^>]*>([^<]+)<', html_text)
                    if um:
                        u_clean = um.group(1).strip().lstrip("@")
                        if u_clean and u_clean.lower() != "instagram":
                            uploader = f"@{u_clean}"

                if not thumbnail:
                    tm = (
                        re.search(r'\\*"display_url\\*"\s*:\s*\\*"(https?:[^"\s]+?)\\*"', html_text)
                        or re.search(r'\\*"thumbnail_src\\*"\s*:\s*\\*"(https?:[^"\s]+?)\\*"', html_text)
                        or re.search(r'<img[^>]+class="[^"]*EmbeddedMediaImage[^"]*"[^>]+src="([^"]+)"', html_text)
                    )
                    if tm:
                        thumbnail = _decode_fb_escaped_url(tm.group(1))

                if description == "Reel de Instagram":
                    cm = re.search(r'<div[^>]+class="[^"]*Caption[^"]*"[^>]*>(.*?)</div>', html_text, re.S)
                    if cm:
                        clean_cap = re.sub(r"<br\s*/?>", "\n", cm.group(1), flags=re.I)
                        clean_cap = re.sub(r"<[^>]+>", " ", clean_cap)
                        clean_cap = html_lib.unescape(re.sub(r"[ \t]+", " ", clean_cap)).strip()
                        if clean_cap:
                            description = clean_cap
                    else:
                        cap_json = re.search(r'\\*"edge_media_to_caption\\*"\s*:\s*\{\\*?"edges\\*"\s*:\s*\[\{\\*?"node\\*"\s*:\s*\{\\*?"text\\*"\s*:\s*\\*"([^"\\]*(?:\\.[^"\\]*)*)\\*"', html_text)
                        if cap_json:
                            description = _decode_fb_escaped_url(cap_json.group(1))

                dur_m = re.search(r'\\*"video_duration\\*"\s*:\s*([0-9.]+)', html_text)
                if dur_m:
                    try:
                        duration_sec = max(1, round(float(dur_m.group(1))))
                    except Exception:
                        pass

                # Find all unique video_urls in embed HTML (supports multi-video carousels in contextJSON)
                found_vurls = []
                for raw_v in re.findall(r'\\*"video_url\\*"\s*:\s*\\*"(https?:[^"\s]+?)\\*"', html_text):
                    cand = _decode_fb_escaped_url(raw_v)
                    if cand.startswith("http") and cand not in found_vurls:
                        found_vurls.append(cand)
                if not found_vurls:
                    for pat in v_patterns[1:]:
                        vm = re.search(pat, html_text)
                        if vm:
                            cand = _decode_fb_escaped_url(vm.group(1))
                            if cand.startswith("http"):
                                found_vurls.append(cand)
                                break
                for fv in found_vurls:
                    ig_slides.append({
                        "video_url": fv,
                        "variants_by_res": {},
                        "thumbnail": thumbnail,
                        "duration": duration_sec,
                    })
            except Exception:
                continue

        # Layer 2: 2026 Instagram Logged-Out GraphQL Handshake
        if not ig_slides:
            api_base_headers = {
                "User-Agent": DEFAULT_UA,
                "X-IG-App-ID": "936619743392459",
                "X-ASBD-ID": "359341",
                "X-IG-WWW-Claim": "0",
                "Origin": "https://www.instagram.com",
                "Accept": "*/*",
                "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
            }
            try:
                await client.get(
                    "https://www.instagram.com/api/v1/web/get_ruling_for_content/",
                    params={"content_type": "MEDIA", "target_id": media_id},
                    headers=api_base_headers,
                )
            except Exception:
                pass

            csrf_token = client.cookies.get("csrftoken") or ""
            gql_queries = [
                (
                    "https://www.instagram.com/api/graphql",
                    "PolarisLoggedOutDesktopWWWPostRootContentQuery",
                    "27130156389949648",
                    json.dumps({"media_id": media_id}, separators=(",", ":")),
                ),
                (
                    "https://www.instagram.com/graphql/query",
                    "PolarisPostRootQuery",
                    "27128499623469141",
                    json.dumps(
                        {
                            "shortcode": shortcode,
                            "__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider": False,
                        },
                        separators=(",", ":"),
                    ),
                ),
                (
                    "https://www.instagram.com/graphql/query",
                    "PolarisPostActionLoadPostQueryQuery",
                    "8845758582119845",
                    json.dumps(
                        {
                            "shortcode": shortcode,
                            "fetch_tagged_user_count": None,
                            "hoisted_comment_id": None,
                            "hoisted_reply_id": None,
                        },
                        separators=(",", ":"),
                    ),
                ),
            ]

            for endpoint_url, friendly_name, doc_id, variables_str in gql_queries:
                if ig_slides:
                    break
                try:
                    gql_headers = {
                        **api_base_headers,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-FB-Friendly-Name": friendly_name,
                        "X-FB-LSD": lsd_token,
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": f"https://www.instagram.com/reel/{shortcode}/",
                    }
                    if csrf_token:
                        gql_headers["X-CSRFToken"] = csrf_token

                    gql_data = {
                        "lsd": lsd_token,
                        "fb_api_caller_class": "RelayModern",
                        "fb_api_req_friendly_name": friendly_name,
                        "server_timestamps": "true",
                        "variables": variables_str,
                        "doc_id": doc_id,
                    }
                    g_resp = await client.post(endpoint_url, headers=gql_headers, data=gql_data)
                    g_text = g_resp.text or ""
                    if g_resp.status_code == 200 and g_text:
                        try:
                            g_json = g_resp.json()
                            data_root = g_json.get("data") or {}
                            polaris = data_root.get("xig_polaris_media") or {}
                            if isinstance(polaris, dict):
                                _consume_v1_or_gql_media(polaris.get("if_not_gated_logged_out") or polaris)
                            if not ig_slides:
                                web_info = data_root.get("xdt_api__v1__media__shortcode__web_info") or {}
                                if isinstance(web_info, dict) and isinstance(web_info.get("items"), list) and web_info["items"]:
                                    _consume_v1_or_gql_media(web_info["items"][0])
                            if not ig_slides:
                                legacy_media = data_root.get("xdt_shortcode_media") or data_root.get("shortcode_media")
                                if isinstance(legacy_media, dict):
                                    _consume_v1_or_gql_media(legacy_media)
                        except Exception:
                            pass

                        if not ig_slides:
                            for pat in v_patterns:
                                vm = re.search(pat, g_text)
                                if vm:
                                    candidate = _decode_fb_escaped_url(vm.group(1))
                                    if candidate.startswith("http"):
                                        ig_slides.append({
                                            "video_url": candidate,
                                            "variants_by_res": {},
                                            "thumbnail": thumbnail,
                                            "duration": duration_sec,
                                        })
                                        break
                except Exception:
                    continue

        # Layer 3: Social Crawler OpenGraph fallback
        if not ig_slides:
            for bot_ua in ("TelegramBot (like TwitterBot)", FB_CRAWLER_UA):
                try:
                    c_resp = await client.get(
                        f"https://www.instagram.com/reel/{shortcode}/",
                        headers={"User-Agent": bot_ua},
                    )
                    c_html = c_resp.text or ""
                    found_og_vurl = None
                    for pat in v_patterns:
                        vm = re.search(pat, c_html)
                        if vm:
                            candidate = _decode_fb_escaped_url(vm.group(1))
                            if candidate.startswith("http"):
                                found_og_vurl = candidate
                                break
                    if found_og_vurl:
                        if not thumbnail:
                            og_img = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', c_html, re.I)
                            if og_img:
                                thumbnail = html_lib.unescape(og_img.group(1)).strip()
                        if description == "Reel de Instagram":
                            og_desc = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', c_html, re.I)
                            if og_desc:
                                description = html_lib.unescape(og_desc.group(1)).strip()
                        ig_slides.append({
                            "video_url": found_og_vurl,
                            "variants_by_res": {},
                            "thumbnail": thumbnail,
                            "duration": duration_sec,
                        })
                        break
                except Exception:
                    continue

    if not ig_slides:
        return None

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

    carousel_items = []
    variant_urls = {}

    for s_idx, slide in enumerate(ig_slides):
        v_by_res = slide["variants_by_res"]
        s_vurl = slide["video_url"]
        slide_video_options = []
        if v_by_res:
            sorted_res = sorted(v_by_res.keys())
            for idx, r in enumerate(sorted_res):
                v = v_by_res[r]
                opt_id = f"igvar_{r}" if s_idx == 0 else f"igvar_s{s_idx + 1}_{r}"
                variant_urls[opt_id] = v["url"]
                slide_video_options.append({
                    "id": opt_id,
                    "label": f"MP4 · {r}p",
                    "meta": f"{v['dim_label']} · Alojado en Instagram",
                    "ext": "mp4",
                    "primary": idx == 0,
                    "recommended": idx == 0,
                    "direct_url": v["url"],
                })
            if s_idx == 0:
                variant_urls["default"] = v_by_res[sorted_res[-1]]["url"]
        elif s_vurl:
            opt_id = "igvar_orig" if s_idx == 0 else f"igvar_s{s_idx + 1}_orig"
            variant_urls[opt_id] = s_vurl
            if s_idx == 0:
                variant_urls["default"] = s_vurl
            slide_video_options.append({
                "id": opt_id,
                "label": "MP4 · Calidad original",
                "meta": "Original · Alojado en Instagram",
                "ext": "mp4",
                "primary": True,
                "recommended": True,
                "direct_url": s_vurl,
            })

        carousel_items.append({
            "index": s_idx + 1,
            "label": f"Video {s_idx + 1}",
            "thumbnail": slide["thumbnail"] or thumbnail,
            "duration": slide["duration"] or duration_sec,
            "video_options": slide_video_options,
            "audio_options": audio_options,
        })

    first_slide = carousel_items[0]
    return {
        "title": description,
        "description": description,
        "uploader": uploader,
        "thumbnail": first_slide["thumbnail"],
        "duration": first_slide["duration"],
        "video_options": first_slide["video_options"],
        "audio_options": audio_options,
        "variant_urls": variant_urls,
        "carousel_items": carousel_items if len(carousel_items) > 1 else [],
    }


async def fetch_tiktok_fallback(url: str) -> Optional[dict]:
    """
    Fallback for public TikTok videos using TikTok's web rehydration state and oEmbed API.
    """
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        try:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": DEFAULT_UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
                },
            )
            html_text = resp.text or ""
            m = re.search(
                r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
                html_text,
                re.S,
            )
            if not m:
                return None

            import json
            raw_json = json.loads(m.group(1))
            default_scope = raw_json.get("__DEFAULT_SCOPE__") or {}
            video_detail = default_scope.get("webapp.video-detail") or {}
            item_info = (video_detail.get("itemInfo") or {}).get("itemStruct") or {}
            video_obj = item_info.get("video") or {}
            author_obj = item_info.get("author") or {}

            description = (item_info.get("desc") or "Video de TikTok").strip()
            uploader = f"@{author_obj['uniqueId']}" if author_obj.get("uniqueId") else ""
            thumbnail = video_obj.get("cover") or video_obj.get("originCover")
            duration_sec = int(video_obj.get("duration") or 15)

            variants_by_res: dict[int, dict] = {}
            for binfo in video_obj.get("bitrateInfo") or []:
                play_addr = binfo.get("PlayAddr") or {}
                url_list = play_addr.get("UrlList") or []
                if not url_list:
                    continue
                vurl = url_list[0]
                w = int(play_addr.get("Width") or 0)
                h = int(play_addr.get("Height") or 0)
                short_side = min(w, h) if (w and h) else 540
                dim_label = f"{w}x{h}" if (w and h) else f"{short_side}p"
                data_size = int(play_addr.get("DataSize") or 0) or None
                bitrate = int(binfo.get("Bitrate") or 0)
                existing = variants_by_res.get(short_side)
                if not existing or bitrate > existing["bitrate"]:
                    variants_by_res[short_side] = {
                        "short_side": short_side,
                        "dim_label": dim_label,
                        "size": data_size,
                        "bitrate": bitrate,
                        "url": vurl,
                    }

            if not variants_by_res and video_obj.get("playAddr"):
                w = int(video_obj.get("width") or 540)
                h = int(video_obj.get("height") or 960)
                short_side = min(w, h) if (w and h) else 540
                variants_by_res[short_side] = {
                    "short_side": short_side,
                    "dim_label": f"{w}x{h}",
                    "size": None,
                    "bitrate": 0,
                    "url": video_obj["playAddr"],
                }

            if not variants_by_res:
                return None

            sorted_res = sorted(variants_by_res.keys())
            video_options = []
            for idx, r in enumerate(sorted_res):
                v = variants_by_res[r]
                size_str = format_size_mb(v["size"])
                meta = f"{v['dim_label']} · {size_str}" if size_str else v["dim_label"]
                video_options.append({
                    "id": f"tkvar_{r}",
                    "label": f"MP4 · {r}p",
                    "meta": meta,
                    "ext": "mp4",
                    "primary": idx == 0,
                    "recommended": idx == 0,
                    "direct_url": v["url"],
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
            return {
                "title": description,
                "description": description,
                "uploader": uploader,
                "thumbnail": thumbnail,
                "duration": duration_sec,
                "video_options": video_options,
                "audio_options": audio_options,
            }
        except Exception:
            return None


async def fetch_reddit_json_fallback(url: str) -> Optional[dict]:
    """
    Fallback for public Reddit video posts using Reddit's public .json endpoint.
    """
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        try:
            clean_url = url.split("?")[0].rstrip("/")
            if "redd.it/" in clean_url or "/s/" in clean_url:
                r_init = await client.get(url, headers={"User-Agent": DEFAULT_UA})
                clean_url = str(r_init.url).split("?")[0].rstrip("/")

            json_url = f"{clean_url}.json?raw_json=1"
            resp = await client.get(json_url, headers={"User-Agent": DEFAULT_UA})
            if resp.status_code != 200:
                return None

            payload = resp.json()
            if not isinstance(payload, list) or not payload:
                return None

            children = ((payload[0].get("data") or {}).get("children")) or []
            if not children:
                return None

            post = children[0].get("data") or {}
            media = post.get("secure_media") or post.get("media")
            if not media and post.get("crosspost_parent_list"):
                parent = post["crosspost_parent_list"][0]
                media = parent.get("secure_media") or parent.get("media")

            rvideo = (media or {}).get("reddit_video") or {}
            fallback_url = rvideo.get("fallback_url")
            if not fallback_url:
                return None

            direct_mp4 = fallback_url.split("?")[0]
            h = int(rvideo.get("height") or 720)
            w = int(rvideo.get("width") or 1280)
            short_side = min(w, h) if (w and h) else 720
            duration_sec = int(rvideo.get("duration") or 15)
            title = (post.get("title") or "Video de Reddit").strip()
            author = f"@{post['author']}" if post.get("author") else ""
            thumb = post.get("thumbnail")
            if thumb and not str(thumb).startswith("http"):
                thumb = None

            video_options = [
                {
                    "id": f"rdvar_{short_side}",
                    "label": f"MP4 · {short_side}p",
                    "meta": f"{w}x{h} · Alojado en Reddit",
                    "ext": "mp4",
                    "primary": True,
                    "recommended": True,
                    "direct_url": direct_mp4,
                }
            ]
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
                "description": title,
                "uploader": author,
                "thumbnail": thumb,
                "duration": duration_sec,
                "video_options": video_options,
                "audio_options": audio_options,
            }
        except Exception:
            return None


@app.get("/")
async def index():
    return FileResponse(
        str(STATIC_DIR / "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/api/health-platforms")
async def health_platforms(probe: int = Query(0)):
    """
    Diagnostic endpoint to inspect the status of all 13 supported platforms,
    their extractors, and (when ?probe=1 is passed) live connectivity from the server IP.
    """
    ffmpeg_path = get_ffmpeg_path()
    matrix = {
        "x": {"label": "X (Twitter)", "mode": "yt-dlp + extractor directo syndication/vxtwitter", "palanca_1_direct_cdn": True},
        "facebook": {"label": "Facebook", "mode": "yt-dlp + extractor directo crawler/embed SD/HD", "palanca_1_direct_cdn": True},
        "instagram": {"label": "Instagram", "mode": "yt-dlp + extractor respaldo embed público", "palanca_1_direct_cdn": True},
        "tiktok": {"label": "TikTok", "mode": "yt-dlp + extractor respaldo rehydration JSON", "palanca_1_direct_cdn": True},
        "reddit": {"label": "Reddit", "mode": "yt-dlp + extractor respaldo JSON público", "palanca_1_direct_cdn": True},
        "pinterest": {"label": "Pinterest", "mode": "yt-dlp (metadatos públicos pinimg.com)", "palanca_1_direct_cdn": True},
        "bsky": {"label": "Bluesky", "mode": "yt-dlp (API pública AT Protocol)", "palanca_1_direct_cdn": True},
        "vimeo": {"label": "Vimeo", "mode": "yt-dlp (videos públicos)", "palanca_1_direct_cdn": True},
        "twitch": {"label": "Twitch Clips", "mode": "yt-dlp (GraphQL público de clips)", "palanca_1_direct_cdn": True},
        "kick": {"label": "Kick Clips", "mode": "yt-dlp", "palanca_1_direct_cdn": True},
        "threads": {"label": "Threads", "mode": "yt-dlp (posts públicos)", "palanca_1_direct_cdn": True},
        "linkedin": {"label": "LinkedIn", "mode": "yt-dlp (posts con visibilidad pública)", "palanca_1_direct_cdn": True},
        "youtube": {"label": "YouTube", "mode": "yt-dlp (sujeto a verificación de cuenta en IPs cloud)", "palanca_1_direct_cdn": False},
    }

    if probe == 1:
        probe_targets = {
            "x": "https://cdn.syndication.twimg.com/tweet-result?id=20&token=a",
            "facebook": "https://www.facebook.com/plugins/video.php",
            "instagram": "https://www.instagram.com/p/C-test/embed/captioned/",
            "tiktok": "https://www.tiktok.com/oembed?url=https://www.tiktok.com/@tiktok",
            "reddit": "https://www.reddit.com/.json?limit=1",
            "bsky": "https://public.api.bsky.app/xrpc/_health",
            "vimeo": "https://vimeo.com/api/oembed.json?url=https://vimeo.com/76979871",
        }
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
            for key, purl in probe_targets.items():
                try:
                    r = await client.get(purl, headers={"User-Agent": DEFAULT_UA})
                    matrix[key]["live_http_status"] = r.status_code
                    matrix[key]["reachable_from_server"] = r.status_code < 500
                except Exception as e:
                    matrix[key]["live_http_status"] = str(e)[:60]
                    matrix[key]["reachable_from_server"] = False

    return {
        "status": "ok",
        "domain": "ddx.cl",
        "yt_dlp_version": getattr(yt_dlp.version, "__version__", "unknown"),
        "ffmpeg_available": ffmpeg_path is not None,
        "platforms": matrix,
    }


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
        "noplaylist": platform == "youtube",
        "skip_download": True,
        "socket_timeout": 12,
        "http_headers": {"User-Agent": DEFAULT_UA},
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=False)
            carousel_items: List[Dict[str, Any]] = []

            if "entries" in info and info["entries"]:
                raw_entries = [e for e in info["entries"] if isinstance(e, dict)]
                video_entries = [
                    e for e in raw_entries
                    if (e.get("formats") or e.get("url")) and e.get("ext") not in ("jpg", "jpeg", "png", "webp")
                ]
                if not video_entries and raw_entries:
                    video_entries = [raw_entries[0]]

                if len(video_entries) > 1:
                    for idx, entry in enumerate(video_entries):
                        v_opts, a_opts = build_exact_blocks_from_info(entry)
                        e_thumb = entry.get("thumbnail")
                        if not e_thumb and entry.get("thumbnails"):
                            e_thumbs = [t.get("url") for t in entry["thumbnails"] if t.get("url")]
                            if e_thumbs:
                                e_thumb = e_thumbs[-1]
                        carousel_items.append({
                            "index": idx,
                            "label": f"Video {idx + 1}",
                            "thumbnail": e_thumb,
                            "duration": entry.get("duration") or 15,
                            "video_options": v_opts,
                            "audio_options": a_opts,
                        })

                if video_entries:
                    parent_desc = (info.get("description") or info.get("title") or "").strip()
                    parent_uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""
                    info = video_entries[0]
                    if parent_desc and not info.get("description"):
                        info["description"] = parent_desc
                    if parent_uploader and not (info.get("uploader") or info.get("channel") or info.get("uploader_id")):
                        info["uploader"] = parent_uploader

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
                "carousel_items": carousel_items,
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
                    "direct_url": fb_resolved["sd_url"],
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
                    "direct_url": fb_resolved["hd_url"],
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
                "carousel_items": [],
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
                    "carousel_items": x_data.get("carousel_items", []),
                }

        # Fallback for Instagram using public embed page
        if platform == "instagram":
            ig_data = await fetch_instagram_embed_fallback(target_url)
            if ig_data:
                return {
                    "original_url": target_url,
                    "platform": "instagram",
                    "source_label": source_label,
                    "title": ig_data["description"],
                    "description": ig_data["description"],
                    "uploader": ig_data["uploader"],
                    "thumbnail": ig_data["thumbnail"],
                    "duration": ig_data["duration"],
                    "video_options": ig_data["video_options"],
                    "audio_options": ig_data["audio_options"],
                    "options": ig_data["video_options"] + ig_data["audio_options"],
                    "carousel_items": ig_data.get("carousel_items", []),
                }

        # Fallback for TikTok using web rehydration JSON
        if platform == "tiktok":
            tk_data = await fetch_tiktok_fallback(raw_url)
            if tk_data:
                return {
                    "original_url": raw_url,
                    "platform": "tiktok",
                    "source_label": source_label,
                    "title": tk_data["description"],
                    "description": tk_data["description"],
                    "uploader": tk_data["uploader"],
                    "thumbnail": tk_data["thumbnail"],
                    "duration": tk_data["duration"],
                    "video_options": tk_data["video_options"],
                    "audio_options": tk_data["audio_options"],
                    "options": tk_data["video_options"] + tk_data["audio_options"],
                }

        # Fallback for Reddit using public .json endpoint
        if platform == "reddit":
            rd_data = await fetch_reddit_json_fallback(raw_url)
            if rd_data:
                return {
                    "original_url": raw_url,
                    "platform": "reddit",
                    "source_label": source_label,
                    "title": rd_data["description"],
                    "description": rd_data["description"],
                    "uploader": rd_data["uploader"],
                    "thumbnail": rd_data["thumbnail"],
                    "duration": rd_data["duration"],
                    "video_options": rd_data["video_options"],
                    "audio_options": rd_data["audio_options"],
                    "options": rd_data["video_options"] + rd_data["audio_options"],
                }

        raise HTTPException(
            status_code=422,
            detail=format_friendly_extraction_error(platform, source_label, exc),
        )


def format_friendly_extraction_error(platform: str, source_label: str, exc: Exception) -> str:
    """Translates raw yt-dlp exceptions into clear, user-friendly Spanish messages."""
    err_lower = str(exc).lower()

    if any(k in err_lower for k in ("not a bot", "sign in", "login required", "cookies", "confirm your age")):
        if platform == "youtube":
            return (
                "YouTube requiere verificación de cuenta o inicio de sesión para este video y "
                "no permite su descarga desde servidores en la nube."
            )
        return (
            f"{source_label} requiere iniciar sesión para acceder a esta publicación. "
            "Solo es posible descargar videos de publicaciones públicas sin restricción de cuenta."
        )

    if "private" in err_lower:
        return f"Este video de {source_label} es privado o solo está visible para usuarios autorizados."

    if any(k in err_lower for k in ("country", "geo", "not available in your")):
        return f"Este video de {source_label} tiene restricción geográfica y no permite el acceso desde el servidor."

    if any(k in err_lower for k in ("404", "not found", "does not exist", "unavailable", "removed")):
        return f"No se encontró el video en {source_label}. Verifica que el enlace sea correcto y que el post siga disponible."

    return f"No se pudo leer el video desde {source_label}. Verifica que la publicación sea pública y contenga un video."


def cleanup_temp_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


SAFE_CDN_SUFFIXES = (
    "twimg.com",
    "fbcdn.net",
    "facebook.com",
    "cdninstagram.com",
    "instagram.com",
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokv.com",
    "byteoversea.com",
    "ibytedtos.com",
    "muscdn.com",
    "googlevideo.com",
    "redd.it",
    "redditmedia.com",
    "pinimg.com",
    "vimeocdn.com",
    "akamaized.net",
    "cloudfront.net",
    "licdn.com",
    "bsky.app",
    "ttvnw.net",
    "kick.com",
)


def is_safe_cdn_url(cdn_url: str) -> bool:
    """Validates that a direct CDN URL belongs to a known social media CDN over HTTPS (SSRF protection)."""
    if not cdn_url or not cdn_url.startswith("https://"):
        return False
    try:
        parsed = urlparse(cdn_url)
        host = (parsed.hostname or "").lower()
        return any(host == s or host.endswith(f".{s}") for s in SAFE_CDN_SUFFIXES)
    except Exception:
        return False


async def try_stream_cdn_url(cdn_url: str, filename: str) -> Optional[StreamingResponse]:
    """
    Streams a progressive MP4 directly from the origin CDN in 64 KB RAM chunks without
    touching local disk (/tmp) or spawning ffmpeg, forwarding Content-Length for live % progress.
    """
    client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)
    try:
        req = client.build_request("GET", cdn_url, headers={"User-Agent": DEFAULT_UA})
        resp = await client.send(req, stream=True)
        if resp.status_code != 200:
            await resp.aclose()
            await client.aclose()
            return None

        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        }
        if resp.headers.get("content-length"):
            headers["Content-Length"] = resp.headers["content-length"]

        async def stream_chunks():
            try:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_chunks(),
            media_type="video/mp4",
            headers=headers,
        )
    except Exception:
        await client.aclose()
        return None


SITE_URL = "https://ddx.cl"

# slug -> (nombre, ejemplo de URL, texto descriptivo, contenido específico del FAQ)
SEO_PLATFORMS: Dict[str, Dict[str, str]] = {
    "instagram": {"name": "Instagram", "example": "https://www.instagram.com/reel/...", "what": "Reels, videos y carruseles de publicaciones públicas de Instagram"},
    "tiktok": {"name": "TikTok", "example": "https://www.tiktok.com/@usuario/video/...", "what": "videos de TikTok sin marca de agua"},
    "facebook": {"name": "Facebook", "example": "https://www.facebook.com/watch/?v=...", "what": "videos y Reels públicos de Facebook en calidad SD y HD"},
    "x": {"name": "X (Twitter)", "example": "https://x.com/usuario/status/...", "what": "videos y GIFs de publicaciones de X (antes Twitter)"},
    "pinterest": {"name": "Pinterest", "example": "https://www.pinterest.com/pin/...", "what": "videos de pines de Pinterest"},
    "threads": {"name": "Threads", "example": "https://www.threads.net/@usuario/post/...", "what": "videos de publicaciones de Threads"},
    "linkedin": {"name": "LinkedIn", "example": "https://www.linkedin.com/posts/...", "what": "videos públicos de publicaciones de LinkedIn"},
    "reddit": {"name": "Reddit", "example": "https://www.reddit.com/r/.../comments/...", "what": "videos de Reddit con audio incluido"},
    "youtube": {"name": "YouTube", "example": "https://www.youtube.com/watch?v=...", "what": "videos, Shorts y audio de YouTube"},
    "twitch": {"name": "Twitch", "example": "https://clips.twitch.tv/...", "what": "clips y videos de Twitch"},
    "kick": {"name": "Kick", "example": "https://kick.com/.../clips/...", "what": "clips de Kick"},
    "vimeo": {"name": "Vimeo", "example": "https://vimeo.com/...", "what": "videos públicos de Vimeo"},
    "bluesky": {"name": "Bluesky", "example": "https://bsky.app/profile/.../post/...", "what": "videos de publicaciones de Bluesky"},
}


def _esc(text: str) -> str:
    return html_lib.escape(text, quote=True)


def render_seo_page(slug: str, mode: str = "video") -> Optional[str]:
    """Renderiza index.html con title, meta, canonical, H1, JSON-LD y FAQ específicos de la plataforma."""
    p = SEO_PLATFORMS.get(slug)
    if not p:
        return None
    name, what, example = p["name"], p["what"], p["example"]
    is_mp3 = mode == "mp3"
    url = f"{SITE_URL}/{'bajar-mp3' if is_mp3 else 'bajar-video'}-{slug}"
    if is_mp3:
        title = f"Bajar MP3 de {name} gratis — Extraer audio — DDX.cl"
        desc = f"Convierte y descarga el audio en MP3 de {name} con solo pegar el enlace. Gratis, sin registro y sin instalar nada. DDX.cl"
    else:
        title = f"Bajar videos de {name} gratis — DDX.cl"
        desc = f"Descarga {what} en MP4 o MP3 con solo pegar el enlace. Gratis, sin registro y sin instalar nada. DDX.cl"

    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    page = re.sub(r"<title>.*?</title>", f"<title>{_esc(title)}</title>", page, count=1, flags=re.S)
    page = re.sub(r'(<meta name="description" content=")[^"]*(")', lambda m: f"{m.group(1)}{_esc(desc)}{m.group(2)}", page, count=1)
    page = page.replace('<link rel="canonical" href="https://ddx.cl/" />', f'<link rel="canonical" href="{url}" />')
    page = re.sub(r'(<meta property="og:title" content=")[^"]*(")', lambda m: f"{m.group(1)}{_esc(title)}{m.group(2)}", page, count=1)
    page = re.sub(r'(<meta property="og:description" content=")[^"]*(")', lambda m: f"{m.group(1)}{_esc(desc)}{m.group(2)}", page, count=1)
    page = re.sub(r'(<meta name="twitter:title" content=")[^"]*(")', lambda m: f"{m.group(1)}{_esc(title)}{m.group(2)}", page, count=1)
    page = re.sub(r'(<meta name="twitter:description" content=")[^"]*(")', lambda m: f"{m.group(1)}{_esc(desc)}{m.group(2)}", page, count=1)
    page = page.replace('<meta property="og:url" content="https://ddx.cl/" />', f'<meta property="og:url" content="{url}" />')
    h1_text = f"Bajar MP3 de {name}" if is_mp3 else f"Bajar video de {name}"
    page = page.replace("<h1>Descargar video</h1>", f"<h1>{_esc(h1_text)}</h1>")
    page = page.replace('placeholder="https://..."', f'placeholder="{_esc(example)}"')

    if is_mp3:
        faqs = [
            (f"¿Cómo bajo el audio en MP3 de {name}?", f"Copia el enlace de la publicación en {name}, pégalo arriba y pulsa Descargar en la opción «Solo audio» (MP3)."),
            (f"¿Puedo extraer la música o el sonido de un video de {name}?", f"Sí. DDX.cl extrae la pista de audio de {what} y la entrega como archivo MP3. Solo funciona con contenido público."),
            ("¿Es gratis y necesito registrarme?", "Es gratis, sin registro y sin instalar aplicaciones. Funciona en celular y computador."),
            ("¿Qué calidad tiene el MP3?", "Se entrega a 128 kbps, extraído directamente del audio original del video."),
        ]
    else:
      faqs = [
        (f"¿Cómo descargo un video de {name}?", f"Copia el enlace de la publicación en {name}, pégalo en el campo de arriba y elige la calidad. El archivo se guarda en tu dispositivo."),
        (f"¿Puedo descargar {what}?", f"Sí. DDX.cl permite bajar {what}. Solo funciona con contenido público."),
        ("¿Es gratis y necesito registrarme?", "Es gratis, sin registro y sin instalar aplicaciones. Funciona en celular y computador."),
        ("¿Puedo bajar solo el audio?", "Sí, cada video incluye la opción de descargar solo el audio en MP3."),
    ]
    faq_html = "".join(f"<h3>{_esc(q)}</h3><p>{_esc(a)}</p>" for q, a in faqs)
    seo_block = (
        f'<section class="seo-copy" aria-label="Información sobre {_esc(name)}">'
        f"<h2>{'Extraer audio MP3 de' if is_mp3 else 'Descargar videos de'} {_esc(name)}</h2>"
        + (
            f"<p>Con DDX.cl puedes bajar el audio en MP3 de {_esc(what)} pegando el enlace del post. "
            f"Elige la opción «Solo audio» y guarda la pista en tu dispositivo.</p>"
            if is_mp3 else
            f"<p>Con DDX.cl puedes bajar {_esc(what)} pegando el enlace del post. "
            f"Detecta la fuente automáticamente y te muestra las calidades disponibles.</p>"
        ) +
        f"{faq_html}</section>"
    )
    # Reemplazar el bloque seo-copy genérico de la portada por el específico de la plataforma
    page = re.sub(r'<section class="seo-copy">.*?</section>', lambda m: seo_block, page, count=1, flags=re.S)

    ld = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebApplication",
                "name": f"DDX.cl — {'Bajar MP3 de' if is_mp3 else 'Bajar videos de'} {name}",
                "url": url,
                "applicationCategory": "MultimediaApplication",
                "operatingSystem": "All",
                "inLanguage": "es-CL",
                "description": desc,
                "offers": {"@type": "Offer", "price": "0", "priceCurrency": "CLP"},
            },
            {
                "@type": "FAQPage",
                "mainEntity": [
                    {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
                    for q, a in faqs
                ],
            },
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "DDX.cl", "item": SITE_URL + "/"},
                    {"@type": "ListItem", "position": 2, "name": h1_text, "item": url},
                ],
            },
        ],
    }
    page = re.sub(
        r'<script type="application/ld\+json">.*?</script>',
        lambda m: '<script type="application/ld+json">' + json.dumps(ld, ensure_ascii=False) + "</script>",
        page,
        count=1,
        flags=re.S,
    )
    return page


@app.api_route("/bajar-mp3-{slug}", methods=["GET", "HEAD"])
@app.api_route("/bajar-mp3-{slug}/", methods=["GET", "HEAD"])
async def seo_mp3_page(slug: str):
    return await seo_platform_page(slug, "mp3")


@app.api_route("/bajar-video-{slug}", methods=["GET", "HEAD"])
@app.api_route("/bajar-video-{slug}/", methods=["GET", "HEAD"])
async def seo_platform_page(slug: str, mode: str = "video"):
    page = render_seo_page(slug, "mp3" if mode == "mp3" else "video")
    if page is None:
        raise HTTPException(status_code=404, detail="Página no encontrada")
    return Response(
        content=page,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "public, max-age=0, s-maxage=3600, stale-while-revalidate=86400"},
    )


@app.api_route("/robots.txt", methods=["GET", "HEAD"])
async def robots_txt():
    content = "User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: https://ddx.cl/sitemap.xml\n"
    return PlainTextResponse(content)


@app.api_route("/sitemap.xml", methods=["GET", "HEAD"])
async def sitemap_xml():
    lastmod = "2026-11-13"
    urls = (
        [(f"{SITE_URL}/", "1.0")]
        + [(f"{SITE_URL}/bajar-video-{s}", "0.8") for s in SEO_PLATFORMS]
        + [(f"{SITE_URL}/bajar-mp3-{s}", "0.7") for s in SEO_PLATFORMS]
    )
    items = "".join(
        f"<url><loc>{u}</loc><lastmod>{lastmod}</lastmod><changefreq>weekly</changefreq><priority>{pr}</priority></url>" for u, pr in urls
    )
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
    author: str = Query(""),
    title: str = Query(""),
    cdn_url: str = Query(""),
):
    platform = detect_platform(url) or "video"
    ffmpeg_path = get_ffmpeg_path()
    has_ffmpeg = ffmpeg_path is not None
    is_audio = quality == "audio"

    # Zero-disk / Zero-CPU fast path if a verified progressive CDN URL was supplied by /api/info
    if not is_audio and not quality.startswith("merge_") and is_safe_cdn_url(cdn_url):
        fast_filename = build_ddx_filename(platform, quality, "mp4", author, title)
        stream_resp = await try_stream_cdn_url(cdn_url, fast_filename)
        if stream_resp:
            return stream_resp

    # Direct MP3 extraction fast path if audio is requested and we already have a verified CDN URL
    if is_audio and has_ffmpeg and is_safe_cdn_url(cdn_url):
        tmp_dir_cdn = tempfile.mkdtemp(prefix="dl_cdn_mp3_")
        out_mp3_cdn = os.path.join(tmp_dir_cdn, "audio.mp3")
        try:
            subprocess.run(
                [ffmpeg_path, "-y", "-i", cdn_url, "-vn", "-acodec", "libmp3lame", "-b:a", "128k", out_mp3_cdn],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=True,
            )
            filename = build_ddx_filename(platform, "audio", "mp3", author, title)
            return FileResponse(
                path=out_mp3_cdn,
                filename=filename,
                media_type="audio/mpeg",
                headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
                background=BackgroundTask(cleanup_temp_dir, tmp_dir_cdn),
            )
        except Exception:
            cleanup_temp_dir(tmp_dir_cdn)

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
                q_tag = "hd" if quality == "fbvar_hd" else "sd"
                filename = build_ddx_filename("facebook", q_tag, "mp4", author, title or fb_resolved.get("title", ""))
                stream_resp = await try_stream_cdn_url(direct_fb_url, filename)
                if stream_resp:
                    return stream_resp
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
                    filename = build_ddx_filename("facebook", "audio", "mp3", author, title or fb_resolved.get("title", ""))
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
                q_tag = f"{quality.replace('xvar_', '')}p"
                filename = build_ddx_filename("x", q_tag, "mp4", author or x_data.get("uploader", ""), title or x_data.get("description", ""))
                stream_resp = await try_stream_cdn_url(direct_x_url, filename)
                if stream_resp:
                    return stream_resp
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
                    filename = build_ddx_filename("x", "audio", "mp3", author or x_data.get("uploader", ""), title or x_data.get("description", ""))
                    return FileResponse(
                        path=out_mp3,
                        filename=filename,
                        media_type="audio/mpeg",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
                        background=BackgroundTask(cleanup_temp_dir, tmp_dir_x),
                    )
                except Exception:
                    cleanup_temp_dir(tmp_dir_x)

    # Handle Instagram variants and share URL resolution
    if platform == "instagram":
        target_url = await resolve_instagram_share_url(url)
        if quality.startswith("igvar_") or is_audio:
            ig_data = await fetch_instagram_embed_fallback(target_url)
            if ig_data and ig_data.get("variant_urls"):
                vurls = ig_data["variant_urls"]
                direct_ig_url = vurls.get(quality) or vurls.get("default")
                if direct_ig_url and not is_audio:
                    q_tag = "original" if quality == "igvar_orig" else f"{quality.replace('igvar_', '')}p"
                    filename = build_ddx_filename(
                        "instagram",
                        q_tag,
                        "mp4",
                        author or ig_data.get("uploader", ""),
                        title or ig_data.get("description", ""),
                    )
                    stream_resp = await try_stream_cdn_url(direct_ig_url, filename)
                    if stream_resp:
                        return stream_resp
                elif direct_ig_url and is_audio and has_ffmpeg:
                    tmp_dir_ig = tempfile.mkdtemp(prefix="dl_ig_")
                    out_mp3 = os.path.join(tmp_dir_ig, "audio.mp3")
                    try:
                        subprocess.run(
                            [ffmpeg_path, "-y", "-i", direct_ig_url, "-vn", "-acodec", "libmp3lame", "-b:a", "128k", out_mp3],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=20,
                            check=True,
                        )
                        filename = build_ddx_filename(
                            "instagram",
                            "audio",
                            "mp3",
                            author or ig_data.get("uploader", ""),
                            title or ig_data.get("description", ""),
                        )
                        return FileResponse(
                            path=out_mp3,
                            filename=filename,
                            media_type="audio/mpeg",
                            headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
                            background=BackgroundTask(cleanup_temp_dir, tmp_dir_ig),
                        )
                    except Exception:
                        cleanup_temp_dir(tmp_dir_ig)

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
            info = ydl.extract_info(target_url, download=True)
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

        files = [f for f in os.listdir(tmp_dir) if not f.endswith(".part")]
        if files:
            chosen_file = os.path.join(tmp_dir, files[0])
            ext = os.path.splitext(chosen_file)[1].lstrip(".") or ("mp3" if is_audio else "mp4")
            media_type = "audio/mpeg" if ext == "mp3" else ("audio/mp4" if ext == "m4a" else "video/mp4")
            filename = build_ddx_filename(
                platform,
                "audio" if is_audio else quality,
                ext,
                author or info.get("uploader") or "",
                title or info.get("title") or "",
            )
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
        source_label = PLATFORM_LABELS.get(platform, platform.capitalize())
        raise HTTPException(
            status_code=422,
            detail=format_friendly_extraction_error(platform, source_label, exc),
        )

    cleanup_temp_dir(tmp_dir)
    raise HTTPException(status_code=422, detail="No se pudo generar el archivo de descarga.")


@app.get("/{full_path:path}")
async def catch_all_prefix_url(full_path: str):
    """Serves index.html for any path prefix (e.g. ddx.cl/https://...) so the frontend can auto-extract the URL."""
    return FileResponse(
        str(STATIC_DIR / "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )

