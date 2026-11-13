"""
fixed_rate_client.py - Open-Loop Fixed-Rate Load Generator for HTTP and gRPC
(Assignment 3: Parts B, C, and D).

Dispatches requests at a fixed rate (requests/sec) for a fixed duration without
waiting for previous responses to complete. Disables client retries and circuit
breakers. For gRPC, multiplexes all calls over a single shared HTTP/2 connection
to Envoy.
"""

import argparse
import asyncio
import datetime
import json
import math
import subprocess
import time
import uuid
from collections import Counter
from typing import Dict, Any, List, Optional

import httpx
import grpc
import grpc.aio

from proto import bookstore_pb2, bookstore_pb2_grpc


def percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


async def send_http_request(
    client: httpx.AsyncClient,
    url: str,
    seq: int,
    t0: float,
) -> Dict[str, Any]:
    req_id = f"a3-http-{int(time.time()*1000)}-{seq}-{uuid.uuid4().hex[:6]}"
    send_ts = time.perf_counter()
    rel_time = send_ts - t0
    try:
        resp = await client.get(url, headers={"x-request-id": req_id})
        latency_ms = (time.perf_counter() - send_ts) * 1000.0
        replica = resp.headers.get("x-replica-id", "UNKNOWN")
        ok = 200 <= resp.status_code < 300
        return {
            "seq": seq,
            "rel_time": round(rel_time, 3),
            "request_id": req_id,
            "ok": ok,
            "status": str(resp.status_code),
            "replica": replica if ok else "ERROR",
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        latency_ms = (time.perf_counter() - send_ts) * 1000.0
        return {
            "seq": seq,
            "rel_time": round(rel_time, 3),
            "request_id": req_id,
            "ok": False,
            "status": type(exc).__name__,
            "replica": "ERROR",
            "latency_ms": latency_ms,
        }


async def send_grpc_request(
    stub: bookstore_pb2_grpc.BookServiceStub,
    book_id: int,
    timeout_s: float,
    seq: int,
    t0: float,
) -> Dict[str, Any]:
    req_id = f"a3-grpc-{int(time.time()*1000)}-{seq}-{uuid.uuid4().hex[:6]}"
    send_ts = time.perf_counter()
    rel_time = send_ts - t0
    try:
        call = stub.GetBook(
            bookstore_pb2.GetBookRequest(id=book_id),
            metadata=[("x-request-id", req_id)],
            timeout=timeout_s,
        )
        response = await call
        latency_ms = (time.perf_counter() - send_ts) * 1000.0

        init_meta = dict(await call.initial_metadata())
        trail_meta = dict(await call.trailing_metadata())
        replica = (
            init_meta.get("x-replica-id")
            or trail_meta.get("x-replica-id")
            or "UNKNOWN"
        )
        return {
            "seq": seq,
            "rel_time": round(rel_time, 3),
            "request_id": req_id,
            "ok": True,
            "status": "OK",
            "replica": replica,
            "latency_ms": latency_ms,
        }
    except grpc.aio.AioRpcError as rpc_err:
        latency_ms = (time.perf_counter() - send_ts) * 1000.0
        return {
            "seq": seq,
            "rel_time": round(rel_time, 3),
            "request_id": req_id,
            "ok": False,
            "status": rpc_err.code().name,
            "replica": "ERROR",
            "latency_ms": latency_ms,
        }
    except Exception as exc:
        latency_ms = (time.perf_counter() - send_ts) * 1000.0
        return {
            "seq": seq,
            "rel_time": round(rel_time, 3),
            "request_id": req_id,
            "ok": False,
            "status": type(exc).__name__,
            "replica": "ERROR",
            "latency_ms": latency_ms,
        }


async def mid_run_fault_controller(
    container_name: str,
    stop_at_s: float,
    start_at_s: float,
    t0: float,
    events_out: Dict[str, Any],
):
    """Stops and restarts a target replica container mid-run for Part D failure/recovery."""
    await asyncio.sleep(max(0.0, stop_at_s - (time.perf_counter() - t0)))
    stop_req_time = time.perf_counter() - t0
    subprocess.run(["docker", "stop", "-t", "1", container_name], capture_output=True)
    stop_done_time = time.perf_counter() - t0
    events_out["container_stop_initiated_s"] = round(stop_req_time, 3)
    events_out["container_stopped_s"] = round(stop_done_time, 3)
    print(f"  [Fault] Stopped {container_name} at t={stop_done_time:.2f}s")

    await asyncio.sleep(max(0.0, start_at_s - (time.perf_counter() - t0)))
    start_req_time = time.perf_counter() - t0
    subprocess.run(["docker", "start", container_name], capture_output=True)
    start_done_time = time.perf_counter() - t0
    events_out["container_start_initiated_s"] = round(start_req_time, 3)
    events_out["container_started_s"] = round(start_done_time, 3)
    print(f"  [Recovery] Restarted {container_name} at t={start_done_time:.2f}s")


async def run_workload(
    interface: str,
    rate: float,
    duration: float,
    http_url: str,
    grpc_target: str,
    book_id: int,
    timeout_s: float,
    label: str = "",
    fault_container: Optional[str] = None,
    stop_at_s: float = 10.0,
    start_at_s: float = 25.0,
) -> Dict[str, Any]:
    total_requests = int(round(rate * duration))
    interval = 1.0 / rate

    start_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(
        f"\n=== Running Open-Loop Fixed-Rate Workload [{label or interface.upper()}] ==="
    )
    print(
        f"Interface: {interface.upper()} | Target Rate: {rate:.1f} req/s | "
        f"Duration: {duration:.1f}s | Total Requests: {total_requests}"
    )

    tasks = []
    fault_events: Dict[str, Any] = {}

    if interface == "http":
        limits = httpx.Limits(
            max_connections=500, max_keepalive_connections=100
        )
        async with httpx.AsyncClient(limits=limits, timeout=timeout_s) as client:
            t0 = time.perf_counter()
            fault_task = None
            if fault_container:
                fault_task = asyncio.create_task(
                    mid_run_fault_controller(
                        fault_container, stop_at_s, start_at_s, t0, fault_events
                    )
                )

            for i in range(total_requests):
                target_ts = t0 + i * interval
                sleep_dur = target_ts - time.perf_counter()
                if sleep_dur > 0:
                    await asyncio.sleep(sleep_dur)
                tasks.append(
                    asyncio.create_task(
                        send_http_request(client, http_url, i + 1, t0)
                    )
                )

            dispatch_elapsed = time.perf_counter() - t0
            results = await asyncio.gather(*tasks)
            if fault_task:
                await fault_task

    elif interface == "grpc":
        # Single shared HTTP/2 connection to Envoy with client retries explicitly disabled
        channel_options = [
            ("grpc.enable_retries", 0),
            ("grpc.keepalive_time_ms", 30000),
        ]
        async with grpc.aio.insecure_channel(
            grpc_target, options=channel_options
        ) as channel:
            stub = bookstore_pb2_grpc.BookServiceStub(channel)
            # Warm up channel handshake prior to timed open-loop ticker
            try:
                await stub.GetBook(
                    bookstore_pb2.GetBookRequest(id=book_id),
                    metadata=[("x-request-id", "warmup")],
                    timeout=timeout_s,
                )
            except Exception:
                pass

            t0 = time.perf_counter()
            fault_task = None
            if fault_container:
                fault_task = asyncio.create_task(
                    mid_run_fault_controller(
                        fault_container, stop_at_s, start_at_s, t0, fault_events
                    )
                )

            for i in range(total_requests):
                target_ts = t0 + i * interval
                sleep_dur = target_ts - time.perf_counter()
                if sleep_dur > 0:
                    await asyncio.sleep(sleep_dur)
                tasks.append(
                    asyncio.create_task(
                        send_grpc_request(stub, book_id, timeout_s, i + 1, t0)
                    )
                )

            dispatch_elapsed = time.perf_counter() - t0
            results = await asyncio.gather(*tasks)
            if fault_task:
                await fault_task
    else:
        raise ValueError(f"Unsupported interface: {interface}")

    end_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    sent = len(results)
    successes = sum(1 for r in results if r["ok"])
    failures = sent - successes
    error_rate_pct = (failures / sent * 100.0) if sent > 0 else 0.0
    actual_rps = sent / dispatch_elapsed if dispatch_elapsed > 0 else 0.0

    all_latencies = sorted(r["latency_ms"] for r in results)
    ok_latencies = sorted(r["latency_ms"] for r in results if r["ok"])
    lat_source = ok_latencies if ok_latencies else all_latencies

    p50_ms = percentile(lat_source, 50.0)
    p95_ms = percentile(lat_source, 95.0)
    mean_ms = sum(lat_source) / len(lat_source) if lat_source else 0.0

    replica_counter = Counter(r["replica"] for r in results if r["ok"])
    status_counter = Counter(r["status"] for r in results)

    replica_counts = {
        rep: replica_counter.get(rep, 0) for rep in ("A", "B", "C")
    }
    replica_shares_pct = {
        rep: round((replica_counts[rep] / successes * 100.0), 2)
        if successes > 0
        else 0.0
        for rep in ("A", "B", "C")
    }

    sample_ids = {}
    for r in results:
        if r["ok"] and r["replica"] not in sample_ids:
            sample_ids[r["replica"]] = r["request_id"]

    # Analyze failure/recovery timeline if fault was injected
    if fault_container and fault_events:
        b_times = [r["rel_time"] for r in results if r["ok"] and r["replica"] == "B"]
        err_times = [r["rel_time"] for r in results if not r["ok"]]
        stop_t = fault_events.get("container_stopped_s", stop_at_s)
        start_t = fault_events.get("container_started_s", start_at_s)
        b_before_stop = [t for t in b_times if t <= stop_t + 3.0]
        b_after_start = [t for t in b_times if t >= start_t]
        fault_events["last_b_before_removal_s"] = (
            max(b_before_stop) if b_before_stop else None
        )
        fault_events["first_b_after_recovery_s"] = (
            min(b_after_start) if b_after_start else None
        )
        fault_events["first_error_s"] = min(err_times) if err_times else None
        fault_events["last_error_s"] = max(err_times) if err_times else None
        if (
            fault_events["first_b_after_recovery_s"] is not None
            and start_t is not None
        ):
            fault_events["recovery_lag_s"] = round(
                fault_events["first_b_after_recovery_s"] - start_t, 3
            )

    summary = {
        "label": label or interface.upper(),
        "interface": interface,
        "start_iso": start_iso,
        "end_iso": end_iso,
        "target_rps": rate,
        "actual_rps": round(actual_rps, 2),
        "duration_s": round(dispatch_elapsed, 2),
        "sent": sent,
        "successes": successes,
        "failures": failures,
        "error_rate_pct": round(error_rate_pct, 2),
        "replica_counts": replica_counts,
        "replica_shares_pct": replica_shares_pct,
        "status_counts": dict(status_counter),
        "client_p50_ms": round(p50_ms, 2),
        "client_p95_ms": round(p95_ms, 2),
        "client_mean_ms": round(mean_ms, 2),
        "sample_request_ids": sample_ids,
        "fault_timeline": fault_events,
    }

    print(
        f"Completed: Sent={sent}, Success={successes}, Failures={failures} ({error_rate_pct:.2f}%)"
    )
    print(
        f"Actual Rate: {actual_rps:.2f} req/s | Client p50: {p50_ms:.2f} ms | Client p95: {p95_ms:.2f} ms"
    )
    print(
        f"Traffic Share (A / B / C): "
        f"A={replica_counts['A']} ({replica_shares_pct['A']:.1f}%), "
        f"B={replica_counts['B']} ({replica_shares_pct['B']:.1f}%), "
        f"C={replica_counts['C']} ({replica_shares_pct['C']:.1f}%)"
    )
    if fault_events:
        print(f"Fault Timeline: {json.dumps(fault_events)}")

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Assignment 3 Open-Loop Fixed-Rate Test Client"
    )
    parser.add_argument(
        "--interface",
        choices=["http", "grpc", "both"],
        default="both",
        help="Protocol interface to test",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=20.0,
        help="Fixed request rate in requests/second (default: 20)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=30.0,
        help="Test duration in seconds (default: 30)",
    )
    parser.add_argument(
        "--http-url",
        type=str,
        default="http://localhost:8080/books/1",
        help="Target HTTP endpoint through Envoy",
    )
    parser.add_argument(
        "--grpc-target",
        type=str,
        default="localhost:50051",
        help="Target gRPC host:port through Envoy",
    )
    parser.add_argument(
        "--book-id",
        type=int,
        default=1,
        help="Book ID to query for read operation",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=2.5,
        help="Per-request timeout in seconds",
    )
    parser.add_argument(
        "--label",
        type=str,
        default="",
        help="Run label for reporting",
    )
    parser.add_argument(
        "--fault-container",
        type=str,
        default=None,
        help="Optional container name to stop and restart during the run (e.g. bookstore-app-2)",
    )
    parser.add_argument(
        "--stop-at",
        type=float,
        default=8.0,
        help="Seconds into the run to stop fault-container",
    )
    parser.add_argument(
        "--start-at",
        type=float,
        default=20.0,
        help="Seconds into the run to restart fault-container",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="Optional file path to save JSON results",
    )

    args = parser.parse_args()

    interfaces = ["http", "grpc"] if args.interface == "both" else [args.interface]
    outputs = []
    for iface in interfaces:
        lbl = f"{args.label}-{iface}" if args.label else iface
        res = asyncio.run(
            run_workload(
                interface=iface,
                rate=args.rate,
                duration=args.duration,
                http_url=args.http_url,
                grpc_target=args.grpc_target,
                book_id=args.book_id,
                timeout_s=args.timeout,
                label=lbl,
                fault_container=args.fault_container,
                stop_at_s=args.stop_at,
                start_at_s=args.start_at,
            )
        )
        outputs.append(res)

    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(outputs if len(outputs) > 1 else outputs[0], f, indent=2)


if __name__ == "__main__":
    main()

