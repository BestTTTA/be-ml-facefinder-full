# syntax=docker/dockerfile:1.6
# ---------- builder: compile wheels (insightface needs a C++ toolchain) ----------
FROM python:3.11-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential g++ cmake git \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---------- model download (cached layer) ----------
FROM python:3.11-slim AS models
RUN apt-get update && apt-get install -y --no-install-recommends wget unzip && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /models/buffalo_l && cd /models/buffalo_l \
    && wget -q https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip \
    && unzip -q buffalo_l.zip && rm buffalo_l.zip

# ---------- runtime ----------
FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    APP_ENV=production FACE_CTX_ID=-1 FACE_MODEL_PATH=/root/.insightface
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
COPY --from=models /models /root/.insightface/models

COPY alembic.ini pyproject.toml ./
COPY migrations ./migrations
COPY app ./app
COPY facefindr ./facefindr
COPY main.py ./main.py
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p uploads

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
    CMD curl -fsS http://localhost:8000/health/live || exit 1
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
