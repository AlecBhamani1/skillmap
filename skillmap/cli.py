"""skillmap CLI — graph-scoped retrieval for Claude skills.

Commands:
  skillmap build [--root DIR ...] [--out DIR] [--max-concepts N]
      Discover installed skills (global + this project), extract a
      skill/concept graph, and hand it to graphify to build graph.json
      (+ community clustering).

  skillmap list [--root DIR ...]
      List discovered skills without building.

  skillmap add-skill NAME --description "..." [--body-file F] [--reference F ...]
      Author a project-level skill (<project>/.claude/skills/NAME/SKILL.md)
      and refresh the graph incrementally so `scope` recalls it immediately.

  skillmap scope "<work context>" [--out DIR] [--top-k N] [--json]
      Return only the relevant neighborhood of skills for a work context.

  skillmap hint [--install]
      Print (or install into the project CLAUDE.md) the tiny always-on hint
      that tells an agent to query and grow the skill graph.

  skillmap query "<question>" [--out DIR]
      Run graphify's own BFS query against the built graph.

  skillmap show [--out DIR]
      Print a summary of the built graph.

Every command accepts --project-dir/--project-only/--root. --project-dir also
anchors the default --out at <project-dir>/skillmap-out, so read commands find
the graph that was built for that project. On `scope`/`show`, --project-only
filters the output to project-level skills; on discovery commands it restricts
scanning to the project's .claude/skills.

Exit codes: 0 success · 1 invalid input / nothing found · 2 graphify missing
· 3 skill already exists (merge, then --force).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .author import SkillExistsError, refresh_graph, write_skill
from .discover import DEFAULT_ROOTS, discover, find_project_root, project_skills_root
from .extract import build_extraction
from .graph import build_graph, find_graphify_python, graphify_query
from .scope import SkillGraph

DEFAULT_OUT = Path("skillmap-out")


def _project_root(args) -> Path | None:
    """Explicit --project-dir wins; else auto-detect from cwd (nearest .git)."""
    pd = getattr(args, "project_dir", None)
    return Path(pd).resolve() if pd else find_project_root()


def _roots(args) -> list[Path]:
    """Discovery roots: --root overrides everything; else global roots plus the
    detected project's .claude/skills; --project-only keeps just the latter."""
    if getattr(args, "root", None):
        return [Path(r) for r in args.root]
    proj = _project_root(args)
    proj_skills = project_skills_root(proj) if proj else None
    if getattr(args, "project_only", False):
        return [proj_skills] if proj_skills else []
    roots: list[Path] = []
    if proj_skills and proj_skills not in DEFAULT_ROOTS:
        # Project root first: discover() dedups same-named skills first-wins,
        # so a project skill must be seen before same-named global ones in
        # order to override them (a team can't otherwise ever shadow a global
        # skill with a project-specific variant).
        roots.append(proj_skills)
    roots.extend(DEFAULT_ROOTS)
    return roots


def _out(args) -> Path:
    """Artifact dir: explicit --out wins; an explicit --project-dir anchors the
    default at <project>/skillmap-out; else ./skillmap-out."""
    if getattr(args, "out", None):
        return Path(args.out)
    pd = getattr(args, "project_dir", None)
    if pd:
        return Path(pd).resolve() / DEFAULT_OUT.name
    return DEFAULT_OUT


def _graph_path(out: Path) -> Path | None:
    """The built graph, or the raw extraction as a graphify-free fallback."""
    for name in ("graph.json", ".skillmap_extract.json"):
        p = out / name
        if p.exists():
            return p
    return None


def cmd_list(args) -> int:
    roots = _roots(args)
    if getattr(args, "project_only", False) and not roots:
        print("No project root found (no .git upward from cwd) — nothing to scan "
              "with --project-only. Pass --project-dir.", file=sys.stderr)
        return 1
    skills = discover(roots)
    if not skills:
        print("No skills found. Checked:", ", ".join(str(r) for r in roots))
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
    out = _out(args)
    roots = _roots(args)
    if getattr(args, "project_only", False) and not roots:
        print("No project root found (no .git upward from cwd) — nothing to build "
              "with --project-only. Pass --project-dir.", file=sys.stderr)
        return 1
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
    out = _out(args)
    graph_path = _graph_path(out)
    if not graph_path:
        print(f"No graph in {out}. Run `skillmap build` (or `skillmap add-skill`) first.")
        return 1
    g = SkillGraph.load(graph_path)
    origin = "project" if args.project_only else None
    results = g.scope(args.context, top_k=args.top_k, min_ratio=args.min_ratio, origin=origin)
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
        tag = " [project]" if r.origin == "project" else ""
        print(f"  {r.score:6.2f} {bar}  {r.name}{tag}")
        if r.matched_concepts:
            # "→ X" entries mean "linked via peer skill X"; plain ones are concepts.
            label = "related to" if r.matched_concepts[0].startswith("→") else "via"
            shown = [c.lstrip("→ ") for c in r.matched_concepts[:6]]
            print(f"          {label}: {', '.join(shown)}")
        if r.triggers:
            print(f"          trigger: {' '.join(r.triggers)}")
        if r.path:
            print(f"          read: {r.path}")
    print(f"\n({len(results)} of the installed skills surfaced — the rest are scoped out.)")
    return 0


