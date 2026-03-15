FROM python:3.13-slim

WORKDIR /app

# Install build dependencies needed for numpy compilation
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    g++ \
    gfortran \
    libopenblas-dev \
    liblapack-dev \
    && rm -rf /var/lib/apt/lists/*

COPY operator/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ common/
COPY operator/ .

CMD ["kopf", "run", "main.py", "--standalone"]