FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ common/
COPY operator/ .

# Models are mounted via PVC at /models, NOT baked into the image.
# Each CR specifies its own model path, defaulting to /models/{target}/ppa_model.tflite

CMD ["kopf", "run", "main.py", "--standalone"]
