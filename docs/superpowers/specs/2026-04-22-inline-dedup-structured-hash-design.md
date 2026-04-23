# Inline-comment dedup: structured-edit hash with prose fallback

**Date:** 2026-04-22
**Branch:** `feat/persistent-inline-comments`
**Status:** Design, awaiting user review before plan + implementation

## Problem

PR-Agent's inline-comment dedup key is currently
`sha256(file | label | normalize(content)[:128])[:12]`, where `content` is the
suggestion's prose (`suggestion_content`). Because LLM output is paraphrased
across runs even when the underlying finding is identical, two runs can produce
two different hashes for the same logical suggestion and both end up posted as
separate inline comments.

A live example that motivated this work: two suggestions that both propose the
edit `cleanup_mode=None if dry_run else cleanup_mode,` at the same line,
accompanied by prose that differs in wording ("passed through unchanged" vs
"forwarded unchanged", "design spec and plan" vs "design doc", etc.). The
actionable fix is byte-identical. The dedup scheme missed it because it keyed
on prose.

## Non-goals

- LLM-based similarity / semantic matching. Explicitly ruled out by the user.
- Embedding-based similarity. Same.
- Fuzzy near-miss matching (shingle / Jaccard / MinHash with a similarity
  threshold). Captured as future work in the Serena memory
  `future_fuzzy_inline_dedup` and in the user-facing docs; not in this change.

## Core design decision

**Identity of an inline suggestion is the actionable edit, not the prose that
describes it.** When the suggestion carries an `improved_code` field (the
proposed replacement code), that code — normalized — is the dedup key. Prose
is only consulted when no `improved_code` is available.

This is deliberately a **strict (a)** rule: prose is **never** used to match
two suggestions that both have `improved_code`. Two suggestions at the same
spot with identical prose but different `improved_code` are treated as distinct
and remain as two separate comments. We'd rather under-merge than over-merge
genuinely distinct fixes.

### What this is NOT

- It is **not** "match on either structured or prose" (OR-semantics). That
  would reintroduce false merges in the same-prose-diff-edit case.
- It is **not** dual-marker emit. Only one marker is embedded per comment.
  Dual-emit with asymmetric lookup was considered to guard against a
  hypothetical flip-flop case (same logical suggestion producing
  `improved_code` on one run and not on the next). The flip-flop does not
  arise in the `/improve` flow, where `improved_code` is effectively required
  (`pr_agent/tools/pr_code_suggestions.py` accesses `d['improved_code']`
  unconditionally and drops suggestions that fail to parse). Dual-emit can be
  added later as an additive change if ever needed — the marker grammar is
  stable.

### Why line-range is NOT in the hash

The current scheme is deliberately **stable across line-number drift**: a
suggestion that moves down the file because a new commit added lines above
still matches on re-run, so the prior comment gets edited in place rather than
replaced by a duplicate. The user-facing docs explicitly promise this.

Putting line-range in the key would break that promise. Instead, we rely on
the `improved_code` itself to provide enough specificity — in practice the
proposed replacement carries enough unique context (function name, variable
names, multiple lines) that two genuinely unrelated suggestions producing the
same normalized `improved_code` in the same file is vanishingly rare. If it
does happen, both comments would carry the same marker; the outcome is a
single comment covering both spots, which is acceptable.

## Hash scheme

Two version-tagged namespaces, exactly one emitted per comment, selected by
input availability.

### Structured path (preferred)

When `suggestion["improved_code"]` is present, non-empty, and
`suggestion["relevant_file"]` is present:

```
sig = "v2s\x00" + file + "\x00" + normalize_code(improved_code)
marker_hash = sha256(sig).hexdigest()[:12]
```

Note: `label` is deliberately **not** part of the structured key. The edit
itself is the identity; classifying the same edit differently across runs
(e.g., "possible issue" vs "best practice") should not split the comment.

### Prose fallback

Otherwise — triggered when `improved_code` is missing or empty, including
prose-only suggestions, malformed data, and non-`/improve` flows:

```
sig = "v2p\x00" + file + "\x00" + label + "\x00" + normalize_prose(content)[:128]
marker_hash = sha256(sig).hexdigest()[:12]
```

If the prose fallback is selected and any of `file`, `label`, or `content`
is missing, `generate_marker` returns `None` (same as today's behavior —
the caller then skips marker embedding for that comment).

### Early-return rules (replacing today's all-or-nothing check)

```
if not file:
    return None
if improved_code is a non-empty string:
    # structured path — label/content not required
    build v2s signature, return hash
else:
    # prose fallback
    if not label or not content:
        return None
    build v2p signature, return hash
```

### Why the `v2s` / `v2p` tags live inside the hashed signature

This makes the two namespaces preimage-distinct: a structured hash and a
prose hash on the same inputs are guaranteed to differ. The marker grammar
`<!-- pr-agent-inline-id:XXXX -->` is unchanged, so the existing extraction
regex continues to work and no data migration is required.

### Why `\x00` as the field separator

`\x00` (ASCII NUL) cannot legally appear in file paths, labels, or source
code the compiler/interpreter would accept, so no field value can forge a
separator. The current scheme uses `|`, which a path or code body could in
principle contain, producing aliasing between otherwise-distinct inputs.
This is defensive and costs nothing.

### `normalize_code(text: str) -> str`

```
1. Expand tabs to spaces
2. Split on "\n"
3. Strip trailing whitespace from each line
4. Drop leading and trailing fully-blank lines
5. textwrap.dedent-style: strip the longest common leading whitespace
   across remaining lines
6. Collapse runs of internal whitespace within each line to a single space
7. Rejoin with "\n"
```

Rationale: the two live example comments differ almost entirely in
indentation of the fenced diff block (step 5 handles this) and in small
whitespace variations (step 6). Token content survives normalization, so
genuinely different edits still produce different hashes.

### `normalize_prose(text: str) -> str`

Unchanged from current behavior: `re.sub(r"\s+", " ", text).strip()`. We are
not trying to catch paraphrase via prose normalization — that's what
`normalize_code` on the structured edit is for.

## Lookup semantics

Single-probe. For each new suggestion:

```
new_hash = generate_marker(suggestion)       # v2s if improved_code present, else v2p
existing = existing_index.get(new_hash)
```

No secondary probe. No cross-namespace fallback at lookup time. This is what
enforces strict (a).

## Migration / backward-compat

No migration code. `v1` markers (existing in live PRs today) are produced by
hashing a signature without the `v2s`/`v2p` prefix, so `v1` and `v2` hashes
are preimage-distinct and cannot collide.

On the first run after deployment, for each PR that already has `v1` markers:

1. The new suggestion computes a `v2` hash. That hash is not in
   `existing_index`, so no update/skip match → a fresh `v2`-marked comment is
   posted.
2. The outdated pass (`resolve_outdated_inline_comments`, already on this
   branch) sees the `v1`-marked comment as "not re-emitted on this run" and
   auto-resolves it with the standard note.

Net effect: one transitional run per PR in which old comments get auto-resolved
while their `v2` replacements appear. Self-healing; no flag, no backfill, no
schema migration.

If `resolve_outdated_inline_comments = false` (non-default), the `v1` comment
remains unresolved alongside the `v2` replacement, exactly as if a user had
hand-rewritten the old suggestion. Acceptable for a one-time transition.

## Code-comment placement

A focused block at the top of `generate_marker` in
`pr_agent/algo/inline_comments_dedup.py`:

```python
# Dedup identity is structured-first, prose-fallback:
#   - If a suggestion has `improved_code`, the hash covers
#     (file + normalized improved_code). Prose wording never affects the key.
#   - Otherwise we fall back to (file + label + prose prefix).
#
# This is a strict (a) design: prose is NOT consulted when a structured
# edit exists. Two suggestions at the same spot with the same prose but
# different edits intentionally remain separate comments — we'd rather
# under-merge than over-merge genuinely distinct fixes.
#
# Line-range is deliberately NOT in the key so dedup stays stable across
# upstream pushes that drift the target line (a property the user-facing
# docs explicitly promise).
#
# A fuzzy near-miss signal (shingle / Jaccard) was considered and
# deferred; see docs/docs/tools/improve.md and the Serena memory
# `future_fuzzy_inline_dedup`.
```

## Documentation changes

### File: `docs/docs/tools/improve.md`

Two changes:

1. **Update the `persistent_inline_comments` row** of the config table so
   "content-derived hash" becomes "hash of the proposed edit (or of the
   suggestion prose when no edit is present)".

2. **Add a new section** `## How inline-comment deduplication works` placed
   immediately after the config tables. Structure:

   - One-paragraph overview: why dedup exists, when it runs.
   - **Identity rule**: the structured-first / prose-fallback rule in plain
     language. Explicitly state that prose wording does not affect the key
     when a structured edit is available, and that this is intentional
     (strict behaviour — we prefer under-merging to over-merging).
   - **What's invariant** (stable across runs): wording/paraphrase changes,
     reindentation of the proposed edit, upstream commits that drift the
     target line.
   - **What still splits**: genuinely different proposed edits at the same
     spot; label changes when the prose fallback is in use.
   - **Future work**: a near-miss fuzzy signal may be added later if users
     report recurring duplicates that the primary/fallback scheme doesn't
     catch; for now the behaviour is deterministic and strict.

## Tests

### `tests/unittest/test_inline_comments_dedup.py` (extend)

This is the existing unit-test file for `generate_marker` and friends.
Add cases:

- `generate_marker` with structured inputs (`improved_code` present)
  produces a deterministic 12-hex hash.
- Reindenting `improved_code` (different common leading whitespace)
  produces the **same** hash. This is the primary regression test for the
  cleanup_mode live example.
- Whitespace-only variations within `improved_code` lines produce the
  same hash.
- Changing a single non-whitespace token in `improved_code` produces a
  **different** hash.
- Paraphrasing `suggestion_content` while `improved_code` stays the same
  produces the **same** hash (prose does not affect the structured key).
- With `improved_code` omitted or empty and prose present, `generate_marker`
  falls back to prose and is deterministic.
- With `improved_code` empty AND `label` missing, `generate_marker`
  returns `None`.
- For otherwise-identical inputs, the structured and prose hashes differ
  (cross-namespace preimage distinctness — sanity check that the `v2s` /
  `v2p` tag prevents aliasing).
- Existing test `test_stable_across_line_shifts` continues to pass — the
  new key still excludes line numbers.

### `tests/unittest/test_github_inline_dedup.py` (add one case)

- Two suggestions with paraphrased prose but identical `improved_code` at
  the same file compute the same marker; with `persistent_inline_comments`
  = `"update"`, the second run reaches the `edit_review_comment` path
  rather than creating a new comment.

No changes required to `test_github_inline_dedup.py`'s existing cases —
they stub `generate_marker` / `get_bot_review_comments` at the provider
seam.

## Files touched

- `pr_agent/algo/inline_comments_dedup.py` — hash scheme + `normalize_code`
  + code comment.
- `docs/docs/tools/improve.md` — updated config-row copy + new section.
- `tests/unittest/test_inline_comments_dedup.py` — new cases.
- `tests/unittest/test_github_inline_dedup.py` — one integration-ish case.

Providers (`github_provider.py`, `gitlab_provider.py`) need no changes: they
call `generate_marker` / `build_marker_index` through the same seam.

## Out of scope

- Fuzzy / near-miss matching (tracked in Serena memory
  `future_fuzzy_inline_dedup`).
- Dual-marker emission and asymmetric lookup (future additive change if
  real flip-flop cases are observed).
- Changes to GitLab / Bitbucket providers beyond what the shared module
  provides.
- Backport / migration code for `v1` markers (self-heals via outdated pass).
