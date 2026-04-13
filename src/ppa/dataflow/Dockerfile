FROM python:3.11-slim

WORKDIR /app

COPY src/ppa /app/src/ppa

RUN pip install --no-cache-dir \
    pandas>=2.0.0 \
    numpy>=1.26.0 \
    requests>=2.31.0 \
    prometheus-client>=0.19.0 \
    joblib>=1.4.0 \
    scikit-learn>=1.5.0

ENV PYTHONPATH=/app/src

CMD ["python3", "-m", "ppa.dataflow.export_training_data"]
