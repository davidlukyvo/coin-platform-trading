FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY research.py app.py ./
RUN useradd --create-home --uid 10003 research && chown -R research:research /app
USER research
CMD ["python", "app.py"]
