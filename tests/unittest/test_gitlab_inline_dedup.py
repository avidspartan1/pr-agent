"""
Tests for GitLabProvider dedup-aware publish_code_suggestions,
get_bot_review_comments, and edit_review_comment.
"""
from unittest.mock import MagicMock, patch

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


def _make_provider():
    """Construct a GitLabProvider bypassing __init__ and set needed attributes."""
    from pr_agent.git_providers.gitlab_provider import GitLabProvider
    p = GitLabProvider.__new__(GitLabProvider)
    p.max_comment_chars = 65000
    p.gl = MagicMock()
    p.mr = MagicMock()
    p.id_mr = 1
    return p


def _set_mode(mode):
    return patch(
        "pr_agent.git_providers.gitlab_provider.get_settings",
        return_value=MagicMock(
            pr_code_suggestions=MagicMock(get=lambda key, default=None:
                mode if key == "persistent_inline_comments" else default),
        ),
    )


# ---------------------------------------------------------------------------
# Off mode
# ---------------------------------------------------------------------------

class TestOffMode:
    def test_off_mode_skips_fetch(self):
        """In 'off' mode, get_bot_review_comments must never be called."""
        p = _make_provider()
        p.get_bot_review_comments = MagicMock()
        p.edit_review_comment = MagicMock()
        p.send_inline_comment = MagicMock()
        p.get_diff_files = MagicMock(return_value=[])

        with _set_mode("off"):
            p.publish_code_suggestions([_sug()])

        p.get_bot_review_comments.assert_not_called()
        p.edit_review_comment.assert_not_called()


# ---------------------------------------------------------------------------
# Update mode
# ---------------------------------------------------------------------------

class TestUpdateMode:
    def test_no_match_creates_new(self):
        """When no existing comment matches, send_inline_comment is called and marker is embedded."""
        p = _make_provider()
        p.get_bot_review_comments = MagicMock(return_value=[])
        p.edit_review_comment = MagicMock()
        p.send_inline_comment = MagicMock()

        # Set up get_diff_files so the suggestion loop completes
        fake_file = MagicMock()
        fake_file.filename = "src/app.py"
        fake_file.head_file = "\n".join(f"line{i}" for i in range(1, 50))
        p.get_diff_files = MagicMock(return_value=[fake_file])

        s = _sug()
        with _set_mode("update"):
            p.publish_code_suggestions([s])

        p.get_bot_review_comments.assert_called_once()
        p.edit_review_comment.assert_not_called()
        p.send_inline_comment.assert_called_once()
        # The body passed to send_inline_comment should contain the marker
        call_body = p.send_inline_comment.call_args[0][0]
        assert MARKER_PREFIX in call_body
        assert MARKER_SUFFIX in call_body

    def test_match_edits_and_skips_creation(self):
        """When an existing comment matches, edit_review_comment is called and send_inline_comment is not."""
        p = _make_provider()
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        existing_body = "old body\n\n" + marker
        p.get_bot_review_comments = MagicMock(
            return_value=[{"id": 777, "body": existing_body, "path": s["relevant_file"]}]
        )
        p.edit_review_comment = MagicMock(return_value=True)
        p.send_inline_comment = MagicMock()
        p.get_diff_files = MagicMock(return_value=[])

        with _set_mode("update"):
            p.publish_code_suggestions([s])

        p.edit_review_comment.assert_called_once()
        called_id, called_body = p.edit_review_comment.call_args[0]
        assert called_id == 777
        assert marker in called_body
        assert s["body"] in called_body
        p.send_inline_comment.assert_not_called()

    def test_edit_failure_falls_back_to_create(self):
        """When edit_review_comment returns False, fall back to send_inline_comment."""
        p = _make_provider()
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        p.get_bot_review_comments = MagicMock(
            return_value=[{"id": 777, "body": "old " + marker, "path": s["relevant_file"]}]
        )
        p.edit_review_comment = MagicMock(return_value=False)
        p.send_inline_comment = MagicMock()

        fake_file = MagicMock()
        fake_file.filename = "src/app.py"
        fake_file.head_file = "\n".join(f"line{i}" for i in range(1, 50))
        p.get_diff_files = MagicMock(return_value=[fake_file])

        with _set_mode("update"):
            p.publish_code_suggestions([s])

        p.edit_review_comment.assert_called_once()
        p.send_inline_comment.assert_called_once()

    def test_mixed_match_and_new(self):
        """Matched suggestions are edited; unmatched ones are created."""
        p = _make_provider()
        matched = _sug(content="Matched suggestion")
        unmatched = _sug(content="Brand new suggestion", start=40, end=42)
        marker_matched = generate_marker(matched["original_suggestion"])

        p.get_bot_review_comments = MagicMock(
            return_value=[{"id": 1, "body": marker_matched, "path": matched["relevant_file"]}]
        )
        p.edit_review_comment = MagicMock(return_value=True)
        p.send_inline_comment = MagicMock()

        fake_file = MagicMock()
        fake_file.filename = "src/app.py"
        fake_file.head_file = "\n".join(f"line{i}" for i in range(1, 50))
        p.get_diff_files = MagicMock(return_value=[fake_file])

        with _set_mode("update"):
            p.publish_code_suggestions([matched, unmatched])

        assert p.edit_review_comment.call_count == 1
        p.send_inline_comment.assert_called_once()
        call_body = p.send_inline_comment.call_args[0][0]
        assert "Brand new" in call_body

    def test_fetch_failure_falls_back_to_creating_all(self):
        """When get_bot_review_comments raises, fall back to creating all suggestions."""
        p = _make_provider()
        p.get_bot_review_comments = MagicMock(side_effect=RuntimeError("api down"))
        p.edit_review_comment = MagicMock()
        p.send_inline_comment = MagicMock()

        fake_file = MagicMock()
        fake_file.filename = "src/app.py"
        fake_file.head_file = "\n".join(f"line{i}" for i in range(1, 50))
        p.get_diff_files = MagicMock(return_value=[fake_file])

        with _set_mode("update"):
            p.publish_code_suggestions([_sug()])

        p.edit_review_comment.assert_not_called()
        p.send_inline_comment.assert_called_once()


