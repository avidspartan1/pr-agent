# Persistent Inline Comments — Design

**Date:** 2026-04-16
**Status:** Approved — ready for implementation plan
**Issue:** Feature request — prevent duplicate inline comments across iterative runs

## Problem

When PR-Agent runs `/improve` (and `/add_docs`) multiple times on the same PR/MR, each run posts fresh inline comments for identical suggestions. Iterative workflows (e.g., re-running after a fix, parallel review jobs triggered by file changes) accumulate near-duplicate inline comments, adding noise to reviews. GitLab is most affected because its review semantics create one discussion per inline note.

Issue-level comments already solve this via `GitProvider.publish_persistent_comment_full` (git_provider.py:300-326), which locates an existing comment by header prefix and edits it in place. Inline comments have no equivalent mechanism — this design extends the same idea to inline comments.

## Goals

- No duplicate inline comments across re-runs when the same suggestion is generated.
- Preserve in-thread context (user replies, resolutions, reactions) by editing in place rather than deleting/re-creating.
- Work across GitHub and GitLab (primary targets); degrade gracefully to current behavior on other providers.
- Zero behavior change when the new setting is disabled.

## Non-goals

- LLM-assisted semantic equivalence ("is this the same issue as that one?"). We rely on a deterministic hash of a stable signature; perfect dedup under heavy AI paraphrasing is out of scope.
- Migrating pre-existing unmarked comments. Dedup kicks in on the *next* run after this change ships — existing inline comments remain as they are.
- De-duping `/review` (issue-level) comments — already handled by `persistent_comment`.

## Approach

**Stable-marker deduplication.** Embed a hidden HTML comment containing a content-derived hash in every inline comment body. Before publishing a new batch, fetch the bot's existing inline comments, build a `hash -> comment` index, and for each new suggestion: edit the matching comment if one exists, otherwise post new.

This mirrors the existing `publish_persistent_comment_full` pattern (which matches by header prefix on issue comments) and applies it to inline comments.

## Architecture

```
pr_agent/algo/inline_comments_dedup.py        (NEW)
    MARKER_PREFIX = "<!-- pr-agent-inline-id:"
    MARKER_SUFFIX = " -->"
    generate_marker(suggestion) -> str
    extract_marker(body)        -> str | None
    append_marker(body, marker) -> str
    build_marker_index(comments) -> dict[str, dict]

pr_agent/git_providers/git_provider.py        (MODIFY)
    + GitProvider.get_bot_review_comments() -> list[dict]   # default: []
    + GitProvider.edit_review_comment(id, body) -> bool     # default: False

pr_agent/git_providers/github_provider.py     (MODIFY)
    + get_bot_review_comments()      -- GET /repos/{repo}/pulls/{n}/comments, filter by bot
    + edit_review_comment()          -- PATCH /repos/{repo}/pulls/comments/{id}
    ~ publish_code_suggestions()     -- dedup-aware

pr_agent/git_providers/gitlab_provider.py     (MODIFY)
    + get_bot_review_comments()      -- mr.discussions.list(), inline + bot author
    + edit_review_comment()          -- discussion.notes update
    ~ publish_code_suggestions()     -- dedup-aware

pr_agent/settings/configuration.toml          (MODIFY)
    + [pr_code_suggestions].persistent_inline_comments = "update"
```

Because both `/improve` (pr_code_suggestions.py:574) and `/add_docs` (pr_add_docs.py:130) route through `publish_code_suggestions`, a single change point covers both tools.

## Marker identity

```
<!-- pr-agent-inline-id:<12-char-sha256-prefix> -->
```

Signature inputs (content-based, NOT positional):
- `relevant_file` (normalized path — `strip()`, POSIX separators)
- `label` from `original_suggestion` (e.g., `"possible issue"`, `"security"`)
- First 128 chars of suggestion summary/content, whitespace normalized to single spaces

```python
sig = f"{file}|{label}|{normalize(content[:128])}"
hash = sha256(sig.encode()).hexdigest()[:12]
```

**Why these fields:** `file + label + content-prefix` is stable across re-runs of the same underlying issue, even when line numbers shift due to new commits. It absorbs minor AI phrasing variation in the content tail but still catches the "same issue at the same place" case.

**Collision risk:** 12 hex chars = 48 bits. Within a single PR (dozens of suggestions), collision probability is vanishingly small.

## Data flow (inside `publish_code_suggestions`)

