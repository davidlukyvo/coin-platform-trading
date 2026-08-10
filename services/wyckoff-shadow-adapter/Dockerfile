FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY engine.py app.py ./

FROM base AS test
COPY requirements-dev.txt test_engine.py ./
RUN pip install --no-cache-dir -r requirements-dev.txt && pytest -q

FROM base AS runtime
RUN useradd --system --uid 10007 --create-home wyckoff
USER 10007:10007
EXPOSE 8070
CMD ["python", "app.py"]
