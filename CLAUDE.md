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
./bin/skillmap build                       # discover → extract → build skillmap-out/graph.json + graph.html
./bin/skillmap list                        # list discovered skills (no build)
./bin/skillmap scope "<work context>"      # ranked, scoped skill neighborhood for a context
./bin/skillmap scope "<ctx>" --json        # machine-readable
./bin/skillmap scope "<ctx>" --min-ratio 0 # keep the full ranked gradient (default 0.1)
./bin/skillmap query "<question>"          # graphify's own raw BFS traversal, for comparison
./bin/skillmap show                        # summarize the built graph
```

`build`/`list`/`scope`/`query`/`show` all take `--out DIR` (default `skillmap-out/`) and
`--root DIR` (repeatable; defaults to `~/.claude/skills` and `~/.agents/skills`).

Tests (pure-Python layer, no graphify required):

```bash
python -m pytest tests/                                     # all tests
python -m pytest tests/test_skillmap.py::test_scope_ranks_relevant_skill_first  # single test
python tests/test_skillmap.py                               # self-runner (no pytest dependency)
```

## Architecture

A four-stage pipeline; `skillmap/cli.py` wires the subcommands to each stage.

```
discover ──► extract ──► graphify build ──► scope
(SKILL.md)   (nodes +     (graph.json +      (work context →
             edges)        clustering +       relevant skills)
                           graph.html)
```

1. **`discover.py`** — walks the skill roots (following symlinks), parses each `SKILL.md`'s
   frontmatter (`name`, `description`), slash-command triggers, headings, and `references/`.
   `DEFAULT_ROOTS` and the `Skill` dataclass live here. Stdlib-only YAML-ish parsing.

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
   feeds it and reads back `graph.json` (networkx node-link format).

4. **`scope.py`** — the payoff. `SkillGraph.scope()` tokenizes + lightly stems the context,
   IDF-weighted-matches concept/skill labels to seed nodes, BFS-propagates a decaying score
   across edges, keeps **skill** nodes above `min_ratio × top_score`, and returns a ranked
   list. `SkillGraph` also accepts a raw extraction dict (with `"edges"`), which the tests use
   to bypass graphify.

## Key constraints when editing

- **skillmap itself is stdlib-only** (`pyproject.toml` has empty `dependencies`). graphify is
  an *external runtime* dependency invoked as a subprocess, never imported into this package.
  Keep it that way — don't add PyPI deps to the core layer.
- **Extraction must stay graphify-compatible.** Edges need `source`, `target`, `relation`,
  `confidence`, `confidence_score`, and `relation` must be one of graphify's three verbs.
  `tests/test_skillmap.py::test_build_extraction_schema` guards this contract.
- **Concept mining is frequency-based, not semantic** — a known limitation. The extraction
  schema is already graphify-native, so an LLM concept pass (Gemini key / host-agent
  subagents) is a drop-in upgrade, not a rewrite.
- The graphify interpreter-discovery in `graph.py` deliberately **mirrors the graphify
  skill's own Step 1 detection** — keep them in sync if graphify's install story changes.

Not yet built (see `DESIGN.md` "next steps"): the always-on router/hint layer, a
dedup/consolidation pass, and graphing bundled skills that have no on-disk `SKILL.md`.
