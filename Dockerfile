# AI AURA worker — the always-on process that holds the live Pocket Option
# session, generates signals, and serves the API. Deploy to Railway / Render /
# Fly / any VPS (NOT Vercel — serverless can't hold a persistent WebSocket).
FROM python:3.13-slim

# System deps kept minimal; pandas/pyarrow/psycopg ship binary wheels.
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Provider needs to write data/ and logs/.
RUN mkdir -p data/raw/ticks logs

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# Bind to the platform-provided $PORT (Railway/Render set it), default 8000.
CMD ["sh", "-c", "python -m uvicorn apps.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
