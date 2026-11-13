"""
grpc_server.py - High-Performance gRPC Bookstore Service with Per-Replica
Prometheus Metrics, Initial & Trailing Metadata (x-replica-id, x-request-id),
gRPC Health Checking (grpc.health.v1.Health), Configurable Per-Replica Delay,
and Shared SQLite Persistence.

Enterprise Software Development - Assignment 3
"""

import time
import uuid
import logging
import os
from concurrent import futures
import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from pythonjsonlogger import jsonlogger
from prometheus_client import start_http_server

from proto import bookstore_pb2, bookstore_pb2_grpc
import shared_db
from metrics import (
    REPLICA_ID,
    GRPC_HANDLED_TOTAL,
    GRPC_HANDLING_SECONDS,
    GRPC_IN_FLIGHT_REQUESTS,
    BOOKS_CREATED_TOTAL,
    BOOKS_IN_INVENTORY,
)

# ---------------------------------------------------------
# 1. Structured JSON Logging Setup (with Replica ID)
# ---------------------------------------------------------
logger = logging.getLogger("bookstore_grpc")
logger.setLevel(logging.INFO)
logHandler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s %(replica)s"
)
logHandler.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(logHandler)


# ---------------------------------------------------------
# 2. Interceptor for Logging, Metadata Tracing, Delay & Metrics
# ---------------------------------------------------------
class ObservabilityInterceptor(grpc.ServerInterceptor):
    def __init__(self, service_name: str = "bookstore.BookService"):
        self.service_name = service_name

    def intercept_service(self, continuation, handler_call_details):
        full_method = handler_call_details.method or ""
        parts = full_method.strip("/").split("/")
        service_part = parts[0] if len(parts) > 1 else self.service_name
        method_name = parts[-1] if parts else "Unknown"

        raw_handler = continuation(handler_call_details)
        if raw_handler is None:
            return None

        # Bypass application metrics and verbose logs for Envoy active health checks
        if "grpc.health.v1.Health" in service_part:
            return raw_handler

        def create_wrapper(behavior):
            def wrapper(request, context):
                metadata = dict(context.invocation_metadata())
                req_id = (
                    metadata.get("x-request-id")
                    or metadata.get("request-id")
                    or str(uuid.uuid4())
                )

                # Send x-replica-id and x-request-id in BOTH initial HTTP/2 headers
                # (for Envoy's %RESP(X-REPLICA-ID)% access log) and trailing metadata
                meta_pairs = [
                    ("x-replica-id", REPLICA_ID),
                    ("x-request-id", req_id),
                ]
                try:
                    context.send_initial_metadata(meta_pairs)
                except Exception:
                    pass
                context.set_trailing_metadata(meta_pairs)

                start_time = time.time()
                GRPC_IN_FLIGHT_REQUESTS.labels(
                    replica=REPLICA_ID,
                    grpc_service=self.service_name,
                    grpc_method=method_name,
                ).inc()
                status_code_name = "OK"

                try:
                    # Inject per-replica fixed delay (Part D Least Request experiment)
                    delay_ms = shared_db.get_replica_delay_ms(REPLICA_ID)
                    if delay_ms > 0:
                        time.sleep(delay_ms / 1000.0)

                    response = behavior(request, context)
                    if context.code() is not None:
                        status_code_name = context.code().name
                    return response
                except grpc.RpcError as rpc_err:
                    if hasattr(rpc_err, "code"):
                        status_code_name = rpc_err.code().name
                    elif context.code() is not None:
                        status_code_name = context.code().name
                    else:
                        status_code_name = "UNKNOWN"
                    raise rpc_err
                except Exception as ex:
                    if context.code() is not None:
                        status_code_name = context.code().name
                    else:
                        status_code_name = "INTERNAL"
                    raise ex
                finally:
                    duration = time.time() - start_time
                    GRPC_IN_FLIGHT_REQUESTS.labels(
                        replica=REPLICA_ID,
                        grpc_service=self.service_name,
                        grpc_method=method_name,
                    ).dec()

                    GRPC_HANDLED_TOTAL.labels(
                        replica=REPLICA_ID,
                        grpc_service=self.service_name,
                        grpc_method=method_name,
                        grpc_code=status_code_name,
                    ).inc()
                    GRPC_HANDLING_SECONDS.labels(
                        replica=REPLICA_ID,
                        grpc_service=self.service_name,
                        grpc_method=method_name,
                    ).observe(duration)

                    logger.info(
                        "gRPC call processed",
                        extra={
                            "request_id": req_id,
                            "replica": REPLICA_ID,
                            "interface": "grpc",
                            "service": self.service_name,
                            "method": method_name,
                            "grpc_status": status_code_name,
                            "status": 200 if status_code_name == "OK" else 500,
                            "duration": round(duration, 4),
                        },
                    )

            return wrapper

        if raw_handler.unary_unary:
            return grpc.unary_unary_rpc_method_handler(
                create_wrapper(raw_handler.unary_unary),
                request_deserializer=raw_handler.request_deserializer,
                response_serializer=raw_handler.response_serializer,
            )
        return raw_handler


