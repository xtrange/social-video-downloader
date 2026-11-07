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
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="Video Downloader API",
    description=(
        "Extrae y descarga videos automáticamente a partir del link desde "
        "Facebook, Instagram, TikTok, X, Pinterest, Threads, LinkedIn, Reddit, "
        "YouTube/Shorts, Twitch, Kick, Vimeo y Bluesky."
    ),
    version="2.3.0",
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

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

SOURCE_SPECIFIC_OPTIONS = {
    "tiktok": {
        "source_label": "TikTok",
        "options": [
            {"id": "audio", "label": "Audio original", "meta": "M4A / MP3 · Solo audio (Más liviano)", "ext": "m4a", "primary": True},
            {"id": "sd", "label": "MP4 · Calidad estándar", "meta": "720p · Carga rápida", "ext": "mp4", "primary": False},
            {"id": "hd", "label": "MP4 · Sin marca de agua (HD)", "meta": "1080p · Máxima definición", "ext": "mp4", "primary": False},
        ],
    },
    "instagram": {
        "source_label": "Instagram",
        "options": [
            {"id": "audio", "label": "Pista de audio", "meta": "M4A / MP3 · Solo audio (Más liviano)", "ext": "m4a", "primary": True},
            {"id": "sd", "label": "MP4 · Calidad media", "meta": "720p · Ligero", "ext": "mp4", "primary": False},
            {"id": "hd", "label": "MP4 · Resolución original (Full HD)", "meta": "1080p · Reel / Post", "ext": "mp4", "primary": False},
        ],
    },
    "x": {
        "source_label": "X (Twitter)",
        "options": [
            {"id": "low", "label": "MP4 · Ligero para móvil (360p)", "meta": "360p · Menor tamaño", "ext": "mp4", "primary": True},
            {"id": "sd", "label": "MP4 · Definición estándar (720p)", "meta": "720p · Balanceado", "ext": "mp4", "primary": False},
            {"id": "hd", "label": "MP4 · Alta definición (1080p)", "meta": "1080p · Máxima calidad", "ext": "mp4", "primary": False},
        ],
    },
    "facebook": {
        "source_label": "Facebook",
        "options": [
            {"id": "audio", "label": "Solo audio", "meta": "M4A / MP3 · Pista de audio (Más liviano)", "ext": "m4a", "primary": True},
            {"id": "sd", "label": "MP4 · Calidad SD", "meta": "480p · Estándar", "ext": "mp4", "primary": False},
            {"id": "hd", "label": "MP4 · Calidad HD", "meta": "1080p / 720p · Alta definición", "ext": "mp4", "primary": False},
        ],
    },
    "pinterest": {
        "source_label": "Pinterest",
        "options": [
            {"id": "sd", "label": "MP4 · Calidad ligera", "meta": "480p · Menor tamaño", "ext": "mp4", "primary": True},
            {"id": "hd", "label": "MP4 · Video Pin Original (HD)", "meta": "1080p / 720p · Alta calidad", "ext": "mp4", "primary": False},
        ],
    },
    "threads": {
        "source_label": "Threads",
        "options": [
            {"id": "audio", "label": "Solo audio", "meta": "M4A / MP3 · Audio del post (Más liviano)", "ext": "m4a", "primary": True},
            {"id": "sd", "label": "MP4 · Calidad estándar", "meta": "720p · Compacto", "ext": "mp4", "primary": False},
            {"id": "hd", "label": "MP4 · Calidad original (HD)", "meta": "1080p · Video de Threads", "ext": "mp4", "primary": False},
        ],
    },
    "linkedin": {
        "source_label": "LinkedIn",
        "options": [
            {"id": "sd", "label": "MP4 · Versión ligera", "meta": "480p · Menor peso", "ext": "mp4", "primary": True},
            {"id": "hd", "label": "MP4 · Presentación / Post HD", "meta": "1080p / 720p · Alta nitidez", "ext": "mp4", "primary": False},
        ],
    },
    "reddit": {
        "source_label": "Reddit",
        "options": [
            {"id": "sd", "label": "MP4 · Compacto para compartir", "meta": "480p · Ideal para WhatsApp", "ext": "mp4", "primary": True},
            {"id": "hd", "label": "MP4 · Video de Reddit (HD)", "meta": "1080p / 720p · Máxima calidad", "ext": "mp4", "primary": False},
        ],
    },
    "youtube": {
        "source_label": "YouTube / Shorts",
        "options": [
            {"id": "audio", "label": "Solo Audio", "meta": "M4A / MP3 · Pista de audio (Más liviano)", "ext": "m4a", "primary": True},
            {"id": "sd", "label": "MP4 · Calidad estándar", "meta": "480p / 360p · Ligero", "ext": "mp4", "primary": False},
            {"id": "hd", "label": "MP4 · Alta definición (HD)", "meta": "1080p / 720p · Video + Audio", "ext": "mp4", "primary": False},
        ],
    },
    "twitch": {
        "source_label": "Twitch",
        "options": [
            {"id": "sd", "label": "MP4 · Clip en 720p", "meta": "720p · Menor peso", "ext": "mp4", "primary": True},
            {"id": "hd", "label": "MP4 · Clip en Full HD (1080p)", "meta": "1080p 60fps · Calidad original", "ext": "mp4", "primary": False},
        ],
    },
    "kick": {
        "source_label": "Kick",
        "options": [
            {"id": "sd", "label": "MP4 · Calidad media (720p)", "meta": "720p · Menor peso", "ext": "mp4", "primary": True},
            {"id": "hd", "label": "MP4 · Clip en Full HD", "meta": "1080p · Calidad original", "ext": "mp4", "primary": False},
        ],
    },
    "vimeo": {
        "source_label": "Vimeo",
        "options": [
            {"id": "audio", "label": "Solo Audio", "meta": "M4A / MP3 · Pista limpia (Más liviano)", "ext": "m4a", "primary": True},
            {"id": "sd", "label": "MP4 · Definición estándar (720p)", "meta": "720p · Balanceado", "ext": "mp4", "primary": False},
            {"id": "hd", "label": "MP4 · Master HD (1080p)", "meta": "Alta tasa de bits · MP4", "ext": "mp4", "primary": False},
        ],
    },
    "bluesky": {
        "source_label": "Bluesky",
        "options": [
            {"id": "sd", "label": "MP4 · Ligero", "meta": "480p · Menor tamaño", "ext": "mp4", "primary": True},
            {"id": "hd", "label": "MP4 · Video en alta definición", "meta": "1080p / 720p · MP4", "ext": "mp4", "primary": False},
        ],
    },
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


def select_format_stream(info: dict, quality: str) -> tuple[Optional[str], dict, str]:
    """Selects the best matching direct stream URL, headers, and extension from yt-dlp info."""
    formats = info.get("formats") or []
    base_headers = info.get("http_headers") or {"User-Agent": DEFAULT_UA}

    progressive = [
        f for f in formats
        if f.get("url")
        and f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
        and not str(f.get("protocol", "")).startswith("m3u8")
        and ".m3u8" not in str(f.get("url", ""))
    ]
    if not progressive:
        progressive = [
            f for f in formats
            if f.get("url") and f.get("vcodec") not in (None, "none") and not str(f.get("protocol", "")).startswith("m3u8")
        ]

    audio_only = [
        f for f in formats
        if f.get("url")
        and f.get("vcodec") in (None, "none")
        and f.get("acodec") not in (None, "none")
        and not str(f.get("protocol", "")).startswith("m3u8")
    ]

    progressive.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    audio_only.sort(key=lambda f: (f.get("abr") or f.get("tbr") or 0), reverse=True)

    chosen = None
    if quality == "audio" and audio_only:
        chosen = audio_only[0]
    elif quality == "low" and progressive:
        low_cands = [f for f in progressive if (f.get("height") or 0) <= 480]
        chosen = low_cands[0] if low_cands else progressive[-1]
    elif quality == "sd" and progressive:
        sd_cands = [f for f in progressive if (f.get("height") or 0) <= 720]
        chosen = sd_cands[0] if sd_cands else progressive[0]
    elif progressive:
        chosen = progressive[0]

    if chosen:
        ext = chosen.get("ext") or ("m4a" if quality == "audio" else "mp4")
        headers = dict(base_headers)
        if chosen.get("http_headers"):
            headers.update(chosen["http_headers"])
        return chosen["url"], headers, ext

    return info.get("url"), dict(base_headers), ("m4a" if quality == "audio" else "mp4")


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

    profile = SOURCE_SPECIFIC_OPTIONS[platform]

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

            return {
                "original_url": url,
                "platform": platform,
                "source_label": profile["source_label"],
                "title": info.get("title") or info.get("description") or f"Video de {profile['source_label']}",
                "duration": info.get("duration") or 15,
                "options": profile["options"],
            }
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"No se pudo extraer el video. Verifica que el post sea público ({str(exc)[:120]}).",
        )


