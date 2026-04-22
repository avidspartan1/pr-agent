# Inline-comment dedup: structured-edit hash — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the prose-based inline-comment dedup hash with a structured-first scheme (hash of the normalized proposed edit) that falls back to prose only when no `improved_code` is available, so two runs that produce the same fix with paraphrased wording stop posting duplicate inline comments.

**Architecture:** A single-marker-per-comment scheme with two version-tagged namespaces (`v2s` for structured, `v2p` for prose), strictly selected by input availability at emit time (structured-first, prose-fallback, never OR). The marker grammar (`<!-- pr-agent-inline-id:XXXX -->`) is unchanged, so the extraction regex, provider lookup seam, and outdated-pass auto-resolve all keep working with zero data migration — pre-existing `v1` markers self-heal via the outdated pass on the first re-run after deployment.

**Tech Stack:** Python 3 stdlib (`hashlib`, `re`, `textwrap`), `pytest`, existing PR-Agent test harness.

**Design reference:** `docs/superpowers/specs/2026-04-22-inline-dedup-structured-hash-design.md`

---

## File Structure

**Modified files**

- `pr_agent/algo/inline_comments_dedup.py` — add `normalize_code`; rewrite `generate_marker` for v2s/v2p selection; add the design-decision code-comment block. No changes to `extract_marker`, `append_marker`, `build_marker_index`, `normalize_persistent_mode`, or the resolved-body helpers — they operate on the marker grammar, not on hash contents.
- `tests/unittest/test_inline_comments_dedup.py` — extend with structured-path, prose-fallback, and cross-namespace cases.
- `tests/unittest/test_github_inline_dedup.py` — one integration-style case covering the live cleanup_mode scenario (paraphrased prose, identical `improved_code`, update-path taken).
- `docs/docs/tools/improve.md` — tighten the `persistent_inline_comments` config-row description and add a new `## How inline-comment deduplication works` section.

**Unchanged by design** — providers (`github_provider.py`, `gitlab_provider.py`) already call the dedup helpers via the shared seam; no provider code paths need edits.

---

## Task 0: Add `normalize_code` helper

**Goal:** A pure function that normalizes an `improved_code` string so reindentation and whitespace variation don't affect the dedup hash, while genuine token-level differences still do.

**Files:**
- Modify: `pr_agent/algo/inline_comments_dedup.py` (add helper; no other code changes yet)
- Test: `tests/unittest/test_inline_comments_dedup.py` (new test class `TestNormalizeCode`)

**Acceptance Criteria:**
- [ ] Empty / `None` / whitespace-only input → `""`
- [ ] Reindenting a block (common leading whitespace differs) produces the same output
- [ ] Trailing whitespace on a line is stripped
- [ ] Runs of internal whitespace within a line collapse to a single space
- [ ] Leading/trailing fully-blank lines are dropped
- [ ] Tabs expand consistently (any tab/space mixing that renders the same indent structure normalizes identically)
- [ ] Changing a single non-whitespace token produces a different output

**Verify:** `uv run pytest tests/unittest/test_inline_comments_dedup.py::TestNormalizeCode -v` → all cases pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/unittest/test_inline_comments_dedup.py` (at the end of the file, after existing test classes):

```python
from pr_agent.algo.inline_comments_dedup import normalize_code


