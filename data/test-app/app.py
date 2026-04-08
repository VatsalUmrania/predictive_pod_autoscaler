"""
PPA Test App — Prometheus-instrumented HTTP server.
Single container, single /metrics endpoint, zero sidecars.
"""

import time
import random
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from prometheus_client import (
    Counter, Histogram, Gauge,
    start_http_server,
)

#  Metrics
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)

REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

ACTIVE_CONNECTIONS = Gauge(
    "http_connections_active",
    "Number of in-flight HTTP requests",
)


class AppHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        ACTIVE_CONNECTIONS.inc()
        start = time.monotonic()

        try:
            # Simulate realistic latency: 5–50ms baseline
            # PLUS 20ms per active connection to simulate load pressure
            # Max artificial latency capped at 2.0s
            active_conn = ACTIVE_CONNECTIONS._value.get()
            load_penalty = min(active_conn * 0.02, 2.0)
            time.sleep(random.uniform(0.005, 0.05) + load_penalty)

            if self.path == "/":
                # ~2% chance of 500 error
                if random.random() < 0.02:
                    status_code = 500
                    body = b"Internal Server Error\n"
                else:
                    status_code = 200
                    body = b"OK\n"
            elif self.path == "/slow":
                time.sleep(random.uniform(0.1, 0.4))
                status_code = 200
                body = b"SLOW OK\n"
            else:
                status_code = 404
                body = b"Not Found\n"

            self.send_response(status_code)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)

        except BrokenPipeError:
            status_code = 500
        finally:
            duration = time.monotonic() - start
            REQUEST_COUNT.labels(method="GET", path=self.path, status=str(status_code)).inc()
            REQUEST_DURATION.labels(method="GET", path=self.path).observe(duration)
            ACTIVE_CONNECTIONS.dec()

    def log_message(self, format, *args):
        pass


def main():
    start_http_server(9091)
    print("Metrics server started on :9091/metrics")

    server = ThreadingHTTPServer(("0.0.0.0", 8080), AppHandler)
    print("App server started on :8080")
    server.serve_forever()


if __name__ == "__main__":
    main()
