FROM python:3.10-slim

RUN pip install mlflow==2.14.2

EXPOSE 5012

CMD [ \
    "mlflow", "server", \
    "--backend-store-uri", "sqlite:///home/mlflow/mlflow.db", \
    "--host", "0.0.0.0", \
    "--port", "5012" \
]