FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app
COPY requirements.txt requirements-sync.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && pip install --no-cache-dir -r requirements-sync.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 sii \
    && useradd --uid 10001 --gid sii --create-home --shell /usr/sbin/nologin sii
COPY app/ ./app/

USER 10001:10001
EXPOSE 8000
CMD ["sh", "-ec", ": \"${PORTAL_USUARIO:?Set PORTAL_USUARIO}\" \"${PORTAL_PASSWORD:?Set PORTAL_PASSWORD}\" \"${SECRET_KEY:?Set SECRET_KEY}\"; exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-proxy-headers --no-access-log"]
