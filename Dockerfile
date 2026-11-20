FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY . /tmp/build_ctx

RUN if [ -f /tmp/build_ctx/backend/requirements.txt ]; then \
        cp -a /tmp/build_ctx/backend/. /app/ ; \
    elif [ -f /tmp/build_ctx/requirements.txt ]; then \
        cp -a /tmp/build_ctx/. /app/ ; \
    else \
        echo "ERROR: requirements.txt & backend files not found in repository!" && ls -la /tmp/build_ctx && exit 1 ; \
    fi && \
    rm -rf /tmp/build_ctx && \
    pip install --no-cache-dir -r /app/requirements.txt && \
    mkdir -p /app/uploads

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --backlog 2048 --limit-concurrency 2500 --timeout-keep-alive 30 --ws auto --ws-ping-interval 25 --ws-ping-timeout 25 --proxy-headers --forwarded-allow-ips='*'"]
