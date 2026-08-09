# Works identically across Render, Railway, or Fly.io — pick whichever
# has the best free tier when you deploy; this file doesn't change.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# No EXPOSE — this is a worker (MQTT pub/sub only), not an HTTP service.
CMD ["python", "main.py"]
