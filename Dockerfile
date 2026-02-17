FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends procps ca-certificates

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --upgrade setuptools

RUN pip install --no-cache-dir -r requirements.txt

RUN getent group nobody || groupadd nobody
RUN chown -R nobody:nobody /app
USER nobody

COPY hdm.py .
COPY src ./src
COPY addresses.txt .
COPY .env .

CMD ["python", "hdm.py", "addresses.txt"]