def cleanup_temp_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


@app.get("/api/download")
async def download_video(
    url: str = Query(...),
    quality: str = Query("hd"),
):
    platform = detect_platform(url) or "video"
    tmp_dir = tempfile.mkdtemp(prefix="dl_")
    output_template = os.path.join(tmp_dir, "media.%(ext)s")

    has_ffmpeg = shutil.which("ffmpeg") is not None

    if quality == "audio":
        fmt_str = "bestaudio[ext=m4a]/bestaudio/best"
    elif quality == "low":
        fmt_str = "best[height<=480][ext=mp4]/best[height<=480]/best"
    elif quality == "sd":
        fmt_str = "best[height<=720][ext=mp4]/best[height<=720]/best"
    else:
        fmt_str = (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            if has_ffmpeg
            else "best[ext=mp4]/best"
        )

    ydl_opts = {
        "format": fmt_str,
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "http_headers": {"User-Agent": DEFAULT_UA},
    }
    if has_ffmpeg and quality != "audio":
        ydl_opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        files = os.listdir(tmp_dir)
        if files:
            chosen_file = os.path.join(tmp_dir, files[0])
            ext = os.path.splitext(chosen_file)[1].lstrip(".") or ("m4a" if quality == "audio" else "mp4")
            media_type = "audio/mp4" if ext in ("m4a", "mp3") else "video/mp4"
            filename = f"video_{platform}_{quality}.{ext}"
            return FileResponse(
                path=chosen_file,
                filename=filename,
                media_type=media_type,
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
                background=BackgroundTask(cleanup_temp_dir, tmp_dir),
            )
    except Exception:
        pass

    cleanup_temp_dir(tmp_dir)

    try:
        info_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "http_headers": {"User-Agent": DEFAULT_UA},
        }
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

        stream_url, req_headers, ext = select_format_stream(info, quality)
        if not stream_url:
            raise HTTPException(status_code=422, detail="No se encontró un enlace de descarga directa.")

        filename = f"video_{platform}_{quality}.{ext}"
        media_type = "audio/mp4" if ext in ("m4a", "mp3") else "video/mp4"

        async def stream_generator():
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                async with client.stream("GET", stream_url, headers=req_headers) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        yield chunk

        return StreamingResponse(
            stream_generator(),
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"No se pudo descargar el archivo: {str(exc)[:120]}",
        )
