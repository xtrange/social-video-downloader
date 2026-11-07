import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="Video Downloader API",
    description="Extrae y descarga videos de Facebook, Instagram, TikTok y X automáticamente a partir del link.",
    version="2.1.0",
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

PLATFORM_PATTERNS = {
    "facebook": re.compile(r"(?:https?://)?(?:www\.|m\.|web\.)?(?:facebook\.com|fb\.watch|fb\.com)/.+", re.I),
    "instagram": re.compile(r"(?:https?://)?(?:www\.)?(?:instagram\.com|instagr\.am)/(?:p|reel|reels|tv|stories)/.+", re.I),
    "tiktok": re.compile(r"(?:https?://)?(?:www\.|vm\.|vt\.)?tiktok\.com/.+", re.I),
    "x": re.compile(r"(?:https?://)?(?:www\.|mobile\.)?(?:twitter\.com|x\.com)/.+", re.I),
}

SOURCE_SPECIFIC_OPTIONS = {
    "tiktok": {
        "source_label": "TikTok",
        "options": [
            {"id": "hd", "label": "MP4 · Sin marca de agua (HD)", "meta": "1080p · Vídeo limpio", "ext": "mp4", "primary": True},
            {"id": "sd", "label": "MP4 · Calidad estándar", "meta": "720p · Carga rápida", "ext": "mp4", "primary": False},
            {"id": "audio", "label": "Audio original", "meta": "M4A / MP3 · Solo audio", "ext": "m4a", "primary": False},
        ],
    },
    "instagram": {
        "source_label": "Instagram",
        "options": [
            {"id": "hd", "label": "MP4 · Resolución original (Full HD)", "meta": "1080p · Reel / Post", "ext": "mp4", "primary": True},
            {"id": "sd", "label": "MP4 · Calidad media", "meta": "720p · Ligero", "ext": "mp4", "primary": False},
            {"id": "audio", "label": "Pista de audio", "meta": "M4A / MP3 · Solo audio", "ext": "m4a", "primary": False},
        ],
    },
    "x": {
        "source_label": "X (Twitter)",
        "options": [
            {"id": "hd", "label": "MP4 · Alta definición (1080p)", "meta": "1080p · Máxima calidad", "ext": "mp4", "primary": True},
            {"id": "sd", "label": "MP4 · Definición estándar (720p)", "meta": "720p · Balanceado", "ext": "mp4", "primary": False},
            {"id": "low", "label": "MP4 · Ligero para móvil (360p)", "meta": "360p · Menor tamaño", "ext": "mp4", "primary": False},
        ],
    },
    "facebook": {
        "source_label": "Facebook",
        "options": [
            {"id": "hd", "label": "MP4 · Calidad HD", "meta": "1080p / 720p · Alta definición", "ext": "mp4", "primary": True},
            {"id": "sd", "label": "MP4 · Calidad SD", "meta": "480p · Estándar", "ext": "mp4", "primary": False},
            {"id": "audio", "label": "Solo audio", "meta": "M4A / MP3 · Pista de audio", "ext": "m4a", "primary": False},
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
    except Exception:
        return None
    return None


def extract_direct_urls(info: dict, base_options: list[dict]) -> list[dict]:
    """Extracts direct CDN URLs from yt-dlp info dict so Vercel Serverless can link directly without 4.5MB payload limits."""
    formats = info.get("formats") or []
    progressive = [
        f for f in formats
        if f.get("url") and f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")
    ]
    audio_only = [
        f for f in formats
        if f.get("url") and f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")
    ]

    progressive.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    audio_only.sort(key=lambda f: (f.get("abr") or f.get("tbr") or 0), reverse=True)

    fallback_url = info.get("url") or (progressive[0]["url"] if progressive else None)
    hd_url = progressive[0]["url"] if progressive else fallback_url
    sd_candidates = [f for f in progressive if (f.get("height") or 0) <= 720]
    sd_url = sd_candidates[0]["url"] if sd_candidates else hd_url
    low_candidates = [f for f in progressive if (f.get("height") or 0) <= 480]
    low_url = low_candidates[0]["url"] if low_candidates else sd_url
    audio_url = audio_only[0]["url"] if audio_only else sd_url

    url_map = {"hd": hd_url, "sd": sd_url, "low": low_url, "audio": audio_url}

    enriched = []
    for opt in base_options:
        copy_opt = dict(opt)
        copy_opt["direct_url"] = url_map.get(opt["id"]) or fallback_url
        enriched.append(copy_opt)
    return enriched


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
            detail="Enlace no reconocido. Pega un link válido de Facebook, Instagram, TikTok o X.",
        )

    profile = SOURCE_SPECIFIC_OPTIONS[platform]

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 10,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if "entries" in info and info["entries"]:
                info = info["entries"][0]

            options = extract_direct_urls(info, profile["options"])
            return {
                "original_url": url,
                "platform": platform,
                "source_label": profile["source_label"],
                "title": info.get("title") or info.get("description") or f"Video de {profile['source_label']}",
                "duration": info.get("duration") or 15,
                "options": options,
            }
    except Exception:
        return {
            "original_url": url,
            "platform": platform,
            "source_label": profile["source_label"],
            "title": f"Publicación de {profile['source_label']} ({url[:40]}...)",
            "duration": 15,
            "options": profile["options"],
        }


def cleanup_temp_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


@app.get("/api/download")
async def download_video(
    url: str = Query(...),
    quality: str = Query("hd"),
):
    platform = detect_platform(url) or "video"
    is_serverless = bool(os.environ.get("VERCEL")) or shutil.which("ffmpeg") is None

    if is_serverless:
        ydl_opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if "entries" in info and info["entries"]:
                info = info["entries"][0]
            profile = SOURCE_SPECIFIC_OPTIONS.get(platform, SOURCE_SPECIFIC_OPTIONS["tiktok"])
            opts = extract_direct_urls(info, profile["options"])
            chosen = next((o for o in opts if o["id"] == quality), opts[0])
            if chosen.get("direct_url"):
                return RedirectResponse(url=chosen["direct_url"], status_code=302)

    tmp_dir = tempfile.mkdtemp(prefix="dl_")
    output_template = os.path.join(tmp_dir, "media.%(ext)s")

    if quality == "audio":
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        }
    elif quality in ("sd", "low"):
        max_h = 480 if quality == "low" else 720
        ydl_opts = {
            "format": f"best[height<={max_h}][ext=mp4]/best[height<={max_h}]/best",
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
        }
    else:
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)

    files = os.listdir(tmp_dir)
    if not files:
        cleanup_temp_dir(tmp_dir)
        raise HTTPException(status_code=422, detail="No se pudo generar el archivo.")

    chosen_file = os.path.join(tmp_dir, files[0])
    ext = os.path.splitext(chosen_file)[1].lstrip(".") or ("mp3" if quality == "audio" else "mp4")
    media_type = "audio/mpeg" if ext == "mp3" else "video/mp4"
    return FileResponse(
        path=chosen_file,
        filename=f"{platform}_{quality}.{ext}",
        media_type=media_type,
        background=BackgroundTask(cleanup_temp_dir, tmp_dir),
    )
