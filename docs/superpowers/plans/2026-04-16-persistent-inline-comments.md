# Persistent Inline Comments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deduplicate inline `/improve` and `/add_docs` comments across re-runs by embedding a stable content-hash marker and editing matching comments in place.

**Architecture:** A shared helper module (`pr_agent/algo/inline_comments_dedup.py`) generates/extracts a hidden `<!-- pr-agent-inline-id:<hash> -->` marker. The `GitProvider` base class gets two optional capability methods (`get_bot_review_comments`, `edit_review_comment`) with no-op defaults. GitHub and GitLab providers implement those methods and make `publish_code_suggestions` dedup-aware, gated by a new `persistent_inline_comments` setting.

**Tech Stack:** Python 3.12, pytest, PyGithub, python-gitlab, Dynaconf.

**Spec:** `docs/superpowers/specs/2026-04-16-persistent-inline-comments-design.md`

---

## File Structure

| File | Role |
|---|---|
| `pr_agent/algo/inline_comments_dedup.py` (NEW) | Pure marker logic — generate/extract/append/index. No provider dependency. |
| `pr_agent/settings/configuration.toml` | Declare `persistent_inline_comments` setting under `[pr_code_suggestions]`. |
| `pr_agent/git_providers/git_provider.py` | Base-class no-op defaults for two new optional capabilities. |
| `pr_agent/git_providers/github_provider.py` | GitHub implementation: list bot review comments, edit review comment, dedup-aware `publish_code_suggestions`. |
| `pr_agent/git_providers/gitlab_provider.py` | GitLab equivalent using `mr.discussions`. |
| `tests/unittest/test_inline_comments_dedup.py` (NEW) | Unit tests for the marker helper. |
| `tests/unittest/test_github_inline_dedup.py` (NEW) | Mock-based tests for GitHub dedup. |
| `tests/unittest/test_gitlab_inline_dedup.py` (NEW) | Mock-based tests for GitLab dedup. |
| `docs/docs/tools/improve.md` | User documentation for the new setting. |

---

### Task 0: Marker helper module with unit tests

**Goal:** Pure, provider-agnostic primitives for generating, extracting, and indexing inline-comment markers.

**Files:**
- Create: `pr_agent/algo/inline_comments_dedup.py`
- Create: `tests/unittest/test_inline_comments_dedup.py`

**Acceptance Criteria:**
- [ ] `generate_marker(suggestion)` returns `"<!-- pr-agent-inline-id:<12hex> -->"` for suggestions with the required fields
- [ ] Same file + label + content prefix → same marker (stable under line-number changes)
- [ ] Different file OR different label OR different content prefix → different marker
- [ ] `extract_marker(body)` returns the hash when marker is present, `None` otherwise
- [ ] `append_marker(body, marker)` round-trips with `extract_marker`
- [ ] `build_marker_index(comments)` returns `{hash: comment}` and ignores comments without markers
- [ ] All unit tests pass

**Verify:** `pytest tests/unittest/test_inline_comments_dedup.py -v` → all green

**Steps:**

- [ ] **Step 1: Write the unit tests first**

Create `tests/unittest/test_inline_comments_dedup.py`:

