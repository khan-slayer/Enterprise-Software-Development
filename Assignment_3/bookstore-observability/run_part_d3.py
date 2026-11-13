"""
run_part_d3.py - Re-runs Part D.3 (Failure & Recovery with Active Health Checks)
with a 36s window (stop B at t=8s, start B at t=20s) so both removal and full
recovery are captured cleanly for HTTP and gRPC, then updates experiment_results.json.
"""

import asyncio
import json
import os
import shutil
import subprocess
import time
import requests

from fixed_rate_client import run_workload

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENVOY_DIR = os.path.join(BASE_DIR, "envoy")
RESULTS_PATH = os.path.join(BASE_DIR, "experiment_results.json")


def switch_envoy(mode: str):
    src = os.path.join(ENVOY_DIR, f"envoy-{mode}.yaml")
    dst = os.path.join(ENVOY_DIR, "envoy.yaml")
    shutil.copyfile(src, dst)
    subprocess.run(["docker", "restart", "bookstore-envoy"], check=True, capture_output=True)
    time.sleep(5.0)
    print(f"[Envoy] Switched to {mode}", flush=True)


def wait_for_healthy_replicas(expected: int = 3, timeout_s: float = 30.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get("http://localhost:9901/clusters", timeout=2)
            healthy_http = r.text.count("bookstore_http_cluster::")
            # Check via /health round-robin
            seen = set()
            for _ in range(9):
                hr = requests.get("http://localhost:8080/health", timeout=1)
                if hr.status_code == 200:
                    seen.add(hr.json().get("replica"))
            if len(seen) >= expected:
                print(f"[Health] All {expected} replicas healthy: {sorted(seen)}", flush=True)
                return
        except Exception:
            pass
        time.sleep(2.0)
    print("[Health] Warning: timed out waiting for all replicas", flush=True)


def main():
    with open(RESULTS_PATH, "r", encoding="utf-8") as f:
        all_results = json.load(f)

    subprocess.run(["docker", "start", "bookstore-app-1", "bookstore-app-2", "bookstore-app-3"], capture_output=True)
    switch_envoy("part-d-hc")
    wait_for_healthy_replicas(3, 30.0)

    all_results["runs"]["part_d_hc_http"] = asyncio.run(
        run_workload(
            "http",
            20.0,
            36.0,
            "http://localhost:8080/books/1",
            "localhost:50051",
            1,
            2.5,
            "Part D.3: Failure & Recovery (HTTP)",
            fault_container="bookstore-app-2",
            stop_at_s=8.0,
            start_at_s=20.0,
        )
    )
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)

    wait_for_healthy_replicas(3, 30.0)

    all_results["runs"]["part_d_hc_grpc"] = asyncio.run(
        run_workload(
            "grpc",
            20.0,
            36.0,
            "http://localhost:8080/books/1",
            "localhost:50051",
            1,
            2.5,
            "Part D.3: Failure & Recovery (gRPC)",
            fault_container="bookstore-app-2",
            stop_at_s=8.0,
            start_at_s=20.0,
        )
    )

    wait_for_healthy_replicas(3, 30.0)
    switch_envoy("part-c-rr")

    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)
    print("Updated experiment_results.json with Part D.3 HTTP & gRPC results.", flush=True)


if __name__ == "__main__":
    main()

