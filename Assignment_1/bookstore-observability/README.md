# Bookstore Observability Pipeline

This repository contains a complete, containerized observability pipeline built for a simulated Bookstore API.

## Prerequisites
- Docker and Docker Compose installed
- Python 3.9+ installed natively

## 1. How to Start the Environment
Run the following command to build the API container and start the entire observability stack (Prometheus, Grafana, Elasticsearch, Kibana, Filebeat, Node Exporter):
```bash
docker-compose up -d --build
```

## 2. How to Generate Normal Traffic
Once the containers are running, you must simulate traffic to generate logs and metrics:
```bash
python traffic_generator.py --mode normal
```
*Leave this running in a separate terminal window.*

## 3. How to Reproduce the Experiments
While the normal traffic generator is running, open a **new** terminal window to trigger the anomalies.

### Experiment 1: p95 Latency Spike (Toggle Delay)
To simulate network degradation, run:
```bash
python traffic_generator.py --mode toggle-delay
```
This adds a 500ms delay to every 5th request. Check the **p95 Latency** graph in Grafana to see the massive spike. Run the command again to toggle it off and watch the graph recover.

### Experiment 2: Cardinality Explosion
To simulate a cardinality explosion (adding 100 unique `request_id` tags to memory):
```bash
python traffic_generator.py --mode cardinality
```
Check the **Unique Time Series Growth** graph in Grafana. To fix the issue, remove the `['request_id']` label from the `demo_requests_total` metric inside `app.py`, run `docker-compose restart api`, and repeat the test.

## 4. How to Safely Clean Up
To stop the application, destroy the containers, and clear all hidden data volumes to save space on your hard drive, run:
```bash
docker-compose down -v
```
