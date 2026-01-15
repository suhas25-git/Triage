import json
from typing import Any, Dict

import boto3


async def s3_put_json(bucket: str, key: str, data: Dict[str, Any]) -> None:
    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


async def s3_put_text(bucket: str, key: str, text: str) -> None:
    s3 = boto3.client("s3")
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=text.encode("utf-8"),
        ContentType="text/markdown",
    )
