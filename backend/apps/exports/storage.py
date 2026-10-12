"""Where export files live. S3 (versioned bucket, server-side encrypted) in production, local disk for development."""
import os
from abc import ABC, abstractmethod
from pathlib import Path

from django.conf import settings
from django.core import signing


class ExportStorage(ABC):
    @abstractmethod
    def save(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def read(self, key: str) -> bytes: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def link(self, export, ttl: int) -> str: ...


class LocalExportStorage(ExportStorage):
    def _path(self, key):
        p = (Path(settings.MEDIA_ROOT) / "exports" / key).resolve()
        if not str(p).startswith(str((Path(settings.MEDIA_ROOT) / "exports").resolve())):
            raise ValueError("bad key")
        return p

    def save(self, key, data):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def read(self, key):
        return self._path(key).read_bytes()

    def delete(self, key):
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass

    def link(self, export, ttl):
        token = signing.TimestampSigner(salt="tenant-export").sign(f"{export.tenant_id}:{export.pk}")
        return f"/api/exports/download/?token={token}"


class S3ExportStorage(ExportStorage):
    def __init__(self):
        import boto3

        self.bucket = settings.EXPORT_S3_BUCKET
        self.client = boto3.client("s3", region_name=settings.AWS_S3_REGION_NAME)

    def save(self, key, data):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ServerSideEncryption="AES256", ContentType="application/zip")

    def read(self, key):
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)  # versioned bucket: this leaves a delete marker, older versions remain

    def link(self, export, ttl):
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": export.file_key, "ResponseContentDisposition": 'attachment; filename="my-data.zip"'}, ExpiresIn=ttl)


def get_storage() -> ExportStorage:
    return S3ExportStorage() if settings.EXPORT_S3_BUCKET else LocalExportStorage()


def read_token(token, ttl):
    """(tenant_id, export_id) from a signed, unexpired link token, else raise signing.BadSignature."""
    value = signing.TimestampSigner(salt="tenant-export").unsign(token, max_age=ttl)
    tenant_id, export_id = value.split(":")
    return tenant_id, export_id
