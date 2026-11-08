# syntax=docker/dockerfile:1.7
# Backend image: API, ingest worker and device-command worker (same code, different command).
# Build context is the REPO ROOT (the API serves ../frontend/public at /ui):
#   docker build -f backend/Dockerfile -t volta-backend .

# ---- builder: resolve and compile wheels once ----
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /wheels
COPY backend/requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---- runtime ----
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 10001 volta \
 && useradd  --system --uid 10001 --gid volta --home /srv/volta --shell /usr/sbin/nologin volta

COPY --from=builder /wheels /wheels
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-index --find-links=/wheels -r /tmp/requirements.txt \
 && rm -rf /wheels /tmp/requirements.txt

# Keep the repo layout: webui.py resolves <repo>/frontend/public relative to the code.
WORKDIR /srv/volta/backend
COPY --chown=volta:volta backend/ /srv/volta/backend/
COPY --chown=volta:volta frontend/public/ /srv/volta/frontend/public/
RUN mkdir -p /srv/volta/backend/var/knowledge \
 && chown -R volta:volta /srv/volta/backend/var

USER volta
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
