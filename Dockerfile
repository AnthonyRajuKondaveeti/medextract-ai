# ── Optimized Production Build ─────────────────────────────────────────────

FROM python:3.10-slim

# Prevent Python buffering & .pyc files
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy requirements first (better caching)
COPY requirements.txt .

# Install system deps + Python packages in one layer, then clean up build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    tesseract-ocr \
    gcc \
    && pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy app code
COPY . .

# Create directories
RUN mkdir -p /app/uploads /app/output /app/static

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8000/', timeout=5)" || exit 1

CMD ["python", "main.py"]