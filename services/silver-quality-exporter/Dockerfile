FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quality.py app.py ./

FROM base AS test
COPY requirements-dev.txt test_quality.py ./
RUN pip install --no-cache-dir -r requirements-dev.txt && pytest -q

FROM base AS runtime
RUN useradd --system --uid 10008 --create-home silver-quality
USER 10008:10008
EXPOSE 8081
CMD ["python", "app.py"]