```python
import pytest

from pr_agent.algo.inline_comments_dedup import (
    MARKER_PREFIX,
    MARKER_SUFFIX,
    PERSISTENT_MODE_OFF,
    PERSISTENT_MODE_SKIP,
    PERSISTENT_MODE_UPDATE,
    VALID_PERSISTENT_MODES,
    append_marker,
    build_marker_index,
    extract_marker,
    generate_marker,
    normalize_persistent_mode,
)


def _suggestion(file="src/app.py", label="possible issue",
                content="Nullable pointer may crash on line 42 when user_id is None",
                start=10, end=12):
    return {
        "relevant_file": file,
        "label": label,
        "suggestion_content": content,
        "relevant_lines_start": start,
        "relevant_lines_end": end,
    }


class TestGenerateMarker:
    def test_shape(self):
        marker = generate_marker(_suggestion())
        assert marker.startswith(MARKER_PREFIX)
        assert marker.endswith(MARKER_SUFFIX)
        hash_part = marker[len(MARKER_PREFIX):-len(MARKER_SUFFIX)]
        assert len(hash_part) == 12
        assert all(c in "0123456789abcdef" for c in hash_part)

    def test_deterministic(self):
        assert generate_marker(_suggestion()) == generate_marker(_suggestion())

    def test_stable_across_line_shifts(self):
        a = generate_marker(_suggestion(start=10, end=12))
        b = generate_marker(_suggestion(start=200, end=202))
        assert a == b

    def test_changes_with_file(self):
        a = generate_marker(_suggestion(file="src/app.py"))
        b = generate_marker(_suggestion(file="src/other.py"))
        assert a != b

    def test_changes_with_label(self):
        a = generate_marker(_suggestion(label="possible issue"))
        b = generate_marker(_suggestion(label="security"))
        assert a != b

    def test_changes_with_content_prefix(self):
        a = generate_marker(_suggestion(content="A totally different suggestion about X"))
        b = generate_marker(_suggestion(content="Another totally different suggestion about Y"))
        assert a != b

    def test_tolerates_trailing_content_variation(self):
        long_base = "Same opening 128-chars " + "x" * 200
        a = generate_marker(_suggestion(content=long_base + "tail-A"))
        b = generate_marker(_suggestion(content=long_base + "tail-B"))
        assert a == b

    def test_whitespace_normalized(self):
        a = generate_marker(_suggestion(content="Same   content  here"))
        b = generate_marker(_suggestion(content="Same content here"))
        assert a == b

    def test_missing_fields_returns_none(self):
        assert generate_marker({"relevant_file": "a.py"}) is None
        assert generate_marker({}) is None


class TestExtractMarker:
    def test_present(self):
        body = "some text\n<!-- pr-agent-inline-id:abc123def456 -->"
        assert extract_marker(body) == "abc123def456"

    def test_missing(self):
        assert extract_marker("no marker here") is None

    def test_empty(self):
        assert extract_marker("") is None

    def test_multiple_returns_last(self):
        body = "<!-- pr-agent-inline-id:oldold000000 -->\nmore\n<!-- pr-agent-inline-id:newnew111111 -->"
        assert extract_marker(body) == "newnew111111"

    def test_roundtrip_with_append(self):
        marker = generate_marker(_suggestion())
        body_plus = append_marker("suggestion body", marker)
        assert extract_marker(body_plus) == marker[len(MARKER_PREFIX):-len(MARKER_SUFFIX)]


class TestAppendMarker:
    def test_adds_separator(self):
        body = append_marker("hello", "<!-- pr-agent-inline-id:abcabcabcabc -->")
        assert body.endswith("<!-- pr-agent-inline-id:abcabcabcabc -->")
        assert "hello\n\n<!--" in body

    def test_idempotent_when_already_marked(self):
        marker = "<!-- pr-agent-inline-id:abcabcabcabc -->"
        once = append_marker("hello", marker)
        twice = append_marker(once, marker)
        assert once == twice


class TestBuildMarkerIndex:
    def test_indexes_marked_comments(self):
        comments = [
            {"id": 1, "body": "body A <!-- pr-agent-inline-id:aaaaaaaaaaaa -->"},
            {"id": 2, "body": "body B <!-- pr-agent-inline-id:bbbbbbbbbbbb -->"},
        ]
        index = build_marker_index(comments)
        assert index["aaaaaaaaaaaa"]["id"] == 1
        assert index["bbbbbbbbbbbb"]["id"] == 2

    def test_ignores_unmarked(self):
        comments = [{"id": 1, "body": "no marker"}]
        assert build_marker_index(comments) == {}

    def test_last_wins_on_duplicate_hash(self):
        comments = [
            {"id": 1, "body": "A <!-- pr-agent-inline-id:aaaaaaaaaaaa -->"},
            {"id": 2, "body": "B <!-- pr-agent-inline-id:aaaaaaaaaaaa -->"},
        ]
        index = build_marker_index(comments)
        assert index["aaaaaaaaaaaa"]["id"] == 2


class TestNormalizePersistentMode:
    def test_valid_values(self):
        assert normalize_persistent_mode("off") == PERSISTENT_MODE_OFF
        assert normalize_persistent_mode("update") == PERSISTENT_MODE_UPDATE
        assert normalize_persistent_mode("skip") == PERSISTENT_MODE_SKIP

    def test_case_and_whitespace(self):
        assert normalize_persistent_mode("  UPDATE  ") == PERSISTENT_MODE_UPDATE

    def test_invalid_falls_back_to_off(self):
        assert normalize_persistent_mode("garbage") == PERSISTENT_MODE_OFF
        assert normalize_persistent_mode(None) == PERSISTENT_MODE_OFF
        assert normalize_persistent_mode("") == PERSISTENT_MODE_OFF

    def test_valid_set_exposed(self):
        assert VALID_PERSISTENT_MODES == {PERSISTENT_MODE_OFF, PERSISTENT_MODE_UPDATE, PERSISTENT_MODE_SKIP}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unittest/test_inline_comments_dedup.py -v`
Expected: ImportError / module not found — the helper module does not yet exist.

- [ ] **Step 3: Write the helper module**

Create `pr_agent/algo/inline_comments_dedup.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unittest/test_inline_comments_dedup.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add pr_agent/algo/inline_comments_dedup.py tests/unittest/test_inline_comments_dedup.py
git commit -m "feat(algo): add inline-comment dedup marker helpers

Introduces pr_agent.algo.inline_comments_dedup with generate_marker /
extract_marker / append_marker / build_marker_index / normalize_persistent_mode.
Pure provider-agnostic logic; full unit coverage."
```

---

### Task 1: Base-class capability methods and config setting

**Goal:** Declare the two new optional capabilities on `GitProvider` (no-op defaults) and expose the `persistent_inline_comments` setting.

**Files:**
- Modify: `pr_agent/git_providers/git_provider.py`
- Modify: `pr_agent/settings/configuration.toml`

