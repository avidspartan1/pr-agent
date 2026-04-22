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
        called_id, called_body = provider.edit_review_comment.call_args[0]
        assert called_id == 777
        assert marker in called_body
        assert s["body"] in called_body
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
