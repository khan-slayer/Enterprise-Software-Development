import asyncio
import os
import re
import time
import uuid
import logging
from contextlib import asynccontextmanager
from typing import List, Optional
from collections import defaultdict

from fastapi import FastAPI, Request, Response, Header, Query, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ConfigDict
from pythonjsonlogger import jsonlogger
from prometheus_fastapi_instrumentator import Instrumentator

import shared_db
import grpc_server
from metrics import (
    REPLICA_ID,
    BOOKS_CREATED_TOTAL,
    BOOKS_IN_INVENTORY,
    BOOK_CREATION_TIME_SECONDS,
    BOOK_CREATION_TIME_SUMMARY,
    HTTP_RATE_LIMITED_TOTAL,
    DEMO_REQUESTS_TOTAL,
    HTTP_SERVER_REQUESTS_TOTAL,
    HTTP_SERVER_IN_FLIGHT_REQUESTS,
    HTTP_SERVER_REQUEST_DURATION_SECONDS,
)

# ---------------------------------------------------------
# 1. Structured JSON Logging Setup (with Replica ID)
# ---------------------------------------------------------
logger = logging.getLogger("bookstore_api")
logger.setLevel(logging.INFO)
logHandler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s %(replica)s"
)
logHandler.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(logHandler)
logging.getLogger("uvicorn.access").disabled = True

# ---------------------------------------------------------
# 2. Lifespan: Start Unified gRPC Server + Shared DB
# ---------------------------------------------------------
_grpc_server_instance = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _grpc_server_instance
    shared_db.init_db()
    BOOKS_IN_INVENTORY.labels(replica=REPLICA_ID).set(
        shared_db.get_inventory_count()
    )
    if os.environ.get("START_GRPC_SERVER", "true").lower() == "true":
        grpc_port = os.environ.get("GRPC_PORT", "50051")
        _grpc_server_instance = grpc_server.create_and_start_grpc_server(
            port=grpc_port
        )
    logger.info(
        f"Bookstore HTTP+gRPC Replica {REPLICA_ID} initialized",
        extra={"request_id": "system", "replica": REPLICA_ID},
    )
    yield
    if _grpc_server_instance is not None:
        _grpc_server_instance.stop(0)


# ---------------------------------------------------------
# 3. FastAPI Application Definition
# ---------------------------------------------------------
app = FastAPI(
    title=f"Bookstore HTTP API (Replica {REPLICA_ID})",
    version="3.0.0",
    description=(
        "Enterprise Software Development Assignment 3 - Scaled HTTP & gRPC "
        "Replica behind Envoy Proxy with Per-Replica Observability."
    ),
    openapi_tags=[
        {
            "name": "Books",
            "description": "Operations on book resources and catalog inventory.",
        },
        {
            "name": "System",
            "description": "Health checks, diagnostics, and experiment controls.",
        },
    ],
    lifespan=lifespan,
)

# Auto-instrument FastAPI routes with in-progress gauge enabled
Instrumentator(
    should_instrument_requests_inprogress=True,
    excluded_handlers=["/metrics", "/health"],
).instrument(app).expose(app)

# ---------------------------------------------------------
# 4. State & Rate Limiter Configuration (Per-Replica In-Memory)
# ---------------------------------------------------------
shared_db.init_db()
BOOKS_IN_INVENTORY.labels(replica=REPLICA_ID).set(
    shared_db.get_inventory_count()
)

problem_enabled = False
request_counter = 0

# Rate Limiter State: kept separately in each replica's memory (5 req / 10s)
RATE_LIMIT_MAX_REQUESTS = 5
RATE_LIMIT_WINDOW_SECONDS = 10
rate_limit_records = defaultdict(list)


def is_rate_limited(client_ip: str) -> tuple[bool, int]:
    """Sliding-window rate limiter per client IP (in-memory per replica)."""
    if client_ip in ("::1", "localhost", "testclient"):
        client_ip = "127.0.0.1"

    now = time.time()
    timestamps = rate_limit_records[client_ip]
    rate_limit_records[client_ip] = [
        ts for ts in timestamps if now - ts < RATE_LIMIT_WINDOW_SECONDS
    ]

    if len(rate_limit_records[client_ip]) >= RATE_LIMIT_MAX_REQUESTS:
        oldest_ts = rate_limit_records[client_ip][0]
        retry_after = max(
            1, int(RATE_LIMIT_WINDOW_SECONDS - (now - oldest_ts))
        )
        return True, retry_after

    rate_limit_records[client_ip].append(now)
    return False, 0


def normalize_handler_path(path: str) -> str:
    """Normalizes URL paths so book IDs never create high-cardinality metric labels."""
    if re.match(r"^/books/\d+$", path):
        return "/books/{book_id}"
    return path


