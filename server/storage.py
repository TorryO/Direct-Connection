"""File storage: local disk for development, S3-compatible buckets for serverless.

Both backends hand out short-lived URLs. With local storage those URLs point
back at this server and are signed with the admin key; with S3 they are
presigned URLs, so bytes never pass through the application.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
import unicodedata
import uuid
from pathlib import Path
from typing import IO, Any

CHUNK_SIZE = 1024 * 1024
URL_TTL_SECONDS = 900

_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")


class FileRejected(ValueError):
    """The upload was refused (bad name, blocked type or too large)."""


class StorageError(RuntimeError):
    """The storage backend could not complete the operation."""


def sanitize_filename(raw: str) -> str:
    """Reduce any client-supplied name to a plain, safe base filename."""
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = _UNSAFE.sub("", unicodedata.normalize("NFC", name)).strip()
    name = name.strip(". ")
    if not name or name in {".", ".."}:
        raise FileRejected("Filename is missing or invalid.")
    if len(name) > 200:
        stem, ext = os.path.splitext(name)
        name = stem[: 200 - len(ext)] + ext
    return name


def extension_of(name: str) -> str:
    return os.path.splitext(name)[1].lower().lstrip(".")


def check_allowed(name: str, blocked: frozenset[str]) -> None:
    ext = extension_of(name)
    if ext and ext in blocked:
        raise FileRejected(
            f"Files of type '.{ext}' are blocked. The agent never runs what it downloads."
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Storage:
    """Interface shared by the local and S3 backends."""

    def save(self, source: IO[bytes], max_bytes: int) -> tuple[str, int, str]:
        raise NotImplementedError

    def save_at(self, key: str, source: IO[bytes], max_bytes: int) -> tuple[int, str]:
        """Store bytes under an already-chosen key. Returns (size, sha256)."""
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def upload_url(self, key: str, ttl: int = URL_TTL_SECONDS) -> str:
        raise NotImplementedError

    def download_url(self, key: str, ttl: int = URL_TTL_SECONDS) -> str:
        raise NotImplementedError

    def head(self, key: str) -> tuple[bool, int]:
        """Return (exists, size_in_bytes)."""
        raise NotImplementedError


class LocalStorage(Storage):
    def __init__(self, root: Path, secret: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.secret = secret.encode("utf-8")

    # -- signing ---------------------------------------------------------

    def _signature(self, kind: str, key: str, expires: int) -> str:
        message = f"{kind}\n{key}\n{expires}".encode("utf-8")
        return hmac.new(self.secret, message, hashlib.sha256).hexdigest()

    def verify(self, kind: str, key: str, expires: int, signature: str) -> bool:
        if expires < int(time.time()):
            return False
        return hmac.compare_digest(signature, self._signature(kind, key, expires))

    def _signed_path(self, kind: str, key: str, ttl: int) -> str:
        expires = int(time.time()) + ttl
        signature = self._signature(kind, key, expires)
        return f"/api/blob/{kind}/{key}?expires={expires}&signature={signature}"

    def upload_url(self, key: str, ttl: int = URL_TTL_SECONDS) -> str:
        return self._signed_path("upload", key, ttl)

    def download_url(self, key: str, ttl: int = URL_TTL_SECONDS) -> str:
        return self._signed_path("download", key, ttl)

    # -- bytes -----------------------------------------------------------

    def path_for(self, key: str) -> Path:
        return self.root / key

    def save(self, source: IO[bytes], max_bytes: int) -> tuple[str, int, str]:
        """Stream `source` to disk. Returns (key, size, sha256)."""
        key = uuid.uuid4().hex
        size, sha256 = self.save_at(key, source, max_bytes)
        return key, size, sha256

    def save_at(self, key: str, source: IO[bytes], max_bytes: int) -> tuple[int, str]:
        target = self.path_for(key)
        tmp = target.with_suffix(".part")
        digest = hashlib.sha256()
        size = 0
        try:
            with tmp.open("wb") as out:
                while True:
                    chunk = source.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise FileRejected(
                            f"File is larger than the {max_bytes} byte limit."
                        )
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise FileRejected("File is empty.")
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        return size, digest.hexdigest()

    def head(self, key: str) -> tuple[bool, int]:
        path = self.path_for(key)
        if not path.is_file():
            return False, 0
        return True, path.stat().st_size

    def delete(self, key: str) -> None:
        self.path_for(key).unlink(missing_ok=True)


class _LimitedReader:
    """Stream wrapper that enforces a byte limit and hashes what it reads."""

    def __init__(self, source: IO[bytes], max_bytes: int) -> None:
        self.source = source
        self.max_bytes = max_bytes
        self.size = 0
        self.digest = hashlib.sha256()
        self._buffer = b""

    def read(self, size: int = CHUNK_SIZE) -> bytes:
        chunk = self._buffer or self.source.read(size)
        self._buffer = b""
        if not chunk:
            return b""
        self.size += len(chunk)
        if self.size > self.max_bytes:
            raise FileRejected(
                f"File is larger than the {self.max_bytes} byte limit."
            )
        self.digest.update(chunk)
        return chunk


class S3Storage(Storage):
    def __init__(
        self,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        *,
        region: str = "auto",
        endpoint_url: str | None = None,
        prefix: str = "",
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is not None:
            self.client = client
        else:
            import boto3

            self.client = boto3.client(
                "s3",
                region_name=region,
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
            )

    def object_key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _presign(self, operation: str, key: str, ttl: int) -> str:
        return self.client.generate_presigned_url(
            operation,
            Params={"Bucket": self.bucket, "Key": self.object_key(key)},
            ExpiresIn=ttl,
        )

    def upload_url(self, key: str, ttl: int = URL_TTL_SECONDS) -> str:
        return self._presign("put_object", key, ttl)

    def download_url(self, key: str, ttl: int = URL_TTL_SECONDS) -> str:
        return self._presign("get_object", key, ttl)

    def save(self, source: IO[bytes], max_bytes: int) -> tuple[str, int, str]:
        key = uuid.uuid4().hex
        return (key, *self.save_at(key, source, max_bytes))

    def save_at(self, key: str, source: IO[bytes], max_bytes: int) -> tuple[int, str]:
        reader = _LimitedReader(source, max_bytes)
        try:
            self.client.upload_fileobj(reader, self.bucket, self.object_key(key))
        except BaseException:
            self.delete(key)
            raise
        if reader.size == 0:
            self.delete(key)
            raise FileRejected("File is empty.")
        return reader.size, reader.digest.hexdigest()

    def head(self, key: str) -> tuple[bool, int]:
        try:
            response = self.client.head_object(
                Bucket=self.bucket, Key=self.object_key(key)
            )
        except Exception as exc:  # boto3 raises ClientError; keep this backend narrow
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False, 0
            raise StorageError(str(exc)) from exc
        return True, int(response.get("ContentLength", 0))

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self.object_key(key))
        except Exception:
            # Deleting a missing object is not an error worth failing an action for.
            pass


def open_storage(settings: Any) -> Storage:
    """Pick the backend from settings."""
    if settings.s3_bucket:
        return S3Storage(
            settings.s3_bucket,
            settings.s3_access_key_id or "",
            settings.s3_secret_access_key or "",
            region=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
            prefix=settings.s3_prefix,
        )
    return LocalStorage(settings.files_dir, settings.admin_key)
