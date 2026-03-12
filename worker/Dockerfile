FROM python:3.12-slim

# Install ffmpeg (minimal) and clean up in same layer to reduce size
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /root/.cache

COPY downloader_worker.py .

# Koyeb uses PORT env var (default 8080)
EXPOSE 8080

# Health check for Koyeb
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

CMD ["python", "-u", "downloader_worker.py"]