**Acceptance Criteria:**
- [ ] `GitProvider.get_bot_review_comments()` exists and returns `[]` by default
- [ ] `GitProvider.edit_review_comment(comment_id, body)` exists and returns `False` by default
- [ ] `configuration.toml` declares `persistent_inline_comments = "update"` under `[pr_code_suggestions]`
- [ ] `pytest tests/unittest/ -k "not e2e" -x` still passes (no regressions)

**Verify:** `pytest tests/unittest/test_inline_comments_dedup.py tests/unittest/test_gitlab_provider.py -v` → green

**Steps:**

- [ ] **Step 1: Add base-class methods**

Open `pr_agent/git_providers/git_provider.py`. Find the `publish_inline_comments` abstract method (around line 338). Immediately after it, add two new non-abstract methods:

```python
    def get_bot_review_comments(self) -> list[dict]:
        """
        Return the bot's existing inline (review) comments on the current PR.

        Each dict must contain at least:
            - 'id':   provider-specific comment id (used by edit_review_comment)
            - 'body': full comment body (used for marker extraction)

        Default: return []. Providers that support inline-comment dedup should override.
        """
        return []

    def edit_review_comment(self, comment_id, body: str) -> bool:
        """
        Edit an existing inline (review) comment in place.

        Returns True on success, False otherwise. Default: return False (unsupported),
        which causes persistent-inline-comment dedup to fall back to the create-new path.
        """
        return False
```

- [ ] **Step 2: Add config setting**

Open `pr_agent/settings/configuration.toml`. In the `[pr_code_suggestions]` section, directly below the existing `persistent_comment=true` line (around line 139), add:

```toml
# Deduplicate inline suggestions across re-runs by embedding a content-hash marker.
# "update": edit matching existing comment in place (default)
# "skip":   skip if a matching comment already exists
# "off":    always post a new comment (legacy behavior)
persistent_inline_comments = "update"
```

- [ ] **Step 3: Verify no regressions**

Run: `pytest tests/unittest/test_inline_comments_dedup.py tests/unittest/test_gitlab_provider.py -v`
Expected: all existing and new tests PASS.

- [ ] **Step 4: Commit**

```bash
git add pr_agent/git_providers/git_provider.py pr_agent/settings/configuration.toml
git commit -m "feat(providers): declare inline-comment dedup capability & setting

Adds no-op defaults for GitProvider.get_bot_review_comments and
edit_review_comment so provider implementations can opt in. Declares
pr_code_suggestions.persistent_inline_comments config (default 'update')."
```

---

### Task 2: GitHub implementation with mock-based tests

**Goal:** Implement `get_bot_review_comments` and `edit_review_comment` on `GithubProvider`, and make `publish_code_suggestions` dedup-aware.

**Files:**
- Modify: `pr_agent/git_providers/github_provider.py`
- Create: `tests/unittest/test_github_inline_dedup.py`

**Acceptance Criteria:**
- [ ] `GithubProvider.get_bot_review_comments()` returns a list of `{id, body, path, line, start_line}` filtered to the bot's own comments (app name for app deployments; `github_user_id` for user deployments)
- [ ] `GithubProvider.edit_review_comment(id, body)` issues `PATCH /repos/{repo}/pulls/comments/{id}` and returns True/False
- [ ] `publish_code_suggestions` appends the marker to every body before publishing
- [ ] In `"update"` mode, matching markers are PATCHed and NOT included in the new `create_review` batch
- [ ] In `"skip"` mode, matching markers are neither PATCHed nor re-published
- [ ] In `"off"` mode, `get_bot_review_comments` is never called (legacy path)
- [ ] If `get_bot_review_comments` raises, publishing proceeds as if no existing comments were found (no exception propagates)
- [ ] If `edit_review_comment` returns False for one suggestion, that suggestion is included in the create-new batch

**Verify:** `pytest tests/unittest/test_github_inline_dedup.py -v` → all green

**Steps:**

- [ ] **Step 1: Write the tests first**

Create `tests/unittest/test_github_inline_dedup.py`:

