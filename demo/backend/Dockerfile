FROM python:3.11-slim
WORKDIR /app

# ── Install NEXUS SDK from local repo source ───────────────────────────────
# Build context is repo root, so paths below are relative to repo root.
# This mirrors exactly what a real developer does: pip install nexus-selfheal
# In production this would be: pip install nexus-selfheal (from PyPI)
COPY pyproject.toml /nexus-src/
COPY src /nexus-src/src
RUN pip install -e "/nexus-src[nexus]" --quiet

# ── Install demo backend dependencies ──────────────────────────────────────
COPY demo/backend/requirements.txt .
RUN pip install -r requirements.txt --quiet

# ── Copy demo backend source ───────────────────────────────────────────────
COPY demo/backend/ .

EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
