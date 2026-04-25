FROM python:3.11-slim
WORKDIR /app

# Install the NEXUS SDK from the local repo (pip install -e — exactly as a dev would)
COPY ../../pyproject.toml ../../README.md /nexus-src/
COPY ../../src /nexus-src/src
RUN pip install -e "/nexus-src[nexus]" --quiet

# Install demo-specific deps
COPY requirements.txt .
RUN pip install -r requirements.txt --quiet

COPY . .

EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
