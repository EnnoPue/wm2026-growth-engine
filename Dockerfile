# ---------------------------------------------------------------------------
# wm2026-growth-engine — Railway / Docker image
# Python 3.11 + ffmpeg + fonts. Single image runs scheduler + status API.
# ---------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=UTC

# System deps:
#   ffmpeg        -> video assembly / encoding (free)
#   fonts-dejavu  -> guaranteed font for drawtext / Pillow
#   libsndfile1   -> faster-whisper audio handling
#   curl          -> healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        fonts-liberation \
        libsndfile1 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python deps first for layer caching
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# App code
COPY . .

# Runtime data dirs (also created at startup; harmless if they exist)
RUN mkdir -p output logs data assets/fonts assets/music assets/backgrounds

# Railway injects $PORT. Default 8080 for local runs.
ENV PORT=8080
EXPOSE 8080

# Healthcheck hits the status API
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

# main.py boots the status API (foreground, binds $PORT) and the scheduler
# (background thread). This is the single entrypoint for Railway.
CMD ["python", "main.py"]
