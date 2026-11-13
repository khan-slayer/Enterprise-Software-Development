"""
run_all_experiments.py - Automated Experiment Suite for Assignment 3 (Parts B, C, D).

Executes all required HTTP and gRPC fixed-rate runs sequentially, switches Envoy
configurations, injects/restores faults (Replica B +250ms delay, Replica B stop/start),
queries Elasticsearch for correlated Envoy + Replica log pairs and access log
distribution verification, and writes `experiment_results.json`.
"""

import asyncio
import json
import os
import shutil
import subprocess
import time
import requests

from fixed_rate_client import run_workload
from verify_logs import search_correlated_trace, check_distribution_in_es

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENVOY_DIR = os.path.join(BASE_DIR, "envoy")
RESULTS_PATH = os.path.join(BASE_DIR, "experiment_results.json")


def save_results(all_results):
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)


def switch_envoy(mode: str):
    src = os.path.join(ENVOY_DIR, f"envoy-{mode}.yaml")
    dst = os.path.join(ENVOY_DIR, "envoy.yaml")
    shutil.copyfile(src, dst)
    subprocess.run(["docker", "restart", "bookstore-envoy"], check=True, capture_output=True)
    time.sleep(3.5)
    print(f"[Envoy] Switched to {mode} and restarted bookstore-envoy", flush=True)


def set_replica_delay(replica: str, delay_ms: int):
    url = f"http://localhost:8080/admin/delay?replica={replica}&delay_ms={delay_ms}"
    resp = requests.post(url, timeout=5)
    resp.raise_for_status()
    time.sleep(0.8)
    print(f"[Delay] Set replica {replica} delay to {delay_ms} ms -> {resp.json()}", flush=True)


