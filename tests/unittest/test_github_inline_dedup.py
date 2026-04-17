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


class TestGetBotReviewCommentsFiltering:
    """Exercises the real get_bot_review_comments filter — not the mocked-out fixture one."""

    def _make_provider_with_raw_comments(self, raw, deployment_type, user_id=None, app_name=""):
        with patch("pr_agent.git_providers.github_provider.GithubProvider._get_repo"), \
             patch("pr_agent.git_providers.github_provider.GithubProvider.set_pr"), \
             patch("pr_agent.git_providers.github_provider.GithubProvider._get_pr"):
            from pr_agent.git_providers.github_provider import GithubProvider
            p = GithubProvider.__new__(GithubProvider)
            p.pr = MagicMock()
            p.pr.url = "https://api.github.com/repos/owner/repo/pulls/1"
            p.pr._requester.requestJsonAndCheck = MagicMock(return_value=({}, raw))
            p.deployment_type = deployment_type
            p.github_user_id = user_id
            return p, app_name

    def test_app_deployment_filters_by_app_name(self):
        raw = [
            {"id": 1, "body": "bot one", "user": {"login": "my-bot[bot]"}, "path": "a.py",
             "line": 1, "start_line": None},
            {"id": 2, "body": "human one", "user": {"login": "alice"}, "path": "a.py",
             "line": 2, "start_line": None},
        ]
        provider, app_name = self._make_provider_with_raw_comments(
            raw, deployment_type="app", app_name="my-bot"
        )
        with patch("pr_agent.git_providers.github_provider.get_settings") as gs:
            gs.return_value.get = lambda key, default="": app_name if key == "GITHUB.APP_NAME" else default
            out = provider.get_bot_review_comments()
        assert [c["id"] for c in out] == [1]

    def test_user_deployment_populates_user_id_lazily(self):
        raw = [
            {"id": 5, "body": "bot", "user": {"login": "pr-agent-bot"}, "path": "x.py",
             "line": 3, "start_line": None},
            {"id": 6, "body": "not bot", "user": {"login": "someone-else"}, "path": "x.py",
             "line": 4, "start_line": None},
        ]
        provider, _ = self._make_provider_with_raw_comments(
            raw, deployment_type="user", user_id=None
        )
        provider.get_user_id = MagicMock(return_value="pr-agent-bot")
        with patch("pr_agent.git_providers.github_provider.get_settings") as gs:
            gs.return_value.get = lambda key, default="": default
            out = provider.get_bot_review_comments()
        assert [c["id"] for c in out] == [5]
        provider.get_user_id.assert_called_once()
