# Resolve Outdated Inline Comments — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a previously-posted inline suggestion is no longer emitted on a re-run of `/improve`, resolve its review thread and append a short explanatory note. Re-open the thread if the suggestion comes back.

**Architecture:** Extend the existing dedup pass in `publish_code_suggestions`. After the main loop emits comments, compute `outdated = existing_markers − emitted_markers` and act on each. GitHub: switch `get_bot_review_comments` from REST to GraphQL (one query exposes `thread_id` + `is_resolved` per comment) and use `resolveReviewThread` / `unresolveReviewThread` mutations. GitLab: extend the existing discussion walk with `is_resolved` and use `discussion.resolved = True/False; save()`. A second hidden marker (`<!-- pr-agent-inline-resolved -->`) in the appended note makes our prior auto-resolve recognizable so we respect manual user unresolves on subsequent runs.

**Tech Stack:** Python 3, PyGithub (REST + GraphQL via `_requester`), python-gitlab, pytest, dynaconf.

**Spec:** [docs/superpowers/specs/2026-04-21-resolve-outdated-inline-comments-design.md](../specs/2026-04-21-resolve-outdated-inline-comments-design.md)

---

## Task 0: Foundation — algo constants, base provider methods, config setting

**Goal:** Land the strictly additive scaffolding that subsequent provider tasks build on. No behavior change.

**Files:**
- Modify: `pr_agent/algo/inline_comments_dedup.py` (add two string constants near existing markers)
- Modify: `pr_agent/git_providers/git_provider.py:341-360` (add two new default methods)
- Modify: `pr_agent/settings/configuration.toml` (add `resolve_outdated_inline_comments = true` under `[pr_code_suggestions]`)
- Test: `tests/unittest/test_inline_comments_dedup_constants.py` (NEW — small constants smoke test)

**Acceptance Criteria:**
- [ ] `RESOLVED_NOTE` and `RESOLVED_BODY_MARKER` importable from `pr_agent.algo.inline_comments_dedup`.
- [ ] `RESOLVED_BODY_MARKER` is a single-line HTML comment that round-trips a substring check.
- [ ] `GitProvider.resolve_review_thread(comment)` and `GitProvider.unresolve_review_thread(comment)` both return `False` by default.
- [ ] `configuration.toml` declares `resolve_outdated_inline_comments = true`.

**Verify:** `uv run pytest tests/unittest/test_inline_comments_dedup_constants.py -v` → 3 passed; existing dedup tests unaffected: `uv run pytest tests/unittest/test_github_inline_dedup.py tests/unittest/test_gitlab_inline_dedup.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Add the two constants to the algo module**

In `pr_agent/algo/inline_comments_dedup.py`, immediately after the existing `MARKER_SUFFIX = " -->"` line (around line 18), insert:

```python
# Constants used by the resolve-outdated-inline-comments feature.
# RESOLVED_BODY_MARKER is appended (with RESOLVED_NOTE) to the body of an
# inline comment whose suggestion was not re-emitted on the current run.
# It also serves as an idempotency signal: if a user manually unresolves a
# thread we previously auto-resolved, the marker remains in the body and
# tells us not to re-resolve on subsequent runs.
RESOLVED_NOTE = "Resolved automatically: this suggestion was not re-emitted on the latest run."
RESOLVED_BODY_MARKER = "<!-- pr-agent-inline-resolved -->"
```

- [ ] **Step 2: Add the two default methods to the base GitProvider class**

In `pr_agent/git_providers/git_provider.py`, immediately after `edit_review_comment` (currently at line 353-360), insert:

```python
    def resolve_review_thread(self, comment: dict) -> bool:
        """
        Mark the review thread containing `comment` as resolved.

        `comment` is one of the dicts returned by get_bot_review_comments();
        providers extract whichever id (thread_id, discussion_id, etc.) they need.

        Returns True on success, False otherwise. Default: return False (unsupported),
        which causes the resolve-outdated pass to skip this comment.
        """
        return False

    def unresolve_review_thread(self, comment: dict) -> bool:
        """
        Mark the review thread containing `comment` as unresolved.

        Used when a previously auto-resolved suggestion is re-emitted on a later run.
        Returns True on success, False otherwise. Default: return False (unsupported).
        """
        return False
```

- [ ] **Step 3: Add the new setting to configuration.toml**

In `pr_agent/settings/configuration.toml`, locate the `[pr_code_suggestions]` section and find `persistent_inline_comments = "update"`. Immediately after that line, append:

```toml
# When a previously-posted inline suggestion is no longer emitted on a re-run,
# resolve its thread (and append a short note) so reviewers see only currently
# relevant comments. Has no effect when persistent_inline_comments = "off".
resolve_outdated_inline_comments = true
```

- [ ] **Step 4: Write the constants test (TDD red)**

Create `tests/unittest/test_inline_comments_dedup_constants.py`:

```python
"""Smoke tests for resolve-outdated constants and base GitProvider defaults."""

from pr_agent.algo.inline_comments_dedup import (
    RESOLVED_BODY_MARKER,
    RESOLVED_NOTE,
)


def test_resolved_marker_is_html_comment():
    assert RESOLVED_BODY_MARKER.startswith("<!--")
    assert RESOLVED_BODY_MARKER.endswith("-->")
    assert "\n" not in RESOLVED_BODY_MARKER


def test_resolved_marker_substring_check_round_trips():
    body = "some comment body\n\n---\n_" + RESOLVED_NOTE + "_\n" + RESOLVED_BODY_MARKER
    assert RESOLVED_BODY_MARKER in body


def test_base_provider_defaults_return_false():
    from pr_agent.git_providers.git_provider import GitProvider

    # GitProvider is abstract; verify defaults via the unbound methods.
    assert GitProvider.resolve_review_thread(None, {"id": 1}) is False
    assert GitProvider.unresolve_review_thread(None, {"id": 1}) is False
