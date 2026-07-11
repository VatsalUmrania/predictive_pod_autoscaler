########################################################################
# Log Monitor — Phase 2 Enhancement
#
# Creates a dedicated Lambda + CloudWatch Logs Subscription Filters.
# Every error line in any monitored log group triggers this Lambda,
# which calls LLM to reason about it and posts to Slack.
#
# This catches errors that bypass CloudWatch Alarms:
#   - try/except'd errors logged via print()
#   - AccessDeniedException printed but not raised
#   - QueueDoesNotExist caught and printed
#   - Any structured log line containing ERROR/Exception
########################################################################

# ── IAM Role for Log Monitor Lambda ──────────────────────────────────────────

resource "aws_iam_role" "log_monitor_role" {
  name = "${var.project_name}-log-monitor-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ 
        Action = "sts:AssumeRole" 
        Effect = "Allow" 
        Principal = { Service = "lambda.amazonaws.com" } 
      }]
  })
}

resource "aws_iam_role_policy" "log_monitor_policy" {
  name = "${var.project_name}-log-monitor-policy"
  role = aws_iam_role.log_monitor_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Write its own logs
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      # Invoke Bedrock models for log analysis
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "*"
      },
    ]
  })
}

# ── Log Monitor Lambda ────────────────────────────────────────────────────────

resource "aws_lambda_function" "log_monitor" {
  filename         = data.archive_file.agent_zip.output_path
  source_code_hash = data.archive_file.agent_zip.output_base64sha256
  function_name    = "${var.project_name}-log-monitor"
  role             = aws_iam_role.log_monitor_role.arn
  handler          = "aws.log_monitor.handler"   # Different entry point
  runtime          = "python3.12"
  timeout          = 60     # 1 minute is enough — no verify wait loop
  memory_size      = 256

  environment {
    variables = {
      AWS_BEARER_TOKEN_BEDROCK = var.aws_bearer_token_bedrock
      AGENT_BEDROCK_MODEL      = var.agent_bedrock_model
      SLACK_WEBHOOK_URL        = var.slack_webhook_url
      DEFAULT_REGION           = var.aws_region
    }
  }
}

resource "aws_cloudwatch_log_group" "log_monitor_logs" {
  name              = "/aws/lambda/${aws_lambda_function.log_monitor.function_name}"
  retention_in_days = 7
}

# ── Grant CloudWatch Logs permission to invoke the monitor Lambda ─────────────

resource "aws_lambda_permission" "allow_cloudwatch_logs_sample_app" {
  statement_id  = "AllowCWLogsInvokeSampleApp"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.log_monitor.function_name
  principal     = "logs.${var.aws_region}.amazonaws.com"
  source_arn    = "${aws_cloudwatch_log_group.sample_app_logs.arn}:*"
}

# ── Subscription Filter: Sample App log group ─────────────────────────────────
# Triggers the log monitor Lambda whenever a line matches the error pattern.
# Pattern: any line containing ERROR, Exception, error, CRITICAL, AccessDenied,
#          Task timed out, OOM, or QueueDoesNotExist

resource "aws_cloudwatch_log_subscription_filter" "sample_app_errors" {
  name            = "${var.project_name}-sample-app-error-monitor"
  log_group_name  = aws_cloudwatch_log_group.sample_app_logs.name
  filter_pattern  = "?ERROR ?Exception ?error ?CRITICAL ?AccessDenied ?QueueDoesNotExist ?OOM ?WARN"
  destination_arn = aws_lambda_function.log_monitor.arn

  depends_on = [aws_lambda_permission.allow_cloudwatch_logs_sample_app]
}

# ── Optional: Also monitor the AI Agent's own log group ──────────────────────
# (useful to catch agent errors too)

resource "aws_lambda_permission" "allow_cloudwatch_logs_agent" {
  statement_id  = "AllowCWLogsInvokeAgent"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.log_monitor.function_name
  principal     = "logs.${var.aws_region}.amazonaws.com"
  source_arn    = "${aws_cloudwatch_log_group.agent_logs.arn}:*"
}

resource "aws_cloudwatch_log_subscription_filter" "agent_errors" {
  name            = "${var.project_name}-agent-error-monitor"
  log_group_name  = aws_cloudwatch_log_group.agent_logs.name
  filter_pattern  = "?ERROR ?Exception ?CRITICAL"
  destination_arn = aws_lambda_function.log_monitor.arn

  depends_on = [aws_lambda_permission.allow_cloudwatch_logs_agent]
}
