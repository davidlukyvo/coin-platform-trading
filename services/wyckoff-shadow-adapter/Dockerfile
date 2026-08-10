FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --system --uid 10007 --create-home wyckoff
COPY engine.py app.py ./
USER 10007:10007
EXPOSE 8070
CMD ["python", "app.py"]
