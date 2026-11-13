# Bookstore Load Balancing & Observability System (Assignment 3)

Enterprise Software Development — Fall 2026  
Extends Assignment 1 & 2 with **Envoy Proxy** L7 HTTP and HTTP/2 gRPC load balancing across **three Docker Compose scaled replicas (`A`, `B`, `C`)**, per-replica Prometheus/Grafana observability, Filebeat/Elasticsearch Envoy + backend trace correlation, and open-loop fixed-rate load-balancing experiments.

---

## 🏗️ Architecture & Shared vs. Per-Replica State

```
HTTP / gRPC Clients ──► Envoy Proxy (:8080 HTTP | :50051 HTTP/2 gRPC | :9901 Admin)
                           ├──► Replica A (bookstore-app-1 :8000 HTTP | :50051 gRPC)
                           ├──► Replica B (bookstore-app-2 :8000 HTTP | :50051 gRPC)
                           └──► Replica C (bookstore-app-3 :8000 HTTP | :50051 gRPC)
                                       │
                                       ▼
                        Shared SQLite Volume (WAL Mode)
```

### State Sharing Summary
1. **Book Catalog (`books` table in SQLite)**: **Shared** across Replicas A, B, and C via a shared Docker volume in Write-Ahead Logging (`WAL`) mode. Writes and duplicate-prevention rules (`UNIQUE(title, author)`) remain consistent regardless of which replica serves the request.
2. **Idempotency Records (`idempotency_keys` table in SQLite)**: **Shared** across Replicas A, B, and C. If a retry with the same `Idempotency-Key` is routed by Envoy to a different replica than the initial write, the receiving replica still finds the cached response and returns `X-Cache-Lookup: HIT`.
3. **Rate-Limit Counters (`rate_limit_records` in `app.py`)**: **Kept separately in each replica's process memory**. When Envoy spreads requests across 3 replicas, a client can send up to `3 × 5 = 15` requests per 10-second window before all replicas reject traffic (`429 Too Many Requests`). Furthermore, without inspecting `X-Forwarded-For`, each replica sees Envoy's container IP as `request.client.host`.

---

## 🚀 1. Setup & Quickstart Commands

### Start the Complete Stack (3 Scaled Replicas + Envoy + Observability)
From `Assignment_3/bookstore-observability`:
```powershell
docker compose up -d --build
```
This launches:
- **`app` (3 scaled replicas)**:
  - `bookstore-app-1` (**Replica A**) — HTTP `:8000`, gRPC `:50051` (internal network)
  - `bookstore-app-2` (**Replica B**) — HTTP `:8000`, gRPC `:50051` (internal network)
  - `bookstore-app-3` (**Replica C**) — HTTP `:8000`, gRPC `:50051` (internal network)
- **`envoy` (`bookstore-envoy`)**:
  - HTTP Listener: `http://localhost:8080` (also mapped on `http://localhost:8000`)
  - gRPC Listener (HTTP/2): `localhost:50051`
  - Envoy Admin & Prometheus Stats: `http://localhost:9901/stats/prometheus`
- **`prometheus` (`bookstore-prometheus`)**: `http://localhost:9090` (scrapes A, B, C, and Envoy directly)
- **`grafana` (`bookstore-grafana`)**: `http://localhost:3001` (`admin` / `admin`, auto-provisioned with Assignment 3 dashboard)
- **`elasticsearch`** (`http://localhost:9200`), **`kibana`** (`http://localhost:5601`), **`filebeat`**, and **`node-exporter`** (`:9100`)

---

## 🧪 2. Experiment & Recovery Commands (Parts A, B, C, D)

### Switch Envoy Routing Policy
Use `switch_envoy_config.ps1` to switch Envoy between configurations:
```powershell
# Part A & B: Route HTTP & gRPC to Replica A only (B and C running but idle)
.\switch_envoy_config.ps1 -Mode part-a-b

# Part C: Equal-weight Round Robin (1:1:1 across A, B, C)
.\switch_envoy_config.ps1 -Mode part-c-rr

# Part D.1: Weighted Round Robin (2:1:1 for A:B:C)
.\switch_envoy_config.ps1 -Mode part-d-wrr

# Part D.2: Least Request (equal weights across A, B, C)
.\switch_envoy_config.ps1 -Mode part-d-lr

# Part D.3: Active Health Checks + Round Robin
.\switch_envoy_config.ps1 -Mode part-d-hc
```

### Run the Open-Loop Fixed-Rate Workload (`fixed_rate_client.py`)
Dispatches requests at a fixed rate (`--rate 20` req/s) for a fixed duration (`--duration 30` s) without waiting for previous responses. Retries and circuit breakers are disabled, and gRPC uses a single shared HTTP/2 connection to Envoy:
```powershell
# Run both HTTP (GET /books/1) and unary gRPC (GetBook id=1) separately:
python fixed_rate_client.py --interface both --rate 20 --duration 30 --label part-b-baseline
```

### Part D.2: Inject & Reset Fixed Delay on Replica B Only
```powershell
# Add +250ms fixed delay to Replica B only (affects both HTTP and gRPC on B):
curl.exe -X POST "http://localhost:8080/admin/delay?replica=B&delay_ms=250"

# Run with Round Robin:
.\switch_envoy_config.ps1 -Mode part-c-rr
python fixed_rate_client.py --interface both --rate 20 --duration 30 --label part-d-slow-b-rr

# Run with Least Request:
.\switch_envoy_config.ps1 -Mode part-d-lr
python fixed_rate_client.py --interface both --rate 20 --duration 30 --label part-d-slow-b-lr

# Recovery: Restore Replica B delay to 0ms and Envoy to Round Robin:
curl.exe -X POST "http://localhost:8080/admin/delay?replica=B&delay_ms=0"
.\switch_envoy_config.ps1 -Mode part-c-rr
```

### Part D.3: Failure & Recovery with Active Health Checks
```powershell
# Enable active health checks (interval=2s, timeout=1s, unhealthy_threshold=2, healthy_threshold=2):
.\switch_envoy_config.ps1 -Mode part-d-hc

# Run fixed-rate traffic while automatically stopping Replica B (bookstore-app-2) at t=8s and restarting at t=20s:
python fixed_rate_client.py --interface http --rate 20 --duration 30 --fault-container bookstore-app-2 --stop-at 8 --start-at 20 --label part-d-hc-http
python fixed_rate_client.py --interface grpc --rate 20 --duration 30 --fault-container bookstore-app-2 --stop-at 8 --start-at 20 --label part-d-hc-grpc

# Manual stop/start commands if testing interactively:
docker stop bookstore-app-2
docker start bookstore-app-2
```

### Verify Correlated Envoy + Replica Logs & Traffic Distribution (`verify_logs.py`)
```powershell
# Search Elasticsearch for both the Envoy access log event and backend replica log event for a single request_id:
python verify_logs.py --request-id <REQUEST_ID>

# Cross-check per-replica traffic share against Envoy's access logs for a run's time window:
python verify_logs.py --interface http --start-iso <START_ISO> --end-iso <END_ISO>
```

---

## 🧹 3. Cleanup
```powershell
docker compose down -v
```