```

- [ ] **Step 5: Run the test (it should pass green; this isn't behavior under test, just contract pinning)**

Run: `uv run pytest tests/unittest/test_inline_comments_dedup_constants.py -v`
Expected: 3 passed.

- [ ] **Step 6: Confirm existing dedup tests still pass**

Run: `uv run pytest tests/unittest/test_github_inline_dedup.py tests/unittest/test_gitlab_inline_dedup.py -v`
Expected: all existing tests pass (Task 0 changes are purely additive).

- [ ] **Step 7: Commit**

```bash
git add pr_agent/algo/inline_comments_dedup.py pr_agent/git_providers/git_provider.py pr_agent/settings/configuration.toml tests/unittest/test_inline_comments_dedup_constants.py
git commit -m "feat(algo,providers): scaffolding for resolve-outdated inline comments"
```

---

## Task 1: GitHub — switch `get_bot_review_comments` to GraphQL + add resolve/unresolve methods

**Goal:** Replace the REST-backed `get_bot_review_comments` with a GraphQL query against `pullRequest.reviewThreads`, exposing `thread_id` and `is_resolved` per comment. Add `resolve_review_thread` / `unresolve_review_thread` using GraphQL mutations.

**Files:**
- Modify: `pr_agent/git_providers/github_provider.py:561-608` (replace `get_bot_review_comments`; insert two new methods after `edit_review_comment`)
- Modify: `tests/unittest/test_github_inline_dedup.py:156-203` (rewrite `TestGetBotReviewCommentsFiltering` to mock the GraphQL response shape; add new test class for resolve/unresolve)

**Acceptance Criteria:**
- [ ] `get_bot_review_comments` issues a GraphQL POST to `{base_url}/graphql` and returns dicts with keys `id, thread_id, body, path, line, start_line, is_resolved`.
- [ ] Pagination (`hasNextPage`/`endCursor`) is honored.
- [ ] Author-filtering preserves the existing app/user split (github_provider.py:578-584).
- [ ] On any GraphQL error or `data["errors"]` array, returns `[]` and logs a warning (no exception propagates).
- [ ] `resolve_review_thread(comment)` posts the `resolveReviewThread` mutation with `comment["thread_id"]` and returns `True` on success, `False` otherwise.
- [ ] `unresolve_review_thread(comment)` mirrors the above with `unresolveReviewThread`.

**Verify:** `uv run pytest tests/unittest/test_github_inline_dedup.py -v` → all existing tests still pass; new resolve/unresolve tests pass.

**Steps:**

- [ ] **Step 1: Rewrite `TestGetBotReviewCommentsFiltering` for the new GraphQL shape (TDD red)**

In `tests/unittest/test_github_inline_dedup.py`, **replace** the existing `TestGetBotReviewCommentsFiltering` class (lines 156-203) with:

```python
class TestGetBotReviewCommentsGraphQL:
    """Exercises the GraphQL-backed get_bot_review_comments."""

    def _make_provider(self, deployment_type, user_id=None):
        with patch("pr_agent.git_providers.github_provider.GithubProvider._get_repo"), \
             patch("pr_agent.git_providers.github_provider.GithubProvider.set_pr"), \
             patch("pr_agent.git_providers.github_provider.GithubProvider._get_pr"):
            from pr_agent.git_providers.github_provider import GithubProvider
            p = GithubProvider.__new__(GithubProvider)
            p.pr = MagicMock()
            p.pr.number = 42
            p.repo = "owner/repo"
            p.base_url = "https://api.github.com"
            p.deployment_type = deployment_type
            p.github_user_id = user_id
            return p

    def _gql_response(self, threads, has_next_page=False, end_cursor=None):
        return ({}, {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
                            "nodes": threads,
                        }
                    }
                }
            }
        })

    def _thread(self, thread_id, is_resolved, comments):
        return {"id": thread_id, "isResolved": is_resolved, "comments": {"nodes": comments}}

    def _comment(self, db_id, login, body="x", path="a.py", line=1, start_line=None):
        return {"databaseId": db_id, "body": body, "path": path, "line": line,
                "startLine": start_line, "author": {"login": login}}

    def test_app_deployment_filters_by_app_name(self):
        provider = self._make_provider(deployment_type="app")
        provider.pr._requester.requestJsonAndCheck = MagicMock(
            return_value=self._gql_response([
                self._thread("T1", False, [self._comment(1, "my-bot[bot]")]),
                self._thread("T2", False, [self._comment(2, "alice")]),
            ])
        )
        with patch("pr_agent.git_providers.github_provider.get_settings") as gs:
            gs.return_value.get = lambda key, default="": "my-bot" if key == "GITHUB.APP_NAME" else default
            out = provider.get_bot_review_comments()
        assert [c["id"] for c in out] == [1]
        assert out[0]["thread_id"] == "T1"
        assert out[0]["is_resolved"] is False

    def test_user_deployment_filters_by_user_id(self):
        provider = self._make_provider(deployment_type="user", user_id=None)
        provider.get_user_id = MagicMock(return_value="pr-agent-bot")
        provider.pr._requester.requestJsonAndCheck = MagicMock(
            return_value=self._gql_response([
                self._thread("T1", True, [self._comment(5, "pr-agent-bot")]),
                self._thread("T2", False, [self._comment(6, "someone-else")]),
            ])
        )
        with patch("pr_agent.git_providers.github_provider.get_settings") as gs:
            gs.return_value.get = lambda key, default="": default
            out = provider.get_bot_review_comments()
        assert [c["id"] for c in out] == [5]
        assert out[0]["is_resolved"] is True
        provider.get_user_id.assert_called_once()

    def test_paginates_until_has_next_page_false(self):
        provider = self._make_provider(deployment_type="user", user_id="pr-agent-bot")
        page1 = self._gql_response(
            [self._thread("T1", False, [self._comment(1, "pr-agent-bot")])],
            has_next_page=True, end_cursor="cur1",
        )
        page2 = self._gql_response(
            [self._thread("T2", False, [self._comment(2, "pr-agent-bot")])],
            has_next_page=False, end_cursor=None,
        )
        provider.pr._requester.requestJsonAndCheck = MagicMock(side_effect=[page1, page2])
        with patch("pr_agent.git_providers.github_provider.get_settings") as gs:
            gs.return_value.get = lambda key, default="": default
            out = provider.get_bot_review_comments()
        assert [c["id"] for c in out] == [1, 2]
        assert provider.pr._requester.requestJsonAndCheck.call_count == 2

    def test_graphql_errors_array_returns_empty(self):
        provider = self._make_provider(deployment_type="user", user_id="pr-agent-bot")
        provider.pr._requester.requestJsonAndCheck = MagicMock(
            return_value=({}, {"errors": [{"message": "boom"}]})
        )
        with patch("pr_agent.git_providers.github_provider.get_settings") as gs:
            gs.return_value.get = lambda key, default="": default
            out = provider.get_bot_review_comments()
        assert out == []

    def test_graphql_exception_returns_empty(self):
        provider = self._make_provider(deployment_type="user", user_id="pr-agent-bot")
        provider.pr._requester.requestJsonAndCheck = MagicMock(side_effect=RuntimeError("net"))
        with patch("pr_agent.git_providers.github_provider.get_settings") as gs:
            gs.return_value.get = lambda key, default="": default
            out = provider.get_bot_review_comments()
        assert out == []