class TestNormalizeCode:
    def test_empty_inputs(self):
        assert normalize_code("") == ""
        assert normalize_code(None) == ""
        assert normalize_code("   \n  \n") == ""

    def test_reindent_produces_same_output(self):
        a = (
            "        cleanup_mode=cleanup_mode,\n"
        )
        b = (
            "    cleanup_mode=cleanup_mode,\n"
        )
        assert normalize_code(a) == normalize_code(b)

    def test_multiline_reindent_produces_same_output(self):
        a = (
            "        bump_version(\n"
            "            github=self.github,\n"
            "            cleanup_mode=None if dry_run else cleanup_mode,\n"
            "        )\n"
        )
        b = (
            "    bump_version(\n"
            "        github=self.github,\n"
            "        cleanup_mode=None if dry_run else cleanup_mode,\n"
            "    )\n"
        )
        assert normalize_code(a) == normalize_code(b)

    def test_trailing_whitespace_stripped(self):
        assert normalize_code("foo = 1   \n") == normalize_code("foo = 1\n")

    def test_internal_whitespace_collapsed(self):
        assert normalize_code("foo   =   1") == normalize_code("foo = 1")

    def test_leading_and_trailing_blank_lines_dropped(self):
        assert normalize_code("\n\nfoo = 1\n\n\n") == normalize_code("foo = 1")

    def test_tabs_expand_consistently(self):
        tabbed = "\tfoo = 1\n\tbar = 2\n"
        spaced = "        foo = 1\n        bar = 2\n"
        assert normalize_code(tabbed) == normalize_code(spaced)

    def test_token_difference_preserved(self):
        assert normalize_code("cleanup_mode=None if dry_run else cleanup_mode") != \
               normalize_code("cleanup_mode=cleanup_mode if not dry_run else None")

    def test_is_idempotent(self):
        sample = "    x = f(1, 2)\n    y = g(3)\n"
        once = normalize_code(sample)
        twice = normalize_code(once)
        assert once == twice
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/unittest/test_inline_comments_dedup.py::TestNormalizeCode -v
```

Expected: all FAIL with `ImportError: cannot import name 'normalize_code'`.

- [ ] **Step 3: Implement `normalize_code`**

Edit `pr_agent/algo/inline_comments_dedup.py`. Add `import textwrap` with the existing imports, then add the helper after the existing `_normalize` function (which you will keep — it's still used by the prose path):

```python
import textwrap

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
```

- [ ] **Step 4: Run tests to verify they pass**

```
uv run pytest tests/unittest/test_inline_comments_dedup.py::TestNormalizeCode -v
```

Expected: 9 PASSED.

- [ ] **Step 5: Run the full existing dedup test module to confirm no regression**

```
uv run pytest tests/unittest/test_inline_comments_dedup.py -v
```

Expected: all previously-passing tests still pass; the 9 new ones also pass.

- [ ] **Step 6: Commit**

```
git add pr_agent/algo/inline_comments_dedup.py tests/unittest/test_inline_comments_dedup.py
git commit -m "$(cat <<'MSG'
feat(inline-dedup): add normalize_code helper for edit-content hashing

Prepares for structured-first dedup: dedents, strips trailing whitespace,
expands tabs, collapses internal whitespace runs so reindentation of the
same proposed edit normalizes identically. Pure function, no behaviour
change yet — generate_marker still keys on prose until Task 1.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
MSG
)"
```

---

## Task 1: Rewrite `generate_marker` with v2s/v2p namespaces

**Goal:** Emit a structured-edit hash (`v2s`) when the suggestion has `improved_code`; otherwise emit a prose-fallback hash (`v2p`). Single marker per comment. Preserve line-drift stability (no line-range in the key).

**Files:**
- Modify: `pr_agent/algo/inline_comments_dedup.py` (replace `generate_marker` body; add `_SEP` constant and version-tag constants; add the design-decision code-comment block)
- Test: `tests/unittest/test_inline_comments_dedup.py` (extend `TestGenerateMarker`)

**Acceptance Criteria:**
- [ ] Two suggestions with paraphrased prose but identical `improved_code` at the same `relevant_file` produce the **same** marker
- [ ] Two suggestions with identical prose and different `improved_code` at the same `relevant_file` produce **different** markers (strict (a))
- [ ] Reindenting `improved_code` does not change the marker
- [ ] Changing `label` when `improved_code` is present does **not** change the marker (label is not part of the structured key)
- [ ] `improved_code` omitted or empty string falls back to prose and still produces a deterministic marker
- [ ] Prose fallback with `label` or `content` missing returns `None`
- [ ] Structured path with `relevant_file` missing returns `None`
- [ ] Structured and prose signatures never collide on otherwise-identical inputs (version tag is inside the hashed signature)
- [ ] Existing `test_stable_across_line_shifts` still passes (line-range is not in the key)

**Verify:** `uv run pytest tests/unittest/test_inline_comments_dedup.py -v` → all cases pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/unittest/test_inline_comments_dedup.py`, inside (or next to) the existing `TestGenerateMarker` class:

