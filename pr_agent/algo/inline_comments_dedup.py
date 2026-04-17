"""
Stable-marker deduplication for inline PR comments.

When PR-Agent re-runs /improve or /add_docs on the same PR, each run would
otherwise post fresh inline comments for suggestions that were already posted.
This module generates a hidden, content-derived marker that providers embed
in inline comment bodies so that subsequent runs can recognize and update
(or skip) the prior comment instead of creating a duplicate.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

MARKER_PREFIX = "<!-- pr-agent-inline-id:"
MARKER_SUFFIX = " -->"

PERSISTENT_MODE_OFF = "off"
PERSISTENT_MODE_UPDATE = "update"
PERSISTENT_MODE_SKIP = "skip"
VALID_PERSISTENT_MODES = {PERSISTENT_MODE_OFF, PERSISTENT_MODE_UPDATE, PERSISTENT_MODE_SKIP}

_HASH_LEN = 12
_CONTENT_PREFIX_LEN = 128
_MARKER_RE = re.compile(
    re.escape(MARKER_PREFIX) + r"([0-9a-f]{" + str(_HASH_LEN) + r"})" + re.escape(MARKER_SUFFIX)
)
_WHITESPACE_RE = re.compile(r"\s+")


def _pick_content(suggestion: dict) -> Optional[str]:
    for key in ("suggestion_content", "suggestion_summary", "content"):
        val = suggestion.get(key)
        if val:
            return str(val)
    return None


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def generate_marker(suggestion: dict) -> Optional[str]:
    """Return a stable marker for this suggestion, or None if required fields are missing."""
    file = suggestion.get("relevant_file")
    label = suggestion.get("label")
    content = _pick_content(suggestion)
    if not file or not label or not content:
        return None
    sig = f"{str(file).strip()}|{str(label).strip()}|{_normalize(content)[:_CONTENT_PREFIX_LEN]}"
    digest = hashlib.sha256(sig.encode("utf-8")).hexdigest()[:_HASH_LEN]
    return f"{MARKER_PREFIX}{digest}{MARKER_SUFFIX}"


def extract_marker(body: str) -> Optional[str]:
    """Return the last marker hash found in `body`, or None."""
    if not body:
        return None
    matches = _MARKER_RE.findall(body)
    if not matches:
        return None
    return matches[-1]


def append_marker(body: str, marker: str) -> str:
    """Append `marker` to `body` if not already present; idempotent."""
    if not marker:
        return body
    if marker in body:
        return body
    sep = "" if body.endswith("\n") else "\n\n"
    return f"{body}{sep}{marker}"


def build_marker_index(comments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index comments by marker hash. Comments without a marker are ignored. Last wins on collision."""
    index: dict[str, dict[str, Any]] = {}
    for c in comments or []:
        body = c.get("body") or ""
        h = extract_marker(body)
        if h:
            index[h] = c
    return index


def normalize_persistent_mode(raw: Any) -> str:
    """Coerce config input to one of the valid modes. Unknown values fall back to 'off'."""
    if raw is None:
        return PERSISTENT_MODE_OFF
    candidate = str(raw).strip().lower()
    if candidate in VALID_PERSISTENT_MODES:
        return candidate
    return PERSISTENT_MODE_OFF
