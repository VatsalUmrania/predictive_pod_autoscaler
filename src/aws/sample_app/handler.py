"""
Sample Failing App — Lambda Handler
=====================================
A test Lambda function that can intentionally produce different AWS failure types.
Deploy this so the AI agent has real incidents to respond to.

Endpoints (via API Gateway or direct Lambda invocation):
  POST /fail/timeout    → Sleeps longer than the Lambda timeout
  POST /fail/oom        → Allocates memory until OOM kill
  POST /fail/error      → Raises an unhandled exception (→ error rate spike)
  POST /fail/throttle   → Writes to DynamoDB rapidly (→ throttle errors)
  POST /fail/dlq        → Sends malformed messages to SQS (→ DLQ fill)
  GET  /health          → Returns 200 OK (for health checks)

Usage:
    aws lambda invoke \\
        --function-name sample-failing-app \\
        --payload '{"path": "/fail/error"}' \\
        response.json
"""

import json
import os
import time


def handler(event, context):
    """Main Lambda handler — route based on path."""
    path = (
        event.get("rawPath")
        or event.get("path")
        or event.get("path", "/health")
    )

    routes = {
        "/health":         _health,
        "/fail/error":     _fail_error,
        "/fail/timeout":   _fail_timeout,
        "/fail/oom":       _fail_oom,
        "/fail/throttle":  _fail_throttle,
        "/fail/dlq":       _fail_dlq,
    }

    handler_fn = routes.get(path, _not_found)
    return handler_fn(event, context)


# ── Route handlers ────────────────────────────────────────────────────────────

def _health(event, context):
    return _ok({"status": "healthy", "function": context.function_name})


def _fail_error(event, context):
    """Raise an unhandled exception — CloudWatch will record an error."""
    count = int(event.get("queryStringParameters", {}).get("count", "1"))
    for i in range(count):
        raise ValueError(
            f"SAMPLE_APP: Intentional error #{i+1} for AI agent testing. "
            "This exception was deliberately thrown to simulate a production error."
        )


def _fail_timeout(event, context):
    """Sleep longer than the function timeout — forces a timeout error."""
    sleep_seconds = int(event.get("queryStringParameters", {}).get("sleep", "60"))
    print(f"SAMPLE_APP: Sleeping {sleep_seconds}s to trigger timeout...")
    time.sleep(sleep_seconds)
    return _ok({"slept_seconds": sleep_seconds})  # Will never reach here if timeout < sleep


def _fail_oom(event, context):
    """Allocate memory rapidly until Lambda is OOM-killed by the runtime."""
    print("SAMPLE_APP: Allocating memory to trigger OOM...")
    blocks = []
    try:
        while True:
            blocks.append("x" * (10 * 1024 * 1024))  # Allocate 10MB chunks
    except MemoryError:
        return _ok({"error": "MemoryError caught — Lambda should have OOM-killed before this"})


def _fail_throttle(event, context):
    """Write to DynamoDB rapidly to trigger throttling on a provisioned table."""
    import boto3
    table_name = os.environ.get("DYNAMODB_TABLE_NAME", "sample-app-table")
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    count = int(event.get("queryStringParameters", {}).get("count", "100"))

    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)

    errors = 0
    for i in range(count):
        try:
            table.put_item(Item={"pk": f"test-{i}", "value": f"data-{i}"})
        except Exception as exc:
            errors += 1
            print(f"SAMPLE_APP: DynamoDB write failed (expected throttle): {exc}")

    return _ok({"writes_attempted": count, "errors": errors})


def _fail_dlq(event, context):
    """Send messages to SQS that will fail processing and end up in the DLQ."""
    import boto3
    queue_url = os.environ.get("SQS_QUEUE_URL", "")
    if not queue_url:
        return _error("SQS_QUEUE_URL not configured")

    sqs = boto3.client("sqs", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    count = int(event.get("queryStringParameters", {}).get("count", "20"))

    for i in range(count):
        sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps({
                "type": "POISON_PILL",
                "sequence": i,
                "data": "INVALID_JSON_THAT_WILL_FAIL_PROCESSING: {{{",
            }),
        )

    return _ok({"messages_sent": count, "note": "These will fail processing and go to DLQ"})


def _not_found(event, context):
    return {
        "statusCode": 404,
        "body": json.dumps({"error": "Not found", "path": event.get("path", "unknown")}),
    }


# ── Response helpers ──────────────────────────────────────────────────────────

def _ok(body: dict) -> dict:
    return {"statusCode": 200, "body": json.dumps(body)}

def _error(message: str) -> dict:
    return {"statusCode": 500, "body": json.dumps({"error": message})}
