"""
grpc_server.py - High-Performance gRPC Bookstore Service with Prometheus Metrics,
Structured JSON Logging, Trace Correlation, and Shared SQLite Persistence.

Enterprise Software Development - Assignment 2 (Part B & C)
"""

import time
import uuid
import logging
import os
from concurrent import futures
import grpc
from pythonjsonlogger import jsonlogger
from prometheus_client import start_http_server, Counter, Gauge, Histogram

from proto import bookstore_pb2, bookstore_pb2_grpc
import shared_db

# ---------------------------------------------------------
# 1. Structured JSON Logging Setup (Preserving A1 format)
# ---------------------------------------------------------
logger = logging.getLogger("bookstore_grpc")
logger.setLevel(logging.INFO)
logHandler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s')
logHandler.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(logHandler)

# ---------------------------------------------------------
# 2. Prometheus Metrics for gRPC Service
# ---------------------------------------------------------
GRPC_HANDLED_TOTAL = Counter(
    'grpc_server_handled_total',
    'Total number of RPCs completed on the server, partitioned by status code',
    ['grpc_service', 'grpc_method', 'grpc_code']
)

GRPC_HANDLING_SECONDS = Histogram(
    'grpc_server_handling_seconds',
    'Histogram of response latency for gRPC methods in seconds',
    ['grpc_service', 'grpc_method'],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5]
)

GRPC_IN_FLIGHT_REQUESTS = Gauge(
    'grpc_server_in_flight_requests',
    'Number of concurrent gRPC requests currently being processed',
    ['grpc_service', 'grpc_method']
)

BOOKS_CREATED_TOTAL = Counter(
    'books_created_total',
    'Total number of books created',
    ['protocol']
)

BOOKS_IN_INVENTORY = Gauge(
    'books_in_inventory',
    'Number of books currently in inventory'
)

# ---------------------------------------------------------
# 3. Interceptor for Logging, Metadata Tracing & Metrics
# ---------------------------------------------------------
class ObservabilityInterceptor(grpc.ServerInterceptor):
    def __init__(self, service_name: str = "bookstore.BookService"):
        self.service_name = service_name

    def intercept_service(self, continuation, handler_call_details):
        method_name = handler_call_details.method.split('/')[-1]
        raw_handler = continuation(handler_call_details)
        if raw_handler is None:
            return None

        def create_wrapper(behavior):
            def wrapper(request, context):
                # Extract correlation ID from gRPC metadata or generate new UUID
                metadata = dict(context.invocation_metadata())
                req_id = metadata.get('x-request-id') or metadata.get('request-id') or str(uuid.uuid4())
                
                # Send correlation ID back in trailing metadata
                context.set_trailing_metadata([('x-request-id', req_id)])

                start_time = time.time()
                GRPC_IN_FLIGHT_REQUESTS.labels(grpc_service=self.service_name, grpc_method=method_name).inc()
                status_code_name = "OK"

                try:
                    response = behavior(request, context)
                    if context.code() is not None:
                        status_code_name = context.code().name
                    return response
                except grpc.RpcError as rpc_err:
                    if hasattr(rpc_err, 'code'):
                        status_code_name = rpc_err.code().name
                    elif context.code() is not None:
                        status_code_name = context.code().name
                    else:
                        status_code_name = "UNKNOWN"
                    raise rpc_err
                except Exception as ex:
                    # Check if servicer called context.abort()
                    if context.code() is not None:
                        status_code_name = context.code().name
                    else:
                        status_code_name = "INTERNAL"
                    raise ex
                finally:
                    duration = time.time() - start_time
                    GRPC_IN_FLIGHT_REQUESTS.labels(grpc_service=self.service_name, grpc_method=method_name).dec()
                    
                    # Record execution metrics
                    GRPC_HANDLED_TOTAL.labels(
                        grpc_service=self.service_name,
                        grpc_method=method_name,
                        grpc_code=status_code_name
                    ).inc()
                    GRPC_HANDLING_SECONDS.labels(
                        grpc_service=self.service_name,
                        grpc_method=method_name
                    ).observe(duration)

                    # Structured JSON log
                    logger.info("gRPC call processed", extra={
                        'request_id': req_id,
                        'service': self.service_name,
                        'method': method_name,
                        'status': status_code_name,
                        'duration': round(duration, 4)
                    })

            return wrapper

        if raw_handler.unary_unary:
            return grpc.unary_unary_rpc_method_handler(
                create_wrapper(raw_handler.unary_unary),
                request_deserializer=raw_handler.request_deserializer,
                response_serializer=raw_handler.response_serializer
            )
        return raw_handler

