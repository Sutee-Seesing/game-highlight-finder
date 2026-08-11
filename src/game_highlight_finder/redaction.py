"""Small, provider-independent secret redaction foundation."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "<redacted>"
_SECRET_KEY = re.compile(
    r"(?:^|[_-])(api[_-]?key|token|secret|password|passwd|authorization|credential)(?:$|[_-])",
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|AUTHORIZATION)[A-Z0-9_]*)"
    r"\s*=\s*([^\s,;]+)"
)


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(f"_{key}_"))


def redact_data(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if is_secret_key(str(key)) else redact_data(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_data(item) for item in value]
    return value


def redact_text(text: str) -> str:
    return _ASSIGNMENT.sub(lambda match: f"{match.group(1)}={REDACTED}", text)
