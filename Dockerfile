FROM python:3.14-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends procps ca-certificates

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --upgrade setuptools

RUN pip install --no-cache-dir -r requirements.txt

COPY hdm.py .
COPY src ./src
COPY addresses.txt .
COPY .env .

CMD ["python", "hdm.py", "addresses.txt"]
