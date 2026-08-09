FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN useradd --system --uid 10004 --create-home systemtrading
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates ./templates

USER 10004:10004
EXPOSE 8443
CMD ["gunicorn", "--bind", "0.0.0.0:8443", "--certfile", "/run/system-trading-tls/server.crt", "--keyfile", "/run/system-trading-tls/server.key", "--workers", "1", "--threads", "4", "--timeout", "30", "--no-control-socket", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
