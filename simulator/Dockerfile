# syntax=docker/dockerfile:1.7
# Simulator image: telemetry generator + live dashboard.
# Build context is the REPO ROOT (dashboard serves frontend/public):
#   docker build -f simulator/Dockerfile -t volta-simulator .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system --gid 10001 volta \
 && useradd  --system --uid 10001 --gid volta --home /srv/volta --shell /usr/sbin/nologin volta

WORKDIR /srv/volta
COPY simulator/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY --chown=volta:volta simulator/ /srv/volta/simulator/
COPY --chown=volta:volta frontend/public/ /srv/volta/frontend/public/
RUN mkdir -p /srv/volta/simulator/data && chown -R volta:volta /srv/volta/simulator/data

USER volta
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/', timeout=3).status == 200 else 1)" || exit 1

CMD ["python", "-m", "simulator.main", "--dashboard", "--dashboard-host", "0.0.0.0", "--quiet"]
