FROM python:3.11-slim

# Install minimal headless LibreOffice Calc components and Poppler
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-calc \
    poppler-utils \
    fonts-dejavu-core \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as an unprivileged user; it owns the analytics data directory.
RUN useradd --create-home appuser && mkdir -p /app/data && chown appuser /app/data
USER appuser

# Render exposes PORT dynamically via an env var (defaults to 10000)
ENV PORT=10000
EXPOSE 10000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
