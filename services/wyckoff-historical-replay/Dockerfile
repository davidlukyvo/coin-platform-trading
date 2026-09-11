FROM python:3.12-slim
WORKDIR /app
COPY services/wyckoff-historical-replay/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/wyckoff-shadow-adapter/engine.py /app/wyckoff_engine.py
COPY services/wyckoff-loss-minimizer/engine.py /app/loss_engine.py
COPY services/wyckoff-historical-replay/download.py services/wyckoff-historical-replay/replay.py services/wyckoff-historical-replay/gate_v3.py services/wyckoff-historical-replay/gate_v4.py services/wyckoff-historical-replay/benchmark_v1.py services/wyckoff-historical-replay/benchmark_v2.py /app/
RUN useradd --system --uid 10013 --create-home replaylab
USER 10013:10013
ENTRYPOINT ["python"]
