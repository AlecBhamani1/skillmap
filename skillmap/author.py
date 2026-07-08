"""Author project-level skills and keep the graph in sync.

This is the write path that makes skillmap self-improving: an agent that just
learned a durable, project-specific procedure calls `skillmap add-skill`, which

  1. writes a lean, well-formed SKILL.md under <project>/.claude/skills/<name>/
     (valid frontmatter, body kept under the ~500-line guideline, detail pushed
     into references/), and
  2. refreshes the graph incrementally so `skillmap scope` recalls the new
     skill immediately — no full graphify rebuild required.

The refresh re-runs skillmap's deterministic extraction over all roots (cheap:
it's just markdown parsing) and merges the result into the existing graph.json
in place, preserving graphify-added node attributes such as community labels.
graphify stays behind the subprocess boundary in graph.py; nothing here calls
it. A later `skillmap build` re-clusters and regenerates the HTML view.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from pathlib import Path

from .discover import discover
from .extract import build_extraction
from .scope import ScopedSkill, SkillGraph

# Keep SKILL.md lean (README ~500-line guideline, with headroom for frontmatter).
MAX_BODY_LINES = 450
# A description shorter than this can't discriminate in the graph or selector.
MIN_DESCRIPTION_LEN = 20

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

# Near-duplicate guard thresholds (author.py's near-duplicate check, below).
# Calibrated against tests/test_learn_loop.py's fixture skills: a genuine
# re-description of an existing procedure under a new name (e.g. "ship app to
# prod with canary" vs. an existing "deploy app to prod with canary/rollback"
# skill) clears both bars comfortably (score ~16, overlap 5); a related but
# distinct skill (e.g. a *staging* deploy vs. a *production* deploy skill)
# clears at most one (score ~8, overlap 2) and must not be blocked. Requiring
# both -- not just a high score -- rules out a false positive from one very
# rare/high-IDF word coincidentally shared by two unrelated skills.
NEAR_DUPLICATE_MIN_SCORE = 6.0
NEAR_DUPLICATE_MIN_OVERLAP = 3


class SkillExistsError(Exception):
    """Raised when the target skill already exists and force=False."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(
            f"Skill already exists: {path}\n"
            "Don't blind-append: read the existing SKILL.md, route -> merge -> "
            "refactor your new material into it (push detail into references/), "
            "then rerun with --force to write the merged version."
        )


class NearDuplicateSkillError(Exception):
    """Raised when an existing (differently-named) skill strongly overlaps the
    new one -- the failure mode SkillExistsError doesn't catch: an agent that
    re-derives a procedure already banked under another name, silently filling
    the graph with near-duplicate skill nodes instead of one better one.
    """

    def __init__(self, match: ScopedSkill):
        self.match = match
        super().__init__(
            f"similar existing skill: {match.name} ({match.path}) — "
            "merge into it, or re-run with --force"
        )


def find_near_duplicate(roots: list[Path], name: str, description: str,
                        max_concepts_per_skill: int = 12) -> ScopedSkill | None:
    """Return the closest currently-installed skill if it looks like the same
    procedure as (name, description) under a different name, else None.

    Builds a fresh extraction + SkillGraph over `roots` (no graphify, no
    on-disk graph.json needed) and scopes the candidate's own name tokens +
    description against it, exactly as `skillmap scope` would once the skill
    existed. A match only counts as a duplicate when it clears both
    NEAR_DUPLICATE_MIN_SCORE and NEAR_DUPLICATE_MIN_OVERLAP -- see the
    constants' docstring for why both are required.
    """
    skills = discover(roots)
    if not skills:
        return None
    extraction = build_extraction(skills, max_concepts_per_skill=max_concepts_per_skill)
    graph = SkillGraph(extraction)
    query = f"{name.replace('-', ' ')} {description}"
    results = graph.scope(query, top_k=1, min_ratio=0.0)
    if not results:
        return None
    top = results[0]
    overlap = [c for c in top.matched_concepts if not c.startswith("→")]
    if top.score >= NEAR_DUPLICATE_MIN_SCORE and len(overlap) >= NEAR_DUPLICATE_MIN_OVERLAP:
        return top
    return None


def validate_name(name: str) -> str | None:
    """Return an error message if `name` is not a valid skill name, else None."""
    if not _NAME_RE.match(name):
        return (
            f"Invalid skill name {name!r}: use lowercase kebab-case "
            "(letters, digits, hyphens; starts with a letter), e.g. 'deploy-staging'."
        )
    return None


def validate_description(description: str) -> str | None:
    """Return an error message if the description is too weak to key a graph on."""
    d = description.strip()
    if len(d) < MIN_DESCRIPTION_LEN:
        return (
            "Description too short. It is the single most important field — the "
            "graph and the selector both key on it. Say concretely what the skill "
            "does and when to use it (e.g. 'Use when …'), in a sentence or two."
        )
    return None


def render_skill_md(name: str, description: str, body: str,
                    references: list[str] | None = None) -> str:
    """Render a well-formed SKILL.md (frontmatter + lean body)."""
    desc = " ".join(description.split())  # collapse newlines for the folded scalar
    lines = [
        "---",
        f"name: {name}",
        "description: >-",
        f"  {desc}",
        "---",
        "",
    ]
    body = body.strip()
    if body:
        lines.append(body)
    if references:
        lines += ["", "## References", ""]
        lines += [f"- `references/{r}`" for r in references]
    return "\n".join(lines).rstrip() + "\n"


