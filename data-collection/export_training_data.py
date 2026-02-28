import requests
import pandas as pd
from datetime import datetime, timedelta
import os

PROMETHEUS = "http://localhost:9090"
OUTPUT_DIR = "predictive_pod_autoscaler/data-collection/training-data"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Export last N days of data
DAYS_BACK = 7
END = datetime.now()
START = END - timedelta(days=DAYS_BACK)
STEP = "60"  # 1 minute resolution

queries = {
    "requests_per_second":  'rate(istio_requests_total{destination_service=~"test-app.*"}[1m])',
    "cpu_usage_percent":    'sum(rate(container_cpu_usage_seconds_total{pod=~"test-app.*"}[1m]))*100',
    "memory_usage_bytes":   'sum(container_memory_working_set_bytes{pod=~"test-app.*"})',
    "latency_p95_ms":       'histogram_quantile(0.95, rate(istio_request_duration_milliseconds_bucket{destination_service=~"test-app.*"}[5m]))',
    "active_connections":   'envoy_server_total_connections{pod=~"test-app.*"}',
    "error_rate":           'sum(istio_requests_total{destination_service=~"test-app.*"})',
}

print(f"Exporting {DAYS_BACK} days of data...")
print(f"From: {START.strftime('%Y-%m-%d %H:%M')}")
print(f"To:   {END.strftime('%Y-%m-%d %H:%M')}\n")

dfs = []
for name, query in queries.items():
    print(f"  Fetching {name}...")
    r = requests.get(f"{PROMETHEUS}/api/v1/query_range", params={
        "query": query,
        "start": START.timestamp(),
        "end":   END.timestamp(),
        "step":  STEP
    })
    data = r.json()["data"]["result"]
    if not data:
        print(f"  ⚠️  No data for {name}")
        continue

    values = data[0]["values"]
    df = pd.DataFrame(values, columns=["timestamp", name])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
    df[name] = df[name].astype(float)
    dfs.append(df.set_index("timestamp"))
    print(f"  ✅ {len(df)} rows")

if dfs:
    # Merge all features into one dataframe
    combined = pd.concat(dfs, axis=1).sort_index()

    # Add temporal features
    combined["hour_of_day"] = combined.index.hour
    combined["day_of_week"] = combined.index.dayofweek

    # Save
    filename = f"{OUTPUT_DIR}/features_{START.strftime('%Y%m%d')}_{END.strftime('%Y%m%d')}.csv"
    combined.to_csv(filename)
    print(f"\n✅ Saved {len(combined)} rows × {len(combined.columns)} features")
    print(f"   File: {filename}")
    print(f"\nFirst 3 rows:")
    print(combined.head(3).to_string())
else:
    print("❌ No data exported — run after collecting data for at least 1 day")
