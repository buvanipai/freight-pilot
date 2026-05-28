# Dockerfile
FROM python:3.11-slim

# System deps for psycopg2 (libpq) and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer-cache friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY api/      api/
COPY consumer/ consumer/
COPY producer/ producer/
COPY db/       db/
RUN mkdir -p models/

# Pre-compiled .pyc files land under /app so PYTHONPATH is clean
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# App Runner expects the container to listen on PORT (default 8080)
ENV PORT=8080
EXPOSE 8080

# Run schema migration then start the API
CMD python db/schema_apply.py && python db/seed_restore.py && \
    uvicorn api.main:app --host 0.0.0.0 --port ${PORT}
