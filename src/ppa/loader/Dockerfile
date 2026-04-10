# Multi-stage build for PPA loader pod
# Handles model file copying and scaler regeneration on PVC

FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.operator.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --user -r requirements.operator.txt


# Final stage - lightweight runtime
FROM python:3.11-slim

WORKDIR /app

# Copy Python dependencies from builder
COPY --from=builder /root/.local /root/.local

# Make sure scripts in .local are usable
ENV PATH=/root/.local/bin:$PATH

# Copy application code
COPY src/ppa/ ./ppa/

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; print('ok'); sys.exit(0)" || exit 1

# Default: just wait for manual commands
CMD ["sleep", "infinity"]