```python
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.algo.inline_comments_dedup import (
    MARKER_PREFIX,
    MARKER_SUFFIX,
    generate_marker,
)


def _sug(label="possible issue", file="src/app.py",
         content="Check for None before dereferencing user_id on this line.",
         start=10, end=12):
    orig = {
        "relevant_file": file,
        "label": label,
        "suggestion_content": content,
        "relevant_lines_start": start,
        "relevant_lines_end": end,
    }
    return {
        "body": f"**Suggestion:** {content} [{label}]\n```suggestion\nfix\n```",
        "relevant_file": file,
        "relevant_lines_start": start,
        "relevant_lines_end": end,
        "original_suggestion": orig,
    }


@pytest.fixture
def provider():
    with patch("pr_agent.git_providers.github_provider.GithubProvider._get_repo"), \
         patch("pr_agent.git_providers.github_provider.GithubProvider.set_pr"), \
         patch("pr_agent.git_providers.github_provider.GithubProvider._get_pr"):
        from pr_agent.git_providers.github_provider import GithubProvider
        p = GithubProvider.__new__(GithubProvider)
        p.pr = MagicMock()
        p.last_commit_id = MagicMock(sha="abc123")
        p.repo = "owner/repo"
        p.base_url = "https://api.github.com"
        p.max_comment_chars = 65000
        p.github_user_id = "pr-agent-bot"
        p.deployment_type = "user"
        p.validate_comments_inside_hunks = lambda x: x
        return p


def _set_mode(mode):
    return patch(
        "pr_agent.git_providers.github_provider.get_settings",
        return_value=MagicMock(
            pr_code_suggestions=MagicMock(get=lambda key, default=None:
                mode if key == "persistent_inline_comments" else default),
        ),
    )


class TestOffMode:
    def test_off_mode_skips_fetch(self, provider):
        provider.get_bot_review_comments = MagicMock()
        provider.edit_review_comment = MagicMock()
        with _set_mode("off"):
            provider.publish_code_suggestions([_sug()])
        provider.get_bot_review_comments.assert_not_called()
        provider.edit_review_comment.assert_not_called()
        provider.pr.create_review.assert_called_once()


class TestUpdateMode:
    def test_no_match_creates_new(self, provider):
        provider.get_bot_review_comments = MagicMock(return_value=[])
        provider.edit_review_comment = MagicMock()
        with _set_mode("update"):
            provider.publish_code_suggestions([_sug()])
        provider.edit_review_comment.assert_not_called()
        provider.pr.create_review.assert_called_once()
        args, kwargs = provider.pr.create_review.call_args
        body_published = kwargs["comments"][0]["body"]
        assert MARKER_PREFIX in body_published and MARKER_SUFFIX in body_published

    def test_match_edits_and_skips_creation(self, provider):
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        existing_body = "old body\n\n" + marker
        provider.get_bot_review_comments = MagicMock(
            return_value=[{"id": 777, "body": existing_body, "path": s["relevant_file"]}]
        )
        provider.edit_review_comment = MagicMock(return_value=True)
        with _set_mode("update"):
            provider.publish_code_suggestions([s])
        provider.edit_review_comment.assert_called_once()
        called_id = provider.edit_review_comment.call_args[0][0]
        assert called_id == 777
        provider.pr.create_review.assert_not_called()

    def test_edit_failure_falls_back_to_create(self, provider):
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        provider.get_bot_review_comments = MagicMock(
            return_value=[{"id": 777, "body": "old " + marker, "path": s["relevant_file"]}]
        )
        provider.edit_review_comment = MagicMock(return_value=False)
        with _set_mode("update"):
            provider.publish_code_suggestions([s])
        provider.pr.create_review.assert_called_once()

    def test_mixed_match_and_new(self, provider):
        matched = _sug(content="Matched suggestion")
        unmatched = _sug(content="Brand new suggestion", start=40, end=42)
        marker_matched = generate_marker(matched["original_suggestion"])
        provider.get_bot_review_comments = MagicMock(
            return_value=[{"id": 1, "body": marker_matched, "path": matched["relevant_file"]}]
        )
        provider.edit_review_comment = MagicMock(return_value=True)
        with _set_mode("update"):
            provider.publish_code_suggestions([matched, unmatched])
        assert provider.edit_review_comment.call_count == 1
        provider.pr.create_review.assert_called_once()
        created = provider.pr.create_review.call_args.kwargs["comments"]
        assert len(created) == 1
        assert "Brand new" in created[0]["body"]

    def test_fetch_failure_falls_back_to_creating_all(self, provider):
        provider.get_bot_review_comments = MagicMock(side_effect=RuntimeError("api down"))
        provider.edit_review_comment = MagicMock()
        with _set_mode("update"):
            provider.publish_code_suggestions([_sug()])
        provider.edit_review_comment.assert_not_called()
        provider.pr.create_review.assert_called_once()


class TestSkipMode:
    def test_match_skips_entirely(self, provider):
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        provider.get_bot_review_comments = MagicMock(
            return_value=[{"id": 1, "body": marker, "path": s["relevant_file"]}]
        )
        provider.edit_review_comment = MagicMock()
        with _set_mode("skip"):
            provider.publish_code_suggestions([s])
        provider.edit_review_comment.assert_not_called()
        provider.pr.create_review.assert_not_called()

    def test_no_match_still_creates(self, provider):
        provider.get_bot_review_comments = MagicMock(return_value=[])
        provider.edit_review_comment = MagicMock()
        with _set_mode("skip"):
            provider.publish_code_suggestions([_sug()])
        provider.pr.create_review.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unittest/test_github_inline_dedup.py -v`
Expected: failures — provider methods don't exist yet and `publish_code_suggestions` does not route through dedup.

- [ ] **Step 3: Implement `get_bot_review_comments` on GithubProvider**

Open `pr_agent/git_providers/github_provider.py`. At the top of the file, add the imports if not already present:

```python
from pr_agent.algo.inline_comments_dedup import (
    PERSISTENT_MODE_OFF,
    PERSISTENT_MODE_SKIP,
    PERSISTENT_MODE_UPDATE,
    append_marker,
    build_marker_index,
    generate_marker,
    normalize_persistent_mode,
)
```

