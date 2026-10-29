"""Storage backends: local signed URLs and S3 presigning."""

from __future__ import annotations

import hashlib
import io
from urllib.parse import parse_qs, urlsplit

import pytest

from server.storage import LocalStorage, S3Storage, FileRejected


def parts(url: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urlsplit(url)
    return parsed.path, parse_qs(parsed.query)


def test_local_signed_upload_and_download(tmp_path):
    storage = LocalStorage(tmp_path / "files", "secret-key")
    key = "a" * 32
    size, sha256 = storage.save_at(key, io.BytesIO(b"hello"), 100)
    assert size == 5
    assert sha256 == hashlib.sha256(b"hello").hexdigest()
    assert storage.head(key) == (True, 5)

    download_path, query = parts(storage.download_url(key))
    assert download_path == f"/api/blob/download/{key}"
    assert storage.verify(
        "download", key, int(query["expires"][0]), query["signature"][0]
    )
    # A download signature cannot be replayed as an upload link.
    assert not storage.verify(
        "upload", key, int(query["expires"][0]), query["signature"][0]
    )
    assert not storage.verify("download", key, int(query["expires"][0]), "0" * 64)


def test_local_links_expire(tmp_path):
    storage = LocalStorage(tmp_path / "files", "secret-key")
    key = "b" * 32
    storage.save_at(key, io.BytesIO(b"x"), 10)
    _, query = parts(storage.download_url(key, ttl=-1))
    assert not storage.verify(
        "download", key, int(query["expires"][0]), query["signature"][0]
    )


def test_local_save_enforces_limit_and_cleans_up(tmp_path):
    storage = LocalStorage(tmp_path / "files", "secret-key")
    with pytest.raises(FileRejected):
        storage.save_at("c" * 32, io.BytesIO(b"toolong"), 3)
    assert list((tmp_path / "files").iterdir()) == []
    assert storage.head("c" * 32) == (False, 0)


class FakeNotFound(Exception):
    response = {"Error": {"Code": "404"}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.presigned: list[tuple[str, dict]] = []

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.presigned.append((operation, {"Params": Params, "ExpiresIn": ExpiresIn}))
        return (
            f"https://storage.example/{Params['Bucket']}/{Params['Key']}"
            f"?op={operation}&expires={ExpiresIn}"
        )

    def upload_fileobj(self, fileobj, bucket, key):
        self.objects[(bucket, key)] = fileobj.read()

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise FakeNotFound()
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def make_s3() -> tuple[S3Storage, FakeS3]:
    client = FakeS3()
    storage = S3Storage(
        "bucket",
        "key-id",
        "secret",
        client=client,
        prefix="drive/files",
    )
    return storage, client


def test_s3_presigned_urls_use_the_prefix():
    storage, client = make_s3()
    upload = storage.upload_url("d" * 32)
    download = storage.download_url("d" * 32, ttl=120)

    assert upload == (
        "https://storage.example/bucket/drive/files/" + "d" * 32
        + "?op=put_object&expires=900"
    )
    assert download.endswith("?op=get_object&expires=120")
    assert [entry[0] for entry in client.presigned] == ["put_object", "get_object"]


def test_s3_save_head_and_delete():
    storage, client = make_s3()
    size, sha256 = storage.save_at("e" * 32, io.BytesIO(b"payload"), 100)
    assert size == 7
    assert sha256 == hashlib.sha256(b"payload").hexdigest()
    assert client.objects[("bucket", "drive/files/" + "e" * 32)] == b"payload"
    assert storage.head("e" * 32) == (True, 7)

    storage.delete("e" * 32)
    assert storage.head("e" * 32) == (False, 0)


def test_s3_save_enforces_limit():
    storage, client = make_s3()
    with pytest.raises(FileRejected):
        storage.save_at("f" * 32, io.BytesIO(b"too much data"), 4)
    assert client.objects == {}