class TestGitHubResolveUnresolve:
    """Exercises resolve_review_thread / unresolve_review_thread mutations."""

    def _make_provider(self):
        with patch("pr_agent.git_providers.github_provider.GithubProvider._get_repo"), \
             patch("pr_agent.git_providers.github_provider.GithubProvider.set_pr"), \
             patch("pr_agent.git_providers.github_provider.GithubProvider._get_pr"):
            from pr_agent.git_providers.github_provider import GithubProvider
            p = GithubProvider.__new__(GithubProvider)
            p.pr = MagicMock()
            p.base_url = "https://api.github.com"
            return p

    def test_resolve_calls_graphql_and_returns_true(self):
        p = self._make_provider()
        p.pr._requester.requestJsonAndCheck = MagicMock(
            return_value=({}, {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}})
        )
        assert p.resolve_review_thread({"thread_id": "T1"}) is True
        method, url = p.pr._requester.requestJsonAndCheck.call_args[0]
        assert method == "POST"
        assert url.endswith("/graphql")
        payload = p.pr._requester.requestJsonAndCheck.call_args.kwargs["input"]
        assert "resolveReviewThread" in payload["query"]
        assert payload["variables"] == {"threadId": "T1"}

    def test_unresolve_calls_graphql_and_returns_true(self):
        p = self._make_provider()
        p.pr._requester.requestJsonAndCheck = MagicMock(
            return_value=({}, {"data": {"unresolveReviewThread": {"thread": {"isResolved": False}}}})
        )
        assert p.unresolve_review_thread({"thread_id": "T1"}) is True
        payload = p.pr._requester.requestJsonAndCheck.call_args.kwargs["input"]
        assert "unresolveReviewThread" in payload["query"]
        assert payload["variables"] == {"threadId": "T1"}

    def test_resolve_returns_false_on_errors_array(self):
        p = self._make_provider()
        p.pr._requester.requestJsonAndCheck = MagicMock(
            return_value=({}, {"errors": [{"message": "perm denied"}]})
        )
        assert p.resolve_review_thread({"thread_id": "T1"}) is False

    def test_resolve_returns_false_on_exception(self):
        p = self._make_provider()
        p.pr._requester.requestJsonAndCheck = MagicMock(side_effect=RuntimeError("net"))
        assert p.resolve_review_thread({"thread_id": "T1"}) is False

    def test_resolve_returns_false_when_thread_id_missing(self):
        p = self._make_provider()
        p.pr._requester.requestJsonAndCheck = MagicMock()
        assert p.resolve_review_thread({"id": 5}) is False
        p.pr._requester.requestJsonAndCheck.assert_not_called()
```

- [ ] **Step 2: Run the new tests to verify they fail (TDD red)**

Run: `uv run pytest tests/unittest/test_github_inline_dedup.py::TestGetBotReviewCommentsGraphQL tests/unittest/test_github_inline_dedup.py::TestGitHubResolveUnresolve -v`
Expected: failures — `get_bot_review_comments` still uses REST; resolve/unresolve methods don't exist on `GithubProvider`.

- [ ] **Step 3: Replace `get_bot_review_comments` with the GraphQL implementation**

In `pr_agent/git_providers/github_provider.py`, replace the existing method (lines 561-595) with:

```python
    _BOT_REVIEW_COMMENTS_QUERY = """
    query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequest(number:$number) {
          reviewThreads(first:100, after:$cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              isResolved
              comments(first:100) {
                nodes {
                  databaseId
                  body
                  path
                  line
                  startLine
                  author { login }
                }
              }
            }
          }
        }
      }
    }
    """

    def get_bot_review_comments(self) -> list[dict]:
        """
        Return the bot's existing inline review comments on this PR.

        Uses GraphQL to expose per-thread resolution state and the thread node id
        (needed by resolve_review_thread / unresolve_review_thread). Filters by
        author to avoid matching human reviewers. Returns dicts with keys:
        id, thread_id, body, path, line, start_line, is_resolved.
        """
        try:
            our_app_name = (get_settings().get("GITHUB.APP_NAME", "") or "").lower()
            bot_user_id = (self.get_user_id() or "").lower() if self.deployment_type == "user" else ""
            owner, _, name = self.repo.partition("/")
            number = self.pr.number

            out: list[dict] = []
            cursor: str | None = None
            while True:
                _, data = self.pr._requester.requestJsonAndCheck(
                    "POST",
                    f"{self.base_url}/graphql",
                    input={
                        "query": self._BOT_REVIEW_COMMENTS_QUERY,
                        "variables": {"owner": owner, "name": name, "number": number, "cursor": cursor},
                    },
                )
                if not data or data.get("errors"):
                    get_logger().warning(
                        f"get_bot_review_comments GraphQL errors: {(data or {}).get('errors')}"
                    )
                    return []
                threads = (((data.get("data") or {}).get("repository") or {})
                           .get("pullRequest") or {}).get("reviewThreads") or {}
                page_info = threads.get("pageInfo") or {}
                for t in threads.get("nodes") or []:
                    thread_id = t.get("id")
                    is_resolved = bool(t.get("isResolved"))
                    for c in ((t.get("comments") or {}).get("nodes") or []):
                        login = ((c.get("author") or {}).get("login") or "").lower()
                        same_author = False
                        if self.deployment_type == "app":
                            same_author = bool(our_app_name) and our_app_name in login
                        elif self.deployment_type == "user":
                            same_author = bool(bot_user_id) and login == bot_user_id
                        if not same_author:
                            continue
                        out.append({
                            "id": c.get("databaseId"),
                            "thread_id": thread_id,
                            "body": c.get("body") or "",
                            "path": c.get("path"),
                            "line": c.get("line"),
                            "start_line": c.get("startLine"),
                            "is_resolved": is_resolved,
                        })
                if not page_info.get("hasNextPage"):
                    break
                cursor = page_info.get("endCursor")
            return out
        except Exception as e:
            get_logger().warning(f"Failed to list GitHub review comments via GraphQL: {e}")
            return []
```

- [ ] **Step 4: Add `resolve_review_thread` and `unresolve_review_thread` after `edit_review_comment`**

In `pr_agent/git_providers/github_provider.py`, immediately after `edit_review_comment` (currently ends at line 608), insert:

```python
    _RESOLVE_THREAD_MUTATION = """
    mutation($threadId:ID!) {
      resolveReviewThread(input:{threadId:$threadId}) { thread { isResolved } }
    }
    """

    _UNRESOLVE_THREAD_MUTATION = """
    mutation($threadId:ID!) {
      unresolveReviewThread(input:{threadId:$threadId}) { thread { isResolved } }
    }
    """

    def _run_thread_mutation(self, query: str, comment: dict) -> bool:
        thread_id = comment.get("thread_id")
        if not thread_id:
            return False
        try:
            _, data = self.pr._requester.requestJsonAndCheck(
                "POST",
                f"{self.base_url}/graphql",
                input={"query": query, "variables": {"threadId": thread_id}},
            )
            if not data or data.get("errors"):
                get_logger().warning(
                    f"GitHub thread mutation errors for {thread_id}: {(data or {}).get('errors')}"
                )
                return False
            return True
        except Exception as e:
            get_logger().warning(f"GitHub thread mutation failed for {thread_id}: {e}")
            return False

    def resolve_review_thread(self, comment: dict) -> bool:
        return self._run_thread_mutation(self._RESOLVE_THREAD_MUTATION, comment)

    def unresolve_review_thread(self, comment: dict) -> bool:
        return self._run_thread_mutation(self._UNRESOLVE_THREAD_MUTATION, comment)
