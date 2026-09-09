"""Verified local files and bounded, atomic archive downloads."""

import hashlib
import tempfile
from pathlib import Path

import httpx

from specfhir.models import Error


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(path: Path, url: str, expected: str | None, max_bytes: int) -> str:
    """Reuse verified files; publish downloads only after size and checksum checks."""
    if not path.exists():
        if not url.startswith("https://"):
            raise Error("Archive downloads require HTTPS")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                with httpx.stream("GET", url, follow_redirects=True, timeout=90) as response:
                    response.raise_for_status()
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise Error(f"Download too large: {path.name}")
                        stream.write(chunk)
                stream.flush()
                actual = checksum(temporary)
                if expected and expected != actual:
                    raise Error(f"Checksum mismatch for {path.name}")
                temporary.replace(path)
                return actual
            finally:
                temporary.unlink(missing_ok=True)
    if path.stat().st_size > max_bytes:
        raise Error(f"Archive too large: {path.name}")
    actual = checksum(path)
    if expected and expected != actual:
        raise Error(f"Checksum mismatch for {path.name}; cache was not modified")
    return actual