```python
def _structured_suggestion(
    file="src/app.py",
    content="some prose",
    improved_code="cleanup_mode=None if dry_run else cleanup_mode,",
    label="possible issue",
    start=10,
    end=12,
):
    return {
        "relevant_file": file,
        "label": label,
        "suggestion_content": content,
        "improved_code": improved_code,
        "relevant_lines_start": start,
        "relevant_lines_end": end,
    }


class TestGenerateMarkerStructured:
    def test_paraphrased_prose_same_edit_collides(self):
        a = _structured_suggestion(
            content="When dry_run=True, cleanup_mode is still passed through unchanged to bump_version.",
        )
        b = _structured_suggestion(
            content="When dry_run=True, the cleanup_mode is still forwarded unchanged to bump_version.",
        )
        assert generate_marker(a) == generate_marker(b)

    def test_same_prose_different_edit_splits(self):
        a = _structured_suggestion(
            improved_code="cleanup_mode=None if dry_run else cleanup_mode,",
        )
        b = _structured_suggestion(
            improved_code="cleanup_mode=cleanup_mode if not dry_run else None,",
        )
        assert generate_marker(a) != generate_marker(b)

    def test_reindented_edit_collides(self):
        a = _structured_suggestion(
            improved_code="        cleanup_mode=None if dry_run else cleanup_mode,\n",
        )
        b = _structured_suggestion(
            improved_code="    cleanup_mode=None if dry_run else cleanup_mode,\n",
        )
        assert generate_marker(a) == generate_marker(b)

    def test_label_change_does_not_split_when_structured(self):
        a = _structured_suggestion(label="possible issue")
        b = _structured_suggestion(label="best practice")
        assert generate_marker(a) == generate_marker(b)

    def test_missing_file_returns_none(self):
        s = _structured_suggestion()
        s["relevant_file"] = ""
        assert generate_marker(s) is None

    def test_empty_improved_code_falls_back_to_prose(self):
        s = _structured_suggestion(improved_code="")
        # With prose + label present, fallback produces a marker.
        assert generate_marker(s) is not None

    def test_fallback_missing_label_returns_none(self):
        s = _structured_suggestion(improved_code="", label="")
        assert generate_marker(s) is None

    def test_fallback_missing_content_returns_none(self):
        s = _structured_suggestion(improved_code="", content="")
        s["suggestion_content"] = ""
        assert generate_marker(s) is None

    def test_structured_and_prose_differ_on_same_inputs(self):
        # Same file/label/content; structured extra input shouldn't alias to prose hash.
        structured = _structured_suggestion(
            improved_code="x = 1",
            content="x = 1",
            label="possible issue",
        )
        prose_only = _structured_suggestion(
            improved_code="",
            content="x = 1",
            label="possible issue",
        )
        assert generate_marker(structured) != generate_marker(prose_only)
```

- [ ] **Step 2: Run tests to verify they fail**

```
uv run pytest tests/unittest/test_inline_comments_dedup.py::TestGenerateMarkerStructured -v
```

