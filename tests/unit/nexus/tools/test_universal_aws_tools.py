"""
Unit tests for Universal AWS Provider Tool Bridge.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ParamValidationError

from nexus.engine.tools.aws.universal import (
    AWSInspectInput,
    AWSMutateInput,
    AWSQueryTelemetryInput,
    execute_aws_inspect,
    execute_aws_mutate,
    execute_aws_query_telemetry,
)


def test_aws_inspect_denies_unauthorized_operations():
    # 1. Denied prefix (e.g. put_ or delete_)
    bad_op = AWSInspectInput(service="lambda", operation="delete_function")
    res = json.loads(execute_aws_inspect(bad_op))
    assert "error" in res
    assert "not a permitted read-only inspection" in res["error"]

    # 2. Denied sensitive read (e.g. get_secret_value)
    bad_secret = AWSInspectInput(service="secretsmanager", operation="get_secret_value")
    res2 = json.loads(execute_aws_inspect(bad_secret))
    assert "error" in res2


def test_aws_inspect_allowed_read_and_error_handling():
    with patch("boto3.client") as mock_boto:
        mock_client = MagicMock()
        mock_boto.return_value = mock_client
        mock_client.describe_db_instances.return_value = {
            "DBInstances": [{"DBInstanceIdentifier": "prod-db", "Status": "available"}],
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

        # Successful inspect
        inp = AWSInspectInput(
            service="rds",
            operation="describe_db_instances",
            parameters={"DBInstanceIdentifier": "prod-db"},
        )
        res = json.loads(execute_aws_inspect(inp))
        assert "DBInstances" in res
        assert "ResponseMetadata" not in res

        # Error self-correction hint on ParamValidationError
        mock_client.describe_db_instances.side_effect = ParamValidationError(
            report="Invalid type for DBInstanceIdentifier"
        )
        err_res = json.loads(execute_aws_inspect(inp))
        assert "error" in err_res
        assert "self_correction_hint" in err_res


def test_aws_query_telemetry():
    with patch("boto3.client") as mock_boto:
        mock_cw = MagicMock()
        mock_boto.return_value = mock_cw
        mock_cw.get_metric_data.return_value = {
            "MetricDataResults": [{"Id": "m1", "Values": [42.0]}],
            "ResponseMetadata": {},
        }

        inp = AWSQueryTelemetryInput(
            telemetry_type="cloudwatch_metrics",
            parameters={"MetricDataQueries": [{"Id": "m1"}]},
        )
        res = json.loads(execute_aws_query_telemetry(inp))
        assert "MetricDataResults" in res


def test_aws_mutate_denies_security_services():
    inp = AWSMutateInput(
        service="iam",
        operation="delete_role",
        parameters={"RoleName": "admin"},
        target_arn="arn:aws:iam::123456789012:role/admin",
    )
    with pytest.raises(PermissionError, match="security/account service"):
        execute_aws_mutate(inp)