# ---------------------------------------------------------
# 5. Pydantic Models & RFC 7807 Error Responses
# ---------------------------------------------------------
class BookCreate(BaseModel):
    title: str = Field(
        ..., min_length=1, max_length=200, description="Title of the book"
    )
    author: str = Field(
        ..., min_length=1, max_length=100, description="Author's full name"
    )
    price: float = Field(
        ..., gt=0.0, description="Retail price in USD, strictly positive"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "title": "Clean Architecture",
                "author": "Robert C. Martin",
                "price": 29.99,
            }
        }
    )


class Book(BaseModel):
    id: int = Field(..., description="Unique book identifier")
    title: str
    author: str
    price: float
    created_at: Optional[str] = None


class RFC7807Problem(BaseModel):
    type: str = Field(
        ..., description="URI reference identifying the problem type"
    )
    title: str = Field(
        ..., description="Short, human-readable summary of problem"
    )
    status: int = Field(..., description="HTTP status code")
    detail: str = Field(
        ..., description="Human-readable explanation specific to this occurrence"
    )
    instance: str = Field(
        ..., description="URI reference identifying the specific occurrence"
    )


def problem_response(
    status_code: int,
    title: str,
    detail: str,
    path: str,
    extra_headers: Optional[dict] = None,
) -> JSONResponse:
    type_slug = title.lower().replace(" ", "-")
    content = {
        "type": f"https://api.bookstore.example/errors/{type_slug}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": path,
        "replica": REPLICA_ID,
    }
    headers = {
        "Content-Type": "application/problem+json",
        "x-replica-id": REPLICA_ID,
    }
    if extra_headers:
        headers.update(extra_headers)
    return JSONResponse(status_code=status_code, content=content, headers=headers)


# ---------------------------------------------------------
# 6. Global Middleware & Exception Handlers
# ---------------------------------------------------------
@app.middleware("http")
async def correlation_and_logging_middleware(request: Request, call_next):
    global request_counter
    request_counter += 1

    req_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.req_id = req_id

    raw_path = request.url.path
    handler = normalize_handler_path(raw_path)
    is_observability_or_admin = raw_path in ("/metrics", "/health") or raw_path.startswith("/admin")

    start_time = time.time()
    if not is_observability_or_admin:
        HTTP_SERVER_IN_FLIGHT_REQUESTS.labels(
            replica=REPLICA_ID, handler=handler
        ).inc()

    status_code = 500
    try:
        # Inject per-replica fixed delay for application traffic (Part D Least Request)
        if not is_observability_or_admin:
            delay_ms = shared_db.get_replica_delay_ms(REPLICA_ID)
            if delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000.0)

        response = await call_next(request)
        status_code = response.status_code
        process_time = time.time() - start_time
        response.headers["x-request-id"] = req_id
        response.headers["x-replica-id"] = REPLICA_ID

        if not is_observability_or_admin:
            logger.info(
                "HTTP request processed",
                extra={
                    "request_id": req_id,
                    "replica": REPLICA_ID,
                    "interface": "http",
                    "method": request.method,
                    "path": raw_path,
                    "handler": handler,
                    "status": status_code,
                    "duration": round(process_time, 4),
                },
            )
        return response
    except Exception as e:
        process_time = time.time() - start_time
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        logger.error(
            f"Unhandled server error: {str(e)}",
            extra={
                "request_id": req_id,
                "replica": REPLICA_ID,
                "interface": "http",
                "method": request.method,
                "path": raw_path,
                "status": status_code,
                "duration": round(process_time, 4),
            },
        )
        return problem_response(
            status_code=status_code,
            title="Internal Server Error",
            detail=f"An unexpected error occurred: {str(e)}",
            path=raw_path,
            extra_headers={"x-request-id": req_id, "x-replica-id": REPLICA_ID},
        )
    finally:
        process_time = time.time() - start_time
        if not is_observability_or_admin:
            HTTP_SERVER_IN_FLIGHT_REQUESTS.labels(
                replica=REPLICA_ID, handler=handler
            ).dec()
            HTTP_SERVER_REQUESTS_TOTAL.labels(
                replica=REPLICA_ID,
                method=request.method,
                handler=handler,
                status=str(status_code),
            ).inc()
            HTTP_SERVER_REQUEST_DURATION_SECONDS.labels(
                replica=REPLICA_ID,
                method=request.method,
                handler=handler,
            ).observe(process_time)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
):
    req_id = getattr(request.state, "req_id", "system")
    errors = exc.errors()
    detail_msg = "; ".join(
        [
            f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
            for err in errors
        ]
    )
    logger.warning(
        f"Validation error on {request.url.path}: {detail_msg}",
        extra={"request_id": req_id, "replica": REPLICA_ID},
    )
    return problem_response(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        title="Unprocessable Entity",
        detail=detail_msg,
        path=request.url.path,
        extra_headers={"x-request-id": req_id, "x-replica-id": REPLICA_ID},
    )


