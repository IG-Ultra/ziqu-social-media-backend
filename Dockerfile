FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/uploads

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --backlog 2048 --limit-concurrency 2500 --timeout-keep-alive 30 --ws websockets --ws-ping-interval 25 --ws-ping-timeout 25 --proxy-headers --forwarded-allow-ips='*'"]
