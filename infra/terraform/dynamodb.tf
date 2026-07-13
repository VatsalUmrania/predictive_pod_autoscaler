########################################################################
# Phase 1 Infrastructure — DynamoDB Tables
# 1. Sample app table (low capacity → easy to throttle for testing)
# 2. Incident memory table (stores AI agent incident history — Phase 9)
########################################################################

# ── Sample app table (intentionally low capacity for throttle testing) ────────

resource "aws_dynamodb_table" "sample" {
  name           = "${var.project_name}-sample-table"
  billing_mode   = "PROVISIONED"
  read_capacity  = 1
  write_capacity = 1   # Very low — trigger throttles with /fail/throttle

  hash_key = "pk"
  attribute {
    name = "pk"
    type = "S"
  }

  tags = { Purpose = "sample-failing-app" }
}

# ── Incident memory table (Phase 9) ──────────────────────────────────────────

resource "aws_dynamodb_table" "incidents" {
  name         = "${var.project_name}-incidents"
  billing_mode = "PAY_PER_REQUEST"   # No throttles on the memory store
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  # GSI for querying by resource name + timestamp
  attribute {
    name = "resource_name"
    type = "S"
  }
  attribute {
    name = "timestamp"
    type = "S"
  }

  global_secondary_index {
    name               = "resource-timestamp-index"
    hash_key           = "resource_name"
    range_key          = "timestamp"
    projection_type    = "ALL"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true   # Auto-delete old incidents after 90 days
  }

  tags = { Purpose = "ai-agent-memory" }
}

# ── Approvals table (command approval flow) ───────────────────────────────────
# Stores LLM-suggested boto3 commands pending user approval.
# TTL = 24h — unapproved suggestions are auto-deleted.

resource "aws_dynamodb_table" "approvals" {
  name         = "${var.project_name}-approvals"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "approval_id"

  attribute {
    name = "approval_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = { Purpose = "ai-agent-approvals" }
}
