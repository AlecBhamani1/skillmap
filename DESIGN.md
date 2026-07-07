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

1. **discover** (`skillmap/discover.py`) — walk `~/.claude/skills` and
   `~/.agents/skills` (following symlinks), parse each `SKILL.md`'s frontmatter
   (`name`, `description`), slash-command triggers, section headings, and
   `references/` listing. Stdlib-only YAML-ish parsing; no dependency.

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
   BFS for comparison.

## Usage

```bash
./bin/skillmap build                       # discover → extract → build graph.json + graph.html
./bin/skillmap list                        # list discovered skills
./bin/skillmap scope "<work context>"      # relevant skill neighborhood for a context
./bin/skillmap scope "<ctx>" --json        # machine-readable
./bin/skillmap scope "<ctx>" --min-ratio 0 # keep the full ranked gradient
./bin/skillmap query "<question>"          # graphify's own BFS traversal
./bin/skillmap show                        # summarize the built graph
```

Output lands in `skillmap-out/` (`graph.json`, `graph.html`).

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
- **No always-on hint yet.** README point 3 ("keep a tiny always-on hint that
  the graph exists") would be a small router skill that calls `skillmap scope`
  at selection time. Not built in this POC.
- **No dedup/consolidation pass** (README point 4). Rebuild is full each time.
