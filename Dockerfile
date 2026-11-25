# ========================================================
# AI Engineering YouTube Shorts Automation - Dockerfile
# ========================================================
FROM python:3.11-slim-bookworm

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata \
    DEBIAN_FRONTEND=noninteractive

# Install system dependencies: FFmpeg, tzdata, curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    tzdata \
    ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency requirements first to leverage Docker layer caching
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code, CLI, and configuration
COPY app/ ./app/
COPY cli.py .
COPY pytest.ini .
COPY tests/ ./tests/

# Pre-create standard data directories with proper permissions
RUN mkdir -p data/packages data/flow_exports data/final data/logs logs

EXPOSE 8000

# Health check using FastAPI /health endpoint (honors Render's $PORT)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD sh -c 'curl -f http://localhost:${PORT:-8000}/health || exit 1'

ENTRYPOINT ["python", "cli.py"]
CMD ["serve", "--host", "0.0.0.0"]
