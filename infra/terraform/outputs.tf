output "agent_function_name" {
  value = aws_lambda_function.agent.function_name
}

output "log_monitor_function_name" {
  description = "Log Monitor Lambda — triggered by CloudWatch Logs subscription filters"
  value       = aws_lambda_function.log_monitor.function_name
}

output "sample_app_function_name" {
  value = aws_lambda_function.sample_app.function_name
}

output "sample_api_endpoint" {
  description = "HTTP API URL — call /fail/* endpoints to trigger incidents"
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "sample_queue_url" {
  value = aws_sqs_queue.sample_queue.url
}

output "sample_dlq_url" {
  value = aws_sqs_queue.sample_dlq.url
}

output "dynamodb_sample_table" {
  value = aws_dynamodb_table.sample.name
}

output "incident_memory_table" {
  value = aws_dynamodb_table.incidents.name
}

output "test_commands" {
  description = "Copy-paste commands to trigger test failures"
  value = {
    # Direct Lambda invocation
    trigger_error = "aws lambda invoke --function-name ${aws_lambda_function.sample_app.function_name} --cli-binary-format raw-in-base64-out --payload '{\"path\":\"/fail/error\"}' response.json"
    trigger_oom   = "aws lambda invoke --function-name ${aws_lambda_function.sample_app.function_name} --cli-binary-format raw-in-base64-out --payload '{\"path\":\"/fail/oom\"}' response.json"
    trigger_dlq   = "aws lambda invoke --function-name ${aws_lambda_function.sample_app.function_name} --cli-binary-format raw-in-base64-out --payload '{\"path\":\"/fail/dlq\",\"queryStringParameters\":{\"count\":\"20\"}}' response.json"
    trigger_throttle = "aws lambda invoke --function-name ${aws_lambda_function.sample_app.function_name} --cli-binary-format raw-in-base64-out --payload '{\"path\":\"/fail/throttle\",\"queryStringParameters\":{\"count\":\"200\"}}' response.json"

    # HTTP API endpoints (Phase 1)
    http_error   = "curl -X POST ${aws_apigatewayv2_stage.default.invoke_url}/fail/error"
    http_timeout = "curl -X POST ${aws_apigatewayv2_stage.default.invoke_url}/fail/timeout"
    http_health  = "curl ${aws_apigatewayv2_stage.default.invoke_url}/health"

    # Direct agent test (no alarm needed)
    test_agent = "aws lambda invoke --function-name ${aws_lambda_function.agent.function_name} --cli-binary-format raw-in-base64-out --payload '{\"resource_name\":\"${aws_lambda_function.sample_app.function_name}\",\"signal_type\":\"lambda_error_rate_high\",\"raw_metrics\":{}}' agent_response.json"
  }
}
