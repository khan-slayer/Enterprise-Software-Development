from fastapi import FastAPI, Request, HTTPException, Response
from pydantic import BaseModel
from typing import List
import random
import time
import logging
import uuid
from pythonjsonlogger import jsonlogger
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Gauge, Histogram, Summary

# 1. Setup JSON Logging
logger = logging.getLogger("bookstore_api")
logger.setLevel(logging.INFO)
logHandler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s')
logHandler.setFormatter(formatter)
logger.addHandler(logHandler)
# Disable uvicorn default logging so it doesn't clutter stdout in ways Filebeat might misunderstand
logging.getLogger("uvicorn.access").disabled = True

app = FastAPI(title="Bookstore API", version="2.0.0")

# 2. Setup Prometheus Metrics (The 4 Types)
books_created_total = Counter('books_created_total', 'Total number of books created')
books_in_inventory = Gauge('books_in_inventory', 'Number of books currently in inventory')
book_creation_time_seconds = Histogram('book_creation_time_seconds', 'Time taken to create a book', buckets=[0.05, 0.1, 0.25, 0.5, 1.0])
book_creation_time_summary = Summary('book_creation_time_summary_seconds', 'Summary of time taken to create a book')

# Metric for Cardinality experiment
demo_requests_total = Counter('demo_requests_total', 'Test counter for cardinality explosion')

# Instrument the app (automatically gives us HTTP metrics)
Instrumentator().instrument(app).expose(app)

# State for experiments
problem_enabled = False
request_counter = 0

class Book(BaseModel):
    id: int
    title: str
    author: str
    price: float

books_db = [
    Book(id=1, title="The Great Gatsby", author="F. Scott Fitzgerald", price=10.99),
    Book(id=2, title="1984", author="George Orwell", price=8.99),
    Book(id=3, title="To Kill a Mockingbird", author="Harper Lee", price=12.50)
]
# Initialize gauge
books_in_inventory.set(len(books_db))

@app.middleware("http") # before reaches code run this function
async def add_request_id_and_log(request: Request, call_next):
    global request_counter
    request_counter += 1
    
    # Generate unique ID for tracing/logging
    req_id = str(uuid.uuid4())
    request.state.req_id = req_id
    
    start_time = time.time()
    
    # Process request
    try:
        response = await call_next(request)
        process_time = time.time() - start_time
        
        logger.info("Request processed successfully", extra={
            'request_id': req_id,
            'method': request.method,
            'path': request.url.path,
            'status': response.status_code,
            'duration': process_time
        })
        return response
    except Exception as e:
        process_time = time.time() - start_time
        logger.error(f"Request failed: {str(e)}", extra={
            'request_id': req_id,
            'method': request.method,
            'path': request.url.path,
            'duration': process_time
        })
        raise e

@app.get("/books", response_model=List[Book])
def get_books(request: Request):
    logger.info("Fetching all books", extra={'request_id': request.state.req_id})
    time.sleep(random.uniform(0.05, 0.2))
    return books_db

@app.get("/books/{book_id}", response_model=Book)
def get_book(book_id: int, request: Request):
    logger.info(f"Fetching book {book_id}", extra={'request_id': request.state.req_id})
    time.sleep(random.uniform(0.01, 0.1))
    for book in books_db:
        if book.id == book_id:
            return book
    logger.warning(f"Book {book_id} not found", extra={'request_id': request.state.req_id})
    raise HTTPException(status_code=404, detail="Book not found")

@app.post("/books", response_model=Book, status_code=201)
def create_book(book: Book, request: Request):
    logger.info(f"Creating book {book.id}", extra={'request_id': request.state.req_id})
    start_time = time.time()
    
    # Check if we need to apply the 500ms delay (Experiment 1)
    if problem_enabled and request_counter % 5 == 0:
        logger.info("Injecting artificial 500ms delay", extra={'request_id': request.state.req_id})
        time.sleep(0.5)
    
    # Simulate DB error occasionally
    if random.random() < 0.1:
        logger.error("Database connection failed during creation", extra={'request_id': request.state.req_id})
        raise HTTPException(status_code=500, detail="Internal Database Error")
        
    for b in books_db:
        if b.id == book.id:
            logger.warning(f"Duplicate book ID {book.id}", extra={'request_id': request.state.req_id})
            raise HTTPException(status_code=400, detail="Book with this ID already exists")
            
    # Simulate processing
    time.sleep(random.uniform(0.1, 0.3))
    
    books_db.append(book)
    
    # Update Metrics
    books_created_total.inc()
    books_in_inventory.set(len(books_db))
    duration = time.time() - start_time
    book_creation_time_seconds.observe(duration)
    book_creation_time_summary.observe(duration)
    
    logger.info(f"Book {book.id} created successfully", extra={'request_id': request.state.req_id})
    return book

# ---- EXPERIMENT ENDPOINTS ----

@app.post("/experiments/toggle-delay")
def toggle_delay():
    global problem_enabled
    problem_enabled = not problem_enabled
    logger.info(f"Toggled artificial delay. Now: {problem_enabled}", extra={'request_id': 'system'})
    return {"message": f"Delay enabled: {problem_enabled}"}

@app.post("/experiments/cardinality")
def trigger_cardinality(request: Request):
    req_id = request.state.req_id
    logger.info("Triggering cardinality explosion point", extra={'request_id': req_id})
    # WARNING: Do not do this in production! It creates a new time series for every unique request_id.
    demo_requests_total.inc()
    return {"message": "Logged request with unique ID label"}
