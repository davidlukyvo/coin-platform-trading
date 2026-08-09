FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --system --uid 10005 --create-home paper
COPY engine.py app.py ./
USER 10005:10005
EXPOSE 8050
CMD ["python", "app.py"]
