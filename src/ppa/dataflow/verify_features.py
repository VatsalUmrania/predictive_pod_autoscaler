import sys
from datetime import datetime, timezone
from pathlib import Path

import requests  # type: ignore[import-untyped]

from ppa.common.feature_spec import QUERIED_FEATURES, TEMPORAL_FEATURES
from ppa.dataflow.config import PROMETHEUS_URL, QUERIES, TARGET_APP

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

print(f"\n{'=' * 55}")
print(
    f"Feature Verification — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
)
print(f"Target App : {TARGET_APP}")
print(f"Prometheus : {PROMETHEUS_URL}")
print(f"{'=' * 55}\n")

all_good = True
# Only verify features that have direct PromQL queries defined in config.py
for feature_name in QUERIED_FEATURES:
    if feature_name not in QUERIES:
        print(f"[SKIP] {feature_name:<22} (calculated feature)")
        continue

    query = QUERIES[feature_name]
    try:
        response = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", {}).get("result", [])
        has_data = len(data) > 0
        status = "OK" if has_data else "WARN"
        if not has_data:
            all_good = False
        value = f"= {float(data[0]['value'][1]):.4f}" if has_data else "no data yet"
        print(f"[{status}] {feature_name:<22} {value}")
    except Exception as exc:
        print(f"[ERR ] {feature_name:<22} {exc}")
        all_good = False

print("\nGenerated temporal features:")
for feature_name in TEMPORAL_FEATURES:
    print(f"[OK  ] {feature_name}")

print(f"\n{'=' * 55}")
print(
    "All required queried features are present."
    if all_good
    else "Some features are missing; do not export training data yet."
)
print(f"{'=' * 55}\n")