```

- [ ] **Step 5: Run the new tests to verify green**

Run: `uv run pytest tests/unittest/test_github_inline_dedup.py::TestGetBotReviewCommentsGraphQL tests/unittest/test_github_inline_dedup.py::TestGitHubResolveUnresolve -v`
Expected: all pass.

- [ ] **Step 6: Run the full GitHub dedup suite to confirm no regression**

Run: `uv run pytest tests/unittest/test_github_inline_dedup.py -v`
Expected: all tests pass. (The pre-existing dedup tests under `TestOffMode`, `TestUpdateMode`, `TestSkipMode` only mock `provider.get_bot_review_comments` directly via `MagicMock`, so they don't depend on the REST-vs-GraphQL transport.)

- [ ] **Step 7: Commit**

```bash
git add pr_agent/git_providers/github_provider.py tests/unittest/test_github_inline_dedup.py
git commit -m "refactor(github): get_bot_review_comments via GraphQL; add resolve/unresolve_review_thread"
```

---

## Task 2: GitHub — wire outdated pass into `publish_code_suggestions`

**Goal:** After the dedup loop, resolve threads whose marker hash is in the existing index but not in the current run's emitted set. Re-open threads when a previously-resolved suggestion comes back.

**Files:**
- Modify: `pr_agent/git_providers/github_provider.py:610-695` (extend `publish_code_suggestions`)
- Modify: `tests/unittest/test_github_inline_dedup.py` (extend `_set_mode` helper to allow per-key values; add `TestOutdatedPass` class)

**Acceptance Criteria:**
- [ ] When the existing index contains hash A and the current run does not emit A, `resolve_review_thread` is called once for A's comment, then `edit_review_comment` is called once with the body suffixed by `RESOLVED_NOTE` and `RESOLVED_BODY_MARKER`.
- [ ] If A's existing comment already has `is_resolved=True`, neither resolve nor edit is called.
- [ ] If A's existing comment body contains `RESOLVED_BODY_MARKER`, neither resolve nor edit is called.
- [ ] When a current-run suggestion matches an existing comment that has `is_resolved=True`, `unresolve_review_thread` is called after the dedup edit succeeds.
- [ ] When `resolve_outdated_inline_comments = false`, the outdated pass is entirely skipped.
- [ ] When `persistent_inline_comments = "off"`, the outdated pass is not entered.
- [ ] If `resolve_review_thread` returns `False`, `edit_review_comment` is **not** called for the resolution note for that comment.
- [ ] No exception propagates from any failure mode.

**Verify:** `uv run pytest tests/unittest/test_github_inline_dedup.py -v` → all tests pass including new `TestOutdatedPass`.

**Steps:**

- [ ] **Step 1: Extend the test settings helper**

In `tests/unittest/test_github_inline_dedup.py`, **replace** the existing `_set_mode` function (lines 49-56) with a more flexible version:

```python
def _set_settings(persistent_mode="update", resolve_outdated=True):
    """Patch get_settings to return both persistent_inline_comments and resolve_outdated_inline_comments."""
    values = {
        "persistent_inline_comments": persistent_mode,
        "resolve_outdated_inline_comments": resolve_outdated,
    }
    return patch(
        "pr_agent.git_providers.github_provider.get_settings",
        return_value=MagicMock(
            pr_code_suggestions=MagicMock(get=lambda key, default=None: values.get(key, default)),
        ),
    )


# Backward-compat wrapper for existing tests that only set persistent mode.
def _set_mode(mode):
    return _set_settings(persistent_mode=mode, resolve_outdated=False)
```

(The existing `_set_mode(mode)` callers in `TestOffMode`, `TestUpdateMode`, `TestSkipMode` keep working unchanged because we default `resolve_outdated=False` for them — those tests must not exercise the new outdated pass.)

- [ ] **Step 2: Add the new `TestOutdatedPass` class to the test file (TDD red)**

At the bottom of `tests/unittest/test_github_inline_dedup.py`, append:

```python
from pr_agent.algo.inline_comments_dedup import RESOLVED_BODY_MARKER, RESOLVED_NOTE


def _existing(c_id, marker, *, is_resolved=False, body_extra="", path="src/app.py", thread_id=None):
    return {
        "id": c_id,
        "thread_id": thread_id or f"T{c_id}",
        "body": "old body" + body_extra + "\n\n" + marker,
        "path": path,
        "line": 12,
        "start_line": 10,
        "is_resolved": is_resolved,
    }


