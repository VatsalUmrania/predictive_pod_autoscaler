FROM python:3.11-slim

WORKDIR /app

COPY data-collection/requirements.txt data-collection/requirements.txt
RUN pip install --no-cache-dir -r data-collection/requirements.txt

COPY common/ common/
COPY data-collection/ data-collection/

CMD ["python3", "data-collection/export_training_data.py"]
