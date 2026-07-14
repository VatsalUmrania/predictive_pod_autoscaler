########################################################################
# Phase 1 Infrastructure — Sample Failing App Lambda
# This is the "victim" Lambda. Deploy it, then trigger /fail/* endpoints
# to simulate real AWS failures for the AI agent to respond to.
#
# Resources shared across files:
#   aws_dynamodb_table.sample   → dynamodb.tf
#   aws_sqs_queue.sample_queue  → sqs.tf
#   aws_sqs_queue.sample_dlq    → sqs.tf
########################################################################

# ── IAM Role for Sample App Lambda ───────────────────────────────────────────

resource "aws_iam_role" "sample_app_role" {
  name = "${var.project_name}-sample-app-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
       Action = "sts:AssumeRole" 
       Effect = "Allow" 
       Principal = { Service = "lambda.amazonaws.com" } 
    }]
  })
}

resource "aws_iam_role_policy" "sample_app_policy" {
  name = "${var.project_name}-sample-app-policy"
  role = aws_iam_role.sample_app_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:GetItem"]
        Resource = aws_dynamodb_table.sample.arn
      },
      {
        Effect = "Allow"
        Action = ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        # Allow all queues in this account/region — the agent may create new queues as fixes
        Resource = "arn:aws:sqs:${var.aws_region}:*:${var.project_name}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
        Resource = "*"
      },
    ]
  })
}

# ── Sample App Lambda ─────────────────────────────────────────────────────────

data "archive_file" "sample_app_zip" {
  type        = "zip"
  source_file = "${path.root}/../../src/aws/sample_app/handler.py"
  output_path = "${path.root}/../../sample_app.zip"
}

resource "aws_lambda_function" "sample_app" {
  filename         = data.archive_file.sample_app_zip.output_path
  source_code_hash = data.archive_file.sample_app_zip.output_base64sha256
  function_name    = "${var.project_name}-sample-app"
  role             = aws_iam_role.sample_app_role.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  timeout          = 5     # Short — easy to trigger timeout errors
  memory_size      = 128   # Small — easier to trigger OOM

  tracing_config { mode = "Active" }  # Enable X-Ray

  environment {
    variables = {
      # Region — MUST match the deployment region.
      # Without this, boto3 falls back to us-east-1 and fails to find resources.
      DEFAULT_REGION      = var.aws_region

      # DynamoDB table for the /fail/throttle endpoint
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.sample.name

      # Main SQS queue — /fail/dlq sends messages here;
      # messages that fail 3x automatically flow to sample_dlq (via redrive policy)
      SQS_QUEUE_URL       = aws_sqs_queue.sample_queue.url

      # DLQ URL — available as context if Lambda needs to reference it directly
      DLQ_URL             = aws_sqs_queue.sample_dlq.url
    }
  }
}

resource "aws_cloudwatch_log_group" "sample_app_logs" {
  name              = "/aws/lambda/${aws_lambda_function.sample_app.function_name}"
  retention_in_days = 7
}