def write_skill(skills_root: Path, name: str, description: str, body: str = "",
                reference_files: list[Path] | None = None,
                force: bool = False,
                dedup_roots: list[Path] | None = None,
                max_concepts_per_skill: int = 12) -> Path:
    """Create <skills_root>/<name>/SKILL.md (+ references/). Returns the SKILL.md path.

    Raises ValueError on invalid name/description/oversized body,
    SkillExistsError if the skill exists (exact-name collision), and
    NearDuplicateSkillError if `dedup_roots` is given and an existing
    (differently-named) skill's description strongly overlaps this one --
    see find_near_duplicate(). --force bypasses *both* checks and overwrites
    SKILL.md, but never deletes existing references/.
    """
    for err in (validate_name(name), validate_description(description)):
        if err:
            raise ValueError(err)
    body_lines = body.strip().splitlines()
    if len(body_lines) > MAX_BODY_LINES:
        raise ValueError(
            f"Body is {len(body_lines)} lines (max {MAX_BODY_LINES}). Keep SKILL.md "
            "lean: keep the procedure in the body and move supporting detail into "
            "references/ (pass files via --reference)."
        )

    reference_files = [Path(r) for r in reference_files or []]
    for src in reference_files:
        if not src.is_file():
            raise ValueError(f"Reference file not found: {src}")

    skill_dir = Path(skills_root) / name
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists() and not force:
        raise SkillExistsError(skill_md)
    if dedup_roots and not force:
        dup = find_near_duplicate(dedup_roots, name, description,
                                  max_concepts_per_skill=max_concepts_per_skill)
        if dup:
            raise NearDuplicateSkillError(dup)

    ref_names: list[str] = []
    if reference_files:
        refdir = skill_dir / "references"
        refdir.mkdir(parents=True, exist_ok=True)
        for src in reference_files:
            shutil.copyfile(src, refdir / src.name)
            ref_names.append(src.name)

    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(render_skill_md(name, description, body, ref_names),
                        encoding="utf-8")
    return skill_md


def merge_extraction_into_graph(graph: dict, extraction: dict) -> dict:
    """Merge a fresh extraction into an existing graph.json dict, in place.

    The extraction is deterministic over the full current skill set, so it *is*
    the desired node/edge content — added skills appear, deleted skills vanish
    (a consolidation pass for free). What we must preserve from the old graph is
    everything graphify computed on top: per-node attributes like community
    labels. New nodes inherit the majority community of their neighbors so they
    land in a sensible cluster until the next full `skillmap build` re-clusters.
    """
    old_nodes = {n.get("id"): n for n in graph.get("nodes", [])}
    edges_key = "links" if "links" in graph or "edges" not in graph else "edges"

    new_nodes: list[dict] = []
    added: list[str] = []
    for n in extraction["nodes"]:
        node = dict(n)
        old = old_nodes.get(n["id"])
        if old:
            for k, v in old.items():
                if k not in node:  # graphify-added attrs (community, …)
                    node[k] = v
        else:
            added.append(n["id"])
        new_nodes.append(node)
    new_ids = {n["id"] for n in new_nodes}
    removed = [nid for nid in old_nodes if nid not in new_ids]

    edges = [dict(e) for e in extraction["edges"]]

    # Neighbor-majority community for new nodes, when the graph has communities.
    by_id = {n["id"]: n for n in new_nodes}
    if any("community" in n for n in new_nodes):
        neigh: dict[str, list[str]] = {}
        for e in edges:
            neigh.setdefault(e["source"], []).append(e["target"])
            neigh.setdefault(e["target"], []).append(e["source"])
        for nid in added:
            node = by_id[nid]
            if "community" in node:
                continue
            votes = Counter(
                by_id[m]["community"] for m in neigh.get(nid, [])
                if m in by_id and "community" in by_id[m]
            )
            if votes:
                node["community"] = votes.most_common(1)[0][0]

    graph["nodes"] = new_nodes
    graph[edges_key] = edges
    return {
        "nodes": len(new_nodes),
        "edges": len(edges),
        "added_nodes": len(added),
        "removed_nodes": len(removed),
    }


def refresh_graph(roots: list[Path], out_dir: Path,
                  max_concepts_per_skill: int = 12) -> dict:
    """Re-extract all skills under `roots` and sync the graph in `out_dir`.

    Incremental: merges into an existing graph.json (preserving graphify's
    clustering) without invoking graphify. Always (re)writes the raw extraction,
    which `skillmap scope` can load directly when no graph.json exists — so the
    create -> recall loop works even without graphify installed.
    """
    skills = discover(roots)
    extraction = build_extraction(skills, max_concepts_per_skill=max_concepts_per_skill)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ".skillmap_extract.json").write_text(
        json.dumps(extraction, ensure_ascii=False), encoding="utf-8")

    graph_path = out_dir / "graph.json"
    if graph_path.exists():
        try:
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            graph = {}
        summary = merge_extraction_into_graph(graph, extraction)
        graph_path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
        return {"mode": "incremental", "graph": str(graph_path),
                "skills": len(skills), **summary}
    return {
        "mode": "extraction-only",
        "graph": str(out_dir / ".skillmap_extract.json"),
        "skills": len(skills),
        "nodes": len(extraction["nodes"]),
        "edges": len(extraction["edges"]),
    }