# ---------------------------------------------------------
# 7. Core Application Endpoints
# ---------------------------------------------------------
@app.get("/health", tags=["System"])
def health_check():
    """Liveness probe verifying service and database availability."""
    count = shared_db.get_inventory_count()
    return {
        "status": "ok",
        "replica": REPLICA_ID,
        "inventory_count": count,
    }


@app.get(
    "/books",
    response_model=List[Book],
    tags=["Books"],
    summary="List all books",
    description="Retrieves all books currently in the bookstore catalog.",
)
def get_books(request: Request, response: Response):
    books = shared_db.get_all_books()
    response.headers["Cache-Control"] = "public, max-age=10"
    return books


@app.get(
    "/books/{book_id}",
    response_model=Book,
    tags=["Books"],
    summary="Get a book by ID",
    responses={
        200: {"description": "Book found and returned."},
        404: {"model": RFC7807Problem, "description": "Book not found."},
    },
)
def get_book(book_id: int, request: Request, response: Response):
    req_id = request.state.req_id
    book = shared_db.get_book_by_id(book_id)
    if not book:
        logger.warning(
            f"Book {book_id} not found",
            extra={"request_id": req_id, "replica": REPLICA_ID},
        )
        return problem_response(
            status_code=status.HTTP_404_NOT_FOUND,
            title="Book Not Found",
            detail=f"No book found with ID {book_id}.",
            path=request.url.path,
            extra_headers={"x-request-id": req_id, "x-replica-id": REPLICA_ID},
        )

    response.headers["Cache-Control"] = "public, max-age=30"
    response.headers["ETag"] = f'"{book["id"]}-{int(book["price"] * 100)}"'
    return book


@app.post(
    "/books",
    response_model=Book,
    status_code=status.HTTP_201_CREATED,
    tags=["Books"],
    summary="Create a new book with Idempotency and Rate Limiting",
    responses={
        201: {"description": "Book successfully created."},
        400: {"model": RFC7807Problem, "description": "Invalid business data."},
        409: {
            "model": RFC7807Problem,
            "description": "Duplicate book already exists.",
        },
        422: {
            "model": RFC7807Problem,
            "description": "Validation error (e.g. negative price).",
        },
        429: {"model": RFC7807Problem, "description": "Rate limit exceeded."},
    },
)
def create_book(
    book_in: BookCreate,
    request: Request,
    response: Response,
    idempotency_key: Optional[str] = Header(
        None,
        alias="Idempotency-Key",
        description="Unique client key for safe request retries",
    ),
):
    req_id = request.state.req_id
    start_time = time.time()
    client_ip = request.client.host if request.client else "127.0.0.1"

    # 1. Rate Limiting Check (Per-replica in-memory counter)
    limited, retry_after = is_rate_limited(client_ip)
    if limited:
        HTTP_RATE_LIMITED_TOTAL.labels(
            endpoint="/books", replica=REPLICA_ID
        ).inc()
        logger.warning(
            f"Rate limit exceeded for client {client_ip}",
            extra={"request_id": req_id, "replica": REPLICA_ID},
        )
        return problem_response(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            title="Too Many Requests",
            detail=(
                f"Rate limit exceeded ({RATE_LIMIT_MAX_REQUESTS} requests per "
                f"{RATE_LIMIT_WINDOW_SECONDS}s on replica {REPLICA_ID}). "
                f"Retry after {retry_after} seconds."
            ),
            path=request.url.path,
            extra_headers={
                "Retry-After": str(retry_after),
                "x-request-id": req_id,
                "x-replica-id": REPLICA_ID,
            },
        )

    # 2. Idempotency Check (Shared SQLite persistence across replicas)
    if idempotency_key:
        record = shared_db.get_idempotency_record(idempotency_key)
        if record:
            logger.info(
                f"Idempotent cache hit for key: {idempotency_key}",
                extra={"request_id": req_id, "replica": REPLICA_ID},
            )
            cached_data = record["response_data"]
            return JSONResponse(
                status_code=record["status_code"],
                content=cached_data,
                headers={
                    "Location": f"/books/{cached_data.get('id')}",
                    "X-Cache-Lookup": "HIT",
                    "Idempotency-Key": idempotency_key,
                    "x-request-id": req_id,
                    "x-replica-id": REPLICA_ID,
                },
            )

    # 3. Artificial Chaos / Delay from A1 (if toggled)
    if problem_enabled and request_counter % 5 == 0:
        time.sleep(0.5)

    # 4. Business Logic: Create Book in Shared Persistence
    try:
        new_book = shared_db.create_book(
            title=book_in.title,
            author=book_in.author,
            price=book_in.price,
        )
    except shared_db.DuplicateBookError as e:
        logger.warning(
            f"Duplicate book conflict: {str(e)}",
            extra={"request_id": req_id, "replica": REPLICA_ID},
        )
        return problem_response(
            status_code=status.HTTP_409_CONFLICT,
            title="Duplicate Book",
            detail=str(e),
            path=request.url.path,
            extra_headers={"x-request-id": req_id, "x-replica-id": REPLICA_ID},
        )
    except ValueError as e:
        return problem_response(
            status_code=status.HTTP_400_BAD_REQUEST,
            title="Bad Request",
            detail=str(e),
            path=request.url.path,
            extra_headers={"x-request-id": req_id, "x-replica-id": REPLICA_ID},
        )

    # 5. Record Idempotency Result
    if idempotency_key:
        shared_db.save_idempotency_record(
            key=idempotency_key,
            endpoint="/books",
            status_code=status.HTTP_201_CREATED,
            response_data=new_book,
        )

    # 6. Observability Metrics
    duration = time.time() - start_time
    BOOKS_CREATED_TOTAL.labels(protocol="http", replica=REPLICA_ID).inc()
    BOOKS_IN_INVENTORY.labels(replica=REPLICA_ID).set(
        shared_db.get_inventory_count()
    )
    BOOK_CREATION_TIME_SECONDS.labels(replica=REPLICA_ID).observe(duration)
    BOOK_CREATION_TIME_SUMMARY.labels(replica=REPLICA_ID).observe(duration)

    response.status_code = status.HTTP_201_CREATED
    response.headers["Location"] = f"/books/{new_book['id']}"
    if idempotency_key:
        response.headers["Idempotency-Key"] = idempotency_key
    response.headers["X-Cache-Lookup"] = "MISS"
    response.headers["x-replica-id"] = REPLICA_ID

    logger.info(
        f"Book {new_book['id']} created successfully via HTTP on replica {REPLICA_ID}",
        extra={"request_id": req_id, "replica": REPLICA_ID},
    )
    return new_book


