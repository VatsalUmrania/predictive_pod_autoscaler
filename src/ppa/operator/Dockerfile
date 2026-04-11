# Single-stage lightweight operator image
FROM python:3.11-slim

WORKDIR /app

# Copy lightweight operator requirements
COPY requirements.operator.txt .

# Install Python dependencies (pre-built wheels available for all packages)
RUN pip install --no-cache-dir -r requirements.operator.txt && rm requirements.operator.txt

# Copy entire ppa module to /app
COPY src/ppa/ ./ppa/

# Set Python environment
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Health check for Kubernetes
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import kopf; import sys; sys.exit(0)" || exit 1

# Start operator
CMD ["kopf", "run", "ppa/operator/main.py", "--standalone"]
