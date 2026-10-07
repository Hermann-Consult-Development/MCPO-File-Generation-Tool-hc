"""Small shared boundaries for the authenticated HC file-tool path."""
import os
import re
from pathlib import Path, PureWindowsPath

import requests

REQUIRE_USER_AUTH = os.getenv("REQUIRE_USER_AUTH", "false").lower() == "true"
REQUEST_TIMEOUT = (5, 60)


class FilePolicyError(ValueError):
    """A deliberately non-sensitive validation message safe for the caller."""


def validate_generated_content(value):
    if not REQUIRE_USER_AUTH:
        return
    if isinstance(value, dict):
        if value.get("type") in ("image", "image_query") or value.get("image_query"):
            raise FilePolicyError("Images are not supported in secure exports; use text and tables")
        for item in value.values():
            validate_generated_content(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_generated_content(item)
    elif isinstance(value, str) and re.search(r"<\s*img\b|!\[", value, re.IGNORECASE):
        raise FilePolicyError("Local and remote images are not supported in secure exports; use text and tables")


def safe_filename(name):
    if (not isinstance(name, str) or not name or len(name) > 240
            or name in (".", "..") or any(ord(c) < 32 for c in name)
            or any(c in name for c in '/\\:') or PureWindowsPath(name).drive):
        raise FilePolicyError("A plain filename without directories is required")
    return name


def safe_join(folder, name):
    name = safe_filename(name)
    root = Path(folder).resolve()
    path = root / name
    if path.resolve().parent != root:
        raise FilePolicyError("Output path is outside its temporary folder")
    return str(path)


def user_token(headers, fallback=None):
    values = headers if isinstance(headers, dict) else {}
    token = next((v for k, v in values.items() if k.lower() == "authorization"), None)
    if REQUIRE_USER_AUTH:
        if (not isinstance(token, str) or not token.startswith("Bearer ")
                or not token[7:].strip() or any(c in token for c in '\r\n')):
            raise FilePolicyError("A forwarded user Authorization bearer token is required")
        return token
    return token or fallback


def file_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise FilePolicyError("Invalid Open WebUI file identifier")
    return value


def http_get(url, **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    kwargs["allow_redirects"] = False
    return requests.get(url, **kwargs)


def http_post(url, **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    kwargs["allow_redirects"] = False
    return requests.post(url, **kwargs)


def public_error(exc):
    # Conversion-library/HTTP errors can contain local paths and credentials.
    if isinstance(exc, FilePolicyError):
        return str(exc)
    return "File operation failed; no artifact was published" if REQUIRE_USER_AUTH else str(exc)
