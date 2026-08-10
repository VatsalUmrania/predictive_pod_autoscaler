variable "aws_region" {
  description = "AWS region to deploy to"
  type        = string
  default     = "us-east-1"
}

variable "aws_bearer_token_bedrock" {
  description = "AWS Bearer Token Bedrock"
  type        = string
  sensitive   = true
}

variable "slack_webhook_url" {
  description = "Slack Incoming Webhook URL for notifications"
  type        = string
  sensitive   = true
  default     = ""
}

variable "slack_signing_secret" {
  description = "Slack app Signing Secret (Basic Information → App Credentials) — used to verify interactive button callbacks"
  type        = string
  sensitive   = true
  default     = ""
}

variable "agent_bedrock_model" {
  description = "Bedrock model ID"
  type        = string
  default     = "zai.glm-5"
}

variable "agent_confidence_threshold" {
  description = "Min confidence for autonomous action (0.0-1.0)"
  type        = number
  default     = 0.85
}

variable "agent_dry_run" {
  description = "If true, agent logs actions but does not modify AWS resources"
  type        = bool
  default     = false
}

variable "agent_verify_wait_seconds" {
  description = "Seconds to wait before post-remediation CloudWatch check"
  type        = number
  default     = 120
}

variable "lambda_error_threshold" {
  description = "Lambda error rate threshold that triggers the agent (0.0-1.0)"
  type        = number
  default     = 0.05
}

variable "project_name" {
  description = "Project name prefix for all resources"
  type        = string
  default     = "nexus-ai-agent"
}