# ---------------------------------------------------------
# 8. Admin & Experiment Endpoints (Part D Fault Injection)
# ---------------------------------------------------------
@app.get("/admin/delay", tags=["System"])
def get_delay_config():
    """Returns current fixed delay settings across replicas."""
    return {
        "current_replica": REPLICA_ID,
        "effective_delay_ms": shared_db.get_replica_delay_ms(REPLICA_ID),
        "all_replicas": shared_db.get_all_replica_delays(),
    }


@app.post("/admin/delay", tags=["System"])
def set_delay_config(
    delay_ms: int = Query(0, ge=0, le=10000, description="Delay in ms"),
    replica: Optional[str] = Query(
        None, description="Target replica ID (e.g., B). Defaults to current replica."
    ),
    local_only: bool = Query(
        False, description="If true, sets in-memory delay on this replica only."
    ),
):
    """Configures fixed artificial delay (ms) on a target replica for Part D experiments."""
    target_replica = (replica or REPLICA_ID).upper()
    shared_db.set_replica_delay_ms(
        replica=target_replica, delay_ms=delay_ms, local_only=local_only
    )
    logger.info(
        f"Configured artificial delay: replica={target_replica}, delay_ms={delay_ms}, local_only={local_only}",
        extra={"request_id": "admin", "replica": REPLICA_ID},
    )
    return {
        "status": "updated",
        "target_replica": target_replica,
        "delay_ms": delay_ms,
        "all_replicas": shared_db.get_all_replica_delays(),
    }


@app.post("/experiments/toggle-delay", tags=["System"])
def toggle_delay():
    global problem_enabled
    problem_enabled = not problem_enabled
    logger.info(
        f"Toggled artificial delay. Now: {problem_enabled}",
        extra={"request_id": "system", "replica": REPLICA_ID},
    )
    return {"replica": REPLICA_ID, "message": f"Delay enabled: {problem_enabled}"}


@app.post("/experiments/cardinality", tags=["System"])
def trigger_cardinality(request: Request):
    req_id = getattr(request.state, "req_id", "system")
    DEMO_REQUESTS_TOTAL.labels(replica=REPLICA_ID).inc()
    return {"replica": REPLICA_ID, "message": "Incremented demo_requests_total"}
