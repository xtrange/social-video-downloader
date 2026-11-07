# Pulso — Descargador Web de Videos (Facebook, Instagram, TikTok y X)

Aplicación web full-stack limpia y directa para extraer y descargar videos en **MP4 (HD / SD)** o **Audio MP3** desde **Facebook, Instagram, TikTok y X (Twitter)** con solo pegar el enlace del post.

## Arquitectura

- **Frontend (`/static`)**: HTML5 semántico, Vanilla CSS con sistema de diseño adaptable (modo claro/oscuro) y Vanilla JavaScript sin dependencias pesadas.
  - Detección automática en tiempo real de la red social al pegar o escribir un link.
  - Vista previa con miniatura, duración, autor y título del post.
  - Selector de formato (`Video HD .mp4`, `Video Ligero .mp4`, `Solo Audio .mp3`).
  - Historial local de enlaces recientes guardado en `localStorage`.
- **Backend (`app.py`)**: Servidor en **FastAPI** impulsado por **`yt-dlp`** y **`FFmpeg`**.
  - `GET /api/health`: Verifica el estado de `yt-dlp` y `ffmpeg`.
  - `POST /api/info`: Extrae los metadatos reales del post sin descargar aún el archivo.
  - `GET /api/download`: Descarga y procesa el stream solicitado en un directorio temporal y lo entrega al navegador con limpieza automática.

## Requisitos previos

- Python 3.10+
- `ffmpeg` instalado en el sistema (`apt install ffmpeg` o `brew install ffmpeg`)

## Ejecución local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```
