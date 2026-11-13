FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Generate gRPC stubs inside container
RUN python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. proto/bookstore.proto

ENV BOOKSTORE_DB_PATH=/app/data/bookstore.db
ENV START_GRPC_SERVER=true
ENV GRPC_PORT=50051
ENV PYTHONUNBUFFERED=1

EXPOSE 8000 50051

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
