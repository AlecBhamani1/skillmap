# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`skillmap` is a proof-of-concept CLI for **graph-scoped retrieval of Claude skills**. It
builds a knowledge graph of installed skills and, given a work context, surfaces only the
relevant *neighborhood* of skills instead of ranking all of them. The goal is **selection
accuracy** (fewer distractors → the model picks the right skill), not token savings — see
`README.md` for the motivation and `DESIGN.md` for the architecture.

## Commands

Run from a source checkout via the launcher (no install needed — it inserts the repo root on
`sys.path`):

```bash
./bin/skillmap build                           # discover → extract → build skillmap-out/graph.json + graph.html
./bin/skillmap build --enrich                  # + semantic concepts via the Anthropic API (needs ANTHROPIC_API_KEY/_AUTH_TOKEN)
./bin/skillmap build --concepts-file F         # + semantic concepts from an answered enrich-prompt (zero-key route)
./bin/skillmap enrich-prompt                   # print the enrichment prompt for a host agent/LLM to answer
./bin/skillmap list                            # list discovered skills (no build)
./bin/skillmap add-skill NAME --description ".." [--body-file F] [--reference F]
                                                # author a project skill + refresh graph.json in place
./bin/skillmap scope "<work context>"          # ranked, scoped skill neighborhood for a context
./bin/skillmap scope "<ctx>" --json            # machine-readable
./bin/skillmap scope "<ctx>" --min-ratio 0     # keep the full ranked gradient (default 0.1)
./bin/skillmap hint [--install]                # print, or install into the project's CLAUDE.md, the always-on hint
./bin/skillmap query "<question>"              # graphify's own raw BFS traversal, for comparison
./bin/skillmap show                            # summarize the built graph
```

