FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# ── Install NEXUS SDK from local repo source ───────────────────────────────
# Build context is repo root, so paths below are relative to repo root.
COPY pyproject.toml /nexus-src/
COPY src /nexus-src/src
RUN uv pip install --system --quiet "/nexus-src[nexus]"

# ── Install demo backend dependencies ──────────────────────────────────────
COPY demo/backend/requirements.txt .
RUN uv pip install --system --quiet -r requirements.txt

# ── Copy demo backend source ───────────────────────────────────────────────
COPY demo/backend/ .

EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
