"""
metrics.py - Centralized Prometheus Metrics & Replica Identity Detection
for Unified HTTP + gRPC Bookstore Application Replicas (Assignment 3).

Prevents duplicate metric registration in prometheus_client.REGISTRY when
FastAPI (app.py) and gRPC (grpc_server.py) run in the same process.
"""

import os
import re
import socket
import time
from prometheus_client import Counter, Gauge, Histogram, Summary

INDEX_TO_REPLICA = {
    "1": "A",
    "2": "B",
    "3": "C",
    "4": "D",
}


def detect_replica_id() -> str:
    """
    Reliably detects the replica identifier (A, B, C, ...) inside a Docker Compose
    scaled container by matching the container's own IP (`socket.gethostbyname(hostname)`)
    against Docker's embedded DNS records for `bookstore-app-1..4` (and fallback reverse DNS).
    """
    env_id = os.environ.get("REPLICA_ID", "").strip()
    if env_id:
        return env_id

    for _ in range(10):
        try:
            hostname = socket.gethostname()
            my_ip = socket.gethostbyname(hostname)

            # 1. Forward DNS IP comparison against scaled container names
            for idx, letter in INDEX_TO_REPLICA.items():
                for prefix in ("bookstore-app-", "bookstore_app_", "app-", "app_"):
                    try:
                        candidate_ip = socket.gethostbyname(f"{prefix}{idx}")
                        if candidate_ip == my_ip:
                            return letter
                    except Exception:
                        pass

            # 2. Reverse DNS / hostname suffix check
            candidates = [hostname]
            try:
                fqdn, aliases, _ = socket.gethostbyaddr(my_ip)
                candidates.insert(0, fqdn)
                candidates.extend(aliases)
            except Exception:
                pass

            for name in candidates:
                short_name = name.split(".")[0]
                match = re.search(r"[-_](\d+)$", short_name)
                if match:
                    idx = match.group(1)
                    return INDEX_TO_REPLICA.get(idx, f"R{idx}")
        except Exception:
            pass
        time.sleep(0.2)

    return "A"


REPLICA_ID = detect_replica_id()

# Fine-grained latency buckets suitable for baseline (2-20ms) and +250ms fault injection
LATENCY_BUCKETS = [0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.25, 0.3, 0.5, 0.75, 1.0, 2.5]

# ---------------------------------------------------------
# 1. Shared Business Metrics (Deduplicated across HTTP & gRPC)
# ---------------------------------------------------------
BOOKS_CREATED_TOTAL = Counter(
    "books_created_total",
    "Total number of books created",
    ["protocol", "replica"],
)

BOOKS_IN_INVENTORY = Gauge(
    "books_in_inventory",
    "Number of books currently in inventory",
    ["replica"],
)

BOOK_CREATION_TIME_SECONDS = Histogram(
    "book_creation_time_seconds",
    "Time taken to create a book via HTTP",
    ["replica"],
    buckets=LATENCY_BUCKETS,
)

BOOK_CREATION_TIME_SUMMARY = Summary(
    "book_creation_time_summary_seconds",
    "Summary of time taken to create a book via HTTP",
    ["replica"],
)

HTTP_RATE_LIMITED_TOTAL = Counter(
    "http_rate_limited_total",
    "Total number of requests rejected due to rate limiting",
    ["endpoint", "replica"],
)

DEMO_REQUESTS_TOTAL = Counter(
    "demo_requests_total",
    "Test counter for cardinality experiment from A1",
    ["replica"],
)

# ---------------------------------------------------------
# 2. Per-Replica HTTP Metrics (for Part B/C/D Dashboards)
# ---------------------------------------------------------
HTTP_SERVER_REQUESTS_TOTAL = Counter(
    "http_server_requests_total",
    "Total number of HTTP requests processed by replica, method, handler, and status",
    ["replica", "method", "handler", "status"],
)

HTTP_SERVER_IN_FLIGHT_REQUESTS = Gauge(
    "http_server_in_flight_requests",
    "Number of concurrent HTTP requests currently being processed on this replica",
    ["replica", "handler"],
)

HTTP_SERVER_REQUEST_DURATION_SECONDS = Histogram(
    "http_server_request_duration_seconds",
    "Histogram of HTTP response latency in seconds per replica",
    ["replica", "method", "handler"],
    buckets=LATENCY_BUCKETS,
)

# ---------------------------------------------------------
# 3. Per-Replica gRPC Metrics (for Part B/C/D Dashboards)
# ---------------------------------------------------------
GRPC_HANDLED_TOTAL = Counter(
    "grpc_server_handled_total",
    "Total number of RPCs completed on the server, partitioned by replica and status code",
    ["replica", "grpc_service", "grpc_method", "grpc_code"],
)

GRPC_HANDLING_SECONDS = Histogram(
    "grpc_server_handling_seconds",
    "Histogram of response latency for gRPC methods in seconds per replica",
    ["replica", "grpc_service", "grpc_method"],
    buckets=LATENCY_BUCKETS,
)

GRPC_IN_FLIGHT_REQUESTS = Gauge(
    "grpc_server_in_flight_requests",
    "Number of concurrent gRPC requests currently being processed on this replica",
    ["replica", "grpc_service", "grpc_method"],
)
