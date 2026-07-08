#!/usr/bin/env python3
"""skillmap diagnostic: measures how viable the skill graph is as an AI scoping layer.

The question this answers: if an agent asks `skillmap scope "<work context>"`
before starting work, does it get the *right* skill first, with few distractors,
without needing every installed skill in context?

Three probe families, all stdlib-only and graphify-optional:

  1. HEALTH   — the pipeline itself: discovery finds skills, extraction obeys
                the graphify schema, a graph (or extraction fallback) loads,
                scope/hint answer without error.
  2. IDENTITY — for each installed skill, build a query from its own most
                distinctive description tokens (its name and triggers are
                excluded, so this is not a string-match giveaway) and check the
                skill ranks #1. Measures whether the graph can discriminate
                between the skills it holds.
  3. CURATED  — hand-written realistic work contexts with an expected winner
                (skipped when the expected skill isn't installed), including
                adversarial ones where a skill's description mentions *another*
                skill's keywords. Measures real-world selection accuracy.

Alongside accuracy it reports the two numbers that make scoping worth doing
for an agent at all:

  - compression: mean fraction of installed skills surfaced per query
                 (lower = fewer distractors in the model's context)
  - MRR:         mean reciprocal rank of the expected skill

Exit codes: 0 = all probes pass, 1 = accuracy or health degraded.
Usage: python diagnostics/diagnose.py [--json] [--graph PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from skillmap.discover import DEFAULT_ROOTS, discover, find_project_root, project_skills_root  # noqa: E402
from skillmap.extract import build_extraction  # noqa: E402
from skillmap.scope import SkillGraph, _tokens  # noqa: E402

# Realistic contexts with a known-correct skill. Only scored when the expected
# skill is installed. `adversarial: True` marks probes where a *different*
# installed skill's description mentions these keywords (cross-mention
# contamination) — the hardest and most valuable cases.
CURATED_PROBES = [
    {"context": "operate a browser window and click buttons on the desktop",
     "expect": "computer-use"},
    {"context": "build a knowledge graph of this codebase and query its architecture",
     "expect": "graphify"},
    {"context": "coordinate multiple agents with task DAGs and decision gates",
     "expect": "orchestration"},
    {"context": "manage worktrees and terminals from the command line",
     "expect": "orca-cli", "adversarial": True},
    {"context": "hand off this task to another agent in another worktree",
     "expect": "orca-cli", "adversarial": True},
    {"context": "find and install a new skill for PDF processing",
     "expect": "find-skills"},
    {"context": "read and wait on an Orca terminal then send a prompt to it",
     "expect": "orca-cli", "adversarial": True},
    {"context": "supervise several workers and wait for worker_done escalations",
     "expect": "orchestration"},
]

REQUIRED_EDGE_KEYS = {"source", "target", "relation", "confidence", "confidence_score"}
ALLOWED_RELATIONS = {"references", "semantically_similar_to", "conceptually_related_to"}


def _load_graph(graph_arg: str | None, skills) -> tuple[SkillGraph, str]:
    """Prefer a built graph.json; fall back to a fresh in-memory extraction."""
    if graph_arg:
        return SkillGraph.load(Path(graph_arg)), f"graph.json ({graph_arg})"
    default = REPO_ROOT / "skillmap-out" / "graph.json"
    if default.exists():
        return SkillGraph.load(default), f"graph.json ({default})"
    return SkillGraph(build_extraction(skills)), "in-memory extraction (no graph.json built)"


def _identity_query(skill, all_skills) -> str:
    """A skill's most distinctive description tokens, minus its own name/slug.

    Distinctive = appears in this skill's description but in few others'.
    """
    own = set(_tokens(skill.name)) | set(_tokens(skill.slug.replace("_", " ")))
    for t in skill.triggers:
        own |= set(_tokens(t))
    df: Counter = Counter()
    for s in all_skills:
        for tok in set(_tokens(s.description)):
            df[tok] += 1
    toks = [t for t in _tokens(skill.description) if t not in own]
    # rank by rarity (df asc), then by first appearance for determinism
    seen, ordered = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    first_pos = {t: i for i, t in enumerate(ordered)}
    ordered.sort(key=lambda t: (df[t], first_pos[t]))
    return " ".join(ordered[:6])


def run(graph_arg: str | None = None) -> dict:
    report: dict = {"health": [], "identity": [], "curated": [], "metrics": {}}
    ok = lambda name, passed, detail="": report["health"].append(
        {"check": name, "pass": bool(passed), "detail": detail})

    # ---- 1. HEALTH -----------------------------------------------------
    proj_root = find_project_root()
    roots = list(DEFAULT_ROOTS)
    if proj_root:
        roots.insert(0, project_skills_root(proj_root))
    skills = discover(roots)
    ok("discovery finds skills", len(skills) > 0, f"{len(skills)} skill(s)")

    extraction = build_extraction(skills)
    skill_nodes = [n for n in extraction["nodes"] if n.get("skillmap_kind") == "skill"]
    concept_nodes = [n for n in extraction["nodes"] if n.get("skillmap_kind") == "concept"]
    edge_schema_ok = all(
        REQUIRED_EDGE_KEYS <= set(e) and e["relation"] in ALLOWED_RELATIONS
        for e in extraction["edges"])
    ok("extraction schema graphify-compatible", edge_schema_ok,
       f"{len(skill_nodes)} skill / {len(concept_nodes)} concept nodes, "
       f"{len(extraction['edges'])} edges")
    orphan_skills = [
        n["label"] for n in skill_nodes
        if not any(e["source"] == n["id"] or e["target"] == n["id"]
                   for e in extraction["edges"])]
    ok("every skill node has edges", not orphan_skills,
       f"orphans: {orphan_skills}" if orphan_skills else "none orphaned")

    graph, graph_src = _load_graph(graph_arg, skills)
    ok("graph loads", len(graph.nodes) > 0, graph_src)

    installed = {s.name for s in skills}
    n_installed = max(len(installed), 1)

    # ---- 2. IDENTITY ---------------------------------------------------
    surfaced_counts: list[int] = []
    for skill in skills:
        q = _identity_query(skill, skills)
        results = graph.scope(q)
        names = [r.name for r in results]
        rank = names.index(skill.name) + 1 if skill.name in names else None
        surfaced_counts.append(len(names))
        report["identity"].append(
            {"skill": skill.name, "query": q, "rank": rank, "surfaced": names})

    # ---- 3. CURATED ----------------------------------------------------
    for probe in CURATED_PROBES:
        if probe["expect"] not in installed:
            continue
        results = graph.scope(probe["context"])
        names = [r.name for r in results]
        rank = names.index(probe["expect"]) + 1 if probe["expect"] in names else None
        surfaced_counts.append(len(names))
        report["curated"].append({
            "context": probe["context"], "expect": probe["expect"],
            "adversarial": probe.get("adversarial", False),
            "rank": rank, "surfaced": names})

    # ---- metrics -------------------------------------------------------
    def _accuracy(rows):
        scored = [r for r in rows if r["rank"] is not None or True]
        if not scored:
            return None, None
        top1 = sum(1 for r in scored if r["rank"] == 1) / len(scored)
        mrr = sum(1 / r["rank"] for r in scored if r["rank"]) / len(scored)
        return round(top1, 3), round(mrr, 3)

    id_top1, id_mrr = _accuracy(report["identity"])
    cu_top1, cu_mrr = _accuracy(report["curated"])
    adv_rows = [r for r in report["curated"] if r["adversarial"]]
    adv_top1, _ = _accuracy(adv_rows) if adv_rows else (None, None)
    compression = (sum(surfaced_counts) / len(surfaced_counts) / n_installed
                   if surfaced_counts else None)

    report["metrics"] = {
        "installed_skills": len(installed),
        "identity_top1": id_top1, "identity_mrr": id_mrr,
        "curated_top1": cu_top1, "curated_mrr": cu_mrr,
        "adversarial_top1": adv_top1,
        "mean_compression": round(compression, 3) if compression else None,
    }
    health_ok = all(h["pass"] for h in report["health"])
    # Viability bar: pipeline healthy, identity perfect (it's the easy tier),
    # curated at least 80% — adversarial misses are reported but only fail the
    # bar when they drag curated below that line.
    report["pass"] = bool(
        health_ok and (id_top1 or 0) == 1.0 and (cu_top1 if cu_top1 is not None else 1.0) >= 0.8)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    ap.add_argument("--graph", help="path to a built graph.json (default: skillmap-out/graph.json, else in-memory extraction)")
    args = ap.parse_args(argv)

    report = run(args.graph)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["pass"] else 1

    print("skillmap diagnostic")
    print("=" * 60)
    print("\n[health]")
    for h in report["health"]:
        print(f"  {'PASS' if h['pass'] else 'FAIL'}  {h['check']}  ({h['detail']})")
    print("\n[identity probes]  (skill's own distinctive words → itself #1?)")
    for r in report["identity"]:
        mark = "PASS" if r["rank"] == 1 else "FAIL"
        print(f"  {mark}  {r['skill']:<16} rank={r['rank']}  q=\"{r['query']}\"")
    print("\n[curated probes]  (realistic work context → expected skill #1?)")
    for r in report["curated"]:
        mark = "PASS" if r["rank"] == 1 else "FAIL"
        adv = " [adversarial]" if r["adversarial"] else ""
        print(f"  {mark}  expect={r['expect']:<14} rank={r['rank']}{adv}")
        print(f"        \"{r['context']}\"  → {r['surfaced']}")
    m = report["metrics"]
    print("\n[metrics]")
    print(f"  installed skills     : {m['installed_skills']}")
    print(f"  identity top-1 / MRR : {m['identity_top1']} / {m['identity_mrr']}")
    print(f"  curated  top-1 / MRR : {m['curated_top1']} / {m['curated_mrr']}")
    print(f"  adversarial top-1    : {m['adversarial_top1']}")
    print(f"  mean compression     : {m['mean_compression']}  (fraction of installed skills surfaced per query; lower = fewer distractors)")
    print(f"\n{'VIABLE' if report['pass'] else 'DEGRADED'} — exit {0 if report['pass'] else 1}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
