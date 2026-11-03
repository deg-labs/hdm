FROM python:3.15-slim AS builder

WORKDIR /app

# ckzg does not yet publish Python 3.15 wheels, so build its C extension.
# Keep compilers and make out of the runtime image.
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.15-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends procps ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt && \
    python -c "import ckzg; import hyperliquid" && \
    rm -rf /wheels

COPY hdm.py .
COPY src ./src

CMD ["python", "hdm.py", "addresses.txt"]
