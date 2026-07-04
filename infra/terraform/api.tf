########################################################################
# Phase 1 Infrastructure — API Gateway
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
