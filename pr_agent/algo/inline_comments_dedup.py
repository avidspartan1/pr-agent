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
import textwrap
from typing import Any, Optional

MARKER_PREFIX = "<!-- pr-agent-inline-id:"
MARKER_SUFFIX = " -->"

# Constants used by the resolve-outdated-inline-comments feature.
# RESOLVED_BODY_MARKER is appended (with RESOLVED_NOTE) to the body of an
# inline comment whose suggestion was not re-emitted on the current run.
# It also serves as an idempotency signal: if a user manually unresolves a
# thread we previously auto-resolved, the marker remains in the body and
# tells us not to re-resolve on subsequent runs.
RESOLVED_NOTE = "Resolved automatically: this suggestion was not re-emitted on the latest run."
RESOLVED_BODY_MARKER = "<!-- pr-agent-inline-resolved -->"

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


_INTERNAL_WS_RE = re.compile(r"(?<=\S)\s+(?=\S)")


def normalize_code(text: Optional[str]) -> str:
    """Normalize a proposed-edit code snippet for stable hashing.

    Expands tabs, strips trailing whitespace per line, drops leading and
    trailing fully-blank lines, removes the longest common leading
    whitespace across remaining lines (textwrap.dedent), and collapses
    runs of internal whitespace within each line. Token content survives,
    so genuinely different edits still produce different outputs.
    """
    if not text:
        return ""
    expanded = text.expandtabs()
    lines = [line.rstrip() for line in expanded.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return ""
    dedented = textwrap.dedent("\n".join(lines))
    return "\n".join(_INTERNAL_WS_RE.sub(" ", line) for line in dedented.split("\n"))


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


def format_resolved_body(original_body: str) -> str:
    """Append the auto-resolved note and idempotency marker to ``original_body``.

    Shared by every provider's outdated pass so the on-screen format stays
    identical and the body marker check (RESOLVED_BODY_MARKER in body) keeps
    working across providers.
    """
    return (
        (original_body or "").rstrip()
        + f"\n\n---\n_{RESOLVED_NOTE}_\n{RESOLVED_BODY_MARKER}"
    )


def normalize_persistent_mode(raw: Any) -> str:
    """Coerce config input to one of the valid modes. Unknown values fall back to 'off'."""
    if raw is None:
        return PERSISTENT_MODE_OFF
    candidate = str(raw).strip().lower()
    if candidate in VALID_PERSISTENT_MODES:
        return candidate
    return PERSISTENT_MODE_OFF
