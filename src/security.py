"""Shared authentication and managed-storage path helpers."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import re
from pathlib import Path, PurePosixPath


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 310_000)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def safe_name(value: str) -> str:
    value = value.strip()
    if (not value or value in {".", ".."} or "/" in value or "\\" in value
            or "\0" in value or any(ord(ch) < 32 for ch in value)
            or value.endswith((".", " ")) or any(ch in '<>:"|?*' for ch in value)):
        raise ValueError("Use a name without path separators or reserved characters.")
    if len(value) > 255:
        raise ValueError("Names must be 255 characters or fewer.")
    if value.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError("That name is reserved by Windows.")
    return value


def safe_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("A managed relative path is required.")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError("The path must stay inside FileBox storage.")
    if re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("Absolute paths are not allowed.")
    if len(normalized) > 1024:
        raise ValueError("The path is too long.")
    return "/".join(path.parts)


def safe_storage_path(storage_root: str | Path, owner_id: int, relative_path: str,
                      *, must_exist: bool = True, expect_file: bool = True) -> Path:
    """Resolve a database relative path under one owner's storage, including symlink checks."""
    relative = safe_relative_path(relative_path)
    root = Path(storage_root).resolve()
    owner_root = (root / str(int(owner_id))).resolve()
    try:
        owner_root.relative_to(root)
    except ValueError as exc:
        raise ValueError("Invalid owner storage root.") from exc
    candidate = owner_root.joinpath(*PurePosixPath(relative).parts)
    resolved = candidate.resolve(strict=must_exist)
    try:
        resolved.relative_to(owner_root)
    except ValueError as exc:
        raise ValueError("The path resolves outside FileBox storage.") from exc
    if must_exist:
        if expect_file and not resolved.is_file():
            raise FileNotFoundError("Managed file not found.")
        if not expect_file and not resolved.is_dir():
            raise FileNotFoundError("Managed folder not found.")
    return resolved
