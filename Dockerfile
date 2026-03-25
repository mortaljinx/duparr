FROM python:3.12-slim

# Install chromaprint (fpcalc) and curl for healthcheck
RUN apt-get update && apt-get install -y \
    libchromaprint-tools \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

RUN mkdir -p /data /duplicates /ui

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5045/health || exit 1

ENTRYPOINT ["python", "/app/app.py"]
