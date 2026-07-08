# skillmap — POC design & usage

A proof-of-concept for the retrieval/graph layer described in `README.md`. It
builds a knowledge graph of installed Claude skills using **graphify** as the
graph engine, then scopes which skills are relevant to a given work context —
surfacing a *neighborhood* instead of the whole set.

## How it works

```
  discover ──► extract ──► graphify build ──► scope
  (SKILL.md)   (nodes+     (graph.json +      (work context →
               edges)       clustering +       relevant skills)
                            graph.html)
```

1. **discover** (`skillmap/discover.py`) — walk the skill roots (following
   symlinks), parse each `SKILL.md`'s frontmatter (`name`, `description`),
   slash-command triggers, section headings, and `references/` listing.
   Stdlib-only YAML-ish parsing; no dependency. Roots are the **global** ones
   (`~/.claude/skills`, `~/.agents/skills`) plus the **project** root
   `<project>/.claude/skills`, auto-detected via the nearest `.git` upward from
   cwd (`--project-dir` overrides detection, `--project-only` drops the global
   roots, `--root` overrides everything). Skill nodes carry a
   `skillmap_origin` of `project` or `global`.

2. **extract** (`skillmap/extract.py`) — turn skills into a graphify-compatible
   extraction JSON with two node tiers:
   - **skill nodes** (`file_type: document`) — one per SKILL.md; the scoping targets.
   - **concept nodes** (`file_type: concept`) — topics mined from each skill's
     description/headings/body, weighted by where they appear.

   Edges use graphify's fixed relation vocabulary only:
   - `skill --references--> concept` (EXTRACTED, 1.0)
   - `skill --semantically_similar_to--> skill` when two skills share ≥2 concepts
     (INFERRED, 0.65–0.85 by overlap) or a slash-command trigger (0.9).

   This step is **deterministic and key-free**, so the POC runs end to end with
   no API key. (Graphify's own doc/semantic extraction needs a Gemini key or
   host-agent subagents; skillmap sidesteps that by extracting structure itself,
   since skills are small, well-structured markdown.)

3. **graphify build** (`skillmap/graph.py`) — locate graphify's interpreter the
   same way its skill does (binary shebang / `uv tool run` / `python3`), then
   call `graphify.build.build_from_json` → `graphify.cluster.cluster` →
   `graphify.export.to_json` + `to_html` in that interpreter. graphify owns the
   graph; skillmap only feeds it and reads the result. Output: `graph.json`
   (networkx node-link format) + interactive `graph.html`.

4. **scope** (`skillmap/scope.py`) — the payoff. Given a work context:
   - tokenize + light stemming, match against concept and skill labels
     (IDF-weighted so rare concepts discriminate more),
   - seed matched nodes and BFS-propagate a decaying score across edges,
   - collect **skill** nodes by accumulated score, drop anything below
     `min_ratio × top_score`, return the ranked neighborhood.

   Mirrors graphify's own query traversal (label-match seeds → BFS neighborhood)
   but post-filters to skill nodes and returns a ranked list — the scoping
   primitive the README calls for. `skillmap query` also exposes graphify's raw
   BFS for comparison. Results include the `SKILL.md` path (so the caller can
   load the body) and the project/global origin. When no `graph.json` has been
   built yet, scope falls back to the raw extraction
   (`.skillmap_extract.json`), so the whole recall path also works with **no
   graphify installed**.

## Project-level skills: the self-improvement loop

`skillmap add-skill` (`skillmap/author.py`) is the agent-facing **write path**.
An agent that just learned a durable, project-specific procedure runs:

```bash
skillmap add-skill fix-flaky-ci \
  --description "Diagnose and fix flaky CI failures in this repo's integration pipeline." \
  --body-file notes.md --reference deep-detail.md
```

which

1. writes a lean, well-formed `SKILL.md` under
   `<project>/.claude/skills/<name>/` — validated frontmatter (kebab-case
   name; a description long enough to key the graph on), body capped at 450
   lines with supporting files pushed into `references/`; and
