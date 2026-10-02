"""
AWS Tool Input Schemas
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AWSGetMetricDataSchema(BaseModel):
    namespace: str = Field(default="AWS/Lambda", description="CloudWatch metric namespace")
    metric_name: str = Field(default="Errors", description="Metric name to query (Errors, Invocations, Duration)")
    dimension_name: str = Field(default="FunctionName", description="Dimension name")
    dimension_value: str = Field(..., description="Target resource dimension value (e.g. Lambda function name)")
    period_seconds: int = Field(default=300, description="Metric aggregation period in seconds")
    stat: str = Field(default="Sum", description="Metric statistic: Sum, Average, Maximum, p99")
    region: str | None = Field(default=None, description="AWS Region")


class AWSGetLogEventsSchema(BaseModel):
    log_group_name: str = Field(..., description="CloudWatch log group name")
    filter_pattern: str | None = Field(default="ERROR", description="Search pattern in log group")
    limit: int = Field(default=20, description="Max log events to return")
    region: str | None = Field(default=None, description="AWS Region")


class AWSUpdateLambdaMemorySchema(BaseModel):
    function_name: str = Field(..., description="Lambda function name")
    memory_mb: int = Field(..., description="Target memory in MB (128 to 10240)")
    region: str | None = Field(default=None, description="AWS Region")


class AWSUpdateLambdaTimeoutSchema(BaseModel):
    function_name: str = Field(..., description="Lambda function name")
    timeout_seconds: int = Field(..., description="Target timeout in seconds (1 to 900)")
    region: str | None = Field(default=None, description="AWS Region")


class AWSRollbackLambdaAliasSchema(BaseModel):
    function_name: str = Field(..., description="Lambda function name")
    alias_name: str = Field(default="live", description="Alias to rollback (e.g. live, prod)")
    target_version: str | None = Field(default=None, description="Target version to point alias to")
    region: str | None = Field(default=None, description="AWS Region")


class AWSReplaySQSDLQSchema(BaseModel):
    dlq_name: str = Field(..., description="Dead-letter queue name")
    max_messages: int = Field(default=50, description="Max messages to replay back to source queue")
    region: str | None = Field(default=None, description="AWS Region")
