# skillmap

Graph-scoped retrieval for Claude skills. skillmap links skills to the work
they actually apply to, so a session surfaces only the relevant *neighborhood*
of skills instead of every installed one competing for selection.

## The problem it solves

Claude preloads the **name + description** of every installed skill into the
system prompt at startup. The full `SKILL.md` body only loads on demand
(progressive disclosure), so idle skills are cheap. The bottleneck is not token
cost and not the context window — it's **selection accuracy**:

- With hundreds of overlapping `Use when…` descriptions, the model has to pick
  the right skill from a large candidate set, and misfires rise.
- A 1M-token window makes everything *fit*, but retrieval accuracy doesn't scale
  with window size (context rot / lost-in-the-middle). "It fits" and "the model
  can reliably pick the right one" are different claims.
- Anthropic shipped a search-then-load layer for **MCP tools**, but there is no
  native equivalent for **skills** yet. That layer has to be built.

## The approach

Build the retrieval/graph layer that scopes skill candidates by relevance:

1. **Anchor edges to stable identifiers**, not "feels related" — repo, object,
   client/org, process stage. Derive edges from the work; don't have the model
   guess them.
2. **Surface a neighborhood, not the whole set.** At selection time, query the
   graph and expose only the skills connected to the current work context.
   Fewer distractors → higher selection accuracy.
3. **Keep a tiny always-on hint** that the graph exists, or the model never
   queries it.
4. **Dedup / consolidation pass** so accumulated routing errors and near-
   duplicate edges get cleaned up instead of compounding.

## Skill authoring principles (what feeds the graph)

- Prefer **broad skills** that encode recurring procedures — not a skill per
  one-off action.
- When new work relates to an existing skill, **route → merge → refactor** into
  progressively-disclosed reference files under the skill dir. "Append" must not
  mean string concatenation, which balloons the `SKILL.md` body past the ~500-
  line guideline and makes every trigger pay for it.
- The **description is the single most important field** — it's what the graph
  and the selector both key on.

## Health metrics

Watch **selection precision** and **body length**, not idle token count. Idle
cost is trivial; the accuracy of retrieval and the leanness of triggered bodies
are what determine whether the system holds up as it grows.

## Status

Working proof-of-concept. A `skillmap` CLI discovers installed skills —
**global** (`~/.claude/skills`, `~/.agents/skills`) and **project-level**
(`<project>/.claude/skills`, auto-detected via the nearest `.git`) — extracts
a skill/concept graph, hands it to **graphify** for graph building +
clustering, and scopes the relevant skill neighborhood for a given work
context. See [`DESIGN.md`](DESIGN.md) for architecture and usage.

```bash
./bin/skillmap build                    # discover → graph.json + graph.html
./bin/skillmap scope "<work context>"   # relevant skill neighborhood
```

The loop is closed for **self-improvement at the project level**: an agent
that learns a durable, project-specific procedure saves it with

```bash
./bin/skillmap learn <name> --description "<when to use it>" --body-file notes.md
```

(`add-skill` is the same command; `learn` is the agent-facing name). This
writes a lean `SKILL.md` into the project and updates the graph
**incrementally** (no graphify call, no full rebuild), so the next
`skillmap scope` in a future session recalls it. `skillmap hint --install`
plants the tiny always-on hint (point 3 above) — including *when* to bank a
skill (a non-obvious, repeatable, project-specific procedure, not a one-off
fact) — into the project's `CLAUDE.md`.

Before writing, `learn`/`add-skill` scores the new description against every
currently-installed skill (a fresh, in-memory graph — no graphify call). A
strong match — by score and by shared, non-generic concepts, not just an
exact name collision — is reported as "similar existing skill: `<name>`
(`<path>`) — merge into it, or re-run with `--force`" and exits 3, the same
contract as an exact-name collision: this is the point-4 dedup pass, scoped
to the moment of creation rather than a sweep over already-existing skills.

Frequency mining only ever finds words an author literally wrote, so an
optional **semantic enrichment** pass layers synonym/abstraction concepts and
concept↔concept bridges onto the same graph — that's what lets
`scope "work with spreadsheets"` find a skill that only ever says "xlsx".
Two routes produce the same payload: zero-key (`skillmap enrich-prompt` →
have any capable agent answer it → `skillmap build --concepts-file
answer.json`) or direct (`skillmap build --enrich`, using
`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` via stdlib `urllib`). Results
cache per SKILL.md content hash, survive `learn`'s incremental refresh, and
never break the key-free deterministic build.

Still to build from the design above: a full dedup/consolidation sweep over
*already-existing* skills (today's guard only catches near-duplicates at
`learn` time).
