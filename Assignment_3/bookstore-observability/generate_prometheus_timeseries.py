"""
generate_prometheus_timeseries.py - Queries Prometheus `query_range` over the
Assignment 3 experiment window and renders per-replica time-series charts
matching the Grafana dashboard PromQL queries.
"""

import os
import datetime
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FIG_DIR = os.path.join(BASE_DIR, "figures")
os.makedirs(FIG_DIR, exist_ok=True)

PROM_URL = "http://localhost:9090/api/v1/query_range"

# Exact window covering Part B -> Part C -> Part D.1 -> Part D.2 -> Part D.3
START_ISO = "2026-11-13T11:52:30Z"
END_ISO = "2026-11-13T12:00:15Z"
STEP = "5s"

COLORS = {"A": "#2563eb", "B": "#dc2626", "C": "#16a34a"}


def query_prom(expr: str):
    resp = requests.get(
        PROM_URL,
        params={"query": expr, "start": START_ISO, "end": END_ISO, "step": STEP},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {data}")
    return data["data"]["result"]


def plot_series(ax, results, label_fn, color_fn=None):
    for res in results:
        metric = res["metric"]
        label = label_fn(metric)
        times = [
            datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
            for ts, _ in res["values"]
        ]
        vals = [
            float(v) if v not in ("NaN", "Inf", "-Inf") else float("nan")
            for _, v in res["values"]
        ]
        color = color_fn(metric) if color_fn else None
        ax.plot(times, vals, label=label, linewidth=1.8, color=color)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.legend(loc="upper right", fontsize=8)


def main():
    # Figure 1: Request Rate & Traffic Share (%) per Replica (HTTP & gRPC)
    fig, axes = plt.subplots(2, 2, figsize=(13, 8), sharex=True)

    http_rate = query_prom("sum by (replica) (rate(http_server_requests_total[1m]))")
    grpc_rate = query_prom("sum by (replica) (rate(grpc_server_handled_total[1m]))")
    http_share = query_prom(
        "100 * sum by (replica) (rate(http_server_requests_total[1m])) / ignoring(replica) group_left sum(rate(http_server_requests_total[1m]))"
    )
    grpc_share = query_prom(
        "100 * sum by (replica) (rate(grpc_server_handled_total[1m])) / ignoring(replica) group_left sum(rate(grpc_server_handled_total[1m]))"
    )

    plot_series(
        axes[0, 0],
        http_rate,
        lambda m: f"Replica {m.get('replica')}",
        lambda m: COLORS.get(m.get("replica")),
    )
    axes[0, 0].set_title("HTTP Request Rate per Replica (req/s) [1m]", fontweight="bold")
    axes[0, 0].set_ylabel("req/s")

    plot_series(
        axes[0, 1],
        grpc_rate,
        lambda m: f"Replica {m.get('replica')}",
        lambda m: COLORS.get(m.get("replica")),
    )
    axes[0, 1].set_title("gRPC Request Rate per Replica (req/s) [1m]", fontweight="bold")
    axes[0, 1].set_ylabel("req/s")

    plot_series(
        axes[1, 0],
        http_share,
        lambda m: f"Replica {m.get('replica')}",
        lambda m: COLORS.get(m.get("replica")),
    )
    axes[1, 0].set_title("HTTP Share of Total Traffic (%) [1m]", fontweight="bold")
    axes[1, 0].set_ylabel("Share (%)")
    axes[1, 0].set_ylim(-5, 108)

    plot_series(
        axes[1, 1],
        grpc_share,
        lambda m: f"Replica {m.get('replica')}",
        lambda m: COLORS.get(m.get("replica")),
    )
    axes[1, 1].set_title("gRPC Share of Total Traffic (%) [1m]", fontweight="bold")
    axes[1, 1].set_ylabel("Share (%)")
    axes[1, 1].set_ylim(-5, 108)

    fig.autofmt_xdate()
    plt.tight_layout()
    out1 = os.path.join(FIG_DIR, "prometheus_timeseries_traffic.png")
    plt.savefig(out1, dpi=220)
    plt.close()

    # Figure 2: Active Requests, p95 Latency, and Errors/Health per Replica
    fig, axes = plt.subplots(2, 2, figsize=(13, 8), sharex=True)

    http_p95 = query_prom(
        "histogram_quantile(0.95, sum by (le, replica) (rate(http_server_request_duration_seconds_bucket[1m]))) * 1000"
    )
    grpc_p95 = query_prom(
        "histogram_quantile(0.95, sum by (le, replica) (rate(grpc_server_handling_seconds_bucket[1m]))) * 1000"
    )
    http_err = query_prom(
        'sum by (replica, status) (rate(http_server_requests_total{status!~"2.."}[1m]))'
    )
    envoy_err = query_prom(
        'sum by (envoy_response_code_class) (rate(envoy_cluster_upstream_rq_xx{envoy_response_code_class="5"}[1m]))'
    )
    hc_healthy = query_prom(
        'envoy_cluster_membership_healthy{envoy_cluster_name=~"bookstore_.*"}'
    )

    plot_series(
        axes[0, 0],
        http_p95,
        lambda m: f"Replica {m.get('replica')} p95",
        lambda m: COLORS.get(m.get("replica")),
    )
    axes[0, 0].set_title("HTTP p95 Latency per Replica (ms) [1m]", fontweight="bold")
    axes[0, 0].set_ylabel("Latency (ms)")

    plot_series(
        axes[0, 1],
        grpc_p95,
        lambda m: f"Replica {m.get('replica')} p95",
        lambda m: COLORS.get(m.get("replica")),
    )
    axes[0, 1].set_title("gRPC p95 Latency per Replica (ms) [1m]", fontweight="bold")
    axes[0, 1].set_ylabel("Latency (ms)")

    plot_series(
        axes[1, 0],
        envoy_err + http_err,
        lambda m: (
            f"Envoy Upstream 5xx"
            if "envoy_response_code_class" in m
            else f"Replica {m.get('replica')} HTTP {m.get('status')}"
        ),
    )
    axes[1, 0].set_title("Envoy 5xx & Backend Error Rate (err/s) [1m]", fontweight="bold")
    axes[1, 0].set_ylabel("Errors / sec")

    plot_series(
        axes[1, 1],
        hc_healthy,
        lambda m: m.get("envoy_cluster_name"),
    )
    axes[1, 1].set_title("Envoy Healthy Upstream Replicas (Membership)", fontweight="bold")
    axes[1, 1].set_ylabel("Healthy Endpoints")
    axes[1, 1].set_ylim(0, 3.5)

    fig.autofmt_xdate()
    plt.tight_layout()
    out2 = os.path.join(FIG_DIR, "prometheus_timeseries_latency_health.png")
    plt.savefig(out2, dpi=220)
    plt.close()

    print("Saved Prometheus time-series figures:", out1, out2)


if __name__ == "__main__":
    main()