class TestOutdatedPass:
    def test_outdated_marker_resolves_and_edits(self, provider):
        s_emitted = _sug(content="A new and different suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="An old suggestion that's no longer flagged")["original_suggestion"])
        existing = _existing(c_id=777, marker=marker_outdated)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock(return_value=True)
        provider.resolve_review_thread = MagicMock(return_value=True)
        provider.unresolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_called_once()
        called_comment = provider.resolve_review_thread.call_args[0][0]
        assert called_comment["id"] == 777
        # edit_review_comment called once for the resolution note
        provider.edit_review_comment.assert_called_once()
        called_id, called_body = provider.edit_review_comment.call_args[0]
        assert called_id == 777
        assert RESOLVED_NOTE in called_body
        assert RESOLVED_BODY_MARKER in called_body
        provider.pr.create_review.assert_called_once()  # for the new suggestion

    def test_already_resolved_is_skipped(self, provider):
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _existing(c_id=778, marker=marker_outdated, is_resolved=True)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock()
        provider.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_not_called()
        # edit_review_comment must not be called for the outdated comment;
        # there are no matched existing comments either, so total calls == 0.
        provider.edit_review_comment.assert_not_called()

    def test_body_marker_signals_user_unresolved_skip(self, provider):
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _existing(
            c_id=779, marker=marker_outdated, is_resolved=False,
            body_extra=f"\n\n---\n_{RESOLVED_NOTE}_\n{RESOLVED_BODY_MARKER}",
        )
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock()
        provider.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_not_called()
        provider.edit_review_comment.assert_not_called()

    def test_re_emit_after_prior_resolve_calls_unresolve(self, provider):
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        existing = _existing(c_id=780, marker=marker, is_resolved=True)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock(return_value=True)
        provider.resolve_review_thread = MagicMock()
        provider.unresolve_review_thread = MagicMock(return_value=True)
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            provider.publish_code_suggestions([s])
        provider.edit_review_comment.assert_called_once()
        provider.unresolve_review_thread.assert_called_once()
        provider.resolve_review_thread.assert_not_called()
        provider.pr.create_review.assert_not_called()

    def test_setting_off_skips_outdated_pass(self, provider):
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _existing(c_id=781, marker=marker_outdated)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock()
        provider.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=False):
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_not_called()
        provider.edit_review_comment.assert_not_called()

    def test_persistent_off_skips_outdated_pass_even_when_setting_on(self, provider):
        s = _sug()
        provider.get_bot_review_comments = MagicMock()
        provider.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="off", resolve_outdated=True):
            provider.publish_code_suggestions([s])
        provider.get_bot_review_comments.assert_not_called()
        provider.resolve_review_thread.assert_not_called()

    def test_resolve_failure_skips_edit(self, provider):
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _existing(c_id=782, marker=marker_outdated)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock()
        provider.resolve_review_thread = MagicMock(return_value=False)
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_called_once()
        # The resolution-note edit must not happen for this outdated comment.
        # No other edit calls expected (the emitted suggestion creates new).
        provider.edit_review_comment.assert_not_called()

    def test_edit_failure_after_resolve_does_not_raise(self, provider):
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _existing(c_id=783, marker=marker_outdated)
        provider.get_bot_review_comments = MagicMock(return_value=[existing])
        provider.edit_review_comment = MagicMock(return_value=False)
        provider.resolve_review_thread = MagicMock(return_value=True)
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            # Should not raise.
            provider.publish_code_suggestions([s_emitted])
        provider.resolve_review_thread.assert_called_once()
        provider.edit_review_comment.assert_called_once()
```

- [ ] **Step 3: Run the new tests to confirm they fail**

Run: `uv run pytest tests/unittest/test_github_inline_dedup.py::TestOutdatedPass -v`
Expected: failures — outdated pass and unresolve-on-re-emit don't exist yet.

- [ ] **Step 4: Modify `publish_code_suggestions` in `pr_agent/git_providers/github_provider.py`**

In `pr_agent/git_providers/github_provider.py`:

(a) At the top of the file, find the existing import block from `inline_comments_dedup` (around line 25-29) and add `RESOLVED_BODY_MARKER` and `RESOLVED_NOTE`:

```python
from pr_agent.algo.inline_comments_dedup import (
    MARKER_PREFIX,
    MARKER_SUFFIX,
    PERSISTENT_MODE_OFF,
    PERSISTENT_MODE_SKIP,
    RESOLVED_BODY_MARKER,
    RESOLVED_NOTE,
    append_marker,
    build_marker_index,
    generate_marker,
    normalize_persistent_mode,
)
```

(b) In `publish_code_suggestions` (line 610), modify the body. The two changes are: track `emitted` hashes in the loop, and add an outdated pass after the loop.

Replace lines 619-695 (from `code_suggestions_validated = ...` through the end of the method) with:

```python
        code_suggestions_validated = self.validate_comments_inside_hunks(code_suggestions)

        mode = normalize_persistent_mode(
            get_settings().pr_code_suggestions.get("persistent_inline_comments", PERSISTENT_MODE_OFF)
        )
        resolve_outdated = bool(
            get_settings().pr_code_suggestions.get("resolve_outdated_inline_comments", True)
        )

        existing_index: dict[str, dict] = {}
        if mode != PERSISTENT_MODE_OFF:
            try:
                existing_index = build_marker_index(self.get_bot_review_comments())
            except Exception as e:
                get_logger().warning(f"persistent_inline_comments: fetch failed, falling back to create-new: {e}")
                existing_index = {}

        emitted_hashes: set[str] = set()
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
                    emitted_hashes.add(marker_hash)
                    existing = existing_index.get(marker_hash)
                    if existing is not None:
                        if mode == PERSISTENT_MODE_SKIP:
                            get_logger().info(
                                f"persistent_inline_comments=skip: existing comment {existing.get('id')} "
                                f"on {relevant_file}; not re-posting")
                            continue
                        # mode == update
                        if self.edit_review_comment(existing.get("id"), body):
                            # If we previously auto-resolved this thread but the
                            # suggestion is back, unresolve it.
                            if resolve_outdated and existing.get("is_resolved"):
                                self.unresolve_review_thread(existing)
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

        # ---- Outdated pass: resolve threads whose marker is no longer emitted ----
        if mode != PERSISTENT_MODE_OFF and resolve_outdated:
            for h, c in existing_index.items():
                if h in emitted_hashes:
                    continue
                if c.get("is_resolved"):
                    continue
                if RESOLVED_BODY_MARKER in (c.get("body") or ""):
                    continue
                if not self.resolve_review_thread(c):
                    continue
                new_body = (
                    (c.get("body") or "").rstrip()
                    + f"\n\n---\n_{RESOLVED_NOTE}_\n{RESOLVED_BODY_MARKER}"
                )
                self.edit_review_comment(c.get("id"), new_body)

        if not post_parameters_list:
            return True

        try:
            self.publish_inline_comments(post_parameters_list)
            return True
        except Exception as e:
            get_logger().error(f"Failed to publish code suggestion, error: {e}")
            return False
```

- [ ] **Step 5: Run the new tests to confirm green**

Run: `uv run pytest tests/unittest/test_github_inline_dedup.py::TestOutdatedPass -v`
Expected: all 8 tests pass.

- [ ] **Step 6: Run the entire GitHub dedup suite to confirm no regression**

Run: `uv run pytest tests/unittest/test_github_inline_dedup.py -v`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add pr_agent/git_providers/github_provider.py tests/unittest/test_github_inline_dedup.py
git commit -m "feat(github): outdated-comment resolve pass with re-emit unresolve"
```

---

## Task 3: GitLab — extend `get_bot_review_comments` with `is_resolved` + add resolve/unresolve methods

**Goal:** Add `is_resolved` to the per-note dict, and implement `resolve_review_thread` / `unresolve_review_thread` via `discussion.resolved = True/False; save()`.

**Files:**
- Modify: `pr_agent/git_providers/gitlab_provider.py:667-732` (extend `get_bot_review_comments`; insert two new methods)
- Modify: `tests/unittest/test_gitlab_inline_dedup.py` (extend existing `get_bot_review_comments` tests if any; add `TestGitLabResolveUnresolve`)

