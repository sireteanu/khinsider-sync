# Khinsider Sync - web interface on port 8485
#   /config  catalog, settings, selection     /data  the synced files

# the dependency list on its own, so the (slow) install below is only redone when it changes -
# not on every new version
FROM python:3.12-slim AS deps
COPY pyproject.toml /tmp/
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('/tmp/pyproject.toml', 'rb'))['project']['dependencies']))" > /requirements.txt

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KHINSIDER_HOST=0.0.0.0 \
    KHINSIDER_PORT=8485 \
    KHINSIDER_CONFIG=/config \
    KHINSIDER_DATA=/data \
    KHINSIDER_DATA_LOCKED=1 \
    KHINSIDER_NO_BROWSER=1 \
    PUID=99 \
    PGID=100 \
    UMASK=000

# tzdata: TZ (e.g. America/New_York) drives file dates and the sync schedule
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deps /requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm -rf /root/.cache

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . && rm -rf /root/.cache

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

EXPOSE 8485
VOLUME ["/config", "/data"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('KHINSIDER_PORT', '8485'), timeout=5)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