HINT_MARKER = "<!-- skillmap:hint -->"
HINT = f"""{HINT_MARKER}
## Skills (skillmap)
This project keeps a skill graph. Before non-trivial work, surface what applies:
`skillmap scope "<what you're about to do>"` — then read the surfaced SKILL.md.
When you learn a durable, project-specific procedure, save it so future
sessions recall it: `skillmap add-skill <name> --description "<when to use it>"
--body-file <notes.md>`. Merge into an existing skill instead of creating
near-duplicates.
<!-- /skillmap:hint -->"""


def cmd_add_skill(args) -> int:
    proj = _project_root(args)
    if not proj:
        print("No project root found (no .git upward from cwd). "
              "Pass --project-dir to say where the project lives.", file=sys.stderr)
        return 1
    skills_root = project_skills_root(proj)

    body = args.body or ""
    if args.body_file:
        if args.body_file == "-":
            body = sys.stdin.read()
        else:
            bf = Path(args.body_file)
            if not bf.is_file():
                print(f"--body-file not found: {bf}", file=sys.stderr)
                return 1
            body = bf.read_text(encoding="utf-8")

    try:
        skill_md = write_skill(
            skills_root, args.name, args.description, body,
            reference_files=[Path(r) for r in args.reference or []],
            force=args.force,
        )
    except SkillExistsError as e:
        print(str(e), file=sys.stderr)
        return 3
    except ValueError as e:
        print(f"Invalid skill: {e}", file=sys.stderr)
        return 1

    summary: dict = {}
    if not args.no_refresh:
        summary = refresh_graph(_roots(args), _out(args),
                                max_concepts_per_skill=args.max_concepts)

    if args.json:
        print(json.dumps({"created": str(skill_md), "name": args.name,
                          "skills_root": str(skills_root), "graph": summary}, indent=2))
        return 0
    print(f"✓ Created {skill_md}")
    if summary:
        mode = summary.get("mode")
        if mode == "incremental":
            print(f"  graph updated in place: {summary['graph']} "
                  f"({summary['nodes']} nodes, +{summary['added_nodes']} new)")
        else:
            print(f"  extraction refreshed: {summary['graph']} "
                  f"(run `skillmap build` for clustering + viz)")
    print(f"  Recall it with: skillmap scope \"{args.description[:60]}\"")
    return 0


def cmd_hint(args) -> int:
    if not args.install:
        print(HINT)
        return 0
    proj = _project_root(args)
    if not proj:
        print("No project root found. Pass --project-dir.", file=sys.stderr)
        return 1
    claude_md = proj / "CLAUDE.md"
    existing = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
    if HINT_MARKER in existing:
        print(f"Hint already installed in {claude_md}")
        return 0
    sep = "\n\n" if existing and not existing.endswith("\n\n") else ""
    claude_md.write_text(existing + sep + HINT + "\n", encoding="utf-8")
    print(f"✓ Installed skillmap hint into {claude_md}")
    return 0