Add these two methods to the `GithubProvider` class (convenient spot: after `get_review_thread_comments` in the file, roughly line 470):

```python
    def get_bot_review_comments(self) -> list[dict]:
        """
        Return the bot's existing inline review comments on this PR.

        Filters by author to avoid matching human reviewers. Returns a list of
        dicts with id, body, path, line, start_line (line fields may be None
        for file-subject comments).
        """
        try:
            our_app_name = (get_settings().get("GITHUB.APP_NAME", "") or "").lower()
            headers, existing = self.pr._requester.requestJsonAndCheck(
                "GET", f"{self.pr.url}/comments"
            )
            out = []
            for c in existing or []:
                login = ((c.get("user") or {}).get("login") or "").lower()
                same_author = False
                if self.deployment_type == "app":
                    same_author = bool(our_app_name) and our_app_name in login
                elif self.deployment_type == "user":
                    same_author = bool(self.github_user_id) and login == str(self.github_user_id).lower()
                if not same_author:
                    continue
                out.append({
                    "id": c.get("id"),
                    "body": c.get("body") or "",
                    "path": c.get("path"),
                    "line": c.get("line"),
                    "start_line": c.get("start_line"),
                })
            return out
        except Exception as e:
            get_logger().warning(f"Failed to list GitHub review comments: {e}")
            return []

    def edit_review_comment(self, comment_id, body: str) -> bool:
        try:
            body = self.limit_output_characters(body, self.max_comment_chars)
            self.pr._requester.requestJsonAndCheck(
                "PATCH",
                f"{self.base_url}/repos/{self.repo}/pulls/comments/{comment_id}",
                input={"body": body},
            )
            return True
        except Exception as e:
            get_logger().warning(f"Failed to edit GitHub review comment {comment_id}: {e}")
            return False
```

- [ ] **Step 4: Make `publish_code_suggestions` dedup-aware**

Replace the existing `publish_code_suggestions` method (github_provider.py:551-597) with:

```python
    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        """
        Publishes code suggestions as review comments on the PR.

        When `pr_code_suggestions.persistent_inline_comments` is 'update' (default)
        or 'skip', a stable marker is embedded in each body so subsequent runs
        can recognize and update (or skip) the existing comment rather than
        creating duplicates.
        """
        code_suggestions_validated = self.validate_comments_inside_hunks(code_suggestions)

        mode = normalize_persistent_mode(
            get_settings().pr_code_suggestions.get("persistent_inline_comments", PERSISTENT_MODE_OFF)
        )

        existing_index = {}
        if mode != PERSISTENT_MODE_OFF:
            try:
                existing_index = build_marker_index(self.get_bot_review_comments())
            except Exception as e:
                get_logger().warning(f"persistent_inline_comments: fetch failed, falling back to create-new: {e}")
                existing_index = {}

        post_parameters_list = []
        for suggestion in code_suggestions_validated:
            body = suggestion["body"]
            relevant_file = suggestion["relevant_file"]
            relevant_lines_start = suggestion["relevant_lines_start"]
            relevant_lines_end = suggestion["relevant_lines_end"]

            if not relevant_lines_start or relevant_lines_start == -1:
                get_logger().exception(
                    f"Failed to publish code suggestion, relevant_lines_start is {relevant_lines_start}")
                continue
            if relevant_lines_end < relevant_lines_start:
                get_logger().exception(
                    f"Failed to publish code suggestion, "
                    f"relevant_lines_end is {relevant_lines_end} and "
                    f"relevant_lines_start is {relevant_lines_start}")
                continue

            if mode != PERSISTENT_MODE_OFF:
                marker = generate_marker(suggestion.get("original_suggestion") or {})
                if marker:
                    body = append_marker(body, marker)
                    marker_hash = marker[len(MARKER_PREFIX):-len(MARKER_SUFFIX)]
                    existing = existing_index.get(marker_hash)
                    if existing is not None:
                        if mode == PERSISTENT_MODE_SKIP:
                            get_logger().info(
                                f"persistent_inline_comments=skip: existing comment {existing.get('id')} "
                                f"on {relevant_file}; not re-posting")
                            continue
                        # mode == update
                        if self.edit_review_comment(existing.get("id"), body):
                            continue
                        get_logger().info(
                            f"persistent_inline_comments=update: edit failed for {existing.get('id')}; "
                            f"falling back to create-new")

            if relevant_lines_end > relevant_lines_start:
                post_parameters = {
                    "body": body,
                    "path": relevant_file,
                    "line": relevant_lines_end,
                    "start_line": relevant_lines_start,
                    "start_side": "RIGHT",
                }
            else:
                post_parameters = {
                    "body": body,
                    "path": relevant_file,
                    "line": relevant_lines_start,
                    "side": "RIGHT",
                }
            post_parameters_list.append(post_parameters)

        if not post_parameters_list:
            return True

        try:
            self.publish_inline_comments(post_parameters_list)
            return True
        except Exception as e:
            get_logger().error(f"Failed to publish code suggestion, error: {e}")
            return False
```

