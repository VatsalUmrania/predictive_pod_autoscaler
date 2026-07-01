########################################################################
# Phase 1 Infrastructure — SQS Queues
########################################################################

# ── Dead Letter Queue ─────────────────────────────────────────────────────────
resource "aws_sqs_queue" "sample_dlq" {
  name                      = "${var.project_name}-sample-dlq"
  message_retention_seconds = 86400   # 1 day
  tags = { Purpose = "sample-failing-app-dlq" }
}

# ── Main queue (messages go to DLQ after 3 failures) ─────────────────────────
resource "aws_sqs_queue" "sample_queue" {
  name                       = "${var.project_name}-sample-queue"
  message_retention_seconds  = 3600
  visibility_timeout_seconds = 30

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.sample_dlq.arn
    maxReceiveCount     = 3
  })

  tags = { Purpose = "sample-failing-app" }
}

# ── SNS topic for escalation notifications ────────────────────────────────────
resource "aws_sns_topic" "agent_alerts" {
  name = "${var.project_name}-alerts"
}