2. **refreshes the graph incrementally**: re-runs the deterministic extraction
   over all roots (milliseconds — it's just markdown parsing) and merges it
   into the existing `graph.json` *in place*, without invoking graphify.
   graphify-computed node attributes (community labels) are preserved; new
   nodes inherit the majority community of their neighbors; skills deleted
   from disk drop out of the graph (a small consolidation pass for free).
   A later `skillmap build` re-clusters properly and regenerates the HTML.

Future sessions then recall it with `skillmap scope "<work context>"`. If the
skill already exists, `add-skill` exits **3** with route → merge → refactor
guidance instead of blind-appending; `--force` writes the merged version.

`skillmap hint --install` appends a tiny marker-guarded block to the project's
`CLAUDE.md` — the always-on hint (README point 3) that tells an agent to query
the graph before working and to save what it learns. Idempotent.

## Usage

```bash
./bin/skillmap build                       # discover → extract → build graph.json + graph.html
./bin/skillmap list                        # list discovered skills (global + project)
./bin/skillmap add-skill <name> --description "…" [--body-file F] [--reference F]
                                           # author a project skill + refresh graph
./bin/skillmap scope "<work context>"      # relevant skill neighborhood for a context
./bin/skillmap scope "<ctx>" --json        # machine-readable (name, score, path, origin)
./bin/skillmap scope "<ctx>" --min-ratio 0 # keep the full ranked gradient
./bin/skillmap hint [--install]            # print/install the always-on router hint
./bin/skillmap query "<question>"          # graphify's own BFS traversal
./bin/skillmap show                        # summarize the built graph
```

**Every command** accepts `--project-dir DIR`, `--project-only`, and
`--root DIR` (no documented flag ever errors). Their effect per command:

- `--project-dir DIR` — explicit project root (instead of auto-detecting via
  the nearest `.git`). Also anchors the default `--out` at
  `DIR/skillmap-out`, so `scope`/`show`/`query` read the graph built *for that
  project* without needing `--out`.
- `--project-only` — on discovery commands (`build`, `list`, `add-skill`'s
  refresh): scan only the project's `.claude/skills`, not the global roots.
  On `scope`: surface only project-level skills in the results. On `show`:
  list only project-level skills. On `query`/`hint`: accepted for
  consistency; graphify's raw traversal doesn't filter by origin (use `scope`).
- `--root DIR` — override all discovery roots (repeatable); only meaningful
  where skills are discovered.

Exit codes: `0` success · `1` invalid input / nothing found · `2` graphify
missing · `3` skill already exists (merge, then `--force`).

Output lands in `skillmap-out/` (`graph.json`, `graph.html`,
`.skillmap_extract.json`).

## What this POC demonstrates

- Skills → a real graphify knowledge graph, keyed on stable identifiers
  (skill name, triggers, mined concepts) rather than "feels related".
- A single work-context query returns a **scoped neighborhood** of skills, with
  the connecting concepts shown for auditability — and returns *nothing* for an
  unrelated context, instead of ranking every installed skill.

## Known limitations / next steps

- **Concept mining is frequency-based**, not semantic. With a Gemini key (or
  host-agent subagents) the extraction could use graphify's LLM path for richer
  concept and cross-skill edges. The extraction JSON schema is already
  graphify-native, so this is a drop-in upgrade.
- **Bundled skills** (deep-research, dataviz, code-review, …) have no on-disk
  `SKILL.md`, so they aren't graphed. Feeding their name+description (from the
  system prompt) as synthetic nodes would complete the map.
- **Dedup/consolidation is partial** (README point 4). The incremental refresh
  drops deleted skills and refuses blind appends on existing names, but there
  is no pass yet that detects *near-duplicate* skills and proposes merges.
- **Incremental refresh doesn't re-cluster.** New nodes inherit a neighbor's
  community; run `skillmap build` periodically for a real re-clustering.
