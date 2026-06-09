# Animora AI backend — production container (Fly.io).
#
# The code is copied to an IMPORTABLE package dir `ai_backend` (the source
# dir is `ai-backend` with a hyphen, which Python can't import) so main.py's
# relative imports work and we run `uvicorn ai_backend.main:app`.
#
# SECURITY: no secret is baked in. The Bedrock key, JWT secret, and Redis URL
# arrive at runtime as env vars from `fly secrets` (see docs/DEPLOY.md). The
# .dockerignore excludes .env so it can never end up in an image layer.

FROM python:3.11-slim

WORKDIR /app

# System deps a couple of wheels want (cryptography/pillow are wheels, but
# keep curl for healthcheck debugging).
RUN apt-get update -q && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps. `boto3` is required for the Anthropic SDK's Bedrock
# client (the requirements pin `anthropic` without the [bedrock] extra).
COPY ai-backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt boto3

# App code as an importable package (hyphen dir -> underscore package).
COPY ai-backend/ ./ai_backend/

# Non-secret config. Secrets (AWS_BEARER_TOKEN_BEDROCK, JWT_SECRET, REDIS_URL)
# come from `fly secrets`, NOT from here and NOT from a committed .env.
ENV ANIMORA_ENV=production \
    ANIMORA_LLM_PROVIDER=bedrock \
    BEDROCK_AWS_REGION=us-east-1 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# ws-ping settings match dev_server/bundled_server so long agentic turns
# don't get killed by an idle-timeout mid-stream.
CMD ["uvicorn", "ai_backend.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--ws-ping-interval", "120", "--ws-ping-timeout", "300"]