Expected: most cases FAIL. Specifically `test_paraphrased_prose_same_edit_collides` and `test_reindented_edit_collides` fail (current prose-based key splits on paraphrase and isn't sensitive to code).

- [ ] **Step 3: Rewrite `generate_marker`**

Edit `pr_agent/algo/inline_comments_dedup.py`. Replace the existing `generate_marker` function with the version below, add two version-tag constants and a `_SEP` constant at module scope, and add the design-decision code-comment block directly above `generate_marker`:

```python
_SEP = "\x00"
_HASH_VERSION_STRUCTURED = "v2s"
_HASH_VERSION_PROSE = "v2p"


# Dedup identity is structured-first, prose-fallback:
#   - If a suggestion has `improved_code`, the hash covers
#     (version_tag + file + normalized improved_code). Prose wording never
#     affects the key, and label is intentionally excluded — the edit
#     itself is the identity.
#   - Otherwise we fall back to (version_tag + file + label + prose prefix).
#
# This is a strict (a) design: prose is NEVER consulted when a structured
# edit exists. Two suggestions at the same spot with the same prose but
# different edits intentionally remain separate comments — we'd rather
# under-merge than over-merge genuinely distinct fixes.
#
# Line-range is deliberately NOT in the key so dedup stays stable across
# upstream pushes that drift the target line (a property the user-facing
# docs explicitly promise).
#
# The version tag (v2s / v2p) lives INSIDE the hashed signature, making
# the two namespaces preimage-distinct and preventing accidental cross-
# namespace collisions. The marker grammar is unchanged, so pre-existing
# v1 markers on live PRs self-heal via the outdated-pass auto-resolve
# (resolve_outdated_inline_comments) on the first re-run after deployment.
#
# A fuzzy near-miss signal (shingle / Jaccard) was considered and deferred;
# see docs/docs/tools/improve.md and the Serena memory
# `future_fuzzy_inline_dedup`.
def generate_marker(suggestion: dict) -> Optional[str]:
    """Return a stable marker for this suggestion, or None if required fields are missing."""
    file = suggestion.get("relevant_file")
    if not file:
        return None
    file = str(file).strip()
    if not file:
        return None

    improved_code = suggestion.get("improved_code")
    if isinstance(improved_code, str) and improved_code.strip():
        sig = _SEP.join([_HASH_VERSION_STRUCTURED, file, normalize_code(improved_code)])
    else:
        label = suggestion.get("label")
        content = _pick_content(suggestion)
        if not label or not content:
            return None
        sig = _SEP.join([
            _HASH_VERSION_PROSE,
            file,
            str(label).strip(),
            _normalize(content)[:_CONTENT_PREFIX_LEN],
        ])

    digest = hashlib.sha256(sig.encode("utf-8")).hexdigest()[:_HASH_LEN]
    return f"{MARKER_PREFIX}{digest}{MARKER_SUFFIX}"
```

- [ ] **Step 4: Run the new tests to verify they pass**

```
uv run pytest tests/unittest/test_inline_comments_dedup.py::TestGenerateMarkerStructured -v
```

Expected: 9 PASSED.

- [ ] **Step 5: Run the full module to check existing tests still pass**

```
uv run pytest tests/unittest/test_inline_comments_dedup.py -v
```

Expected: all existing tests pass (including `test_stable_across_line_shifts`, `test_deterministic`, `test_shape`) plus the new ones.

**Note on existing tests:** Any existing test that passed a suggestion with `improved_code` set would now take the structured path and its expected hash value will change. The assertions in `test_inline_comments_dedup.py` as of now do NOT hard-code hash values — they assert shape, determinism, equality/inequality between variants. Those invariants still hold. If a new failure surfaces, re-read the failing assertion and decide if it's still a valid invariant under v2s/v2p; update the test to match the new semantics rather than fighting the design.

- [ ] **Step 6: Commit**

```
git add pr_agent/algo/inline_comments_dedup.py tests/unittest/test_inline_comments_dedup.py
git commit -m "$(cat <<'MSG'
feat(inline-dedup): hash structured edit, fall back to prose

Inline-comment dedup now hashes the normalized improved_code when
present (v2s namespace), falling back to the prior prose-based key
only when no structured edit is available (v2p namespace). Same
marker grammar, no data migration — v1 markers on live PRs self-heal
via the existing outdated-pass auto-resolve on the first re-run.

Paraphrased prose with the same proposed edit now collapses to one
inline comment instead of two. Genuinely different edits at the same
spot intentionally remain separate (strict (a) — see the code-comment
block in inline_comments_dedup.py for the rationale).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
MSG
)"
```

---

## Task 2: Integration test for the live cleanup_mode scenario

**Goal:** Lock in end-to-end behaviour: two paraphrased suggestions with identical `improved_code` route through the provider's update path on the second run, not the create path.

**Files:**
- Test: `tests/unittest/test_github_inline_dedup.py` (add one test class or test function)

**Acceptance Criteria:**
- [ ] Given an `existing_index` that contains one comment with a v2s marker, a new suggestion with paraphrased prose but the same `improved_code` reaches `edit_review_comment` rather than being added to `post_parameters_list`
- [ ] The test fails if someone reverts to prose-based hashing in `generate_marker`

**Verify:** `uv run pytest tests/unittest/test_github_inline_dedup.py -v` → all cases pass, new case included

**Steps:**

- [ ] **Step 1: Inspect the existing `TestGitHubPersistentInlineComments` harness so the new test follows the established patching pattern**

```
uv run pytest tests/unittest/test_github_inline_dedup.py -v --collect-only | head -40
```

Read `tests/unittest/test_github_inline_dedup.py` lines 1–170 (imports, `_set_mode`, provider construction helpers, `_sug`). Your test will reuse these.

- [ ] **Step 2: Add the integration test**

Append to `tests/unittest/test_github_inline_dedup.py` at the end of the file:

```python
class TestStructuredHashLivePath:
    """End-to-end: paraphrased prose + identical improved_code → update, not create."""

    def test_paraphrased_prose_same_edit_routes_to_edit_review_comment(self):
        from pr_agent.algo.inline_comments_dedup import (
            MARKER_PREFIX,
            MARKER_SUFFIX,
            generate_marker,
        )

        # Two suggestions that differ only in wording; improved_code is identical.
        improved = "cleanup_mode=None if dry_run else cleanup_mode,"
        file = "src/release.py"
        first_run = {
            "relevant_file": file,
            "label": "possible issue",
            "suggestion_content": (
                "When dry_run=True, cleanup_mode is still passed through "
                "unchanged to bump_version."
            ),
            "improved_code": improved,
        }
        second_run = {
            "relevant_file": file,
            "label": "possible issue",
            "suggestion_content": (
                "When dry_run=True, the cleanup_mode is still forwarded "
                "unchanged to bump_version."
            ),
            "improved_code": improved,
        }

        marker_first = generate_marker(first_run)
        marker_second = generate_marker(second_run)
        assert marker_first == marker_second, \
            "paraphrased prose with identical improved_code must collide"
        marker_hash = marker_first[len(MARKER_PREFIX):-len(MARKER_SUFFIX)]

        # Build a provider as in the existing test helpers, with an
        # existing comment carrying the v2s marker in its body.
        with patch("pr_agent.git_providers.github_provider.GithubProvider._get_repo"), \
             patch("pr_agent.git_providers.github_provider.GithubProvider.set_pr"), \
             patch("pr_agent.git_providers.github_provider.GithubProvider._get_pr"):
            from pr_agent.git_providers.github_provider import GithubProvider
            provider = GithubProvider.__new__(GithubProvider)
            provider.pr = MagicMock()
            provider.base_url = "https://api.github.com"
            provider.repo = "owner/repo"
            provider.deployment_type = "user"
            provider.github_user_id = "pr-agent-bot"

        existing_comment = {
            "id": 777,
            "thread_id": "T1",
            "body": f"old body\n\n{marker_first}",
            "path": file,
            "line": 12,
            "start_line": 10,
            "is_resolved": False,
        }
        provider.get_bot_review_comments = MagicMock(return_value=[existing_comment])
        provider.edit_review_comment = MagicMock(return_value=True)
        provider.unresolve_review_thread = MagicMock()
        provider.validate_comments_inside_hunks = lambda xs: xs
        provider.pr.create_review = MagicMock()

        # The suggestion dict shape expected by publish_code_suggestions.
        body_text = (
            "**Suggestion:** paraphrased wording, same fix [possible issue]\n"
            "```suggestion\n"
            f"{improved}\n"
            "```"
        )
        code_suggestion = {
            "body": body_text,
            "relevant_file": file,
            "relevant_lines_start": 10,
            "relevant_lines_end": 12,
            "original_suggestion": second_run,
        }

        with _set_mode("update"):
            provider.publish_code_suggestions([code_suggestion])

        # Update path: edit called once with the existing comment's id;
        # create_review not called.
        provider.edit_review_comment.assert_called_once()
        (called_id, called_body), _ = provider.edit_review_comment.call_args
        assert called_id == 777
        assert marker_first in called_body
        provider.pr.create_review.assert_not_called()