Every subcommand accepts `--root DIR` (repeatable; defaults to `~/.claude/skills` and
`~/.agents/skills`), `--project-dir DIR`, and `--project-only` — no documented flag ever hits an
argparse error. `--out DIR` (default `skillmap-out/`) is only on `build`/`add-skill`/`scope`/
`query`/`show` — `list` and `hint` don't touch a graph directory, so they don't have it.
`--project-dir` also anchors the default `--out` at `<project-dir>/skillmap-out`.
`--project-only` means "scan only the project's `.claude/skills`" on discovery commands, but
"surface/list only project-level skills" on `scope`/`show` (it's accepted-but-inert on `query`/
`hint`). Exit codes (see `cli.py`'s module docstring): `0` success · `1` invalid input / nothing
found · `2` graphify missing · `3` skill already exists (`add-skill`; merge into it, then
`--force`).

Tests (pure-Python layer, no graphify required):

```bash
python -m pytest tests/                                     # all tests
python -m pytest tests/test_skillmap.py::test_scope_ranks_relevant_skill_first  # single test
python tests/test_skillmap.py                               # self-runner (no pytest dependency)
```

## Architecture

A four-stage pipeline plus a graphify-free write path; `skillmap/cli.py` wires the
subcommands to each.

```
discover ──► extract ──► graphify build ──► scope
(SKILL.md)   (nodes +     (graph.json +      (work context →
             edges)        clustering +       relevant skills)
                           graph.html)

add-skill: write SKILL.md ──► extract ──► merge into existing graph.json in place
           (author.py)                    (no graphify call)
```

1. **`discover.py`** — walks the skill roots (following symlinks), parses each `SKILL.md`'s
   frontmatter (`name`, `description`), slash-command triggers (explicit `Trigger:`/`types /x`
   framing, plus the skill's own name as an implicit `/slug`), headings, and `references/`.
   `DEFAULT_ROOTS` (the two global roots) and the `Skill` dataclass live here;
   `find_project_root()`/`project_skills_root()` locate the project root (nearest `.git`
   upward from cwd) and its `<project>/.claude/skills`. Stdlib-only YAML-ish parsing.

2. **`extract.py`** — turns skills into a **graphify-native extraction JSON** with two node
   tiers: **skill nodes** (`skillmap_kind: "skill"`, the scoping targets) and **concept
   nodes** (`skillmap_kind: "concept"`, topics mined by weighted frequency). Edges use only
   graphify's fixed relation vocabulary (`references`, `semantically_similar_to`,
   `conceptually_related_to`). This step is **deterministic and key-free** — the POC runs end
   to end with no API key.

3. **`graph.py`** — skillmap does **not** reimplement graph building. `find_graphify_python()`
   locates a graphify install (binary shebang → `uv tool run graphifyy` → `python3`), then
   runs `graphify.build.build_from_json` → `cluster` → `export.to_json`/`to_html` **inside
   that interpreter** as a subprocess. The graph layer stays 100% graphify; skillmap only
   feeds it and reads back `graph.json` (networkx node-link format). When graphify isn't
   found, `build` still writes the raw extraction to `.skillmap_extract.json` and exits 2 —
   the POC degrades gracefully instead of dying with nothing to show.

4. **`scope.py`** — the payoff. `SkillGraph.scope()` tokenizes + lightly stems the context,
   IDF-weighted-matches concept/skill labels to seed nodes, BFS-propagates a decaying score
   across edges, keeps **skill** nodes above `min_ratio × top_score` (optionally restricted to
   one `skillmap_origin` first), and returns a ranked list. `SkillGraph` also accepts a raw
   extraction dict (with `"edges"`), which lets `cli._graph_path()` transparently fall back to
   `.skillmap_extract.json` when no `graph.json` exists yet — so `scope` works even without
   graphify installed — and which the tests use to bypass graphify.

5. **`enrich.py`** — the optional **semantic layer** on the same extraction. A payload of
   per-skill concepts + concept↔concept pairs (produced either by any host agent answering
   `skillmap enrich-prompt`, or by `build --enrich` calling the Anthropic Messages API via
   stdlib `urllib` — model `claude-opus-4-8`, override with `SKILLMAP_ENRICH_MODEL`) is
   validated/clamped, then applied as enriched concept nodes (`skillmap_source:
   "enrichment"`), `references` edges, and `conceptually_related_to` edges — the synonym
   bridges that let `scope` resolve queries whose words appear in no `SKILL.md`. Validated
   payloads cache per SKILL.md content hash in `<out>/.skillmap_enrich.json`;
   `refresh_graph` re-applies the cache so `add-skill` never loses enrichment, and a stale
   entry (edited skill) silently falls back to deterministic mining. Enrichment failure
   never fails a build — the deterministic graph is the floor.

6. **`author.py`** — the agent-facing **write path** behind `add-skill`/`learn`. Validates and
   writes a project-level `SKILL.md` (+ `references/`) under
   `<project>/.claude/skills/<name>/`, then re-runs the deterministic extraction over all roots
   and merges it into the existing `graph.json` **in place** — preserving graphify-computed
   attributes (e.g. community) on unchanged nodes and giving new nodes the majority community
   of their neighbors — with **no graphify subprocess call**. Raises `SkillExistsError` (CLI
   exit 3) unless `--force`, so re-running never blind-appends over an existing skill.

## Key constraints when editing

- **skillmap itself is stdlib-only** (`pyproject.toml` has empty `dependencies`). graphify is
  an *external runtime* dependency invoked as a subprocess, never imported into this package.
  Keep it that way — don't add PyPI deps to the core layer.
- **Extraction must stay graphify-compatible.** Edges need `source`, `target`, `relation`,
  `confidence`, `confidence_score`, and `relation` must be one of graphify's three verbs.
  `tests/test_skillmap.py::test_build_extraction_schema` guards this contract.
- **Exit codes are a documented contract** (`cli.py`'s module docstring): `0` success, `1`
  invalid input / nothing found, `2` graphify missing, `3` skill already exists. Keep new
  failure paths consistent with these rather than inventing new codes.
- **`author.py`'s incremental merge must keep preserving graphify-computed node attributes**
  (e.g. `community`) across a refresh — that's what lets `add-skill` update `graph.json`
  without ever invoking graphify. Don't regress it into a full node replace.
- **Deterministic mining is the floor; enrichment is an overlay.** `enrich.py`'s semantic
  pass must stay optional and non-fatal: no credentials/cache → the key-free deterministic
  build still works end to end, and `apply_cached` never raises. Keep the direct-API call on
  stdlib `urllib` (no `anthropic` PyPI dep — same rule as graphify-as-subprocess).
- The graphify interpreter-discovery in `graph.py` deliberately **mirrors the graphify
  skill's own Step 1 detection** — keep them in sync if graphify's install story changes.

Not yet built (see `DESIGN.md` "next steps"): graphing bundled skills that have no on-disk
`SKILL.md`, and a full dedup/consolidation pass over already-existing skills (near-duplicate
detection beyond `add-skill`'s pre-write guard and the incremental refresh's
drop-deleted-skills behavior).
