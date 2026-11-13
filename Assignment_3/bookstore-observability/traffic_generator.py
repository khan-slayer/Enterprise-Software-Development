"""
traffic_generator.py - Dual Protocol Traffic Generator for HTTP and gRPC.
Generates balanced, repeatable success and error calls to populate Prometheus & Grafana panels.

Enterprise Software Development - Assignment 2 (Part C)
"""

import sys
import time
import random
import uuid
import argparse
import requests
import grpc

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from proto import bookstore_pb2, bookstore_pb2_grpc

HTTP_URL = "http://127.0.0.1:8000"
GRPC_HOST = "127.0.0.1:50051"

def send_http_request():
    try:
        req_type = random.choice(["get_all", "get_one", "create_valid", "create_invalid"])
        if req_type == "get_all":
            requests.get(f"{HTTP_URL}/books", timeout=2.0)
        elif req_type == "get_one":
            book_id = random.choice([1, 2, 3, 9999])
            requests.get(f"{HTTP_URL}/books/{book_id}", timeout=2.0)
        elif req_type == "create_valid":
            book = {
                "title": f"HTTP Title {uuid.uuid4().hex[:4]}",
                "author": f"Author {random.randint(1, 50)}",
                "price": round(random.uniform(10.0, 50.0), 2)
            }
            requests.post(f"{HTTP_URL}/books", json=book, headers={"Idempotency-Key": str(uuid.uuid4())}, timeout=2.0)
        elif req_type == "create_invalid":
            # Negative price triggers 422 Unprocessable Entity
            requests.post(f"{HTTP_URL}/books", json={"title": "Invalid", "author": "Bad", "price": -5.0}, timeout=2.0)
    except requests.exceptions.RequestException as e:
        print(f"[HTTP] Request error: {e}")

def send_grpc_request(stub):
    try:
        rpc_type = random.choice(["list", "get_valid", "get_invalid", "create_valid", "create_invalid", "create_duplicate"])
        if rpc_type == "list":
            stub.ListBooks(bookstore_pb2.ListBooksRequest(), timeout=2.0)
        elif rpc_type == "get_valid":
            stub.GetBook(bookstore_pb2.GetBookRequest(id=1), timeout=2.0)
        elif rpc_type == "get_invalid":
            stub.GetBook(bookstore_pb2.GetBookRequest(id=9999), timeout=2.0)
        elif rpc_type == "create_valid":
            stub.CreateBook(
                bookstore_pb2.CreateBookRequest(
                    title=f"gRPC Title {uuid.uuid4().hex[:4]}",
                    author=f"Author {random.randint(1, 50)}",
                    price=round(random.uniform(12.0, 60.0), 2),
                    idempotency_key=str(uuid.uuid4())
                ),
                timeout=2.0
            )
        elif rpc_type == "create_invalid":
            stub.CreateBook(bookstore_pb2.CreateBookRequest(title="", author="", price=-1.0), timeout=2.0)
        elif rpc_type == "create_duplicate":
            # Triggers ALREADY_EXISTS
            stub.CreateBook(bookstore_pb2.CreateBookRequest(title="1984", author="George Orwell", price=8.99), timeout=2.0)
    except grpc.RpcError:
        pass # Expected errors generated for metrics tracking

def generate_normal_traffic():
    print(f"Generating continuous HTTP ({HTTP_URL}) & gRPC ({GRPC_HOST}) traffic... (Press Ctrl+C to stop)")
    with grpc.insecure_channel(GRPC_HOST) as channel:
        stub = bookstore_pb2_grpc.BookServiceStub(channel)
        while True:
            send_http_request()
            send_grpc_request(stub)
            time.sleep(random.uniform(0.1, 0.4))

def generate_metrics_burst():
    """Generates a structured, repeatable batch of calls to light up Grafana dashboard panels."""
    print("Generating structured metrics burst for Grafana dashboard...")
    with grpc.insecure_channel(GRPC_HOST) as channel:
        stub = bookstore_pb2_grpc.BookServiceStub(channel)
        
        # 1. Successful HTTP and gRPC calls
        print(" -> Generating successful HTTP and gRPC calls...")
        for _ in range(15):
            send_http_request()
            send_grpc_request(stub)
            time.sleep(0.05)

        # 2. Trigger Rate Limiting 429s on HTTP
        print(" -> Generating HTTP rate limit burst (429 Too Many Requests)...")
        for i in range(8):
            try:
                requests.post(
                    f"{HTTP_URL}/books",
                    json={"title": f"Burst Book {uuid.uuid4().hex[:4]}", "author": "Spammer", "price": 10.0},
                    timeout=2.0
                )
            except Exception:
                pass

        # 3. Controlled gRPC error codes
        print(" -> Generating controlled gRPC status codes (OK, INVALID_ARGUMENT, ALREADY_EXISTS, NOT_FOUND)...")
        for _ in range(10):
            send_grpc_request(stub)
            time.sleep(0.05)

    print("✅ Metrics burst completed successfully. Grafana panels populated!")

def run_cardinality_experiment():
    print("Running Cardinality Explosion Experiment from A1...")
    for i in range(100):
        try:
            requests.post(f"{HTTP_URL}/experiments/cardinality")
            print(f"Sent request {i+1}/100 to cardinality endpoint")
        except requests.exceptions.RequestException as e:
            print(f"Error: {e}")
        time.sleep(0.05)

def toggle_delay():
    try:
        res = requests.post(f"{HTTP_URL}/experiments/toggle-delay")
        print(f"Server response: {res.json()}")
    except requests.exceptions.RequestException as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Traffic Generator for HTTP & gRPC Bookstore")
    parser.add_argument('--mode', choices=['normal', 'metrics', 'cardinality', 'toggle-delay'], default='metrics')
    args = parser.parse_args()

    if args.mode == 'normal':
        generate_normal_traffic()
    elif args.mode == 'metrics':
        generate_metrics_burst()
    elif args.mode == 'cardinality':
        run_cardinality_experiment()
    elif args.mode == 'toggle-delay':
        toggle_delay()
