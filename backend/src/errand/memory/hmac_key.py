"""The per-install secret behind the surface-form hashes (gate 2 decision 9).

It lives in `data/memory_hmac.key`, outside agent.sqlite, created 0600 on first use.
The hashes stop re-display of a forgotten value; they do not stop someone who holds
both the key file and the database, and the README says so.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

KEY_BYTES = 32


def load_or_create(path: Path) -> bytes:
    """Read the key, or create it atomically with mode 0600 if there is none."""
    try:
        return _read(path)
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a private temp file and hard-link it into place: the link is atomic and fails
    # if the key exists, so a reader never sees a half-written key.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(os.urandom(KEY_BYTES).hex().encode("ascii"))
    try:
        os.link(tmp, path)
    except FileExistsError:
        pass  # another process won the race; use its key
    finally:
        tmp.unlink(missing_ok=True)
    return _read(path)


def _read(path: Path) -> bytes:
    key = bytes.fromhex(path.read_text("ascii").strip())
    if len(key) != KEY_BYTES:
        raise ValueError(f"{path}: expected a {KEY_BYTES}-byte key")
    return key


def fixed_key(label: str) -> bytes:
    """A deterministic key for the seed generator, so two builds hash alike."""
    return hashlib.sha256(f"errand-seed-key:{label}".encode()).digest()


def hmac_hex(key: bytes, normalised: str) -> str:
    return hmac.digest(key, normalised.encode("utf-8"), "sha256").hex()
