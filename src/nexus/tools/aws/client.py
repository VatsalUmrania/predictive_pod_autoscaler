"""
AWS Boto3 Client Factory
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def get_boto3_client(service_name: str, region: str | None = None):
    import boto3
    target_region = region or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    return boto3.client(service_name, region_name=target_region)
