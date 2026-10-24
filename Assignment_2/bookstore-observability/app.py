import time
import uuid
import logging
import random
from typing import List, Optional
from collections import defaultdict

from fastapi import FastAPI, Request, Response, Header, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ConfigDict
from pythonjsonlogger import jsonlogger
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Gauge, Histogram, Summary

import shared_db

# ---------------------------------------------------------
# 1. Structured JSON Logging Setup (Preserving A1 format)
# ---------------------------------------------------------
logger = logging.getLogger("bookstore_api")
logger.setLevel(logging.INFO)
logHandler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s')
logHandler.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(logHandler)
logging.getLogger("uvicorn.access").disabled = True

# ---------------------------------------------------------
# 2. FastAPI Application Definition
# ---------------------------------------------------------
app = FastAPI(
    title="Bookstore HTTP API",
    version="2.1.0",
    description="Enterprise Software Development Assignment 2 - Resilient RESTful HTTP API with Idempotency, Rate Limiting, and Observability.",
    openapi_tags=[
        {"name": "Books", "description": "Operations on book resources and catalog inventory."},
        {"name": "System", "description": "Health checks, diagnostics, and experiment controls."}
    ]
)

# ---------------------------------------------------------
# 3. Prometheus Metrics Setup
# ---------------------------------------------------------
books_created_total = Counter(
    'books_created_total',
    'Total number of books created',
    ['protocol']
)
books_in_inventory = Gauge(
    'books_in_inventory',
    'Number of books currently in inventory'
)
book_creation_time_seconds = Histogram(
    'book_creation_time_seconds',
    'Time taken to create a book via HTTP',
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5]
)
book_creation_time_summary = Summary(
    'book_creation_time_summary_seconds',
    'Summary of time taken to create a book via HTTP'
)
http_rate_limited_total = Counter(
    'http_rate_limited_total',
    'Total number of requests rejected due to rate limiting',
    ['endpoint']
)
demo_requests_total = Counter(
    'demo_requests_total',
    'Test counter for cardinality experiment from A1'
)

# Auto-instrument FastAPI routes
Instrumentator().instrument(app).expose(app)

# ---------------------------------------------------------
# 4. State & Rate Limiter Configuration
# ---------------------------------------------------------
# Initialize database
shared_db.init_db()
books_in_inventory.set(shared_db.get_inventory_count())

# State flags for chaos / delay experiments from A1
problem_enabled = False
request_counter = 0

# Rate Limiter State: Token Bucket / Sliding Window (5 requests per 10 seconds per IP on POST /books)
RATE_LIMIT_MAX_REQUESTS = 5
RATE_LIMIT_WINDOW_SECONDS = 10
rate_limit_records = defaultdict(list)

def is_rate_limited(client_ip: str) -> tuple[bool, int]:
    """Sliding-window rate limiter per client IP."""
    # Normalize localhost IPv6 and IPv4
    if client_ip in ("::1", "localhost", "testclient"):
        client_ip = "127.0.0.1"

    now = time.time()
    timestamps = rate_limit_records[client_ip]
    # Prune timestamps older than window
    rate_limit_records[client_ip] = [ts for ts in timestamps if now - ts < RATE_LIMIT_WINDOW_SECONDS]
    
    if len(rate_limit_records[client_ip]) >= RATE_LIMIT_MAX_REQUESTS:
        oldest_ts = rate_limit_records[client_ip][0]
        retry_after = max(1, int(RATE_LIMIT_WINDOW_SECONDS - (now - oldest_ts)))
        return True, retry_after

    rate_limit_records[client_ip].append(now)
    return False, 0

# ---------------------------------------------------------
# 5. Pydantic Models & RFC 7807 Error Responses
# ---------------------------------------------------------
class BookCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="Title of the book")
    author: str = Field(..., min_length=1, max_length=100, description="Author's full name")
    price: float = Field(..., gt=0.0, description="Retail price in USD, strictly positive")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "title": "Clean Architecture",
                "author": "Robert C. Martin",
                "price": 29.99
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
    type: str = Field(..., description="URI reference identifying the problem type")
    title: str = Field(..., description="Short, human-readable summary of problem")
    status: int = Field(..., description="HTTP status code")
    detail: str = Field(..., description="Human-readable explanation specific to this occurrence")
    instance: str = Field(..., description="URI reference identifying the specific occurrence")