Ensure this import block is present near the top of `github_provider.py` (add it if absent):

```python
from pr_agent.algo.inline_comments_dedup import (
    MARKER_PREFIX,
    MARKER_SUFFIX,
    PERSISTENT_MODE_OFF,
    PERSISTENT_MODE_SKIP,
    PERSISTENT_MODE_UPDATE,
    append_marker,
    build_marker_index,
    generate_marker,
    normalize_persistent_mode,
)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unittest/test_github_inline_dedup.py tests/unittest/test_inline_comments_dedup.py -v`
Expected: all tests PASS.

- [ ] **Step 6: Run full unit-test suite to check for regressions**

Run: `pytest tests/unittest/ -x`
Expected: no new failures relative to baseline.

- [ ] **Step 7: Commit**

```bash
git add pr_agent/git_providers/github_provider.py tests/unittest/test_github_inline_dedup.py
git commit -m "feat(github): persistent inline comments via content-hash marker

GithubProvider now lists its own existing inline comments, edits them in
place when the generated marker matches, and otherwise creates new
comments as before. Controlled by pr_code_suggestions.persistent_inline_comments
('off' | 'update' | 'skip'). Mock-based tests cover all three modes,
mixed match/new, and fetch/edit failures falling back to create-new."
```

---

### Task 3: GitLab implementation with mock-based tests

**Goal:** Implement `get_bot_review_comments` and `edit_review_comment` on `GitLabProvider`, and make its `publish_code_suggestions` dedup-aware.

**Files:**
- Modify: `pr_agent/git_providers/gitlab_provider.py`
- Create: `tests/unittest/test_gitlab_inline_dedup.py`

**Acceptance Criteria:**
- [ ] `GitLabProvider.get_bot_review_comments()` returns `[{id, body, path, discussion_id, note_id}]` for inline (positioned) notes authored by the authenticated user
- [ ] `GitLabProvider.edit_review_comment(id, body)` updates the matching inline note body and returns True/False
- [ ] `publish_code_suggestions` appends the marker and routes matched suggestions through edit; unmatched continue to `send_inline_comment`
- [ ] `"off"` mode preserves current behavior exactly
- [ ] Fetch/edit failures never prevent publishing
- [ ] `pytest tests/unittest/test_gitlab_inline_dedup.py -v` passes

**Verify:** `pytest tests/unittest/test_gitlab_inline_dedup.py -v` → all green

**Steps:**

- [ ] **Step 1: Write tests first**

Create `tests/unittest/test_gitlab_inline_dedup.py`:

```python
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.algo.inline_comments_dedup import MARKER_PREFIX, MARKER_SUFFIX, generate_marker


def _sug(label="possible issue", file="src/app.py",
         content="Nullable pointer may crash here when x is None.",
         start=10, end=12):
    orig = {
        "relevant_file": file,
        "label": label,
        "suggestion_content": content,
        "relevant_lines_start": start,
        "relevant_lines_end": end,
        "existing_code": "old",
        "improved_code": "new",
    }
    return {
        "body": f"**Suggestion:** {content} [{label}]\n```suggestion\nfix\n```",
        "relevant_file": file,
        "relevant_lines_start": start,
        "relevant_lines_end": end,
        "original_suggestion": orig,
    }


@pytest.fixture
def provider():
    from pr_agent.git_providers.gitlab_provider import GitLabProvider
    p = GitLabProvider.__new__(GitLabProvider)
    p.mr = MagicMock()
    p.gl = MagicMock()
    p.id_project = "g/p"
    p.id_mr = 1
    p.diff_files = []
    # Provide a file with head_file lines so get_diff_files() access inside
    # publish_code_suggestions works. Its only use is for
    # `lines[relevant_lines_start - 1]` — keep it simple.
    f = MagicMock()
    f.filename = "src/app.py"
    f.head_file = "\n".join([f"line{i}" for i in range(1, 100)])
    p.get_diff_files = lambda: [f]
    p.send_inline_comment = MagicMock()
    return p


def _set_mode(mode):
    return patch(
        "pr_agent.git_providers.gitlab_provider.get_settings",
        return_value=MagicMock(
            pr_code_suggestions=MagicMock(get=lambda key, default=None:
                mode if key == "persistent_inline_comments" else default),
        ),
    )


class TestOffMode:
    def test_off_mode_uses_legacy_path(self, provider):
        provider.get_bot_review_comments = MagicMock()
        provider.edit_review_comment = MagicMock()
        with _set_mode("off"):
            assert provider.publish_code_suggestions([_sug()]) is True
        provider.get_bot_review_comments.assert_not_called()
        provider.edit_review_comment.assert_not_called()
        provider.send_inline_comment.assert_called_once()


class TestUpdateMode:
    def test_match_edits_no_send(self, provider):
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        provider.get_bot_review_comments = MagicMock(return_value=[{
            "id": 42, "body": "old\n\n" + marker, "path": "src/app.py",
        }])
        provider.edit_review_comment = MagicMock(return_value=True)
        with _set_mode("update"):
            provider.publish_code_suggestions([s])
        provider.edit_review_comment.assert_called_once()
        provider.send_inline_comment.assert_not_called()

    def test_no_match_sends_inline(self, provider):
        provider.get_bot_review_comments = MagicMock(return_value=[])
        provider.edit_review_comment = MagicMock()
        with _set_mode("update"):
            provider.publish_code_suggestions([_sug()])
        provider.edit_review_comment.assert_not_called()
        provider.send_inline_comment.assert_called_once()

    def test_fetch_failure_falls_back(self, provider):
        provider.get_bot_review_comments = MagicMock(side_effect=RuntimeError("api down"))
        provider.edit_review_comment = MagicMock()
        with _set_mode("update"):
            provider.publish_code_suggestions([_sug()])
        provider.edit_review_comment.assert_not_called()
        provider.send_inline_comment.assert_called_once()

    def test_edit_failure_falls_back(self, provider):
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        provider.get_bot_review_comments = MagicMock(return_value=[{
            "id": 42, "body": marker, "path": "src/app.py",
        }])
        provider.edit_review_comment = MagicMock(return_value=False)
        with _set_mode("update"):
            provider.publish_code_suggestions([s])
        provider.send_inline_comment.assert_called_once()


class TestSkipMode:
    def test_match_skips(self, provider):
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        provider.get_bot_review_comments = MagicMock(return_value=[{
            "id": 42, "body": marker, "path": "src/app.py",
        }])
        provider.edit_review_comment = MagicMock()
        with _set_mode("skip"):
            provider.publish_code_suggestions([s])
        provider.edit_review_comment.assert_not_called()
        provider.send_inline_comment.assert_not_called()

    def test_no_match_still_sends(self, provider):
        provider.get_bot_review_comments = MagicMock(return_value=[])
        with _set_mode("skip"):
            provider.publish_code_suggestions([_sug()])
        provider.send_inline_comment.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unittest/test_gitlab_inline_dedup.py -v`
