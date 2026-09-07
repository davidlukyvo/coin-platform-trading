FROM python:3.12-slim AS base
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY engine.py app.py ./

FROM base AS runtime
RUN useradd --system --uid 10012 --create-home lossresearch
USER 10012:10012
CMD ["python", "app.py"]
