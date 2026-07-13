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
    Statement = [{ 
      Action = "sts:AssumeRole" 
      Effect = "Allow" 
      Principal = { Service = "lambda.amazonaws.com" } 
    }]
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
      # SQS — read + replay + create (executor needs CreateQueue for missing-queue fixes)
      {
        Effect = "Allow"
        Action = [
          "sqs:GetQueueAttributes", "sqs:GetQueueUrl",    "sqs:ListQueues",
          "sqs:ReceiveMessage",     "sqs:SendMessage",    "sqs:DeleteMessage",
          "sqs:CreateQueue",        "sqs:SetQueueAttributes", "sqs:PurgeQueue",
        ]
        Resource = "*"
      },
      # DynamoDB — read + write (incident memory + approvals table)
      {
        Effect = "Allow"
        Action = [
          "dynamodb:ListTables", "dynamodb:DescribeTable",
          "dynamodb:PutItem",    "dynamodb:GetItem",
          "dynamodb:UpdateItem", "dynamodb:Query",
          "dynamodb:UpdateTable",
        ]
        Resource = "*"
      },
      # IAM — safe writes for executor (add policies to existing roles only)
      {
        Effect = "Allow"
        Action = [
          "iam:PutRolePolicy",
          "iam:AttachRolePolicy",
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies",
        ]
        Resource = "arn:aws:iam::*:role/*"
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
        Resource = "*"
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
data "archive_file" "agent_zip" {
  type        = "zip"
  source_dir  = "${path.root}/../../lambda_package"
  output_path = "${path.root}/../../agent.zip"
}

resource "aws_lambda_function" "agent" {
  filename         = data.archive_file.agent_zip.output_path
  source_code_hash = data.archive_file.agent_zip.output_base64sha256
  function_name    = "${var.project_name}-function"
  role             = aws_iam_role.agent_role.arn
  handler          = "aws.handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 600    # 10 min — allows 120s verify wait + retries
  memory_size      = 512

  environment {
    variables = {
      AWS_BEARER_TOKEN_BEDROCK   = var.aws_bearer_token_bedrock
      SLACK_WEBHOOK_URL          = var.slack_webhook_url
      DEFAULT_REGION             = var.aws_region
      AGENT_BEDROCK_MODEL        = var.agent_bedrock_model
      AGENT_CONFIDENCE_THRESHOLD = tostring(var.agent_confidence_threshold)
      AGENT_DRY_RUN              = tostring(var.agent_dry_run)
      AGENT_VERIFY_WAIT_SECONDS  = tostring(var.agent_verify_wait_seconds)
      AGENT_ERROR_THRESHOLD      = tostring(var.lambda_error_threshold)
      INCIDENT_TABLE_NAME        = aws_dynamodb_table.incidents.name
    }
  }
}