**Acceptance Criteria:**
- [ ] `get_bot_review_comments` includes `is_resolved` in every dict, derived from `discussion.attributes.get("resolved")` (or `False` when missing).
- [ ] `resolve_review_thread(comment)` calls `mr.discussions.get(comment["thread_id"])`, sets `resolved=True`, calls `save()`, returns `True`.
- [ ] When `getattr(d, "resolvable", True)` is `False`, `resolve_review_thread` returns `False` without calling `save()`.
- [ ] `unresolve_review_thread(comment)` mirrors with `resolved=False`.
- [ ] Both return `False` on any exception, with a warning logged.

**Verify:** `uv run pytest tests/unittest/test_gitlab_inline_dedup.py -v` → all tests pass.

**Note:** GitLab's existing `get_bot_review_comments` (gitlab_provider.py:704-711) already returns `discussion_id` per note. Task 0 added the abstract method named `resolve_review_thread(comment)` which takes the **whole dict** — we use `comment["thread_id"]`. We need to update GitLab's dict to also use the key `thread_id` for symmetry with GitHub, but keep `discussion_id` as an alias to avoid breaking callers that may already use it.

**Steps:**

- [ ] **Step 1: Write the new tests (TDD red)**

In `tests/unittest/test_gitlab_inline_dedup.py`, append:

```python
class TestGitLabResolveUnresolve:
    def _provider(self):
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.mr = MagicMock()
        return p

    def test_resolve_calls_save_with_resolved_true(self):
        p = self._provider()
        d = MagicMock()
        d.resolvable = True
        d.resolved = False
        p.mr.discussions.get = MagicMock(return_value=d)
        assert p.resolve_review_thread({"thread_id": "DIS123"}) is True
        p.mr.discussions.get.assert_called_once_with("DIS123")
        assert d.resolved is True
        d.save.assert_called_once()

    def test_unresolve_calls_save_with_resolved_false(self):
        p = self._provider()
        d = MagicMock()
        d.resolvable = True
        d.resolved = True
        p.mr.discussions.get = MagicMock(return_value=d)
        assert p.unresolve_review_thread({"thread_id": "DIS123"}) is True
        assert d.resolved is False
        d.save.assert_called_once()

    def test_non_resolvable_discussion_returns_false(self):
        p = self._provider()
        d = MagicMock()
        d.resolvable = False
        p.mr.discussions.get = MagicMock(return_value=d)
        assert p.resolve_review_thread({"thread_id": "DIS123"}) is False
        d.save.assert_not_called()

    def test_resolve_returns_false_on_exception(self):
        p = self._provider()
        p.mr.discussions.get = MagicMock(side_effect=RuntimeError("api down"))
        assert p.resolve_review_thread({"thread_id": "DIS123"}) is False

    def test_resolve_returns_false_when_thread_id_missing(self):
        p = self._provider()
        p.mr.discussions.get = MagicMock()
        assert p.resolve_review_thread({"id": 1}) is False
        p.mr.discussions.get.assert_not_called()


class TestGetBotReviewCommentsIncludesIsResolved:
    def test_is_resolved_propagates_from_discussion(self):
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.gl = MagicMock()
        p.gl.user.username = "pr-agent-bot"
        d_resolved = MagicMock()
        d_resolved.id = "D-1"
        d_resolved.attributes = {
            "notes": [{
                "type": "DiffNote",
                "id": 1,
                "body": "x",
                "author": {"username": "pr-agent-bot"},
                "position": {"new_path": "a.py", "new_line": 5},
                "resolved": True,
            }]
        }
        d_unresolved = MagicMock()
        d_unresolved.id = "D-2"
        d_unresolved.attributes = {
            "notes": [{
                "type": "DiffNote",
                "id": 2,
                "body": "y",
                "author": {"username": "pr-agent-bot"},
                "position": {"new_path": "b.py", "new_line": 6},
                "resolved": False,
            }]
        }
        p.mr = MagicMock()
        p.mr.discussions.list = MagicMock(return_value=[d_resolved, d_unresolved])
        out = p.get_bot_review_comments()
        ids_to_resolved = {c["id"]: c["is_resolved"] for c in out}
        assert ids_to_resolved == {1: True, 2: False}
        assert all("thread_id" in c for c in out)
```

- [ ] **Step 2: Run the new tests to confirm they fail**

Run: `uv run pytest tests/unittest/test_gitlab_inline_dedup.py::TestGitLabResolveUnresolve tests/unittest/test_gitlab_inline_dedup.py::TestGetBotReviewCommentsIncludesIsResolved -v`
Expected: failures — methods missing, `is_resolved` not in returned dicts.

- [ ] **Step 3: Modify `get_bot_review_comments` in `pr_agent/git_providers/gitlab_provider.py`**

Locate the `out.append({...})` block (lines 704-711) and replace with:

```python
                    out.append({
                        "id": note.get("id"),
                        "thread_id": discussion.id,
                        "discussion_id": discussion.id,  # back-compat alias
                        "body": note.get("body") or "",
                        "path": position.get("new_path"),
                        "line": position.get("new_line"),
                        "start_line": start_line,
                        "is_resolved": bool(
                            (discussion.attributes.get("notes") or [{}])[0].get("resolved", False)
                        ),
                    })
```

**Note on the resolution source:** GitLab attaches `resolved` to each note in the discussion's notes list (not on the discussion itself in the REST shape we're walking). All notes in a resolvable discussion share the same resolved state, so reading the first note is sufficient. If the notes list is empty (defensive), `is_resolved` defaults to `False`.

- [ ] **Step 4: Add `resolve_review_thread` and `unresolve_review_thread` after `edit_review_comment`**

In `pr_agent/git_providers/gitlab_provider.py`, immediately after `edit_review_comment` (currently ends at line 732), insert:

```python
    def _set_discussion_resolved(self, comment: dict, resolved: bool) -> bool:
        thread_id = comment.get("thread_id") or comment.get("discussion_id")
        if not thread_id:
            return False
        try:
            d = self.mr.discussions.get(thread_id)
            if not getattr(d, "resolvable", True):
                return False
            d.resolved = resolved
            d.save()
            return True
        except Exception as e:
            get_logger().warning(f"GitLab set-resolved={resolved} failed for {thread_id}: {e}")
            return False

    def resolve_review_thread(self, comment: dict) -> bool:
        return self._set_discussion_resolved(comment, True)

    def unresolve_review_thread(self, comment: dict) -> bool:
        return self._set_discussion_resolved(comment, False)
```

- [ ] **Step 5: Run the new tests to confirm green**

Run: `uv run pytest tests/unittest/test_gitlab_inline_dedup.py::TestGitLabResolveUnresolve tests/unittest/test_gitlab_inline_dedup.py::TestGetBotReviewCommentsIncludesIsResolved -v`
Expected: all pass.

- [ ] **Step 6: Run the full GitLab dedup suite to confirm no regression**

