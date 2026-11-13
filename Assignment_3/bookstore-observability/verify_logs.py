"""
verify_logs.py - Elasticsearch & Envoy Access Log Verification Helper (Assignment 3).

1. Queries Elasticsearch (`filebeat-*`) by `json.request_id` to retrieve both
   the Envoy proxy JSON access log event and the backend replica JSON log event
   for HTTP and gRPC (Part B requirement).
2. Queries Elasticsearch / Envoy access logs for a given time range `[start_iso, end_iso]`
   to cross-check per-replica traffic distribution against client measurements (Part D requirement).
"""

import argparse
import json
import subprocess
from collections import Counter
from typing import Dict, Any, Optional
import requests


def search_correlated_trace(es_url: str, request_id: str) -> Dict[str, Any]:
    """Searches Elasticsearch for all log events matching `json.request_id`."""
    query = {
        "size": 10,
        "sort": [{"@timestamp": {"order": "asc"}}],
        "query": {"term": {"json.request_id.keyword": request_id}},
    }
    resp = requests.post(f"{es_url}/filebeat-*/_search", json=query, timeout=10)
    if resp.status_code != 200 or resp.json().get("hits", {}).get("total", {}).get("value", 0) == 0:
        # Fallback to match query if .keyword mapping is not used
        query = {
            "size": 10,
            "sort": [{"@timestamp": {"order": "asc"}}],
            "query": {"match_phrase": {"json.request_id": request_id}},
        }
        resp = requests.post(f"{es_url}/filebeat-*/_search", json=query, timeout=10)

    data = resp.json()
    hits = data.get("hits", {}).get("hits", [])
    events = [h.get("_source", {}).get("json", {}) for h in hits]
    return {
        "request_id": request_id,
        "total_hits": len(events),
        "events": events,
    }


def check_distribution_in_es(
    es_url: str,
    interface: str,
    start_iso: str,
    end_iso: str,
) -> Dict[str, Any]:
    """Aggregates Envoy access logs in Elasticsearch over [start_iso, end_iso] by replica."""
    query = {
        "size": 10000,
        "query": {
            "bool": {
                "must": [
                    {"match": {"json.logger": "envoy_proxy"}},
                    {"match": {"json.interface": interface}},
                    {
                        "range": {
                            "json.timestamp": {
                                "gte": start_iso,
                                "lte": end_iso,
                            }
                        }
                    },
                ]
            }
        },
    }
    try:
        resp = requests.post(f"{es_url}/filebeat-*/_search", json=query, timeout=10)
        hits = resp.json().get("hits", {}).get("hits", [])
    except Exception:
        hits = []

    counts = Counter()
    for h in hits:
        js = h.get("_source", {}).get("json", {})
        rep = js.get("replica")
        if rep in ("A", "B", "C"):
            counts[rep] += 1

    # Also support direct Envoy container stdout JSON log parsing for exact time window
    if sum(counts.values()) == 0:
        proc = subprocess.run(
            [
                "docker",
                "logs",
                "--since",
                start_iso,
                "--until",
                end_iso,
                "bookstore-envoy",
            ],
            capture_output=True,
            text=True,
        )
        for line in (proc.stdout + "\n" + proc.stderr).splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
                if (
                    rec.get("logger") == "envoy_proxy"
                    and rec.get("interface") == interface
                    and rec.get("replica") in ("A", "B", "C")
                ):
                    counts[rec["replica"]] += 1
            except Exception:
                pass

    total = sum(counts.values())
    shares = {
        r: round(counts.get(r, 0) / total * 100.0, 2) if total > 0 else 0.0
        for r in ("A", "B", "C")
    }
    return {
        "interface": interface,
        "start_iso": start_iso,
        "end_iso": end_iso,
        "total_envoy_logs": total,
        "replica_counts": {r: counts.get(r, 0) for r in ("A", "B", "C")},
        "replica_shares_pct": shares,
    }


def main():
    parser = argparse.ArgumentParser(description="Verify Envoy + Replica Logs in Elasticsearch")
    parser.add_argument("--es-url", default="http://localhost:9200")
    parser.add_argument("--request-id", default=None, help="Search correlated logs for a request_id")
    parser.add_argument("--interface", choices=["http", "grpc"], default="http")
    parser.add_argument("--start-iso", default=None)
    parser.add_argument("--end-iso", default=None)
    args = parser.parse_args()

    if args.request_id:
        res = search_correlated_trace(args.es_url, args.request_id)
        print(json.dumps(res, indent=2))
    elif args.start_iso and args.end_iso:
        res = check_distribution_in_es(args.es_url, args.interface, args.start_iso, args.end_iso)
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()