# ---------------------------------------------------------
# 3. BookService Implementation
# ---------------------------------------------------------
class BookServiceServicer(bookstore_pb2_grpc.BookServiceServicer):
    def __init__(self):
        shared_db.init_db()
        BOOKS_IN_INVENTORY.labels(replica=REPLICA_ID).set(
            shared_db.get_inventory_count()
        )

    def CreateBook(self, request, context):
        metadata = dict(context.invocation_metadata())
        req_id = metadata.get("x-request-id", "unknown")

        # 1. Input Validation
        if not request.title or not request.title.strip():
            logger.warning(
                "Rejected CreateBook: Empty title",
                extra={"request_id": req_id, "replica": REPLICA_ID},
            )
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Title must not be empty.")
        if not request.author or not request.author.strip():
            logger.warning(
                "Rejected CreateBook: Empty author",
                extra={"request_id": req_id, "replica": REPLICA_ID},
            )
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Author must not be empty.")
        if request.price <= 0:
            logger.warning(
                f"Rejected CreateBook: Non-positive price {request.price}",
                extra={"request_id": req_id, "replica": REPLICA_ID},
            )
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "Price must be strictly positive."
            )

        # 2. Idempotency Check
        if request.idempotency_key:
            record = shared_db.get_idempotency_record(request.idempotency_key)
            if record:
                logger.info(
                    f"gRPC Idempotency hit for key: {request.idempotency_key}",
                    extra={"request_id": req_id, "replica": REPLICA_ID},
                )
                data = record["response_data"]
                return bookstore_pb2.BookResponse(
                    id=data["id"],
                    title=data["title"],
                    author=data["author"],
                    price=float(data["price"]),
                    created_at=data.get("created_at", ""),
                )

        # 3. Create Book in Shared Persistence
        try:
            book_dict = shared_db.create_book(
                title=request.title,
                author=request.author,
                price=request.price,
            )
        except shared_db.DuplicateBookError as err:
            logger.warning(
                f"gRPC Duplicate book conflict: {str(err)}",
                extra={"request_id": req_id, "replica": REPLICA_ID},
            )
            context.abort(grpc.StatusCode.ALREADY_EXISTS, str(err))
        except ValueError as err:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(err))
        except Exception as err:
            logger.error(
                f"gRPC Internal database error: {str(err)}",
                extra={"request_id": req_id, "replica": REPLICA_ID},
            )
            context.abort(grpc.StatusCode.INTERNAL, "Internal persistence error.")

        # 4. Save Idempotency Record if key provided
        if request.idempotency_key:
            shared_db.save_idempotency_record(
                key=request.idempotency_key,
                endpoint="gRPC:CreateBook",
                status_code=200,
                response_data=book_dict,
            )

        # 5. Update Metrics
        BOOKS_CREATED_TOTAL.labels(protocol="grpc", replica=REPLICA_ID).inc()
        BOOKS_IN_INVENTORY.labels(replica=REPLICA_ID).set(
            shared_db.get_inventory_count()
        )

        return bookstore_pb2.BookResponse(
            id=book_dict["id"],
            title=book_dict["title"],
            author=book_dict["author"],
            price=float(book_dict["price"]),
            created_at=str(book_dict.get("created_at", "")),
        )

    def GetBook(self, request, context):
        metadata = dict(context.invocation_metadata())
        req_id = metadata.get("x-request-id", "unknown")

        book_dict = shared_db.get_book_by_id(request.id)
        if not book_dict:
            logger.warning(
                f"gRPC Book ID {request.id} not found",
                extra={"request_id": req_id, "replica": REPLICA_ID},
            )
            context.abort(
                grpc.StatusCode.NOT_FOUND, f"Book with ID {request.id} not found."
            )

        return bookstore_pb2.BookResponse(
            id=book_dict["id"],
            title=book_dict["title"],
            author=book_dict["author"],
            price=float(book_dict["price"]),
            created_at=str(book_dict.get("created_at", "")),
        )

    def ListBooks(self, request, context):
        books_list = shared_db.get_all_books()
        responses = [
            bookstore_pb2.BookResponse(
                id=b["id"],
                title=b["title"],
                author=b["author"],
                price=float(b["price"]),
                created_at=str(b.get("created_at", "")),
            )
            for b in books_list
        ]
        return bookstore_pb2.BookListResponse(
            books=responses, total_count=len(responses)
        )


def create_and_start_grpc_server(port: str = "50051") -> grpc.Server:
    """Starts the gRPC server (with BookService and HealthService) in a background thread pool."""
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=32),
        interceptors=[ObservabilityInterceptor()],
    )
    bookstore_pb2_grpc.add_BookServiceServicer_to_server(
        BookServiceServicer(), server
    )

    # Register standard gRPC Health Checking service for Envoy active health checks
    health_servicer = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    health_servicer.set(
        "bookstore.BookService", health_pb2.HealthCheckResponse.SERVING
    )

    server.add_insecure_port(f"0.0.0.0:{port}")
    server.start()
    logger.info(
        f"Bookstore gRPC Service (Replica {REPLICA_ID}) listening on 0.0.0.0:{port}",
        extra={"request_id": "system", "replica": REPLICA_ID},
    )
    return server


def serve():
    port = os.environ.get("GRPC_PORT", "50051")
    metrics_port = int(os.environ.get("METRICS_PORT", "8001"))

    start_http_server(metrics_port)
    logger.info(
        f"Prometheus metrics scraping endpoint active at http://0.0.0.0:{metrics_port}/metrics",
        extra={"request_id": "system", "replica": REPLICA_ID},
    )

    server = create_and_start_grpc_server(port)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info(
            "Stopping gRPC server...",
            extra={"request_id": "system", "replica": REPLICA_ID},
        )
        server.stop(0)


if __name__ == "__main__":
    serve()