def problem_response(status_code: int, title: str, detail: str, path: str, extra_headers: Optional[dict] = None) -> JSONResponse:
    type_slug = title.lower().replace(" ", "-")
    content = {
        "type": f"https://api.bookstore.example/errors/{type_slug}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": path
    }
    headers = {"Content-Type": "application/problem+json"}
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

    # Extract or generate correlation request ID
    req_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.req_id = req_id

    start_time = time.time()
    try:
        response = await call_next(request)
        process_time = time.time() - start_time
        response.headers["x-request-id"] = req_id

        logger.info("Request processed successfully", extra={
            'request_id': req_id,
            'method': request.method,
            'path': request.url.path,
            'status': response.status_code,
            'duration': round(process_time, 4)
        })
        return response
    except Exception as e:
        process_time = time.time() - start_time
        logger.error(f"Unhandled server error: {str(e)}", extra={
            'request_id': req_id,
            'method': request.method,
            'path': request.url.path,
            'duration': round(process_time, 4)
        })
        return problem_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            title="Internal Server Error",
            detail=f"An unexpected error occurred: {str(e)}",
            path=request.url.path,
            extra_headers={"x-request-id": req_id}
        )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    req_id = getattr(request.state, "req_id", "system")
    errors = exc.errors()
    detail_msg = "; ".join([f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in errors])
    logger.warning(f"Validation error on {request.url.path}: {detail_msg}", extra={'request_id': req_id})
    return problem_response(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        title="Unprocessable Entity",
        detail=detail_msg,
        path=request.url.path,
        extra_headers={"x-request-id": req_id}
    )

# ---------------------------------------------------------
# 7. Core Application Endpoints
# ---------------------------------------------------------
@app.get("/health", tags=["System"])
def health_check():
    """Liveness probe verifying service and database availability."""
    count = shared_db.get_inventory_count()
    return {"status": "ok", "inventory_count": count}

@app.get(
    "/books",
    response_model=List[Book],
    tags=["Books"],
    summary="List all books",
    description="Retrieves all books currently in the bookstore catalog. Employs caching headers for REST compliance."
)
def get_books(request: Request, response: Response):
    req_id = request.state.req_id
    logger.info("Fetching all books", extra={'request_id': req_id})
    time.sleep(random.uniform(0.01, 0.05))

    books = shared_db.get_all_books()
    # REST constraint: Cacheable responses
    response.headers["Cache-Control"] = "public, max-age=10"
    return books

