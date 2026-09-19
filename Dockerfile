FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render injects PORT env var. Default to 8000 for local runs.
ENV PORT=8000
EXPOSE 8000

# Use shell form so ${PORT} is interpolated at runtime (not build time)
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