```
mode = settings.pr_code_suggestions.persistent_inline_comments  # "off" | "update" | "skip"

if mode == "off":
    <current behavior, unchanged>
    return

try:
    existing = git_provider.get_bot_review_comments()
except Exception:
    log warning; existing = []           # best-effort, never block publishing

index = build_marker_index(existing)     # { hash: comment_dict }

for suggestion in validated_suggestions:
    marker = generate_marker(suggestion['original_suggestion'])
    body   = append_marker(suggestion['body'], marker)
    hash   = extract_marker(marker)

    if hash in index:
        if mode == "skip":
            log; continue
        # mode == "update"
        if git_provider.edit_review_comment(index[hash]['id'], body):
            continue                      # edited in place; done
        # edit failed → fall through to create-new

    <create inline comment with body>     # existing pathway, unchanged
```

## Error handling

The dedup layer is a best-effort optimization. It never prevents publishing:

1. `get_bot_review_comments()` failure → log warning, treat as empty index, fall through to current create-new path.
2. `edit_review_comment()` failure for one suggestion → log, fall through to create-new for *that* suggestion only; other suggestions in the batch continue normally.
3. Providers that don't implement the new methods inherit base-class defaults (`[]` and `False`). `edit_review_comment` returning `False` routes to the create-new path. No errors, no surprises.
4. `persistent_inline_comments` with an unknown value → log warning once, treat as `"off"`.

This matches the silent-fail philosophy of `publish_persistent_comment_full`, which wraps its whole body in `try/except` and falls back to `publish_comment` on any error.

## Bot-identity resolution

Dedup must only match the bot's own comments, never human reviewers'.

- **GitHub:** Reuse the pattern from `publish_file_comments` (github_provider.py:658-663): for `deployment_type == 'app'`, match `GITHUB.APP_NAME` against `existing_comment['user']['login']`; for `deployment_type == 'user'`, compare to `self.github_user_id`. If neither resolves, `get_bot_review_comments()` returns `[]`.
- **GitLab:** Use the authenticated user's username from the python-gitlab client (`self.gl.user.username` when available). If unresolvable, return `[]`.

## Configuration

Add to `pr_agent/settings/configuration.toml` under `[pr_code_suggestions]`:

```toml
# Deduplicate inline suggestions across re-runs by embedding a content hash marker.
# "update": edit matching existing comment in place (default)
# "skip":   skip if a matching comment already exists
# "off":    always post a new comment (legacy behavior)
persistent_inline_comments = "update"
```

Document under `docs/docs/tools/improve.md` next to the existing `persistent_comment` setting.

## Testing

### Unit tests — `tests/unittest/test_inline_comments_dedup.py` (NEW)
- `generate_marker` deterministic for same suggestion
- `generate_marker` stable under line-number changes (same content, different lines → same hash)
- `generate_marker` differs for different files / labels / content prefixes
- `extract_marker` returns hash when present, `None` when absent
- `append_marker` then `extract_marker` round-trips
- `build_marker_index` — last-wins on hash duplicates; ignores comments without markers

### Provider tests (mock-based, extending existing suites)
- `"off"` mode → current pathway; `get_bot_review_comments` not called
- `"update"` mode, no existing markers → creates new, does not edit
- `"update"` mode, marker matches → calls `edit_review_comment`, does NOT create
- `"skip"` mode, marker matches → neither edits nor creates
- `get_bot_review_comments()` raising → falls back to create-new (no exception propagates)
- `edit_review_comment()` returning `False` for one suggestion → that one is posted as new; other matched suggestions still edited
- Bot-identity unresolved → behaves like empty index

## Out-of-scope providers

Bitbucket (cloud & server), Azure DevOps, Gitea, Gerrit, CodeCommit, local: not modified in this iteration. They inherit base-class no-ops, so `persistent_inline_comments` silently degrades to `"off"` behavior. A follow-up can extend them once the GitHub/GitLab implementation stabilizes.

## Risk analysis

- **False positives** (dedup collapses two genuinely different suggestions): bounded by the 12-char hash collision rate and by the 128-char content prefix being distinctive. Worst case: one of two similar suggestions updates into the other's slot — strictly better than the status quo of duplication.
- **False negatives** (AI rephrases enough that hashes diverge): degrades to current behavior for that specific suggestion — no regression relative to today.
- **Cost** of an extra API call to list existing review comments: one paginated GET per `publish_code_suggestions` invocation. Negligible vs. the AI call that preceded it.

## Rollout

Default `"update"`. Users who prefer the old behavior set `"off"` in their `.pr_agent.toml`. No migration needed for historical comments.
