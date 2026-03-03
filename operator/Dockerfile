FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Copy model artifacts (mounted or baked in)
COPY ../model/artifacts/ppa_model.tflite /app/model/ppa_model.tflite
COPY ../model/artifacts/scaler.pkl /app/model/scaler.pkl

CMD ["kopf", "run", "main.py", "--standalone"]