# ---------------------------------------------------------------------------
# Skip mode
# ---------------------------------------------------------------------------

class TestSkipMode:
    def test_match_skips_entirely(self):
        """In skip mode, a matched suggestion is not re-posted at all."""
        p = _make_provider()
        s = _sug()
        marker = generate_marker(s["original_suggestion"])
        p.get_bot_review_comments = MagicMock(
            return_value=[{"id": 1, "body": marker, "path": s["relevant_file"]}]
        )
        p.edit_review_comment = MagicMock()
        p.send_inline_comment = MagicMock()
        p.get_diff_files = MagicMock(return_value=[])

        with _set_mode("skip"):
            p.publish_code_suggestions([s])

        p.edit_review_comment.assert_not_called()
        p.send_inline_comment.assert_not_called()

    def test_no_match_still_creates(self):
        """In skip mode, suggestions without a matching existing comment are still created."""
        p = _make_provider()
        p.get_bot_review_comments = MagicMock(return_value=[])
        p.edit_review_comment = MagicMock()
        p.send_inline_comment = MagicMock()

        fake_file = MagicMock()
        fake_file.filename = "src/app.py"
        fake_file.head_file = "\n".join(f"line{i}" for i in range(1, 50))
        p.get_diff_files = MagicMock(return_value=[fake_file])

        with _set_mode("skip"):
            p.publish_code_suggestions([_sug()])

        p.send_inline_comment.assert_called_once()


# ---------------------------------------------------------------------------
# get_bot_review_comments — bot identity filtering
# ---------------------------------------------------------------------------

