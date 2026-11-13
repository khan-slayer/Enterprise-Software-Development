"""
generate_figures.py - Generates publication-quality charts from `experiment_results.json`
and Prometheus time-series metrics for Assignment 3 Report.
"""

import json
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_PATH = os.path.join(BASE_DIR, "experiment_results.json")
FIG_DIR = os.path.join(BASE_DIR, "figures")
os.makedirs(FIG_DIR, exist_ok=True)


def generate_all_charts():
    with open(RESULTS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    runs = data["runs"]

    # 1. Traffic Share Comparison Across Policies (HTTP & gRPC)
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), sharey=True)
    policies = [
        ("Part B\nA-Only", "part_b"),
        ("Part C\nRR (1:1:1)", "part_c_rr"),
        ("Part D.1\nWRR (2:1:1)", "part_d_wrr"),
        ("Part D.2a\nSlow B (RR)", "part_d_slow_b_rr"),
        ("Part D.2b\nSlow B (LR)", "part_d_slow_b_lr"),
        ("Part D.3\nFail/Rec (HC)", "part_d_hc"),
    ]

    x = np.arange(len(policies))
    width = 0.25
    colors = {"A": "#2563eb", "B": "#dc2626", "C": "#16a34a"}

    for ax_idx, (iface, title) in enumerate([("http", "HTTP (GET /books/1)"), ("grpc", "gRPC (GetBook id=1)")]):
        ax = axes[ax_idx]
        for i, rep in enumerate(["A", "B", "C"]):
            shares = [
                runs[f"{key}_{iface}"]["replica_shares_pct"].get(rep, 0.0)
                for _, key in policies
            ]
            bars = ax.bar(x + (i - 1) * width, shares, width, label=f"Replica {rep}", color=colors[rep])
            for b in bars:
                h = b.get_height()
                if h > 1.0:
                    ax.text(
                        b.get_x() + b.get_width() / 2.0,
                        h + 1.2,
                        f"{h:.0f}%",
                        ha="center",
                        va="bottom",
                        fontsize=7.5,
                        fontweight="bold",
                    )

        ax.set_title(f"{title} — Traffic Share by Replica (%)", fontsize=11, fontweight="bold")
        ax.set_xticks(x)
        ax.set_xticklabels([p[0] for p in policies], fontsize=8.5)
        ax.set_ylim(0, 112)
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        if ax_idx == 0:
            ax.set_ylabel("Share of Successful Traffic (%)", fontsize=10)
        ax.legend(loc="upper right", fontsize=9)

    plt.tight_layout()
    plt.savefig(os.path.join(FIG_DIR, "traffic_shares_comparison.png"), dpi=220)
    plt.close()

    # 2. Client p50 and p95 Latency Comparison (especially Slow Replica B: RR vs Least Request)
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax_idx, (iface, title) in enumerate([("http", "HTTP Client Latency"), ("grpc", "gRPC Client Latency")]):
        ax = axes[ax_idx]
        p50s = [runs[f"{key}_{iface}"]["client_p50_ms"] for _, key in policies]
        p95s = [runs[f"{key}_{iface}"]["client_p95_ms"] for _, key in policies]

        b1 = ax.bar(x - width / 2, p50s, width, label="Client p50 (ms)", color="#0ea5e9")
        b2 = ax.bar(x + width / 2, p95s, width, label="Client p95 (ms)", color="#f97316")

        for b in list(b1) + list(b2):
            h = b.get_height()
            ax.text(
                b.get_x() + b.get_width() / 2.0,
                h + 3.0,
                f"{h:.1f}",
                ha="center",
                va="bottom",
                fontsize=7.5,
            )

        ax.set_title(f"{title} — p50 vs p95 Across Experiments (ms)", fontsize=11, fontweight="bold")
        ax.set_xticks(x)
        ax.set_xticklabels([p[0] for p in policies], fontsize=8.5)
        ax.set_ylabel("Latency (ms)", fontsize=10)
        ax.set_ylim(0, max(p95s) * 1.18 + 15)
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        ax.legend(loc="upper left", fontsize=9)

    plt.tight_layout()
    plt.savefig(os.path.join(FIG_DIR, "latency_p50_p95_comparison.png"), dpi=220)
    plt.close()

    print("Saved charts to", FIG_DIR)


if __name__ == "__main__":
    generate_all_charts()

