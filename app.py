import os
import re
import shutil
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
        "Extrae y descarga videos y audio automáticamente a partir del link desde "
        "Facebook, Instagram, TikTok, X, Pinterest, Threads, LinkedIn, Reddit, "
        "YouTube/Shorts, Twitch, Kick, Vimeo y Bluesky."
    ),
    version="3.0.0",
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
    "kick": re.compile(r"(?:https?://)?(?:www\.)?kick\.com/.+", re.I),
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

DEFAULT_VIDEO_OPTIONS = {
    "tiktok": [
        {"id": "res_540", "label": "MP4 · Calidad ligera (540p)", "meta": "540p · Menor tamaño", "ext": "mp4"},
        {"id": "res_720", "label": "MP4 · Calidad estándar (720p)", "meta": "720p · Sin marca de agua", "ext": "mp4"},
        {"id": "res_1080", "label": "MP4 · Full HD (1080p)", "meta": "1080p · Máxima definición", "ext": "mp4"},
    ],
    "instagram": [
        {"id": "res_480", "label": "MP4 · Calidad ligera (480p)", "meta": "480p · Descarga rápida", "ext": "mp4"},
        {"id": "res_720", "label": "MP4 · Calidad media (720p)", "meta": "720p · Reel / Post", "ext": "mp4"},
        {"id": "res_1080", "label": "MP4 · Resolución original (1080p)", "meta": "1080p · Full HD", "ext": "mp4"},
    ],
    "x": [
        {"id": "res_360", "label": "MP4 · Ligero para móvil (360p)", "meta": "360p · Menor tamaño", "ext": "mp4"},
        {"id": "res_720", "label": "MP4 · Definición estándar (720p)", "meta": "720p · Balanceado", "ext": "mp4"},
        {"id": "res_1080", "label": "MP4 · Alta definición (1080p)", "meta": "1080p · Máxima calidad", "ext": "mp4"},
    ],
    "facebook": [
        {"id": "res_480", "label": "MP4 · Calidad SD (480p)", "meta": "480p · Menor peso", "ext": "mp4"},
        {"id": "res_720", "label": "MP4 · Calidad HD (720p)", "meta": "720p · Alta definición", "ext": "mp4"},
        {"id": "res_1080", "label": "MP4 · Full HD (1080p)", "meta": "1080p · Máxima definición", "ext": "mp4"},
    ],
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
        return f"{max(100, int(size_bytes / 1024))} KB"
    return f"~{mb:.1f} MB"


def build_two_blocks_from_info(info: dict, platform: str) -> tuple[list[dict], list[dict]]:
    """
    Builds two ordered blocks from yt-dlp info:
    1) video_options: ordered strictly from lowest resolution to highest resolution, with the 1st marked recommended & primary.
    2) audio_options: audio-only download option.
    """
    formats = info.get("formats") or []
    duration = info.get("duration") or 15

    # Discover real video resolutions (short side in pixels: 360, 480, 540, 720, 1080, etc.)
    res_buckets: dict[int, dict] = {}
    for f in formats:
        vcodec = f.get("vcodec")
        if not vcodec or vcodec == "none" or not f.get("url"):
            continue
        w = f.get("width")
        h = f.get("height")
        if w and h:
            short_side = min(int(w), int(h))
        elif h:
            short_side = int(h)
        elif w:
            short_side = int(w)
        else:
            continue

        if short_side < 144:
            continue

        # Normalize to nearest standard bucket so UI is clean
        if short_side <= 380:
            bucket = 360
        elif short_side <= 500:
            bucket = 480
        elif short_side <= 600:
            bucket = 540
        elif short_side <= 800:
            bucket = 720
        elif short_side <= 1200:
            bucket = 1080
        else:
            bucket = 1440

        has_audio = f.get("acodec") not in (None, "none")
        size = f.get("filesize") or f.get("filesize_approx")
        if not size and f.get("tbr") and duration:
            size = (float(f["tbr"]) * 1000 / 8) * float(duration)

        existing = res_buckets.get(bucket)
        # Prefer progressive formats that already contain audio
        if not existing or (has_audio and not existing["has_audio"]):
            res_buckets[bucket] = {
                "bucket": bucket,
                "has_audio": has_audio,
                "size": size,
            }

    sorted_buckets = sorted(res_buckets.keys())

    video_options: list[dict] = []
    if len(sorted_buckets) >= 2:
        for idx, b in enumerate(sorted_buckets):
            item = res_buckets[b]
            size_str = format_size_mb(item["size"])
            if b <= 480:
                desc = "Calidad ligera"
            elif b <= 540:
                desc = "Calidad media"
            elif b <= 720:
                desc = "Calidad HD"
            else:
                desc = "Full HD"

            meta_parts = [f"{b}p"]
            if size_str:
                meta_parts.append(size_str)
            else:
                meta_parts.append("Menor peso" if idx == 0 else "Alta nitidez")

            video_options.append({
                "id": f"res_{b}",
                "label": f"MP4 · {desc} ({b}p)",
                "meta": " · ".join(meta_parts),
                "ext": "mp4",
                "primary": idx == 0,
                "recommended": idx == 0,
            })
    else:
        base_list = DEFAULT_VIDEO_OPTIONS.get(
            platform,
            [
                {"id": "res_480", "label": "MP4 · Calidad ligera (480p)", "meta": "480p · Menor tamaño", "ext": "mp4"},
                {"id": "res_720", "label": "MP4 · Calidad estándar (720p)", "meta": "720p · Balanceado", "ext": "mp4"},
                {"id": "res_1080", "label": "MP4 · Alta definición (1080p)", "meta": "1080p · Máxima calidad", "ext": "mp4"},
            ],
        )
        for idx, opt in enumerate(base_list):
            copy_opt = dict(opt)
            copy_opt["primary"] = idx == 0
            copy_opt["recommended"] = idx == 0
            video_options.append(copy_opt)

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


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


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

            video_options, audio_options = build_two_blocks_from_info(info, platform)
            return {
                "original_url": url,
                "platform": platform,
                "source_label": source_label,
                "title": info.get("title") or info.get("description") or f"Video de {source_label}",
                "duration": info.get("duration") or 15,
                "video_options": video_options,
                "audio_options": audio_options,
            }
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"No se pudo extraer el video. Verifica que el post sea público ({str(exc)[:120]}).",
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
    """
    Returns (format_selector_string, is_audio) using yt-dlp's 'res' (short side) filter
    so both 9:16 vertical Reels/TikToks and 16:9 horizontal videos match accurately.
    """
    if quality == "audio":
        return "bestaudio[ext=m4a]/bestaudio/worst[vcodec!=none]/best", True

    # Extract target resolution number (e.g. res_360 -> 360, res_720 -> 720)
    target_res = 720
    if quality.startswith("res_"):
        try:
            target_res = int(quality.split("_")[1])
        except ValueError:
            target_res = 720
    elif quality == "low":
        target_res = 480
    elif quality == "sd":
        target_res = 720
    elif quality == "hd":
        target_res = 1080

    # Progressive single-file MP4 at or below target_res first, then ffmpeg-muxed if available, then fallback to worst/best so it never fails
    if has_ffmpeg:
        selector = (
            f"best[vcodec!=none][acodec!=none][res<={target_res}][ext=mp4]/"
            f"bestvideo[res<={target_res}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[vcodec!=none][acodec!=none][res<={target_res}]/"
            f"bestvideo[res<={target_res}]+bestaudio/"
            f"worst[vcodec!=none][acodec!=none][ext=mp4]/"
            f"best[ext=mp4]/best"
        )
    else:
        selector = (
            f"best[vcodec!=none][acodec!=none][res<={target_res}][ext=mp4]/"
            f"best[vcodec!=none][acodec!=none][res<={target_res}]/"
            f"worst[vcodec!=none][acodec!=none][ext=mp4]/"
            f"best[vcodec!=none][acodec!=none]/best"
        )
    return selector, False


@app.get("/api/download")
async def download_video(
    url: str = Query(...),
    quality: str = Query("res_480"),
):
    platform = detect_platform(url) or "video"
    tmp_dir = tempfile.mkdtemp(prefix="dl_")
    output_template = os.path.join(tmp_dir, "media.%(ext)s")

    ffmpeg_path = get_ffmpeg_path()
    has_ffmpeg = ffmpeg_path is not None
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
            filename = f"ddx_{platform}_{quality}.{ext}"
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
            detail=f"No se pudo procesar la descarga ({str(exc)[:140]}).",
        )

    cleanup_temp_dir(tmp_dir)
    raise HTTPException(status_code=422, detail="No se pudo generar el archivo de descarga.")