# ---------------------------------------------------------
# 4. BookService Implementation
# ---------------------------------------------------------
class BookServiceServicer(bookstore_pb2_grpc.BookServiceServicer):
    def __init__(self):
        shared_db.init_db()
        BOOKS_IN_INVENTORY.set(shared_db.get_inventory_count())

    def CreateBook(self, request, context):
        metadata = dict(context.invocation_metadata())
        req_id = metadata.get('x-request-id', 'unknown')

        # 1. Input Validation
        if not request.title or not request.title.strip():
            logger.warning("Rejected CreateBook: Empty title", extra={'request_id': req_id})
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Title must not be empty.")
        if not request.author or not request.author.strip():
            logger.warning("Rejected CreateBook: Empty author", extra={'request_id': req_id})
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Author must not be empty.")
        if request.price <= 0:
            logger.warning(f"Rejected CreateBook: Non-positive price {request.price}", extra={'request_id': req_id})
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Price must be strictly positive.")

        # 2. Idempotency Check
        if request.idempotency_key:
            record = shared_db.get_idempotency_record(request.idempotency_key)
            if record:
                logger.info(f"gRPC Idempotency hit for key: {request.idempotency_key}", extra={'request_id': req_id})
                data = record["response_data"]
                return bookstore_pb2.BookResponse(
                    id=data["id"],
                    title=data["title"],
                    author=data["author"],
                    price=float(data["price"]),
                    created_at=data.get("created_at", "")
                )

        # 3. Create Book in Shared Persistence
        try:
            book_dict = shared_db.create_book(
                title=request.title,
                author=request.author,
                price=request.price
            )
        except shared_db.DuplicateBookError as err:
            logger.warning(f"gRPC Duplicate book conflict: {str(err)}", extra={'request_id': req_id})
            context.abort(grpc.StatusCode.ALREADY_EXISTS, str(err))
        except ValueError as err:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(err))
        except Exception as err:
            logger.error(f"gRPC Internal database error: {str(err)}", extra={'request_id': req_id})
            context.abort(grpc.StatusCode.INTERNAL, "Internal persistence error.")

        # 4. Save Idempotency Record if key provided
        if request.idempotency_key:
            shared_db.save_idempotency_record(
                key=request.idempotency_key,
                endpoint="gRPC:CreateBook",
                status_code=200,
                response_data=book_dict
            )

        # 5. Update Metrics
        BOOKS_CREATED_TOTAL.labels(protocol="grpc").inc()
        BOOKS_IN_INVENTORY.set(shared_db.get_inventory_count())

        return bookstore_pb2.BookResponse(
            id=book_dict["id"],
            title=book_dict["title"],
            author=book_dict["author"],
            price=float(book_dict["price"]),
            created_at=str(book_dict.get("created_at", ""))
        )

    def GetBook(self, request, context):
        metadata = dict(context.invocation_metadata())
        req_id = metadata.get('x-request-id', 'unknown')

        book_dict = shared_db.get_book_by_id(request.id)
        if not book_dict:
            logger.warning(f"gRPC Book ID {request.id} not found", extra={'request_id': req_id})
            context.abort(grpc.StatusCode.NOT_FOUND, f"Book with ID {request.id} not found.")

        return bookstore_pb2.BookResponse(
            id=book_dict["id"],
            title=book_dict["title"],
            author=book_dict["author"],
            price=float(book_dict["price"]),
            created_at=str(book_dict.get("created_at", ""))
        )

    def ListBooks(self, request, context):
        books_list = shared_db.get_all_books()
        responses = [
            bookstore_pb2.BookResponse(
                id=b["id"],
                title=b["title"],
                author=b["author"],
                price=float(b["price"]),
                created_at=str(b.get("created_at", ""))
            )
            for b in books_list
        ]
        return bookstore_pb2.BookListResponse(books=responses, total_count=len(responses))

def serve():
    port = os.environ.get("GRPC_PORT", "50051")
    metrics_port = int(os.environ.get("METRICS_PORT", "8001"))

    # Start Prometheus HTTP metrics server on port 8001
    start_http_server(metrics_port)
    logger.info(f"Prometheus metrics scraping endpoint active at http://0.0.0.0:{metrics_port}/metrics", extra={'request_id': 'system'})

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10),
        interceptors=[ObservabilityInterceptor()]
    )
    bookstore_pb2_grpc.add_BookServiceServicer_to_server(BookServiceServicer(), server)
    server.add_insecure_port(f"0.0.0.0:{port}")
    server.start()
    logger.info(f"Bookstore gRPC Service listening on 0.0.0.0:{port}", extra={'request_id': 'system'})
    
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Stopping gRPC server...", extra={'request_id': 'system'})
        server.stop(0)

if __name__ == "__main__":
    serve()
