# Resolve Outdated Inline Comments — Design

**Date:** 2026-04-21
**Status:** Approved — ready for implementation plan
**Branch:** `feat/persistent-inline-comments`
**Builds on:** [Persistent Inline Comments](2026-04-16-persistent-inline-comments-design.md)

## Problem

The persistent inline-comment dedup feature (just landed on this branch) edits in place when the same suggestion is re-emitted, eliminating duplicates. But it does nothing about the inverse case: a comment was posted on a previous run, the underlying issue is no longer flagged on the current run, yet the comment remains open. Reviewers have no signal that the suggestion is no longer current.

This design extends the dedup pass to also *resolve* threads whose suggestion was not re-emitted, so reviewers see only currently relevant inline comments.

## Goals

- Resolve inline-comment threads whose marker hash was present on a prior run but is absent on the current run.
- Append a small, reviewer-facing note explaining why the thread was auto-resolved.
- Respect manual user action: if a user has already resolved a thread, or has explicitly unresolved a thread we previously closed, do not touch it.
- Re-open (unresolve) when a previously outdated suggestion comes back.
- Work across GitHub and GitLab; degrade to no-op on other providers.
- Zero behavior change when the new setting is disabled.

## Non-goals

- Diff-driven outdated detection (GitHub's native `outdated` flag triggered by line shift). The trigger here is purely marker-set delta.
- Resolving threads that lack our dedup marker (i.e., comments posted before the dedup feature shipped or by other tools).
- Per-thread state stored outside the providers; we infer everything from comment bodies and provider-side resolution state.

## Approach

After the existing dedup loop in `publish_code_suggestions` finishes, compute `outdated = existing_markers − emitted_markers` and, for each, resolve the thread and append an explanatory note to the comment body. Use a second hidden marker (`<!-- pr-agent-inline-resolved -->`) in the appended note to make our prior action recognizable on subsequent runs.

This is purely additive to the dedup pathway — same data source, same loop's index, same best-effort error posture.

## Architecture

```
pr_agent/algo/inline_comments_dedup.py        (MODIFY — constants only)
    + RESOLVED_NOTE         = "Resolved automatically: this suggestion was not re-emitted on the latest run."
    + RESOLVED_BODY_MARKER  = "<!-- pr-agent-inline-resolved -->"

pr_agent/git_providers/git_provider.py        (MODIFY)
    + GitProvider.resolve_review_thread(comment)   -> bool   default: False
    + GitProvider.unresolve_review_thread(comment) -> bool   default: False

pr_agent/git_providers/github_provider.py     (MODIFY)
    ~ get_bot_review_comments()         -- switch from REST to GraphQL;
                                           returns dicts with thread_id + is_resolved
    + resolve_review_thread(comment)    -- GraphQL resolveReviewThread
    + unresolve_review_thread(comment)  -- GraphQL unresolveReviewThread
    ~ publish_code_suggestions()        -- track emitted hashes; outdated pass

pr_agent/git_providers/gitlab_provider.py     (MODIFY)
    ~ get_bot_review_comments()         -- include discussion_id, resolved
    + resolve_review_thread(comment)    -- discussion.resolved = True
    + unresolve_review_thread(comment)  -- discussion.resolved = False
    ~ publish_code_suggestions()        -- track emitted hashes; outdated pass

pr_agent/settings/configuration.toml          (MODIFY)
    + [pr_code_suggestions].resolve_outdated_inline_comments = true

tests/unittest/test_github_inline_dedup.py    (EXTEND)
tests/unittest/test_gitlab_inline_dedup.py    (EXTEND)
docs/docs/tools/improve.md                    (DOCUMENT)
```

## Comment dict shape

`get_bot_review_comments()` returns a uniform shape across providers; new fields are additive so the existing dedup loop is unaffected.

```python
{
  "id":          <str|int>,    # provider comment id (GitHub REST databaseId; GitLab note id)
  "thread_id":   <str>,        # GitHub: review-thread node id; GitLab: discussion id
  "body":        <str>,
  "path":        <str|None>,
  "line":        <int|None>,
  "start_line":  <int|None>,
  "is_resolved": <bool>,
}
```

`resolve_review_thread` and `unresolve_review_thread` take the **whole dict** (not just an id) so each provider can pull whichever field it needs without coupling callers to provider-specific id schemes.

## Data flow inside `publish_code_suggestions`

```python
mode             = normalize_persistent_mode(settings.persistent_inline_comments)
resolve_outdated = settings.get("resolve_outdated_inline_comments", True)

existing_index = {}
if mode != PERSISTENT_MODE_OFF:
    try:
        existing_index = build_marker_index(get_bot_review_comments())
    except Exception as e:
        get_logger().warning(f"persistent_inline_comments: fetch failed: {e}")
        existing_index = {}

emitted = set()

for suggestion in validated:
    marker = generate_marker(suggestion["original_suggestion"])
    body   = append_marker(suggestion["body"], marker)
    h      = marker[len(MARKER_PREFIX):-len(MARKER_SUFFIX)]
    emitted.add(h)

    existing = existing_index.get(h)
    if existing:
        if mode == "skip":
            continue
        if edit_review_comment(existing["id"], body):
            # Re-emit case: if this thread had been auto-resolved on a prior
            # run but the suggestion is back, unresolve so the thread reflects
            # current relevance.
            if resolve_outdated and existing.get("is_resolved"):
                unresolve_review_thread(existing)
            continue
        # edit failed → fall through to create-new
    <create new inline comment>

# ---- Outdated pass (NEW) ----
if mode != PERSISTENT_MODE_OFF and resolve_outdated:
    for h, c in existing_index.items():
        if h in emitted:                            continue   # still relevant
        if c.get("is_resolved"):                    continue   # idempotent
        if RESOLVED_BODY_MARKER in c.get("body",""):
            continue                                            # we resolved it before; user may have unresolved
        if not resolve_review_thread(c):
            continue
        edit_review_comment(
            c["id"],
            c["body"].rstrip()
            + f"\n\n---\n_{RESOLVED_NOTE}_\n{RESOLVED_BODY_MARKER}",
        )
```

### Idempotency guards

Three independent skip conditions in the outdated pass:

1. `is_resolved` already true (someone — us or a human — has already resolved it).
2. Body contains `RESOLVED_BODY_MARKER` (we resolved-and-edited it on a prior run; if the user has since unresolved it, we respect that and stand down).
3. Hash is in `emitted` (suggestion is back; the dedup loop took care of it, including unresolving if needed).

### Order: resolve before edit

The mutation runs *before* the body edit so a partial failure leaves a resolved-but-unannotated thread (clear in the UI) rather than an unresolved-but-marked thread (confusing).

## GitHub provider

GitHub's resolve/unresolve mutations require the **review-thread node id**, which the REST `pulls/comments` endpoint does not expose. Switch the data source to GraphQL `pullRequest.reviewThreads`, which returns thread id + resolution state + comments in one paginated query.

### Fetch

```graphql
query($owner:String!, $name:String!, $number:Int!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first:100, after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id                              # thread node id (resolve/unresolve target)
          isResolved
          comments(first:100) {
            nodes {
              databaseId                  # REST id (edit_review_comment PATCH target)
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
```

Paginate until `hasNextPage` is false. Flatten to one dict per comment, attaching the parent `thread_id` and `is_resolved`. The bot-identity filter (github_provider.py:568-584) is reused unchanged on `comment.author.login`.

### Mutations

```graphql
mutation($threadId:ID!) {
  resolveReviewThread(input:{threadId:$threadId})   { thread { isResolved } }
}
mutation($threadId:ID!) {
  unresolveReviewThread(input:{threadId:$threadId}) { thread { isResolved } }
}
```

Return values are inspected only to confirm no error; we don't read the response.

### Transport

GraphQL uses the same auth path as the existing REST calls via PyGithub's `_requester`:

```python
_, data = self.pr._requester.requestJsonAndCheck(
    "POST", f"{self.base_url}/graphql",
    input={"query": QUERY, "variables": {...}},
)
```

No new dependency. Same token, same rate-limit bucket. A `data["errors"]` array (or any raised exception) is treated as failure.

## GitLab provider

No architecture change — `mr.discussions` already exposes per-discussion `id` and `resolved`. Only additive surface.

### Extend `get_bot_review_comments`

```python
{
  "id":          note.id,
  "thread_id":   discussion.id,                                    # NEW
  "body":        note.body or "",
  "path":        ...,
  "line":        ...,
  "start_line":  ...,
  "is_resolved": bool(getattr(discussion, "resolved", False)),     # NEW
}
```

`is_resolved` reads from the *discussion* (resolution is a discussion-level state on GitLab) but is attached to each note dict so callers stay provider-agnostic.

### Mutations

```python
def resolve_review_thread(self, c) -> bool:
    try:
        d = self.mr.discussions.get(c["thread_id"])
        if not getattr(d, "resolvable", True):
            return False                              # general (non-inline) discussions can't resolve
        d.resolved = True
        d.save()
        return True
    except Exception as e:
        get_logger().warning(f"GitLab resolve failed for {c.get('thread_id')}: {e}")
        return False
```

`unresolve_review_thread` is the same with `d.resolved = False`. Both are idempotent on GitLab (setting to current state is a no-op).

The bot-identity filter (case-insensitive, conditional on auth — `da89eb14`) is unchanged.

## Configuration

Append to `pr_agent/settings/configuration.toml` under `[pr_code_suggestions]`:

```toml
# When a previously-posted inline suggestion is no longer emitted on a re-run,
# resolve its thread (and append a short note) so reviewers see only currently
# relevant comments. Has no effect when persistent_inline_comments = "off".
resolve_outdated_inline_comments = true
```

Document under `docs/docs/tools/improve.md` next to `persistent_inline_comments`:

> `resolve_outdated_inline_comments` (default `true`) — when dedup is enabled, automatically resolve inline-comment threads whose suggestion is no longer emitted on a re-run. The thread body gets a short auto-resolve note. Has no effect if `persistent_inline_comments = "off"`. Reviewers can manually unresolve to opt that thread out of future auto-resolution; the bot detects the prior resolution marker and respects it.

## Testing

Two existing test files extended; no new test files.

### `tests/unittest/test_github_inline_dedup.py`

| Case | Setup | Assertion |
|---|---|---|
| outdated → resolve + edit | existing has hash A; new run emits hash B | `resolve_review_thread(A)` called once; `edit_review_comment(A.id, body+RESOLVED_NOTE)` called once; new comment created for B |
| outdated, already resolved | existing A has `is_resolved=True` | resolve and edit **not** called for A |
| outdated, body has resolved-marker | existing A body contains `RESOLVED_BODY_MARKER` | resolve and edit **not** called for A (respects manual unresolve) |
| re-emit after prior resolve | existing A has `is_resolved=True`; new run emits hash A | `edit_review_comment` called (dedup edit); `unresolve_review_thread(A)` called |
| setting off | `resolve_outdated_inline_comments=false` | dedup loop runs as before; outdated pass entirely skipped |
| `persistent_inline_comments = "off"` | setting off | outdated pass not entered (gated on dedup mode) |
| resolve mutation fails | `resolve_review_thread` returns `False` | `edit_review_comment` for the resolution note **not** called; no exception raised |
| edit-after-resolve fails | resolve OK, edit returns `False` | no exception; thread remains resolved without the note |

GraphQL is mocked at the `_requester.requestJsonAndCheck` seam, matching how the existing dedup tests mock REST.

### `tests/unittest/test_gitlab_inline_dedup.py`

Symmetric set, with `discussion.save()` and `discussion.resolved` as the seam:

- outdated → discussion fetched, `resolved=True` set, `save()` called, body edited.
- already-resolved discussion → no `save()` call.
- non-resolvable discussion (`resolvable=False`) → skipped, no `save()`.
- re-emit after prior resolve → `discussion.resolved=False` + `save()` called.
- setting off → outdated pass entirely skipped.

### Algo unit tests

None new. The algo module gains only two string constants (`RESOLVED_NOTE`, `RESOLVED_BODY_MARKER`); their effect is exercised end-to-end by the provider tests.

## Error handling

| Failure | Behavior |
|---|---|
| `get_bot_review_comments` throws | Log warning. Dedup index is empty. Outdated pass becomes a no-op. Publishing proceeds as create-new. |
| `resolve_review_thread` returns `False` | Skip the body-edit for that comment. Continue with next outdated comment. |
| `edit_review_comment` returns `False` after a successful resolve | Log. Thread is resolved but lacks the explanatory note. UI clearly shows resolved state. Acceptable. |
| `unresolve_review_thread` returns `False` (re-emit case) | Log. Body is freshly edited via dedup; thread stays resolved. Mildly confusing UI but content is current. |
| GraphQL `errors` array (GitHub) | Treated as failure: fetch returns `[]`; mutations return `False`. |
| Setting absent | `get(..., True)` defaults to enabled, matching `configuration.toml`. (Misconfigured non-bool values follow normal Python truthiness; user-visible outcome makes this self-correcting.) |

The outdated pass inherits the dedup layer's silent-fail posture: it never blocks publishing.

## Risks

- **Bot wrongly resolves a thread the user still cares about.** Mitigation: user manually unresolves once → `RESOLVED_BODY_MARKER` in the body causes the bot to stand down on every subsequent run. The note explains the cause clearly.
- **Marker collisions.** Same 12-char SHA prefix as the dedup spec; no new collision surface.
- **Rate cost.** GitHub: switches one REST call to one paginated GraphQL call (net latency neutral or better) plus O(outdated) resolve mutations. GitLab: one extra `discussions.get` per outdated comment. Both negligible vs. the AI call.
- **Order sensitivity.** The outdated pass runs *after* the dedup loop completes, so we never resolve a thread that the same run is about to re-emit. The `emitted` set is the source of truth.

## Out-of-scope providers

Bitbucket (cloud & server), Azure DevOps, Gitea, Gerrit, CodeCommit, local: not modified. Inherit base-class `False` returns from `resolve_review_thread` / `unresolve_review_thread`, so the outdated pass is a logged no-op. Follow-up can extend them once GitHub/GitLab is stable.

## Rollout

Default `true`. Behavior visible only on PRs that already carry dedup markers — i.e., PRs touched by `/improve` after the dedup feature shipped on this branch. No migration needed for historical comments. Rollback is a one-line `.pr_agent.toml` flip.