Expected: failures — dedup branch not implemented.

- [ ] **Step 3: Add imports and provider methods**

Open `pr_agent/git_providers/gitlab_provider.py`. Add the same import block at the top:

```python
from pr_agent.algo.inline_comments_dedup import (
    MARKER_PREFIX,
    MARKER_SUFFIX,
    PERSISTENT_MODE_OFF,
    PERSISTENT_MODE_SKIP,
    PERSISTENT_MODE_UPDATE,
    append_marker,
    build_marker_index,
    generate_marker,
    normalize_persistent_mode,
)
```

Add these methods to the `GitLabProvider` class (after the existing `publish_inline_comments` method around line 890):

```python
    def _get_bot_gitlab_username(self) -> str:
        """Return the authenticated user's username, '' if unavailable."""
        try:
            if getattr(self.gl, "user", None) is None:
                try:
                    self.gl.auth()
                except Exception:
                    pass
            user = getattr(self.gl, "user", None)
            if user is not None:
                return str(getattr(user, "username", "") or "").lower()
        except Exception as e:
            get_logger().debug(f"Could not resolve GitLab bot username: {e}")
        return ""

    def get_bot_review_comments(self) -> list[dict]:
        """
        Return the bot's existing inline (positioned) notes on this MR.

        Each dict contains: id (== note id), body, path, discussion_id, note_id.
        Returns [] if the username cannot be resolved or listing fails.
        """
        bot_username = self._get_bot_gitlab_username()
        if not bot_username:
            return []
        try:
            discussions = self.mr.discussions.list(get_all=True)
        except Exception as e:
            get_logger().warning(f"Failed to list GitLab discussions: {e}")
            return []
        out = []
        for disc in discussions or []:
            attrs = getattr(disc, "attributes", {}) or {}
            notes = attrs.get("notes") or []
            if not notes:
                continue
            first_note = notes[0]
            position = first_note.get("position") or {}
            if not position.get("new_path") and not position.get("old_path"):
                continue  # not an inline note
            author = (first_note.get("author") or {}).get("username", "")
            if str(author or "").lower() != bot_username:
                continue
            out.append({
                "id": first_note.get("id"),
                "body": first_note.get("body") or "",
                "path": position.get("new_path") or position.get("old_path"),
                "discussion_id": attrs.get("id"),
                "note_id": first_note.get("id"),
            })
        return out

    def edit_review_comment(self, comment_id, body: str) -> bool:
        """Update an inline note body. comment_id is the note id."""
        try:
            discussions = self.mr.discussions.list(get_all=True)
            for disc in discussions or []:
                attrs = getattr(disc, "attributes", {}) or {}
                for note in attrs.get("notes") or []:
                    if note.get("id") == comment_id:
                        disc_obj = self.mr.discussions.get(attrs.get("id"))
                        note_obj = disc_obj.notes.get(comment_id)
                        note_obj.body = body
                        note_obj.save()
                        return True
            get_logger().warning(f"GitLab inline note {comment_id} not found for edit")
            return False
        except Exception as e:
            get_logger().warning(f"Failed to edit GitLab inline note {comment_id}: {e}")
            return False
```

