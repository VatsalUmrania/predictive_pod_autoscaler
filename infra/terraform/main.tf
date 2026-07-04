########################################################################
# main.tf — Provider + AI Agent Lambda + IAM
# All other resources are in dedicated .tf files:
#   api.tf         → API Gateway
#   sqs.tf         → SQS + SNS
#   dynamodb.tf    → DynamoDB tables
#   cloudwatch.tf  → Alarms + EventBridge
#   sample_app.tf  → Sample failing Lambda
########################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# ── IAM Role for the AI Agent Lambda ─────────────────────────────────────────

resource "aws_iam_role" "agent_role" {
  name = "${var.project_name}-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole" ,Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "agent_policy" {
  name = "${var.project_name}-policy"
  role = aws_iam_role.agent_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # CloudWatch — read metrics and logs
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricStatistics", "cloudwatch:GetMetricData",
          "cloudwatch:DescribeAlarms",       "cloudwatch:DescribeAlarmHistory",
          "logs:FilterLogEvents",            "logs:GetLogEvents",
          "logs:DescribeLogGroups",
        ]
        Resource = "*"
      },
      # Lambda — read + safe writes (memory, timeout, alias, concurrency)
      {
        Effect = "Allow"
        Action = [
          "lambda:ListFunctions",            "lambda:GetFunctionConfiguration",
          "lambda:ListVersionsByFunction",   "lambda:GetAlias",
          "lambda:ListAliases",              "lambda:UpdateFunctionConfiguration",
          "lambda:UpdateAlias",              "lambda:PutFunctionConcurrency",
        ]
        Resource = "*"
      },
      # SQS — read + replay
      {
        Effect = "Allow"
        Action = [
          "sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:ListQueues",
          "sqs:ReceiveMessage",     "sqs:SendMessage",  "sqs:DeleteMessage",
        ]
        Resource = "*"
      },
      # DynamoDB — read + write (for incident memory table only)
      {
        Effect = "Allow"
        Action = [
          "dynamodb:ListTables", "dynamodb:DescribeTable",
          "dynamodb:PutItem",    "dynamodb:GetItem",
          "dynamodb:Query",      "dynamodb:UpdateTable",
        ]
        Resource = "*"
      },
      # X-Ray — read traces
      {
        Effect   = "Allow"
        Action   = ["xray:GetServiceGraph", "xray:GetTraceSummaries", "xray:BatchGetTraces"]
        Resource = "*"
      },
      # CloudTrail — lookup events
      {
        Effect   = "Allow"
        Action   = ["cloudtrail:LookupEvents"]
        Resource = "*"
      },
      # Amazon Bedrock — invoke models (GLM-5 via converse)
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = "arn:aws:bedrock:${var.aws_region}::foundation-model/google.gemma-3-4b-it"
      },
      # Lambda execution — write logs
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
    ]
  })
}

# ── AI Agent Lambda ───────────────────────────────────────────────────────────

# NOTE: Before `terraform apply`, run the packaging script:
#   scripts/package_lambda.sh

resource "aws_lambda_function" "agent" {
  filename         = "${path.root}/../../agent.zip"
  source_code_hash = filebase64sha256("${path.root}/../../agent.zip")
  function_name    = "${var.project_name}-function"
  role             = aws_iam_role.agent_role.arn
  handler          = "aws.handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 600    # 10 min — allows 120s verify wait + retries
  memory_size      = 512

  environment {
    variables = {
      SLACK_WEBHOOK_URL          = var.slack_webhook_url
      AGENT_CONFIDENCE_THRESHOLD = tostring(var.agent_confidence_threshold)
      AGENT_DRY_RUN              = tostring(var.agent_dry_run)
      AGENT_VERIFY_WAIT_SECONDS  = tostring(var.agent_verify_wait_seconds)
      AGENT_ERROR_THRESHOLD      = tostring(var.lambda_error_threshold)
      INCIDENT_TABLE_NAME        = aws_dynamodb_table.incidents.name
    }
  }
}