```

- [ ] **Step 3: Run the new test to verify it passes**

```
uv run pytest tests/unittest/test_github_inline_dedup.py::TestStructuredHashLivePath -v
```

Expected: 1 PASSED.

- [ ] **Step 4: Run the full provider test module for regressions**

```
uv run pytest tests/unittest/test_github_inline_dedup.py -v
```

Expected: all cases pass.

- [ ] **Step 5: Commit**

```
git add tests/unittest/test_github_inline_dedup.py
git commit -m "$(cat <<'MSG'
test(inline-dedup): lock in structured-hash update path for live scenario

Covers the cleanup_mode regression: two suggestions with paraphrased
prose but identical improved_code collide on the v2s marker and route
through edit_review_comment rather than posting a duplicate inline
comment.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
MSG
)"
```

---

## Task 3: Documentation updates

**Goal:** User-facing docs reflect the structured-first rule, the strict-(a) behaviour, and what's invariant across runs.

**Files:**
- Modify: `docs/docs/tools/improve.md` (tighten existing config-row description; add new section)

**Acceptance Criteria:**
- [ ] The `persistent_inline_comments` row no longer says "content-derived hash"; it names the structured-first / prose-fallback rule concisely
- [ ] A new `## How inline-comment deduplication works` section is present, placed after the configuration tables
- [ ] The new section covers: overview, identity rule, invariants, what still splits, future work pointer
- [ ] Strict-(a) behaviour (same prose + diff edit → separate comments) is explicitly stated

