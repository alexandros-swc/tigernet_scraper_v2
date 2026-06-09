"""Raw response storage for replayable scraping output."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from urllib.parse import urlparse


class RawStore:
    """Store raw endpoint payloads either locally or in S3."""

    def __init__(self, root: str = "output/raw"):
        self.root = root

    def put_json(self, key: str, payload: dict) -> str:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        compressed = gzip.compress(data)

        if self.root.startswith("s3://"):
            return self._put_s3(key, compressed)
        return self._put_local(key, compressed)

    def _put_local(self, key: str, compressed: bytes) -> str:
        root = Path(self.root)
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(compressed)
        return str(path)

    def _put_s3(self, key: str, compressed: bytes) -> str:
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError(
                "boto3 is required for s3:// raw storage. "
                "Install dependencies with: pip install -r requirements.txt"
            ) from exc

        parsed = urlparse(self.root)
        bucket = parsed.netloc
        prefix = parsed.path.strip("/")
        object_key = f"{prefix}/{key}" if prefix else key
        boto3.client("s3").put_object(
            Bucket=bucket,
            Key=object_key,
            Body=compressed,
            ContentType="application/json",
            ContentEncoding="gzip",
        )
        return f"s3://{bucket}/{object_key}"