class TestGetBotReviewCommentsFiltering:
    """Exercises the real get_bot_review_comments filter (not mocked out)."""

    def _make_discussions(self, notes_list):
        """Build a list of mock discussion objects from a list of note dicts."""
        discussions = []
        for i, notes in enumerate(notes_list):
            d = MagicMock()
            d.id = f"discussion-{i}"
            d.attributes = {"notes": notes}
            discussions.append(d)
        return discussions

    def _note(self, note_id, username, body="comment body", path="src/app.py", new_line=5):
        return {
            "id": note_id,
            "type": "DiffNote",
            "body": body,
            "author": {"username": username},
            "position": {
                "new_path": path,
                "new_line": new_line,
                "line_range": None,
            },
        }

    def test_bot_authored_notes_kept(self):
        """Notes authored by the bot username are returned."""
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.max_comment_chars = 65000

        bot_note = self._note(10, "my-bot", body="bot comment")
        human_note = self._note(11, "alice", body="human comment")

        p.mr = MagicMock()
        p.mr.discussions.list.return_value = self._make_discussions([[bot_note, human_note]])

        p.gl = MagicMock()
        p.gl.auth = MagicMock()
        p.gl.user = MagicMock()
        p.gl.user.username = "my-bot"

        result = p.get_bot_review_comments()
        assert [c["id"] for c in result] == [10]

    def test_human_authored_notes_rejected(self):
        """Notes not authored by the bot are excluded."""
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.max_comment_chars = 65000

        human_note = self._note(20, "alice", body="human")

        p.mr = MagicMock()
        p.mr.discussions.list.return_value = self._make_discussions([[human_note]])

        p.gl = MagicMock()
        p.gl.auth = MagicMock()
        p.gl.user = MagicMock()
        p.gl.user.username = "my-bot"

        result = p.get_bot_review_comments()
        assert result == []

    def test_non_diff_notes_excluded(self):
        """Regular MR notes (type != DiffNote) are excluded."""
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.max_comment_chars = 65000

        regular_note = {
            "id": 30,
            "type": "Note",  # NOT a DiffNote
            "body": "general comment",
            "author": {"username": "my-bot"},
            "position": None,
        }

        p.mr = MagicMock()
        d = MagicMock()
        d.id = "disc-0"
        d.attributes = {"notes": [regular_note]}
        p.mr.discussions.list.return_value = [d]

        p.gl = MagicMock()
        p.gl.auth = MagicMock()
        p.gl.user = MagicMock()
        p.gl.user.username = "my-bot"

        result = p.get_bot_review_comments()
        assert result == []

    def test_returns_empty_on_exception(self):
        """If discussions.list raises, return [] and log a warning."""
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.max_comment_chars = 65000

        p.mr = MagicMock()
        p.mr.discussions.list.side_effect = RuntimeError("api failure")

        p.gl = MagicMock()
        p.gl.auth = MagicMock()
        p.gl.user = MagicMock()
        p.gl.user.username = "my-bot"

        result = p.get_bot_review_comments()
        assert result == []

    def test_multiline_note_extracts_start_line(self):
        """Multi-line DiffNote has start_line extracted from line_range.start.new_line."""
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.max_comment_chars = 65000

        multiline_note = {
            "id": 40,
            "type": "DiffNote",
            "body": "multi-line",
            "author": {"username": "my-bot"},
            "position": {
                "new_path": "src/foo.py",
                "new_line": 20,
                "line_range": {
                    "start": {"new_line": 15},
                    "end": {"new_line": 20},
                },
            },
        }

        p.mr = MagicMock()
        d = MagicMock()
        d.id = "disc-0"
        d.attributes = {"notes": [multiline_note]}
        p.mr.discussions.list.return_value = [d]

        p.gl = MagicMock()
        p.gl.auth = MagicMock()
        p.gl.user = MagicMock()
        p.gl.user.username = "my-bot"

        result = p.get_bot_review_comments()
        assert len(result) == 1
        assert result[0]["start_line"] == 15
        assert result[0]["line"] == 20


# ---------------------------------------------------------------------------
# edit_review_comment
# ---------------------------------------------------------------------------

class TestEditReviewComment:
    def test_returns_true_on_success(self):
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.max_comment_chars = 65000

        mock_note = MagicMock()
        p.mr = MagicMock()
        p.mr.notes.get.return_value = mock_note

        result = p.edit_review_comment(99, "new body")

        assert result is True
        p.mr.notes.get.assert_called_once_with(99)
        assert mock_note.body == "new body"
        mock_note.save.assert_called_once()

    def test_returns_false_on_exception(self):
        from pr_agent.git_providers.gitlab_provider import GitLabProvider
        p = GitLabProvider.__new__(GitLabProvider)
        p.max_comment_chars = 65000

        p.mr = MagicMock()
        p.mr.notes.get.side_effect = RuntimeError("not found")

        result = p.edit_review_comment(99, "new body")

        assert result is False
