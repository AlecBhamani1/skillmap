"""skillmap CLI — graph-scoped retrieval for Claude skills.

Commands:
  skillmap build [--root DIR ...] [--out DIR] [--max-concepts N]
      Discover installed skills, extract a skill/concept graph, and hand it to
      graphify to build graph.json (+ community clustering).

  skillmap list [--root DIR ...]
      List discovered skills without building.

  skillmap scope "<work context>" [--out DIR] [--top-k N] [--json]
      Return only the relevant neighborhood of skills for a work context.

  skillmap query "<question>" [--out DIR]
      Run graphify's own BFS query against the built graph.

  skillmap show [--out DIR]
      Print a summary of the built graph.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .discover import DEFAULT_ROOTS, discover
from .extract import build_extraction
from .graph import build_graph, find_graphify_python, graphify_query
from .scope import SkillGraph

DEFAULT_OUT = Path("skillmap-out")


def _roots(args) -> list[Path]:
    return [Path(r) for r in args.root] if args.root else DEFAULT_ROOTS


def cmd_list(args) -> int:
    skills = discover(_roots(args))
    if not skills:
        print("No skills found. Checked:", ", ".join(str(r) for r in _roots(args)))
        return 1
    print(f"Discovered {len(skills)} skill(s):\n")
    for s in skills:
        trig = " ".join(s.triggers) if s.triggers else "(no trigger)"
        refs = f" · {len(s.references)} refs" if s.references else ""
        print(f"  • {s.name}  [{trig}]{refs}")
        desc = (s.description[:110] + "…") if len(s.description) > 110 else s.description
        if desc:
            print(f"      {desc}")
    return 0


def cmd_build(args) -> int:
    out = Path(args.out)
    roots = _roots(args)
    print(f"→ Discovering skills under: {', '.join(str(r) for r in roots)}")
    skills = discover(roots)
    if not skills:
        print("No skills found — nothing to build.")
        return 1
    print(f"  found {len(skills)} skill(s): {', '.join(s.name for s in skills)}")

    print("→ Extracting skill/concept graph…")
    extraction = build_extraction(skills, max_concepts_per_skill=args.max_concepts)
    n_skill = sum(1 for n in extraction["nodes"] if n.get("skillmap_kind") == "skill")
    n_concept = sum(1 for n in extraction["nodes"] if n.get("skillmap_kind") == "concept")
    print(f"  {n_skill} skill nodes · {n_concept} concept nodes · {len(extraction['edges'])} edges")

    print("→ Locating graphify engine…")
    gpy = find_graphify_python()
    if not gpy:
        print("  ERROR: graphify not installed. Install with: uv tool install graphifyy")
        # Still write the extraction so it's inspectable / usable elsewhere.
        out.mkdir(parents=True, exist_ok=True)
        (out / ".skillmap_extract.json").write_text(
            json.dumps(extraction, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  wrote raw extraction to {out / '.skillmap_extract.json'}")
        return 2
    print(f"  using {gpy}")

    print("→ Building graph via graphify…")
    summary = build_graph(extraction, gpy, out)
    print(f"  graph.json: {summary.get('nodes')} nodes · {summary.get('edges')} edges "
          f"· {summary.get('communities')} communities")
    print(f"\n✓ Built {out / 'graph.json'}")
    if summary.get("html"):
        print(f"  Viz:  open {out / 'graph.html'}")
    print(f"  Try:  skillmap scope \"fixing a failing test\" --out {out}")
    return 0


def cmd_scope(args) -> int:
    out = Path(args.out)
    graph_path = out / "graph.json"
    if not graph_path.exists():
        print(f"No graph at {graph_path}. Run `skillmap build` first.")
        return 1
    g = SkillGraph.load(graph_path)
    results = g.scope(args.context, top_k=args.top_k, min_ratio=args.min_ratio)
    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
        return 0
    if not results:
        print(f"No skills matched: {args.context!r}")
        print("(The work context shares no vocabulary with any skill's concepts.)")
        return 0
    print(f"Relevant skills for: {args.context!r}\n")
    for r in results:
        bar = "█" * min(int(r.score * 4) + 1, 20)
        print(f"  {r.score:6.2f} {bar}  {r.name}")
        if r.matched_concepts:
            # "→ X" entries mean "linked via peer skill X"; plain ones are concepts.
            label = "related to" if r.matched_concepts[0].startswith("→") else "via"
            shown = [c.lstrip("→ ") for c in r.matched_concepts[:6]]
            print(f"          {label}: {', '.join(shown)}")
        if r.triggers:
            print(f"          trigger: {' '.join(r.triggers)}")
    print(f"\n({len(results)} of the installed skills surfaced — the rest are scoped out.)")
    return 0


def cmd_query(args) -> int:
    out = Path(args.out)
    graph_path = out / "graph.json"
    if not graph_path.exists():
        print(f"No graph at {graph_path}. Run `skillmap build` first.")
        return 1
    gpy = find_graphify_python()
    if not gpy:
        print("graphify not installed.")
        return 2
    print(graphify_query(gpy, graph_path, args.question, budget=args.budget, dfs=args.dfs))
    return 0


def cmd_show(args) -> int:
    out = Path(args.out)
    graph_path = out / "graph.json"
    if not graph_path.exists():
        print(f"No graph at {graph_path}. Run `skillmap build` first.")
        return 1
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    nodes = data.get("nodes", [])
    skills = [n for n in nodes if n.get("skillmap_kind") == "skill"]
    concepts = [n for n in nodes if n.get("skillmap_kind") == "concept"]
    n_edges = len(data.get("links", data.get("edges", [])))
    print(f"Graph: {len(nodes)} nodes ({len(skills)} skills, {len(concepts)} concepts), "
          f"{n_edges} edges")
    print("\nSkills:")
    for s in sorted(skills, key=lambda n: n.get("label", "")):
        print(f"  • {s.get('label')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skillmap", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"skillmap {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--root", action="append", help="skills root dir (repeatable)")

    b = sub.add_parser("build", help="build the skill graph")
    add_common(b)
    b.add_argument("--out", default=str(DEFAULT_OUT), help="output dir (default: skillmap-out)")
    b.add_argument("--max-concepts", type=int, default=12, help="concepts per skill")
    b.set_defaults(func=cmd_build)

    ls = sub.add_parser("list", help="list discovered skills")
    add_common(ls)
    ls.set_defaults(func=cmd_list)

    sc = sub.add_parser("scope", help="scope skills to a work context")
    sc.add_argument("context", help="the work context, e.g. 'fixing a failing test'")
    sc.add_argument("--out", default=str(DEFAULT_OUT))
    sc.add_argument("--top-k", type=int, default=8)
    sc.add_argument("--min-ratio", type=float, default=0.1,
                    help="drop skills below this fraction of the top score (0=keep all)")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(func=cmd_scope)

    q = sub.add_parser("query", help="graphify BFS query against the graph")
    q.add_argument("question")
    q.add_argument("--out", default=str(DEFAULT_OUT))
    q.add_argument("--budget", type=int, default=2000)
    q.add_argument("--dfs", action="store_true")
    q.set_defaults(func=cmd_query)

    sh = sub.add_parser("show", help="summarize the built graph")
    sh.add_argument("--out", default=str(DEFAULT_OUT))
    sh.set_defaults(func=cmd_show)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