def main():
    rate = 20.0
    duration = 20.0
    http_url = "http://localhost:8080/books/1"
    grpc_target = "localhost:50051"

    all_results = {
        "runs": {},
        "log_correlations": {},
        "envoy_access_log_check": {},
    }

    subprocess.run(["docker", "start", "bookstore-app-1", "bookstore-app-2", "bookstore-app-3"], capture_output=True)
    time.sleep(2.0)

    # ---------------------------------------------------------
    # 1. Part B: Baseline (A-Only Routing)
    # ---------------------------------------------------------
    switch_envoy("part-a-b")
    set_replica_delay("A", 0)
    set_replica_delay("B", 0)
    set_replica_delay("C", 0)

    all_results["runs"]["part_b_http"] = asyncio.run(
        run_workload("http", rate, duration, http_url, grpc_target, 1, 2.5, "Part B: A-Only (HTTP)")
    )
    save_results(all_results)

    all_results["runs"]["part_b_grpc"] = asyncio.run(
        run_workload("grpc", rate, duration, http_url, grpc_target, 1, 2.5, "Part B: A-Only (gRPC)")
    )
    save_results(all_results)

    # Wait briefly for Filebeat -> Elasticsearch ingestion and fetch correlated trace logs
    time.sleep(4.0)
    http_sample_id = all_results["runs"]["part_b_http"]["sample_request_ids"].get("A")
    grpc_sample_id = all_results["runs"]["part_b_grpc"]["sample_request_ids"].get("A")
    if http_sample_id:
        all_results["log_correlations"]["http"] = search_correlated_trace("http://localhost:9200", http_sample_id)
    if grpc_sample_id:
        all_results["log_correlations"]["grpc"] = search_correlated_trace("http://localhost:9200", grpc_sample_id)
    save_results(all_results)

    # ---------------------------------------------------------
    # 2. Part C: Equal-Weight Round Robin (1:1:1)
    # ---------------------------------------------------------
    switch_envoy("part-c-rr")
    all_results["runs"]["part_c_rr_http"] = asyncio.run(
        run_workload("http", rate, duration, http_url, grpc_target, 1, 2.5, "Part C: Round Robin 1:1:1 (HTTP)")
    )
    save_results(all_results)

    all_results["runs"]["part_c_rr_grpc"] = asyncio.run(
        run_workload("grpc", rate, duration, http_url, grpc_target, 1, 2.5, "Part C: Round Robin 1:1:1 (gRPC)")
    )
    save_results(all_results)

    # ---------------------------------------------------------
    # 3. Part D.1: Weighted Round Robin (2:1:1)
    # ---------------------------------------------------------
    switch_envoy("part-d-wrr")
    wrr_http = asyncio.run(
        run_workload("http", rate, duration, http_url, grpc_target, 1, 2.5, "Part D.1: Weighted RR 2:1:1 (HTTP)")
    )
    all_results["runs"]["part_d_wrr_http"] = wrr_http
    save_results(all_results)

    wrr_grpc = asyncio.run(
        run_workload("grpc", rate, duration, http_url, grpc_target, 1, 2.5, "Part D.1: Weighted RR 2:1:1 (gRPC)")
    )
    all_results["runs"]["part_d_wrr_grpc"] = wrr_grpc
    save_results(all_results)

    time.sleep(3.0)
    all_results["envoy_access_log_check"]["part_d_wrr_http"] = check_distribution_in_es(
        "http://localhost:9200", "http", wrr_http["start_iso"], wrr_http["end_iso"]
    )
    all_results["envoy_access_log_check"]["part_d_wrr_grpc"] = check_distribution_in_es(
        "http://localhost:9200", "grpc", wrr_grpc["start_iso"], wrr_grpc["end_iso"]
    )
    save_results(all_results)

    # ---------------------------------------------------------
    # 4. Part D.2: Slow Replica B (+250ms) — Round Robin vs. Least Request
    # ---------------------------------------------------------
    switch_envoy("part-c-rr")
    set_replica_delay("B", 250)

    all_results["runs"]["part_d_slow_b_rr_http"] = asyncio.run(
        run_workload("http", rate, duration, http_url, grpc_target, 1, 2.5, "Part D.2a: Slow B +250ms with Round Robin (HTTP)")
    )
    save_results(all_results)

    all_results["runs"]["part_d_slow_b_rr_grpc"] = asyncio.run(
        run_workload("grpc", rate, duration, http_url, grpc_target, 1, 2.5, "Part D.2a: Slow B +250ms with Round Robin (gRPC)")
    )
    save_results(all_results)

    switch_envoy("part-d-lr")
    all_results["runs"]["part_d_slow_b_lr_http"] = asyncio.run(
        run_workload("http", rate, duration, http_url, grpc_target, 1, 2.5, "Part D.2b: Slow B +250ms with Least Request (HTTP)")
    )
    save_results(all_results)

    all_results["runs"]["part_d_slow_b_lr_grpc"] = asyncio.run(
        run_workload("grpc", rate, duration, http_url, grpc_target, 1, 2.5, "Part D.2b: Slow B +250ms with Least Request (gRPC)")
    )
    save_results(all_results)

    # Restore Replica B delay to 0ms
    set_replica_delay("B", 0)

    # ---------------------------------------------------------
    # 5. Part D.3: Failure & Recovery with Active Health Checks
    # ---------------------------------------------------------
    switch_envoy("part-d-hc")
    time.sleep(2.5)

    all_results["runs"]["part_d_hc_http"] = asyncio.run(
        run_workload(
            "http",
            rate,
            24.0,
            http_url,
            grpc_target,
            1,
            2.5,
            "Part D.3: Failure & Recovery (HTTP)",
            fault_container="bookstore-app-2",
            stop_at_s=6.0,
            start_at_s=15.0,
        )
    )
    save_results(all_results)
    time.sleep(4.0)

    all_results["runs"]["part_d_hc_grpc"] = asyncio.run(
        run_workload(
            "grpc",
            rate,
            24.0,
            http_url,
            grpc_target,
            1,
            2.5,
            "Part D.3: Failure & Recovery (gRPC)",
            fault_container="bookstore-app-2",
            stop_at_s=6.0,
            start_at_s=15.0,
        )
    )
    save_results(all_results)

    # Restore normal healthy Round Robin behavior
    subprocess.run(["docker", "start", "bookstore-app-2"], capture_output=True)
    switch_envoy("part-c-rr")
    set_replica_delay("B", 0)

    save_results(all_results)
    print(f"\nAll experiment results saved to {RESULTS_PATH}", flush=True)


if __name__ == "__main__":
    main()
