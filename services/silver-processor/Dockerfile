FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py processor.py ./
RUN useradd --create-home --uid 10002 silver && chown -R silver:silver /app
USER silver
CMD ["python", "app.py"]