- [ ] **Step 4: Make `publish_code_suggestions` dedup-aware**

Replace the existing method (gitlab_provider.py:657-694) with:

```python
    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        mode = normalize_persistent_mode(
            get_settings().pr_code_suggestions.get("persistent_inline_comments", PERSISTENT_MODE_OFF)
        )

        existing_index = {}
        if mode != PERSISTENT_MODE_OFF:
            try:
                existing_index = build_marker_index(self.get_bot_review_comments())
            except Exception as e:
                get_logger().warning(f"persistent_inline_comments: fetch failed, falling back to create-new: {e}")
                existing_index = {}

        for suggestion in code_suggestions:
            try:
                if suggestion and 'original_suggestion' in suggestion:
                    original_suggestion = suggestion['original_suggestion']
                else:
                    original_suggestion = suggestion
                body = suggestion['body']
                relevant_file = suggestion['relevant_file']
                relevant_lines_start = suggestion['relevant_lines_start']
                relevant_lines_end = suggestion['relevant_lines_end']

                if mode != PERSISTENT_MODE_OFF:
                    marker = generate_marker(original_suggestion or {})
                    if marker:
                        body = append_marker(body, marker)
                        marker_hash = marker[len(MARKER_PREFIX):-len(MARKER_SUFFIX)]
                        existing = existing_index.get(marker_hash)
                        if existing is not None:
                            if mode == PERSISTENT_MODE_SKIP:
                                get_logger().info(
                                    f"persistent_inline_comments=skip: existing inline note {existing.get('id')} "
                                    f"on {relevant_file}; not re-posting")
                                continue
                            # update
                            if self.edit_review_comment(existing.get("id"), body):
                                continue
                            get_logger().info(
                                f"persistent_inline_comments=update: edit failed for {existing.get('id')}; "
                                f"falling back to create-new")

                diff_files = self.get_diff_files()
                target_file = None
                for file in diff_files:
                    if file.filename == relevant_file:
                        target_file = file
                        break
                range = relevant_lines_end - relevant_lines_start  # no need to add 1
                body = body.replace('```suggestion', f'```suggestion:-0+{range}')
                lines = target_file.head_file.splitlines()
                relevant_line_in_file = lines[relevant_lines_start - 1]

                source_line_no = -1
                target_line_no = relevant_lines_start + 1
                found = True
                edit_type = 'addition'

                self.send_inline_comment(body, edit_type, found, relevant_file, relevant_line_in_file,
                                         source_line_no, target_file, target_line_no, original_suggestion)
            except Exception as e:
                get_logger().exception(f"Could not publish code suggestion:\nsuggestion: {suggestion}\nerror: {e}")

        return True
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unittest/test_gitlab_inline_dedup.py tests/unittest/test_gitlab_provider.py tests/unittest/test_inline_comments_dedup.py -v`
Expected: all tests PASS.

- [ ] **Step 6: Run full unit-test suite**

Run: `pytest tests/unittest/ -x`
Expected: no regressions.

- [ ] **Step 7: Commit**

```bash
git add pr_agent/git_providers/gitlab_provider.py tests/unittest/test_gitlab_inline_dedup.py
git commit -m "feat(gitlab): persistent inline comments via content-hash marker

GitLabProvider now lists its own existing inline discussion notes,
updates them in place when the marker matches, and otherwise falls
through to the legacy send_inline_comment path. Controlled by the
pr_code_suggestions.persistent_inline_comments setting introduced earlier."
```

---

### Task 4: User documentation

**Goal:** Document `persistent_inline_comments` in the `/improve` tool page.

**Files:**
- Modify: `docs/docs/tools/improve.md`

**Acceptance Criteria:**
- [ ] `persistent_inline_comments` row added next to `persistent_comment`
- [ ] Possible values and default clearly stated

**Verify:** `grep -n persistent_inline_comments docs/docs/tools/improve.md` shows the new row

**Steps:**

- [ ] **Step 1: Add the documentation row**

Open `docs/docs/tools/improve.md`. Find the `persistent_comment` row (around line 304). Directly after its closing `</tr>`, insert:

```html
      <tr>
        <td><b>persistent_inline_comments</b></td>
        <td>Controls deduplication of inline code-suggestion comments across re-runs. Set to <code>"update"</code> (default) to edit a matching existing comment in place, <code>"skip"</code> to leave it untouched, or <code>"off"</code> to always post a new comment. Matching is done via a hidden content-hash marker embedded in the comment body.</td>
      </tr>
```

- [ ] **Step 2: Verify**

Run: `grep -n persistent_inline_comments docs/docs/tools/improve.md`
Expected: a single match showing the new row.

- [ ] **Step 3: Commit**

```bash
git add docs/docs/tools/improve.md
git commit -m "docs(improve): document persistent_inline_comments setting"
```

---

## Final verification (after all tasks)

Run the full unit-test suite one more time as a smoke check:

```bash
pytest tests/unittest/ -x
```

Expected: all tests pass, no regressions relative to the baseline before this feature.
