FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends procps ca-certificates

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --upgrade setuptools

RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd -g 1000 appuser || true
RUN useradd -m -u 1000 -g 1000 -s /bin/sh appuser || true
RUN chown -R 1000:1000 /app
USER 1000:1000

COPY hdm.py .
COPY src ./src
COPY addresses.txt .
COPY .env .

CMD ["python", "hdm.py", "addresses.txt"]
