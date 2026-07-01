########################################################################
# Phase 2 + 3 Infrastructure — CloudWatch Alarms + EventBridge
# Alarms detect failures. EventBridge triggers the AI agent Lambda.
########################################################################

# ── EventBridge rule — triggers AI agent on ANY alarm ────────────────────────
resource "aws_cloudwatch_event_rule" "alarm_trigger" {
  name        = "${var.project_name}-alarm-trigger"
  description = "Trigger NEXUS AI agent when any CloudWatch alarm fires"

  event_pattern = jsonencode({
    source        = ["aws.cloudwatch"]
    "detail-type" = ["CloudWatch Alarm State Change"]
    detail        = { state = { value = ["ALARM"] } }
  })
}

resource "aws_cloudwatch_event_target" "agent_target" {
  rule      = aws_cloudwatch_event_rule.alarm_trigger.name
  target_id = "nexus-agent-lambda"
  arn       = aws_lambda_function.agent.arn
}

resource "aws_lambda_permission" "eventbridge_invoke" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.agent.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.alarm_trigger.arn
}

# ── CloudWatch Alarms (Phase 2 monitoring triggers) ───────────────────────────

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${var.project_name}-sample-app-lambda-errors"
  alarm_description   = "Lambda error rate is high — trigger NEXUS AI agent"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  treat_missing_data  = "notBreaching"
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 60
  statistic           = "Sum"
  dimensions          = { FunctionName = aws_lambda_function.sample_app.function_name }
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  alarm_name          = "${var.project_name}-lambda-throttles"
  alarm_description   = "Lambda throttling detected"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 5
  treat_missing_data  = "notBreaching"
  metric_name         = "Throttles"
  namespace           = "AWS/Lambda"
  period              = 60
  statistic           = "Sum"
  dimensions          = { FunctionName = aws_lambda_function.sample_app.function_name }
}

resource "aws_cloudwatch_metric_alarm" "sqs_dlq_depth" {
  alarm_name          = "${var.project_name}-sqs-dlq-depth"
  alarm_description   = "DLQ has messages — trigger NEXUS AI agent"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  treat_missing_data  = "notBreaching"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  dimensions          = { QueueName = aws_sqs_queue.sample_dlq.name }
}

resource "aws_cloudwatch_metric_alarm" "dynamo_write_throttles" {
  alarm_name          = "${var.project_name}-dynamo-write-throttle"
  alarm_description   = "DynamoDB write throttling"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  treat_missing_data  = "notBreaching"
  metric_name         = "WriteThrottleEvents"
  namespace           = "AWS/DynamoDB"
  period              = 60
  statistic           = "Sum"
  dimensions          = { TableName = aws_dynamodb_table.sample.name }
}

resource "aws_cloudwatch_metric_alarm" "apigw_5xx" {
  alarm_name          = "${var.project_name}-apigw-5xx"
  alarm_description   = "API Gateway 5XX errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 5
  treat_missing_data  = "notBreaching"
  metric_name         = "5XXError"
  namespace           = "AWS/ApiGateway"
  period              = 60
  statistic           = "Sum"
  dimensions          = { ApiName = aws_apigatewayv2_api.sample_api.name }
}

# ── CloudWatch Log Group for the AI Agent ─────────────────────────────────────
resource "aws_cloudwatch_log_group" "agent_logs" {
  name              = "/aws/lambda/${aws_lambda_function.agent.function_name}"
  retention_in_days = 30
}
