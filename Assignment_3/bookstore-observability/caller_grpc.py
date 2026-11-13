"""
caller_grpc.py - Standalone Python gRPC Client with Call Deadlines,
Trace Correlation Metadata, Argument Parsing, and Robust Error Handling.

Enterprise Software Development - Assignment 2 (Part B)
"""

import argparse
import sys
import uuid
import grpc

# Ensure utf-8 encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from proto import bookstore_pb2, bookstore_pb2_grpc

def call_create_book(stub, title: str, author: str, price: float, idempotency_key: str = "", timeout: float = 2.0):
    req_id = f"grpc-client-{uuid.uuid4().hex[:8]}"
    metadata = [('x-request-id', req_id)]
    request = bookstore_pb2.CreateBookRequest(
        title=title,
        author=author,
        price=price,
        idempotency_key=idempotency_key
    )

    print(f"\n[gRPC] Sending CreateBook (Deadline: {timeout}s, Request-ID: {req_id})")
    print(f"       Payload: Title='{title}', Author='{author}', Price=${price:.2f}, IdempotencyKey='{idempotency_key}'")

    try:
        response, call = stub.CreateBook.with_call(request, timeout=timeout, metadata=metadata)
        trailing_md = dict(call.trailing_metadata())
        print(f"✅ [gRPC OK] Book Created successfully!")
        print(f"   ID: {response.id} | Title: '{response.title}' | Author: '{response.author}' | Price: ${response.price:.2f}")
        print(f"   Trailing Correlation ID: {trailing_md.get('x-request-id', 'N/A')}")
        return response
    except grpc.RpcError as rpc_err:
        status_code = rpc_err.code()
        details = rpc_err.details()
        print(f"❌ [gRPC {status_code.name}] Status Code: {status_code.value[0]} ({status_code.name})")
        print(f"   Error Details: {details}")
        return None

def call_get_book(stub, book_id: int, timeout: float = 2.0):
    req_id = f"grpc-client-{uuid.uuid4().hex[:8]}"
    metadata = [('x-request-id', req_id)]
    request = bookstore_pb2.GetBookRequest(id=book_id)

    print(f"\n[gRPC] Sending GetBook (ID: {book_id}, Deadline: {timeout}s, Request-ID: {req_id})")

    try:
        response, call = stub.GetBook.with_call(request, timeout=timeout, metadata=metadata)
        print(f"✅ [gRPC OK] Book Found: ID={response.id}, Title='{response.title}', Author='{response.author}', Price=${response.price:.2f}")
        return response
    except grpc.RpcError as rpc_err:
        print(f"❌ [gRPC {rpc_err.code().name}] Status: {rpc_err.code().name} | Details: {rpc_err.details()}")
        return None

def call_list_books(stub, timeout: float = 2.0):
    req_id = f"grpc-client-{uuid.uuid4().hex[:8]}"
    metadata = [('x-request-id', req_id)]
    request = bookstore_pb2.ListBooksRequest()

    print(f"\n[gRPC] Sending ListBooks (Deadline: {timeout}s, Request-ID: {req_id})")
    try:
        response = stub.ListBooks(request, timeout=timeout, metadata=metadata)
        print(f"✅ [gRPC OK] Total Books in Catalog: {response.total_count}")
        for b in response.books:
            print(f"   - [#{b.id}] {b.title} by {b.author} (${b.price:.2f})")
        return response
    except grpc.RpcError as rpc_err:
        print(f"❌ [gRPC {rpc_err.code().name}] Details: {rpc_err.details()}")
        return None

def run_demonstration(server_address: str = "localhost:50051"):
    print("=" * 70)
    print("ASSIGNMENT 2 - PART B: gRPC CLIENT DEMONSTRATION")
    print(f"Target Server: {server_address}")
    print("=" * 70)

    with grpc.insecure_channel(server_address) as channel:
        stub = bookstore_pb2_grpc.BookServiceStub(channel)

        # 1. Successful CreateBook
        print("\n--- 1. SUCCESSFUL REQUEST: CreateBook ---")
        unique_key = f"grpc-key-{uuid.uuid4().hex[:6]}"
        demo_title = f"Distributed Systems Vol {uuid.uuid4().hex[:4]}"
        call_create_book(
            stub,
            title=demo_title,
            author="Martin Kleppmann",
            price=38.50,
            idempotency_key=unique_key
        )

        # 2. Safe Repeat / Idempotent CreateBook
        print("\n--- 2. SAFE REPEAT: CreateBook with identical Idempotency-Key ---")
        call_create_book(
            stub,
            title=demo_title,
            author="Martin Kleppmann",
            price=38.50,
            idempotency_key=unique_key
        )

        # 3. Failed CreateBook - Validation Error (Price <= 0)
        print("\n--- 3. FAILED REQUEST: Invalid Argument (Price <= 0) ---")
        call_create_book(
            stub,
            title="Free E-Book",
            author="Unknown",
            price=-5.00
        )

        # 4. Failed CreateBook - Duplicate Resource (ALREADY_EXISTS)
        print("\n--- 4. FAILED REQUEST: Duplicate Book (ALREADY_EXISTS) ---")
        call_create_book(
            stub,
            title="1984",
            author="George Orwell",
            price=9.99
        )

        # 5. Successful GetBook
        print("\n--- 5. SUCCESSFUL REQUEST: GetBook (ID=1) ---")
        call_get_book(stub, book_id=1)

        # 6. Failed GetBook - Resource Not Found (NOT_FOUND)
        print("\n--- 6. FAILED REQUEST: GetBook Non-Existent ID (NOT_FOUND) ---")
        call_get_book(stub, book_id=9999)

        # 7. List All Books
        print("\n--- 7. CATALOG LISTING: ListBooks ---")
        call_list_books(stub)

def main():
    parser = argparse.ArgumentParser(description="Bookstore gRPC Client")
    parser.add_argument("--server", default="localhost:50051", help="gRPC server address host:port")
    parser.add_argument("--mode", choices=["demo", "create", "get", "list"], default="demo", help="Client operation mode")
    parser.add_argument("--title", help="Book title for create")
    parser.add_argument("--author", help="Book author for create")
    parser.add_argument("--price", type=float, help="Book price for create")
    parser.add_argument("--id", type=int, help="Book ID for get")
    parser.add_argument("--timeout", type=float, default=2.0, help="RPC deadline in seconds")
    parser.add_argument("--idempotency-key", default="", help="Client idempotency key")
    args = parser.parse_args()

    if args.mode == "demo":
        run_demonstration(args.server)
    else:
        with grpc.insecure_channel(args.server) as channel:
            stub = bookstore_pb2_grpc.BookServiceStub(channel)
            if args.mode == "create":
                if not args.title or not args.author or args.price is None:
                    print("Error: --title, --author, and --price are required for create.")
                    sys.exit(1)
                call_create_book(stub, args.title, args.author, args.price, args.idempotency_key, args.timeout)
            elif args.mode == "get":
                if args.id is None:
                    print("Error: --id is required for get.")
                    sys.exit(1)
                call_get_book(stub, args.id, args.timeout)
            elif args.mode == "list":
                call_list_books(stub, args.timeout)

if __name__ == "__main__":
    main()
