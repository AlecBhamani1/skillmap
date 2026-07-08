# skillmap improvement plan

Grounded in `diagnostics/diagnose.py` (2026-07-08 baseline). North star: **an AI agent
working in a project should (a) surface the one right skill before starting work, and
(b) capture what it figured out as a project-scoped skill so no future session re-figures
it out.** Every item below serves one of those two loops.

## Baseline (what the diagnostic measured)

| Metric | Value | Reading |
|---|---|---|
| Pipeline health | 4/4 pass | build → graph → scope all work, degrade gracefully without graphify |
| Identity top-1 | 1.00 | graph discriminates its own skills |
| Curated top-1 / MRR | 0.625 / 0.812 | realistic contexts mostly resolve |
| **Adversarial top-1** | **0.00** | **cross-mention contamination: all 3 fail** |
| Mean compression | 0.385 | ~2 of 5 skills surfaced per query — the scoping win is real |

## P1 — Fix cross-mention contamination (scope.py, extract.py) — the accuracy blocker

Skill descriptions routinely contain **redirection sentences**: "Use `orca-cli` instead
for …", "Prefer this over raw `git worktree` …", "Use Computer Use for browser windows …".
Today those sentences' tokens are credited to the *mentioning* skill, both in concept
mining (`extract._pick_concepts`) and query-time seeding (`scope._seed_scores`' description
haystack). Result: orchestration outranks orca-cli on "manage worktrees and terminals" —
the exact query its own description says belongs to orca-cli.

Required behavior:
- Detect redirection spans in a description (sentences matching patterns like
  "use X instead …", "use X for …", "prefer X over …", "X is better for …" where X names
  another installed skill, backticked or plain).
- Tokens from those spans must **not** seed or grow concepts for the mentioning skill.
- Where the named skill exists in the graph, the span is a **routing signal**: credit it
  to the named skill (seed boost and/or a directed `references` edge), so the disclaimer
  actively helps the right skill win.
- The displayed `skillmap_description` in scope output stays verbatim — only scoring
  changes.
- Success bar: `diagnostics/diagnose.py` adversarial top-1 = 1.0, identity stays 1.0,
  curated ≥ 0.875, compression ≤ 0.45.

## P2 — Phrase-level concepts (extract.py, scope.py)

Concept mining is unigram-frequency only, so "knowledge graph", "task DAG", "work
context" shatter into generic single words that collide across skills. Add bigram
concepts (adjacent non-stopword pairs above a frequency floor, description/heading
weighted like unigrams), and make scope's matching reward a query that hits both words of
a bigram label (adjacency/full-label bonus > two independent unigram hits). Keeps the
extraction schema graphify-native (bigram label is just a concept node label with a
space). Success bar: schema test still passes; diagnostic metrics do not regress and
compression improves or holds.

## P3 — Close the learn loop (author.py, cli.py, hint)

The write path exists (`add-skill`, exit 3 on name collision) but only blocks **exact
name** duplicates. The product idea — agents banking project procedures — dies if each
session invents a new name for the same procedure and the graph fills with near-dupes.

- Before writing, `add-skill` runs the new skill's description through `scope()` against
  existing skills; a strong match (top score over a threshold, or matched-concept overlap)
  prints the merge target ("similar existing skill: X — merge into it, or pass --force")
  and exits 3, same contract as the name collision.
- Add `skillmap learn` as the agent-facing verb (thin alias of `add-skill`, same flags) —
  CLAUDE.md already talks about it; make it real.
- Sharpen `hint` text so an agent knows *when* to bank a skill: after figuring out a
  non-obvious, repeatable, project-specific procedure (build quirks, deploy steps, test
  invocations, data locations), not one-off facts.
- Keep exit codes exactly: 0/1/2/3. No new codes.

## P4 — Make selection quality a regression gate (tests/)

`diagnostics/diagnose.py` probes the live machine; CI needs the same probes over
**fixture skills**. Add a pytest module with a small fixture skill set that reproduces the
cross-mention pattern (a "coordinator" skill whose description says "use worker-cli
instead for terminals") plus phrase-concept and learn-dedup cases, driven through
`build_extraction` + `SkillGraph` (no graphify needed — same trick the existing tests
use). This locks P1–P3 behavior forever.

## Constraints (unchanged, non-negotiable)

- skillmap core stays **stdlib-only**; graphify is subprocess-only.
- Extraction schema stays graphify-native (three relations; required edge keys).
- Exit-code contract 0/1/2/3 (`cli.py` docstring).
- `author.py` incremental merge keeps preserving graphify-computed node attrs.

## Later (out of scope for this round)

LLM/semantic concept pass (drop-in per DESIGN.md), bundled-skill graphing, full
near-duplicate consolidation sweep over already-existing skills, session-end auto-capture
hook wiring.