Run: `uv run pytest tests/unittest/test_gitlab_inline_dedup.py -v`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add pr_agent/git_providers/gitlab_provider.py tests/unittest/test_gitlab_inline_dedup.py
git commit -m "feat(gitlab): is_resolved on bot comments; add resolve/unresolve_review_thread"
```

---

## Task 4: GitLab — wire outdated pass into `publish_code_suggestions`

**Goal:** Mirror Task 2 for GitLab: track emitted hashes, add the outdated pass after the dedup loop, unresolve on re-emit.

**Files:**
- Modify: `pr_agent/git_providers/gitlab_provider.py:734-779` (extend `publish_code_suggestions`)
- Modify: `tests/unittest/test_gitlab_inline_dedup.py` (extend test settings helper; add `TestGitLabOutdatedPass`)

**Acceptance Criteria:**
Same matrix as Task 2, adapted to GitLab seams:
- [ ] outdated → discussion fetched, `resolved=True` set, `save()` called, body edited via `note.save()`.
- [ ] already-resolved → no `save()` call.
- [ ] body-marker → no `save()` call.
- [ ] re-emit after prior resolve → `discussion.resolved=False` + `save()` called.
- [ ] `resolve_outdated_inline_comments=false` → outdated pass entirely skipped.
- [ ] `persistent_inline_comments="off"` → outdated pass not entered.
- [ ] resolve failure → no edit-note for that comment.
- [ ] No exception propagates.

**Verify:** `uv run pytest tests/unittest/test_gitlab_inline_dedup.py -v` → all tests pass.

**Steps:**

- [ ] **Step 1: Extend the test settings helper**

In `tests/unittest/test_gitlab_inline_dedup.py`, **replace** the existing `_set_mode` (lines 44-51) with:

```python
def _set_settings(persistent_mode="update", resolve_outdated=True):
    values = {
        "persistent_inline_comments": persistent_mode,
        "resolve_outdated_inline_comments": resolve_outdated,
    }
    return patch(
        "pr_agent.git_providers.gitlab_provider.get_settings",
        return_value=MagicMock(
            pr_code_suggestions=MagicMock(get=lambda key, default=None: values.get(key, default)),
        ),
    )


def _set_mode(mode):
    return _set_settings(persistent_mode=mode, resolve_outdated=False)
```

- [ ] **Step 2: Add `TestGitLabOutdatedPass` (TDD red)**

Append to `tests/unittest/test_gitlab_inline_dedup.py`:

```python
from pr_agent.algo.inline_comments_dedup import RESOLVED_BODY_MARKER, RESOLVED_NOTE


def _gl_existing(c_id, marker, *, is_resolved=False, body_extra="", thread_id=None):
    return {
        "id": c_id,
        "thread_id": thread_id or f"DIS-{c_id}",
        "discussion_id": thread_id or f"DIS-{c_id}",
        "body": "old body" + body_extra + "\n\n" + marker,
        "path": "src/app.py",
        "line": 12,
        "start_line": 10,
        "is_resolved": is_resolved,
    }


class TestGitLabOutdatedPass:
    def _provider(self):
        p = _make_provider()
        p.send_inline_comment = MagicMock()
        p.get_diff_files = MagicMock(return_value=[])
        return p

    def test_outdated_marker_resolves_and_edits(self):
        p = self._provider()
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _gl_existing(c_id=10, marker=marker_outdated)
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock(return_value=True)
        p.resolve_review_thread = MagicMock(return_value=True)
        p.unresolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            p.publish_code_suggestions([s_emitted])
        p.resolve_review_thread.assert_called_once()
        p.edit_review_comment.assert_called_once()
        called_id, called_body = p.edit_review_comment.call_args[0]
        assert called_id == 10
        assert RESOLVED_NOTE in called_body
        assert RESOLVED_BODY_MARKER in called_body

    def test_already_resolved_is_skipped(self):
        p = self._provider()
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _gl_existing(c_id=11, marker=marker_outdated, is_resolved=True)
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock()
        p.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            p.publish_code_suggestions([s_emitted])
        p.resolve_review_thread.assert_not_called()
        p.edit_review_comment.assert_not_called()

    def test_body_marker_skips_resolve(self):
        p = self._provider()
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _gl_existing(
            c_id=12, marker=marker_outdated, is_resolved=False,
            body_extra=f"\n\n---\n_{RESOLVED_NOTE}_\n{RESOLVED_BODY_MARKER}",
        )
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock()
        p.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            p.publish_code_suggestions([s_emitted])
        p.resolve_review_thread.assert_not_called()
        p.edit_review_comment.assert_not_called()

    def test_re_emit_after_prior_resolve_calls_unresolve(self):
        p = self._provider()
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        existing = _gl_existing(c_id=13, marker=marker, is_resolved=True)
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock(return_value=True)
        p.resolve_review_thread = MagicMock()
        p.unresolve_review_thread = MagicMock(return_value=True)
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            p.publish_code_suggestions([s])
        p.edit_review_comment.assert_called_once()
        p.unresolve_review_thread.assert_called_once()
        p.resolve_review_thread.assert_not_called()

    def test_setting_off_skips_outdated_pass(self):
        p = self._provider()
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _gl_existing(c_id=14, marker=marker_outdated)
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock()
        p.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="update", resolve_outdated=False):
            p.publish_code_suggestions([s_emitted])
        p.resolve_review_thread.assert_not_called()
        p.edit_review_comment.assert_not_called()

    def test_persistent_off_skips_outdated_pass(self):
        p = self._provider()
        s = _sug()
        p.get_bot_review_comments = MagicMock()
        p.resolve_review_thread = MagicMock()
        with _set_settings(persistent_mode="off", resolve_outdated=True):
            p.publish_code_suggestions([s])
        p.get_bot_review_comments.assert_not_called()
        p.resolve_review_thread.assert_not_called()

    def test_resolve_failure_skips_edit(self):
        p = self._provider()
        s_emitted = _sug(content="A new suggestion", start=40, end=42)
        marker_outdated = generate_marker(_sug(content="Old")["original_suggestion"])
        existing = _gl_existing(c_id=15, marker=marker_outdated)
        p.get_bot_review_comments = MagicMock(return_value=[existing])
        p.edit_review_comment = MagicMock()
        p.resolve_review_thread = MagicMock(return_value=False)
        with _set_settings(persistent_mode="update", resolve_outdated=True):
            p.publish_code_suggestions([s_emitted])
        p.resolve_review_thread.assert_called_once()
        p.edit_review_comment.assert_not_called()
