import requests
from datetime import datetime
from config import PROMETHEUS_URL, QUERIES, TARGET_APP

print(f"\n{'='*55}")
print(f"Feature Verification — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"Target App : {TARGET_APP}")
print(f"Prometheus : {PROMETHEUS_URL}")
print(f"{'='*55}\n")

all_good = True
groups = {
    "── Core Load Signals ───────────────────────────": [
        "requests_per_second", "cpu_usage_percent",
        "memory_usage_bytes", "latency_p95_ms"
    ],
    "── State Awareness ─────────────────────────────": [
        "current_replicas"
    ],
    "── Unique Indicators ───────────────────────────": [
        "active_connections", "error_rate"
    ],
    "── Momentum Signals ────────────────────────────": [
        "cpu_acceleration", "rps_acceleration"
    ],
}

for group_label, feature_names in groups.items():
    print(group_label)
    for feature in feature_names:
        query = QUERIES[feature]
        try:
            r = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": query},
                timeout=5
            )
            result = r.json()
            data = result.get("data", {}).get("result", [])
            has_data = len(data) > 0
            status = "✅" if has_data else "⚠️ "
            if not has_data:
                all_good = False
            val = f"= {float(data[0]['value'][1]):.4f}" if has_data else "no data yet"
            print(f"  {status}  {feature:<35} {val}")
        except Exception as e:
            print(f"  ❌  {feature:<35} ERROR: {e}")
            all_good = False
    print()

print("── Generated (no query needed) ─────────────────")
print("  ✅  hour_of_day")
print("  ✅  day_of_week")
print("  ✅  is_weekend")

print(f"\n{'='*55}")
print("✅ All features ready!" if all_good else "⚠️  Some features missing — is traffic running and Prometheus scraping?")
print(f"{'='*55}\n")