"""Local-disk file storage plus filename and type validation."""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
import uuid
from pathlib import Path
from typing import IO

CHUNK_SIZE = 1024 * 1024
_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")


class FileRejected(ValueError):
    """The upload was refused (bad name, blocked type or too large)."""


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


class FileStorage:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, storage_key: str) -> Path:
        return self.root / storage_key

    def save(self, source: IO[bytes], max_bytes: int) -> tuple[str, int, str]:
        """Stream `source` to disk. Returns (storage_key, size, sha256).

        The file is written under a random key and removed again if the stream is
        larger than `max_bytes`, so a rejected upload never stays on disk.
        """
        storage_key = uuid.uuid4().hex
        target = self.path_for(storage_key)
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
        return storage_key, size, digest.hexdigest()

    def delete(self, storage_key: str) -> None:
        self.path_for(storage_key).unlink(missing_ok=True)
