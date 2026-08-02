FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN useradd --create-home --uid 10001 collector \
    && mkdir -p /data/bronze /data/quarantine \
    && chown -R collector:collector /app /data

USER collector

CMD ["python", "app.py"]
