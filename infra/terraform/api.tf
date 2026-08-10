########################################################################
# Phase 1 Infrastructure — API Gateway (Sample App)
# Creates an HTTP API that sits in front of the sample failing app Lambda.
# Use this to trigger failures via HTTP requests.
########################################################################

resource "aws_apigatewayv2_api" "sample_api" {
  name          = "${var.project_name}-sample-api"
  protocol_type = "HTTP"
  description   = "API for the sample failing app — used to trigger AI agent incidents"
}

resource "aws_apigatewayv2_integration" "sample_lambda" {
  api_id                 = aws_apigatewayv2_api.sample_api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.sample_app.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "catch_all" {
  api_id    = aws_apigatewayv2_api.sample_api.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.sample_lambda.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.sample_api.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access_logs.arn
    format = jsonencode({
      requestId      = "$context.requestId"
      sourceIp       = "$context.identity.sourceIp"
      httpMethod     = "$context.httpMethod"
      routeKey       = "$context.routeKey"
      path           = "$context.path"
      status         = "$context.status"
      responseLength = "$context.responseLength"
      requestTime    = "$context.requestTime"
      integrationError = "$context.integrationErrorMessage"
    })
  }
}

resource "aws_cloudwatch_log_group" "api_access_logs" {
  name              = "/aws/apigateway/${var.project_name}-sample-api"
  retention_in_days = 7
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.sample_app.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.sample_api.execution_arn}/*/*"
}

########################################################################
# Agent API — Lambda Function URL
# Exposes server.py (FastAPI + Mangum) as a direct HTTPS endpoint.
# Handles: /approve/{id}, /reject/{id}, /health, /status, /webhook/*
#
# Lambda Function URL is free, instant, and needs no API Gateway setup.
# The URL looks like: https://<id>.lambda-url.<region>.on.aws
########################################################################

resource "aws_lambda_function" "agent_api" {
  filename         = data.archive_file.agent_zip.output_path
  source_code_hash = data.archive_file.agent_zip.output_base64sha256
  function_name    = "${var.project_name}-api"
  role             = aws_iam_role.agent_role.arn   # Reuse the agent role — same permissions
  handler          = "aws.api.server.handler"       # Mangum adapter in server.py
  runtime          = "python3.12"
  timeout          = 30     # API calls should be fast; approve runs executor synchronously
  memory_size      = 256

  environment {
    variables = {
      SLACK_WEBHOOK_URL        = var.slack_webhook_url
      SLACK_SIGNING_SECRET     = var.slack_signing_secret
      DEFAULT_REGION           = var.aws_region
      AWS_BEARER_TOKEN_BEDROCK = var.aws_bearer_token_bedrock
      AGENT_BEDROCK_MODEL      = var.agent_bedrock_model
      AGENT_DRY_RUN            = tostring(var.agent_dry_run)
      INCIDENT_TABLE_NAME      = aws_dynamodb_table.incidents.name
      APPROVALS_TABLE_NAME     = aws_dynamodb_table.approvals.name
    }
  }
}

# Lambda Function URL — gives the FastAPI server a public HTTPS address
# authorization_type = "NONE" means anyone who knows the URL can call it.
# The approval_id UUID acts as the authorization token (unguessable 36-char string).
resource "aws_lambda_function_url" "agent_api" {
  function_name      = aws_lambda_function.agent_api.function_name
  authorization_type = "NONE"

  cors {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST"]
    allow_headers = ["content-type"]
  }
}

# REQUIRED: even with authorization_type=NONE, AWS needs an explicit resource policy
# granting lambda:InvokeFunctionUrl from * — without this you get 403 Forbidden.
resource "aws_lambda_permission" "agent_api_public" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.agent_api.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_cloudwatch_log_group" "agent_api_logs" {
  name              = "/aws/lambda/${aws_lambda_function.agent_api.function_name}"
  retention_in_days = 7
}

########################################################################
# Inject SLACK_INTERACTIVE_URL after both Lambda + Function URL exist.
# This breaks the circular dependency: Lambda needs its own URL, but the
# URL resource needs the Lambda — so we set the env var post-creation.
#
# All other env vars are re-stated here explicitly so the update-function-
# configuration call is idempotent and doesn't wipe existing variables.
########################################################################
resource "null_resource" "agent_api_set_interactive_url" {
  # Re-run whenever the function URL changes (or any other env var changes).
  triggers = {
    function_url             = aws_lambda_function_url.agent_api.function_url
    slack_webhook_url        = var.slack_webhook_url
    slack_signing_secret     = var.slack_signing_secret
    aws_bearer_token_bedrock = var.aws_bearer_token_bedrock
    agent_bedrock_model      = var.agent_bedrock_model
    agent_dry_run            = tostring(var.agent_dry_run)
    incident_table_name      = aws_dynamodb_table.incidents.name
    approvals_table_name     = aws_dynamodb_table.approvals.name
  }

  provisioner "local-exec" {
    # Build the env string in AWS CLI format: {KEY=VALUE,KEY=VALUE,...}
    # ConvertTo-Json produces KEY:VALUE (JSON), which the CLI rejects.
    interpreter = ["PowerShell", "-Command"]
    command     = <<-EOT
      $env = 'Variables={' +
        'SLACK_WEBHOOK_URL=${var.slack_webhook_url},' +
        'SLACK_SIGNING_SECRET=${var.slack_signing_secret},' +
        'SLACK_INTERACTIVE_URL=${aws_lambda_function_url.agent_api.function_url},' +
        'DEFAULT_REGION=${var.aws_region},' +
        'AWS_BEARER_TOKEN_BEDROCK=${var.aws_bearer_token_bedrock},' +
        'AGENT_BEDROCK_MODEL=${var.agent_bedrock_model},' +
        'AGENT_DRY_RUN=${tostring(var.agent_dry_run)},' +
        'INCIDENT_TABLE_NAME=${aws_dynamodb_table.incidents.name},' +
        'APPROVALS_TABLE_NAME=${aws_dynamodb_table.approvals.name}' +
        '}'
      aws lambda update-function-configuration `
        --function-name ${aws_lambda_function.agent_api.function_name} `
        --region ${var.aws_region} `
        --environment $env
    EOT
  }

  depends_on = [
    aws_lambda_function.agent_api,
    aws_lambda_function_url.agent_api,
  ]
}
