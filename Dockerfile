FROM python:3.12-slim

LABEL org.opencontainers.image.title="AlertNotes"
LABEL org.opencontainers.image.description="Operational memory for your production alerts"
LABEL org.opencontainers.image.source="https://github.com/yourusername/alertnotes"

WORKDIR /app

# Install dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY alertnotes/ ./alertnotes/

# Create data directory
RUN mkdir -p /data

# Non-root user for security
RUN useradd -r -u 1001 -g root alertnotes && \
    chown -R alertnotes:root /app /data
USER alertnotes

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/stats')"

CMD ["uvicorn", "alertnotes.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]