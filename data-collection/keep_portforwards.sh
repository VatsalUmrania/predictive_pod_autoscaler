#!/bin/bash
# Keeps port-forwards alive — run this in a separate terminal
echo "Starting port-forward watchdog..."

while true; do
    # Prometheus
    if ! curl -s http://localhost:9090/-/ready > /dev/null 2>&1; then
        echo "$(date) — Restarting Prometheus port-forward..."
        pkill -f "port-forward.*9090" 2>/dev/null
        kubectl port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 -n monitoring &
    fi

    # Grafana
    if ! curl -s http://localhost:3000/api/health > /dev/null 2>&1; then
        echo "$(date) — Restarting Grafana port-forward..."
        pkill -f "port-forward.*3000" 2>/dev/null
        kubectl port-forward svc/prometheus-grafana 3000:80 -n monitoring &
    fi

    sleep 30
done