@app.get(
    "/books/{book_id}",
    response_model=Book,
    tags=["Books"],
    summary="Get a book by ID",
    responses={
        200: {"description": "Book found and returned."},
        404: {"model": RFC7807Problem, "description": "Book not found."}
    }
)
def get_book(book_id: int, request: Request, response: Response):
    req_id = request.state.req_id
    logger.info(f"Fetching book {book_id}", extra={'request_id': req_id})

    book = shared_db.get_book_by_id(book_id)
    if not book:
        logger.warning(f"Book {book_id} not found", extra={'request_id': req_id})
        return problem_response(
            status_code=status.HTTP_404_NOT_FOUND,
            title="Book Not Found",
            detail=f"No book found with ID {book_id}.",
            path=request.url.path,
            extra_headers={"x-request-id": req_id}
        )

    # REST constraint: ETag and Cache-Control
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
        409: {"model": RFC7807Problem, "description": "Duplicate book already exists."},
        422: {"model": RFC7807Problem, "description": "Validation error (e.g. negative price)."},
        429: {"model": RFC7807Problem, "description": "Rate limit exceeded."}
    }
)
def create_book(
    book_in: BookCreate,
    request: Request,
    response: Response,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key", description="Unique client key for safe request retries")
):
    req_id = request.state.req_id
    start_time = time.time()
    client_ip = request.client.host if request.client else "127.0.0.1"

    # 1. Rate Limiting Check
    limited, retry_after = is_rate_limited(client_ip)
    if limited:
        http_rate_limited_total.labels(endpoint="/books").inc()
        logger.warning(f"Rate limit exceeded for client {client_ip}", extra={'request_id': req_id})
        return problem_response(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            title="Too Many Requests",
            detail=f"Rate limit exceeded ({RATE_LIMIT_MAX_REQUESTS} requests per {RATE_LIMIT_WINDOW_SECONDS}s). Retry after {retry_after} seconds.",
            path=request.url.path,
            extra_headers={
                "Retry-After": str(retry_after),
                "x-request-id": req_id
            }
        )

    # 2. Idempotency Check (Safe Repeats)
    if idempotency_key:
        record = shared_db.get_idempotency_record(idempotency_key)
        if record:
            logger.info(f"Idempotent cache hit for key: {idempotency_key}", extra={'request_id': req_id})
            cached_data = record["response_data"]
            return JSONResponse(
                status_code=record["status_code"],
                content=cached_data,
                headers={
                    "Location": f"/books/{cached_data.get('id')}",
                    "X-Cache-Lookup": "HIT",
                    "Idempotency-Key": idempotency_key,
                    "x-request-id": req_id
                }
            )

    # 3. Artificial Chaos / Delay from A1 (if toggled)
    if problem_enabled and request_counter % 5 == 0:
        logger.info("Injecting artificial 500ms delay", extra={'request_id': req_id})
        time.sleep(0.5)

    # 4. Business Logic: Create Book in Shared Persistence
    try:
        new_book = shared_db.create_book(
            title=book_in.title,
            author=book_in.author,
            price=book_in.price
        )
    except shared_db.DuplicateBookError as e:
        logger.warning(f"Duplicate book conflict: {str(e)}", extra={'request_id': req_id})
        return problem_response(
            status_code=status.HTTP_409_CONFLICT,
            title="Duplicate Book",
            detail=str(e),
            path=request.url.path,
            extra_headers={"x-request-id": req_id}
        )
    except ValueError as e:
        return problem_response(
            status_code=status.HTTP_400_BAD_REQUEST,
            title="Bad Request",
            detail=str(e),
            path=request.url.path,
            extra_headers={"x-request-id": req_id}
        )

    # 5. Record Idempotency Result
    if idempotency_key:
        shared_db.save_idempotency_record(
            key=idempotency_key,
            endpoint="/books",
            status_code=status.HTTP_201_CREATED,
            response_data=new_book
        )

    # 6. Observability Metrics
    duration = time.time() - start_time
    books_created_total.labels(protocol="http").inc()
    books_in_inventory.set(shared_db.get_inventory_count())
    book_creation_time_seconds.observe(duration)
    book_creation_time_summary.observe(duration)

    response.status_code = status.HTTP_201_CREATED
    response.headers["Location"] = f"/books/{new_book['id']}"
    if idempotency_key:
        response.headers["Idempotency-Key"] = idempotency_key
    response.headers["X-Cache-Lookup"] = "MISS"

    logger.info(f"Book {new_book['id']} created successfully via HTTP", extra={'request_id': req_id})
    return new_book

# ---------------------------------------------------------
# 8. Experiment Endpoints (Preserved from A1)
# ---------------------------------------------------------
@app.post("/experiments/toggle-delay", tags=["System"])
def toggle_delay():
    global problem_enabled
    problem_enabled = not problem_enabled
    logger.info(f"Toggled artificial delay. Now: {problem_enabled}", extra={'request_id': 'system'})
    return {"message": f"Delay enabled: {problem_enabled}"}

@app.post("/experiments/cardinality", tags=["System"])
def trigger_cardinality(request: Request):
    req_id = getattr(request.state, "req_id", "system")
    logger.info("Triggering cardinality explosion point", extra={'request_id': req_id})
    demo_requests_total.inc()
    return {"message": "Logged request with unique ID label"}
