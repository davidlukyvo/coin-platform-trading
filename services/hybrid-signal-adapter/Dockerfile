FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --system --uid 10006 --create-home hybrid
COPY adapter.py app.py ./
USER 10006:10006
EXPOSE 8060
CMD ["python", "app.py"]