**Verify:** Read `docs/docs/tools/improve.md` and confirm the config-row edit and the new section render sensibly.

**Steps:**

- [ ] **Step 1: Update the `persistent_inline_comments` config-row description**

Edit `docs/docs/tools/improve.md` around the existing row:

Replace the full `<td>` content for `persistent_inline_comments` (currently starting with `Controls how inline suggestions are deduplicated across re-runs…`) with:

```html
<td>Controls how inline suggestions are deduplicated across re-runs on the same PR/MR. <code>"update"</code> (default) edits the matching existing inline comment in place; <code>"skip"</code> leaves the existing one untouched; <code>"off"</code> always posts a new inline comment (legacy behavior). The dedup key is the hash of the proposed edit (file + normalized <code>improved_code</code>), with a fallback to a prose-based hash only when no <code>improved_code</code> is available. Line numbers are intentionally excluded so dedup stays stable across upstream pushes that drift the target line. See <a href="#how-inline-comment-deduplication-works">How inline-comment deduplication works</a> below for details.</td>
```

- [ ] **Step 2: Add the new section after the configuration tables**

Append to the end of `docs/docs/tools/improve.md` (or place immediately after the last configuration table if there are additional sections after it — read the file first to confirm placement):

```markdown
## How inline-comment deduplication works

When `persistent_inline_comments` is enabled (the default is `"update"`), re-running `/improve` on the same PR/MR will recognize and update — instead of duplicating — inline comments that were already posted for the same suggestion on a previous run. This uses a hidden marker (an HTML comment of the form `<!-- pr-agent-inline-id:XXXX -->`) embedded in each inline-comment body.

### Identity rule

Two inline comments are considered the same suggestion when the marker matches. The marker is a short hash computed as follows:

- **Structured (preferred):** When the suggestion has an `improved_code` field (i.e., the model proposed replacement code), the hash covers `(file, normalized improved_code)`. Wording changes in the suggestion's prose do **not** affect the key; `label` is **not** part of the key either — the edit itself is the identity.
- **Prose fallback:** When no `improved_code` is present, the hash falls back to `(file, label, normalized prose prefix)`.

Normalization of `improved_code` expands tabs, strips trailing whitespace, drops leading/trailing blank lines, removes the longest common leading indent, and collapses internal whitespace runs — so reindentation of the same proposed edit does not split comments.

### Strict behaviour

This is intentionally a strict rule: when a suggestion has `improved_code`, its prose is never consulted for dedup. Two suggestions at the same spot with identical prose but **different** proposed edits are treated as distinct and remain as two separate inline comments. We'd rather under-merge (and show two comments) than over-merge two genuinely different fixes into one.

### What's invariant across runs

- Prose paraphrase of the same finding (same proposed edit) — does **not** split.
- Reindentation or whitespace variation in the proposed edit — does **not** split.
- Upstream commits that push the target line up or down in the file — do **not** split. Line numbers are not part of the key.

### What still splits

- A genuinely different proposed edit at the same spot — by design.
- A different `label` when the prose fallback is in use (e.g., the same prose-only suggestion now tagged "best practice" vs "possible issue").

### Future work

A fuzzy near-miss signal (e.g., shingle/Jaccard similarity) may be added later if users report recurring duplicates that this deterministic scheme doesn't catch. For now the behaviour is strictly deterministic, with no similarity threshold to tune.
```

