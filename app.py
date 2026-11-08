import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

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
    version="3.3.0",
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


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

    # Group real video streams by their actual short-side resolution (e.g. 320, 360, 480, 540, 720, 1080)
    by_resolution: dict[int, dict] = {}

    for f in formats:
        vcodec = f.get("vcodec")
        if vcodec == "none":
            continue
        url = f.get("url")
        if not url:
            continue
        # Skip storyboard/mhtml images
        ext = (f.get("ext") or "mp4").lower()
        if ext in ("mhtml", "jpg", "png", "webp"):
            continue

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

        # Score candidate within the exact same resolution:
        # prefer direct MP4 with audio > any stream with audio > video-only stream
        score = (
            1 if (is_direct_mp4 and has_audio) else 0,
            1 if has_audio else 0,
            1 if is_direct_mp4 else 0,
            tbr,
        )

        existing = by_resolution.get(short_side)
        if not existing or score > existing["score"]:
            by_resolution[short_side] = {
                "short_side": short_side,
                "dim_label": dim_label,
                "format_id": str(f.get("format_id") or "best"),
                "has_audio": has_audio,
                "size": size,
                "score": score,
            }

    # If multiple real resolutions exist alongside an unlabelled '0' resolution, drop the unlabelled one
    if len(by_resolution) > 1 and 0 in by_resolution:
        del by_resolution[0]

    sorted_keys = sorted(by_resolution.keys())

    video_options: list[dict] = []
    if sorted_keys:
        for idx, res_key in enumerate(sorted_keys):
            item = by_resolution[res_key]
            size_str = format_size_mb(item["size"])
            res_tag = f"{res_key}p" if res_key > 0 else "Resolución única"

            meta_parts = [item["dim_label"]]
            if size_str:
                meta_parts.append(size_str)

            # Encode exact format_id (or res) so /api/download fetches this exact hosted version
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
    else:
        # Single direct URL on info dict without a multi-format list
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
        # 1. Try Twitter Syndication API which lists all real MP4 variants on video.twimg.com
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

        # 2. If Syndication API didn't return variants, query vxtwitter which returns the real hosted MP4 and its exact dimensions
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
    """Proxies remote social media thumbnail images so Instagram/FB/TikTok/X CORP headers never block the screenshot."""
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
    url = (payload.url or "").strip()
    platform = detect_platform(url)
    if not platform:
        raise HTTPException(
            status_code=400,
            detail=(
                "Enlace no reconocido. Soportamos Facebook, Instagram, TikTok, X, "
                "Pinterest, Threads, LinkedIn, Reddit, YouTube, Twitch, Kick, Vimeo y Bluesky."
            ),
        )

    source_label = PLATFORM_LABELS.get(platform, platform.capitalize())

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
            info = ydl.extract_info(url, download=False)
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            video_options, audio_options = build_exact_blocks_from_info(info)
            raw_desc = (info.get("description") or "").strip()
            raw_title = (info.get("title") or "").strip()
            # Combine or pick the richest text description available
            if raw_desc and len(raw_desc) > len(raw_title):
                full_description = raw_desc
            else:
                full_description = raw_title or raw_desc or f"Video de {source_label}"

            uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""
            if uploader and not str(uploader).startswith("@"):
                uploader = f"@{uploader}"

            thumb = info.get("thumbnail")
            if not thumb and info.get("thumbnails"):
                thumbs = [t.get("url") for t in info["thumbnails"] if t.get("url")]
                if thumbs:
                    thumb = thumbs[-1]

            return {
                "original_url": url,
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
        if platform == "x":
            x_data = await fetch_x_real_variants(url)
            if x_data:
                return {
                    "original_url": url,
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

    # Exact format_id discovered from the origin server during /api/info
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

    # If the user selected a specific X syndication variant (xvar_...), stream that exact variant URL
    if platform == "x" and (quality.startswith("xvar_") or is_audio):
        x_data = await fetch_x_real_variants(url)
        if x_data and x_data.get("variant_urls"):
            vurls = x_data["variant_urls"]
            target_url = vurls.get(quality) or vurls.get("default")
            if target_url and not is_audio:
                filename = f"ddx_x_{quality.replace('xvar_', '')}p.mp4"
                async def stream_x_variant():
                    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                        async with client.stream("GET", target_url, headers={"User-Agent": DEFAULT_UA}) as resp:
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
            elif target_url and is_audio and has_ffmpeg:
                tmp_dir_x = tempfile.mkdtemp(prefix="dl_x_")
                out_mp3 = os.path.join(tmp_dir_x, "audio.mp3")
                try:
                    subprocess.run(
                        [ffmpeg_path, "-y", "-i", target_url, "-vn", "-acodec", "libmp3lame", "-b:a", "128k", out_mp3],
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
            ydl.extract_info(url, download=True)

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
