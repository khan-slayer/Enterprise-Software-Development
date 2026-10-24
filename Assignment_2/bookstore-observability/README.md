# Bookstore Observability & Dual Protocol System (Assignment 2)

Enterprise Software Development - Fall 2026  
Extended from Assignment 1 with RESTful best practices, Idempotency, Token-Bucket Rate Limiting, OpenAPI specification, gRPC microservice implementation, and extended dual-service Prometheus/Grafana observability.

---

## 🏗️ Architecture & Component Overview

- **HTTP REST API (`app.py`)**: Runs on port `8000`. Built with FastAPI. Features Pydantic data validation, RFC 7807 standardized problem JSON error responses, `Idempotency-Key` caching for safe retries, and sliding-window rate limiting (5 req/10s per client).
- **gRPC Microservice (`grpc_server.py`)**: Runs on port `50051`. Implements unary RPCs (`CreateBook`, `GetBook`, `ListBooks`) compiled from Protobuf. Serves Prometheus metrics on port `8001`. Emits structured JSON logs with trace correlation IDs.
- **Shared Persistence (`shared_db.py`)**: Thread-safe SQLite database configured in Write-Ahead Logging (`WAL`) mode, mounted across containers to provide a single, consistent source of truth for both HTTP and gRPC services.
- **Resilient HTTP Caller (`caller_http.py`)**: External client demonstrating a 2.0s timeout, exponential backoff retries with idempotency protection, and a 3-state Circuit Breaker (`CLOSED`, `OPEN`, `HALF-OPEN`).
- **Python gRPC Client (`caller_grpc.py`)**: Command-line gRPC client with deadline enforcement, metadata trace propagation (`x-request-id`), and comprehensive status code handling.
- **Dual Traffic Generator (`traffic_generator.py`)**: Generates balanced HTTP & gRPC traffic, controlled error bursts, and rate-limit triggers.
- **Observability Stack**: Prometheus (`:9090`), Grafana (`:3001`), Elasticsearch (`:9200`), Kibana (`:5601`), and Filebeat.

---

## 🚀 Quickstart: Running with Docker Compose

### 1. Start All Services
```bash
docker compose up -d --build
```
This builds and starts:
- `api` on `http://localhost:8000`
- `grpc-service` on `localhost:50051` (RPC) and `http://localhost:8001/metrics` (Prometheus)
- `prometheus` on `http://localhost:9090`
- `grafana` on `http://localhost:3001` (Credentials: `admin` / `admin`)
- `elasticsearch` (`:9200`), `kibana` (`:5601`), `filebeat`, and `node-exporter` (`:9100`)

### 2. Verify Health
- HTTP API Health: `curl http://localhost:8000/health`
- HTTP Swagger Docs: `http://localhost:8000/docs`
- gRPC Metrics: `curl http://localhost:8001/metrics`
- Prometheus Targets: `http://localhost:9090/targets` (Verifies both `bookstore-api` and `bookstore-grpc` are UP)

---

## 💻 Running & Testing Locally (Without Docker)

### Prerequisites
Install Python dependencies:
```bash
pip install -r requirements.txt
```

### 1. Protobuf Code Generation
To regenerate Python stubs from `proto/bookstore.proto`:
```bash
python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. proto/bookstore.proto
```

### 2. Run the HTTP API
```bash
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

### 3. Run the gRPC Microservice (in a separate terminal)
```bash
python grpc_server.py
```

---

## 🧪 Verification & Demonstration Scripts

### 1. Resilient HTTP Caller (Part A)
Executes a 6-phase test verifying:
1. Normal creation (201 Created) and safe idempotent repeat (`X-Cache-Lookup: HIT`)
2. Input validation rejection (RFC 7807 422 Unprocessable Entity)
3. Simulated downstream failure and retry with exponential backoff
4. Tripping the Circuit Breaker from `CLOSED` to `OPEN`
5. Immediate 0.01ms local fast-fail call blocking
6. Recovery period cooldown and transition through `HALF-OPEN` back to `CLOSED`
7. Rapid request burst triggering `429 Too Many Requests` with `Retry-After` header

```bash
python caller_http.py
```

### 2. gRPC Client Demonstration (Part B)
Executes unary RPC calls verifying:
1. Successful `CreateBook` RPC with deadline and correlation metadata
2. Safe idempotent repeat with identical `idempotency_key`
3. Invalid argument rejection (Price <= 0 -> `INVALID_ARGUMENT`, code 3)
4. Duplicate resource conflict (Title/Author match -> `ALREADY_EXISTS`, code 6)
5. Successful `GetBook` RPC
6. Missing resource rejection (Non-existent ID -> `NOT_FOUND`, code 5)
7. Full catalog listing (`ListBooks`)

```bash
python caller_grpc.py --mode demo
```

You can also run ad-hoc commands:
```bash
python caller_grpc.py --mode create --title "Clean Code" --author "Robert Martin" --price 29.99
python caller_grpc.py --mode get --id 1
python caller_grpc.py --mode list
```

### 3. Dual-Protocol Traffic Generator (Part C)
To populate Grafana panels with repeatable success and error calls:
```bash
# Send a structured burst of HTTP & gRPC calls (ideal for dashboard screenshots):
python traffic_generator.py --mode metrics

# Or run continuous traffic:
python traffic_generator.py --mode normal
```

---

## 📊 Grafana Dashboard Setup

1. Open Grafana at `http://localhost:3001` (login `admin` / `admin`).
2. Navigate to **Dashboards -> Import**.
3. Upload `grafana_dashboard.json` (or paste its JSON content).
4. The dashboard displays 10 side-by-side panels:
   - Panel 1: HTTP Request Rate & HTTP Status Codes
   - Panel 2: Books Currently in Inventory (Gauge)
   - Panel 3: HTTP P95 Latency Histogram
   - Panel 4: Average HTTP Book Creation Time
   - Panel 5: System CPU Usage
   - Panel 6: Unique Time Series Growth (Cardinality)
   - **Panel 7 (New): gRPC RPC Calls & Error Rate by Method and Status**
   - **Panel 8 (New): gRPC P50 and P95 Response Latency**
   - **Panel 9 (New): gRPC In-Flight Requests (Active Concurrency)**
   - **Panel 10 (New): HTTP Rate-Limit Drops (429) vs Creations by Protocol (HTTP vs gRPC)**

---

## 🧹 Cleanup
To tear down the Docker environment and purge volumes:
```bash
docker compose down -v
```