- [ ] **Step 3: Confirm the anchor link works**

Most Markdown renderers auto-generate anchors from `##` headings by lowercasing and replacing spaces/punctuation with hyphens. `## How inline-comment deduplication works` → `#how-inline-comment-deduplication-works`. Open `docs/docs/tools/improve.md` in a rendered preview (e.g. the mkdocs dev server, or whatever pipeline the repo uses) if you want to double-check — otherwise trust the convention.

- [ ] **Step 4: Commit**

```
git add docs/docs/tools/improve.md
git commit -m "$(cat <<'MSG'
docs(improve): document structured-first inline-comment dedup

Tightens the persistent_inline_comments config row and adds a "How
inline-comment deduplication works" section covering the identity
rule (structured-first, prose-fallback), the strict-(a) behaviour,
what's invariant (paraphrase, reindent, line drift), what still
splits, and the deferred fuzzy near-miss signal.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
MSG
)"
```

---

## Final Verification

Run the full test suite once at the end:

```
uv run pytest tests/unittest/test_inline_comments_dedup.py tests/unittest/test_github_inline_dedup.py tests/unittest/test_gitlab_inline_dedup.py tests/unittest/test_inline_comments_dedup_constants.py -v
```

Expected: all pass.

Sanity-check the git log has the expected three commits in order (Task 0 → Task 1 → Task 2 → Task 3):

```
git log --oneline -4
```

Expected: four new commits in order, each scoped to its own task.

---

## Self-Review

Checked against `docs/superpowers/specs/2026-04-22-inline-dedup-structured-hash-design.md`:

- [x] Core design decision (structured-first, prose-fallback) → Task 1 body and code-comment block
- [x] `normalize_code` semantics → Task 0 implementation + unit tests
- [x] Version tags inside signature (`v2s` / `v2p`) → Task 1 `_HASH_VERSION_*` constants
- [x] `\x00` field separator → Task 1 `_SEP` constant
- [x] Early-return rules (relaxed from today's all-or-nothing) → Task 1 `generate_marker` rewrite
- [x] No line-range in key → spec, plan, code comment, docs all state this
- [x] No migration code; self-healing via outdated pass → documented in spec and code comment
- [x] Code-comment placement at `generate_marker` → Task 1 Step 3
- [x] Documentation (`improve.md` config row + new section) → Task 3
- [x] Test coverage (`test_inline_comments_dedup.py` structured/prose/cross-namespace) → Task 1 Step 1
- [x] Integration test (`test_github_inline_dedup.py` live cleanup_mode scenario) → Task 2

No placeholders; each step has concrete code or concrete commands. Type/name consistency verified across tasks (`normalize_code`, `_SEP`, `_HASH_VERSION_STRUCTURED`, `_HASH_VERSION_PROSE`, `generate_marker` signature).