```

- [ ] **Step 3: Run the new tests to confirm they fail**

Run: `uv run pytest tests/unittest/test_gitlab_inline_dedup.py::TestGitLabOutdatedPass -v`
Expected: failures — outdated pass and unresolve-on-re-emit don't exist yet for GitLab.

- [ ] **Step 4: Modify `publish_code_suggestions` in `pr_agent/git_providers/gitlab_provider.py`**

(a) At the top of the file, find the existing import block from `inline_comments_dedup` and add the two new constants:

```python
from pr_agent.algo.inline_comments_dedup import (
    MARKER_PREFIX,
    MARKER_SUFFIX,
    PERSISTENT_MODE_OFF,
    PERSISTENT_MODE_SKIP,
    RESOLVED_BODY_MARKER,
    RESOLVED_NOTE,
    append_marker,
    build_marker_index,
    generate_marker,
    normalize_persistent_mode,
)
```

(b) In `publish_code_suggestions` (line 734), modify to track `emitted_hashes`, unresolve on re-emit, and add an outdated pass at the end of the function. Replace the entire function body (lines 735-end-of-function) with:

```python
        mode = normalize_persistent_mode(
            get_settings().pr_code_suggestions.get("persistent_inline_comments", PERSISTENT_MODE_OFF)
        )
        resolve_outdated = bool(
            get_settings().pr_code_suggestions.get("resolve_outdated_inline_comments", True)
        )

        existing_index: dict[str, dict] = {}
        if mode != PERSISTENT_MODE_OFF:
            try:
                existing_index = build_marker_index(self.get_bot_review_comments())
            except Exception as e:
                get_logger().warning(
                    f"persistent_inline_comments: fetch failed, falling back to create-new: {e}"
                )
                existing_index = {}

        emitted_hashes: set[str] = set()

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
                    marker = generate_marker(suggestion.get("original_suggestion") or {})
                    if marker:
                        body = append_marker(body, marker)
                        marker_hash = marker[len(MARKER_PREFIX):-len(MARKER_SUFFIX)]
                        emitted_hashes.add(marker_hash)
                        existing = existing_index.get(marker_hash)
                        if existing is not None:
                            if mode == PERSISTENT_MODE_SKIP:
                                get_logger().info(
                                    f"persistent_inline_comments=skip: existing comment {existing.get('id')} "
                                    f"on {relevant_file}; not re-posting"
                                )
                                continue
                            # mode == update
                            if self.edit_review_comment(existing.get("id"), body):
                                if resolve_outdated and existing.get("is_resolved"):
                                    self.unresolve_review_thread(existing)
                                continue
                            get_logger().info(
                                f"persistent_inline_comments=update: edit failed for {existing.get('id')}; "
                                f"falling back to create-new"
                            )

                # ... (the rest of the existing per-suggestion publishing logic stays unchanged) ...
```

**Important: preserve everything from the existing per-suggestion publishing logic onward.** The block starting with `diff_files = self.get_diff_files()` (currently around line 781) through the existing `try/except` for `send_inline_comment` is unchanged.

After the `for suggestion in code_suggestions:` loop terminates (and before the function returns), insert the outdated pass:

```python
        # ---- Outdated pass: resolve threads whose marker is no longer emitted ----
        if mode != PERSISTENT_MODE_OFF and resolve_outdated:
            for h, c in existing_index.items():
                if h in emitted_hashes:
                    continue
                if c.get("is_resolved"):
                    continue
                if RESOLVED_BODY_MARKER in (c.get("body") or ""):
                    continue
                if not self.resolve_review_thread(c):
                    continue
                new_body = (
                    (c.get("body") or "").rstrip()
                    + f"\n\n---\n_{RESOLVED_NOTE}_\n{RESOLVED_BODY_MARKER}"
                )
                self.edit_review_comment(c.get("id"), new_body)
```

If the existing function returns a value (it doesn't appear to — verify by reading the current end of `publish_code_suggestions`), keep that return statement after the outdated pass.

- [ ] **Step 5: Run the new tests to confirm green**

Run: `uv run pytest tests/unittest/test_gitlab_inline_dedup.py::TestGitLabOutdatedPass -v`
Expected: all 7 tests pass.

- [ ] **Step 6: Run the full GitLab dedup suite to confirm no regression**

Run: `uv run pytest tests/unittest/test_gitlab_inline_dedup.py -v`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add pr_agent/git_providers/gitlab_provider.py tests/unittest/test_gitlab_inline_dedup.py
git commit -m "feat(gitlab): outdated-comment resolve pass with re-emit unresolve"
```

---

## Task 5: Documentation

**Goal:** Document the new setting next to `persistent_inline_comments` in the user-facing tools doc.

**Files:**
- Modify: `docs/docs/tools/improve.md` (add a row to the existing settings table immediately after `persistent_inline_comments`)

**Acceptance Criteria:**
- [ ] `docs/docs/tools/improve.md` documents `resolve_outdated_inline_comments`, its default (`true`), the dependency on `persistent_inline_comments != "off"`, and the manual-unresolve opt-out.

**Verify:** `grep -n resolve_outdated_inline_comments docs/docs/tools/improve.md` → match found.

**Steps:**

- [ ] **Step 1: Find the existing `persistent_inline_comments` row**

Run: `grep -n persistent_inline_comments docs/docs/tools/improve.md`
Inspect the surrounding markdown table. There is an existing row documenting `persistent_inline_comments` (added in commit `43f42d3b`). The new row goes immediately after it, with the same column structure.

- [ ] **Step 2: Insert the new documentation row**

In `docs/docs/tools/improve.md`, immediately after the existing `persistent_inline_comments` row, add a new row of the same shape. The cell content:

> `resolve_outdated_inline_comments` (default `true`) — When dedup is enabled (`persistent_inline_comments != "off"`), automatically resolve inline-comment threads whose suggestion was not re-emitted on the latest run. The thread body gets a short auto-resolve note. Has no effect when `persistent_inline_comments = "off"`. Reviewers can manually unresolve to opt that thread out of future auto-resolution; the bot detects the prior resolution marker in the body and respects it.

(If the existing `persistent_inline_comments` documentation uses prose rather than a table row, mirror that structure instead — add a parallel paragraph immediately after.)

- [ ] **Step 3: Verify the doc change**

Run: `grep -n resolve_outdated_inline_comments docs/docs/tools/improve.md`
Expected: at least one match.

- [ ] **Step 4: Commit**

```bash
git add docs/docs/tools/improve.md
git commit -m "docs(improve): document resolve_outdated_inline_comments setting"
```

---

## Final verification

After all five tasks land, run the full inline-dedup suite end-to-end:

```bash
uv run pytest tests/unittest/test_inline_comments_dedup_constants.py \
       tests/unittest/test_github_inline_dedup.py \
       tests/unittest/test_gitlab_inline_dedup.py -v
```

Expected: all tests pass, including the new `TestOutdatedPass` (GitHub) and `TestGitLabOutdatedPass` (GitLab) classes.