def cmd_query(args) -> int:
    out = _out(args)
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
    out = _out(args)
    graph_path = _graph_path(out)
    if not graph_path:
        print(f"No graph in {out}. Run `skillmap build` first.")
        return 1
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    nodes = data.get("nodes", [])
    skills = [n for n in nodes if n.get("skillmap_kind") == "skill"]
    concepts = [n for n in nodes if n.get("skillmap_kind") == "concept"]
    n_edges = len(data.get("links", data.get("edges", [])))
    print(f"Graph: {len(nodes)} nodes ({len(skills)} skills, {len(concepts)} concepts), "
          f"{n_edges} edges")
    if args.project_only:
        skills = [s for s in skills if s.get("skillmap_origin") == "project"]
        print(f"\nProject skills ({len(skills)}):")
    else:
        print("\nSkills:")
    for s in sorted(skills, key=lambda n: n.get("label", "")):
        tag = " [project]" if s.get("skillmap_origin") == "project" else ""
        print(f"  • {s.get('label')}{tag}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skillmap", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"skillmap {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp, project_only_help: str):
        """Shared project/root flags. EVERY subcommand gets these, so no
        documented flag ever hits an argparse error on any command."""
        sp.add_argument("--root", action="append",
                        help="skills root dir (repeatable; overrides global+project "
                             "defaults when discovering skills)")
        sp.add_argument("--project-dir", metavar="DIR",
                        help="project root (default: auto-detect via nearest .git); "
                             "also anchors the default --out at DIR/skillmap-out")
        sp.add_argument("--project-only", action="store_true", help=project_only_help)

    def add_out(sp, what: str = "output"):
        sp.add_argument("--out", default=None,
                        help=f"{what} dir (default: skillmap-out, or "
                             "<project-dir>/skillmap-out when --project-dir is given)")

    SCAN_ONLY = "scan only the project's .claude/skills, not global roots"

    b = sub.add_parser("build", help="build the skill graph")
    add_common(b, SCAN_ONLY)
    add_out(b)
    b.add_argument("--max-concepts", type=int, default=12, help="concepts per skill")
    b.set_defaults(func=cmd_build)

    ls = sub.add_parser("list", help="list discovered skills")
    add_common(ls, SCAN_ONLY)
    ls.set_defaults(func=cmd_list)

    ad = sub.add_parser(
        "add-skill", aliases=["learn"],
        help="author a project-level skill and make it recallable via scope",
        description="Write <project>/.claude/skills/NAME/SKILL.md and refresh the "
                    "graph incrementally so `skillmap scope` surfaces it immediately. "
                    "Exit 3 means the skill exists: merge into it, then use --force.")
    ad.add_argument("name", help="skill name (lowercase kebab-case, e.g. deploy-staging)")
    ad.add_argument("--description", required=True,
                    help="what the skill does and when to use it — the field the "
                         "graph and selector key on; make it specific")
    ad.add_argument("--body", default="", help="SKILL.md body (markdown)")
    ad.add_argument("--body-file", metavar="PATH",
                    help="read the body from a file ('-' for stdin)")
    ad.add_argument("--reference", action="append", metavar="PATH",
                    help="copy a file into the skill's references/ (repeatable); "
                         "put supporting detail here, keep the body lean")
    ad.add_argument("--force", action="store_true",
                    help="overwrite an existing SKILL.md (after merging into it)")
    ad.add_argument("--no-refresh", action="store_true",
                    help="write the skill but skip the graph refresh")
    add_out(ad, "graph output")
    ad.add_argument("--max-concepts", type=int, default=12, help="concepts per skill")
    ad.add_argument("--json", action="store_true")
    add_common(ad, SCAN_ONLY + " when refreshing the graph")
    ad.set_defaults(func=cmd_add_skill)

    h = sub.add_parser("hint", help="print/install the always-on 'query the graph' hint")
    h.add_argument("--install", action="store_true",
                   help="append the hint to the project's CLAUDE.md (idempotent)")
    add_common(h, "accepted for consistency; hint only uses --project-dir")
    h.set_defaults(func=cmd_hint)

    sc = sub.add_parser("scope", help="scope skills to a work context")
    sc.add_argument("context", help="the work context, e.g. 'fixing a failing test'")
    add_out(sc, "graph")
    sc.add_argument("--top-k", type=int, default=8)
    sc.add_argument("--min-ratio", type=float, default=0.1,
                    help="drop skills below this fraction of the top score (0=keep all)")
    sc.add_argument("--json", action="store_true")
    add_common(sc, "surface only project-level skills in the results")
    sc.set_defaults(func=cmd_scope)

    q = sub.add_parser("query", help="graphify BFS query against the graph")
    q.add_argument("question")
    add_out(q, "graph")
    q.add_argument("--budget", type=int, default=2000)
    q.add_argument("--dfs", action="store_true")
    add_common(q, "accepted for consistency; graphify's raw traversal does not "
                  "filter by origin (use `scope` for that)")
    q.set_defaults(func=cmd_query)

    sh = sub.add_parser("show", help="summarize the built graph")
    add_out(sh, "graph")
    add_common(sh, "list only project-level skills")
    sh.set_defaults(func=cmd_show)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
