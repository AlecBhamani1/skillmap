"""Tests for skillmap's pure-Python layer (discovery, extraction, scoping).

These do not require graphify — they exercise the parsing/extraction/scoping
logic directly. Run with: python -m pytest tests/ (or python tests/test_skillmap.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
from contextlib import redirect_stderr, redirect_stdout  # noqa: E402
from unittest.mock import patch  # noqa: E402

from skillmap import cli  # noqa: E402
from skillmap import discover as discover_mod  # noqa: E402
from skillmap.author import SkillExistsError, refresh_graph, write_skill  # noqa: E402
from skillmap.discover import (  # noqa: E402
    Skill, discover, find_project_root, parse_skill, project_skills_root,
)
from skillmap.extract import build_extraction  # noqa: E402
from skillmap.scope import SkillGraph, _stem  # noqa: E402
from skillmap import graph as graph_mod  # noqa: E402


def _write_skill(root: Path, name: str, description: str, body: str = "",
                 refs: list[str] | None = None) -> Path:
    d = root / name
    (d / "references").mkdir(parents=True, exist_ok=True) if refs else d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: >-\n  {description}\n---\n{body}"
    (d / "SKILL.md").write_text(fm, encoding="utf-8")
    for r in refs or []:
        (d / "references" / r).write_text("# ref\n", encoding="utf-8")
    return d / "SKILL.md"


def test_parse_frontmatter(tmp_path):
    p = _write_skill(tmp_path, "widget", "Build widgets and gadgets for testing.",
                     body="## Usage\nTrigger: `/widget`\n## Details\nMore text.")
    s = parse_skill(p)
    assert s is not None
    assert s.name == "widget"
    assert "widgets" in s.description
    assert "/widget" in s.triggers
    assert "Usage" in s.headings


def test_parse_frontmatter_no_trailing_newline(tmp_path):
    # Some editors/heredocs omit the final newline, so the file ends exactly
    # at the closing "---" fence with no body and no trailing "\n".
    d = tmp_path / "notrail"
    d.mkdir(parents=True)
    fm = "---\nname: notrail\ndescription: >-\n  No trailing newline after the fence.\n---"
    (d / "SKILL.md").write_text(fm, encoding="utf-8")
    s = parse_skill(d / "SKILL.md")
    assert s is not None
    assert s.name == "notrail"
    assert "trailing newline" in s.description


def test_trigger_blocklist(tmp_path):
    p = _write_skill(tmp_path, "foo", "A foo skill.",
                     body="see `/tmp/x` and `/graphify-out` and `/abs` paths")
    s = parse_skill(p)
    # only the skill's own /foo should survive, not path-like slash tokens
    assert s.triggers == ["/foo"]


def test_trigger_framing_extracts_declared_trigger(tmp_path):
    # A body line framed with "Trigger:" declares an extra slash-command
    # alongside the skill's own name-derived trigger.
    p = _write_skill(tmp_path, "router", "Routes work to the right subagent.",
                     body="## Usage\nTrigger: `/route-work`\nDo the routing.")
    s = parse_skill(p)
    assert s is not None
    assert s.triggers == ["/route-work", "/router"]


def test_trigger_framing_ignores_unframed_paths(tmp_path):
    # A plain filesystem path mentioned in prose (no "trigger"/"types /x"
    # framing) must not be picked up as a declared trigger.
    p = _write_skill(tmp_path, "installer", "Installs the CLI binary.",
                     body="The binary is installed to /usr/local/bin by default.")
    s = parse_skill(p)
    assert s is not None
    assert s.triggers == ["/installer"]


def test_trigger_overlap_edge_in_extraction(tmp_path):
    # Two differently-named skills that both declare the same trigger should
    # get a high-confidence trigger-overlap edge in the extraction output.
    _write_skill(tmp_path, "alpha", "Alpha does alpha things.",
                 body="Trigger: `/shared-cmd`")
    _write_skill(tmp_path, "beta", "Beta does unrelated beta things.",
                 body="Trigger: `/shared-cmd`")
    skills = discover([tmp_path])
    ext = build_extraction(skills)
    overlap = [e for e in ext["edges"]
              if e.get("source_file") == "skillmap://trigger-overlap"]
    assert len(overlap) == 1
    e = overlap[0]
    assert {e["source"], e["target"]} == {"skill_alpha", "skill_beta"}
    assert e["relation"] in {"references", "semantically_similar_to", "conceptually_related_to"}
    assert e["confidence_score"] == 0.9


def test_discover_dedup(tmp_path):
    _write_skill(tmp_path, "alpha", "Alpha does alpha things.")
    _write_skill(tmp_path, "beta", "Beta does beta things.")
    skills = discover([tmp_path])
    names = {s.name for s in skills}
    assert names == {"alpha", "beta"}


def test_discover_explicit_empty_roots_finds_nothing(tmp_path):
    """An explicit roots=[] means "scan nothing", not "fall back to defaults"
    (discover() used to do `roots or DEFAULT_ROOTS`, which conflated the two)."""
    assert discover([]) == []


def test_project_skill_overrides_same_named_global_skill(tmp_path):
    """A project skill must take precedence over a same-named global skill:
    cli._roots() puts the project root ahead of the global roots, and
    discover()'s name dedup is first-wins, so the project version survives
    (path under the project's .claude/skills, origin "project") instead of
    being silently shadowed by the global one."""
    global_root = tmp_path / "global"
    _write_skill(global_root, "review", "Generic global review skill.")
    proj = _make_project(tmp_path)
    proj_skills_dir = project_skills_root(proj)
    _write_skill(proj_skills_dir, "review", "This repo's specific review checklist.")

    orig_roots = list(discover_mod.DEFAULT_ROOTS)
    discover_mod.DEFAULT_ROOTS[:] = [global_root]  # isolate from the real ~/.claude/skills
    try:
        args = argparse.Namespace(root=None, project_dir=str(proj), project_only=False)
        roots = cli._roots(args)
        # cli._roots() resolves --project-dir, so compare against the resolved form.
        assert roots[0] == project_skills_root(proj.resolve())  # project scanned (and won) first

        skills = discover(roots)
        matches = [s for s in skills if s.name == "review"]
        assert len(matches) == 1  # the global "review" was shadowed, not kept
        assert matches[0].path == (proj_skills_dir / "review" / "SKILL.md").resolve()

        ext = build_extraction(matches)
        node = next(n for n in ext["nodes"] if n.get("skillmap_kind") == "skill")
        assert node["skillmap_origin"] == "project"
    finally:
        discover_mod.DEFAULT_ROOTS[:] = orig_roots


def test_project_only_without_project_scans_nothing(tmp_path):
    """`--project-only` with no detected project root (no .git upward from cwd)
    must scope discovery to zero roots, not silently fall back to the global
    ~/.claude/skills / ~/.agents/skills roots."""
    orig_find_project_root = cli.find_project_root
    cli.find_project_root = lambda start=None: None  # simulate "no project" regardless of cwd
    try:
        args = argparse.Namespace(root=None, project_dir=None, project_only=True)
        roots = cli._roots(args)
        assert roots == []
        assert discover(roots) == []

        # end-to-end through the CLI: exits 1 with a clear message, no global skills listed
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["list", "--project-only"])
        assert rc == 1
        assert "Discovered" not in out.getvalue()  # never lists the global skills
        assert "no project root found" in err.getvalue().lower()
    finally:
        cli.find_project_root = orig_find_project_root


def test_build_extraction_schema(tmp_path):
    _write_skill(tmp_path, "deploy", "Deploy applications to production servers with rollback.")
    _write_skill(tmp_path, "monitor", "Monitor production servers and alert on failures.")
    skills = discover([tmp_path])
    ext = build_extraction(skills, max_concepts_per_skill=8)
    # required top-level keys for graphify
    assert set(ext) >= {"nodes", "edges", "hyperedges", "input_tokens", "output_tokens"}
    skill_nodes = [n for n in ext["nodes"] if n.get("skillmap_kind") == "skill"]
    concept_nodes = [n for n in ext["nodes"] if n.get("skillmap_kind") == "concept"]
    assert len(skill_nodes) == 2
    assert concept_nodes  # concepts extracted
    # every edge has the required graphify fields
    for e in ext["edges"]:
        assert {"source", "target", "relation", "confidence", "confidence_score"} <= set(e)
        assert e["relation"] in {
            "references", "semantically_similar_to", "conceptually_related_to",
        }
    # shared concept "production"/"servers" should link the two skills
    sim = [e for e in ext["edges"] if e["relation"] == "semantically_similar_to"]
    assert any({e["source"], e["target"]} == {"skill_deploy", "skill_monitor"} for e in sim)


def test_find_graphify_python_uv_fallback_uses_from_flag(tmp_path):
    # Modern `uv` rejects `uv tool run graphifyy python -c ...` (it demands
    # `--from`), so the fallback must invoke `uv tool run --from graphifyy
    # python -c ...`. Force fallback 1 (graphify launcher shebang) to miss so
    # fallback 2 (uv) is exercised, and stub subprocess.run to capture argv
    # without needing uv/graphify actually installed.
    calls = []

    def fake_which(name):
        if name == "graphify":
            return None
        if name == "uv":
            return "/usr/bin/uv"
        return None

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="/fake/python\n", stderr="")

    with patch.object(graph_mod.shutil, "which", side_effect=fake_which), \
         patch.object(graph_mod.subprocess, "run", side_effect=fake_run), \
         patch.object(graph_mod, "_can_import_graphify", return_value=True):
        result = graph_mod.find_graphify_python()

    assert result == "/fake/python"
    assert len(calls) == 1
    assert calls[0][:5] == ["uv", "tool", "run", "--from", "graphifyy"]


def test_parse_shebang_tokenizes_direct_and_env_styles(tmp_path):
    # Pure tokenizer: a direct-interpreter shebang stays a single token; an
    # env-style shebang (with or without env's own flags) keeps every token
    # so the caller can invoke through `env` instead of stuffing
    # "/usr/bin/env python3" into a single argv[0] slot.
    assert graph_mod._parse_shebang("#!/opt/foo/bin/python3") == ["/opt/foo/bin/python3"]
    assert graph_mod._parse_shebang("#!/usr/bin/env python3") == ["/usr/bin/env", "python3"]
    assert graph_mod._parse_shebang("#!/usr/bin/env -S python3") == ["/usr/bin/env", "-S", "python3"]
    assert graph_mod._parse_shebang("no shebang here") is None


def test_find_graphify_python_env_style_shebang(tmp_path):
    # Regression: an env-style launcher shebang ("#!/usr/bin/env python3",
    # common for pipx/venv installs) used to be read as one remainder string
    # and passed as a single argv[0] ("/usr/bin/env python3"), which
    # subprocess.run can't exec (FileNotFoundError) — silently skipping
    # fallback 1 every time. It must split into a proper multi-element argv.
    launcher = tmp_path / "graphify"
    launcher.write_text("#!/usr/bin/env python3\n# rest of the launcher\n", encoding="utf-8")
    launcher.chmod(0o755)

    calls = []

    def fake_which(name):
        return str(launcher) if name == "graphify" else None

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch.object(graph_mod.shutil, "which", side_effect=fake_which), \
         patch.object(graph_mod.subprocess, "run", side_effect=fake_run):
        result = graph_mod.find_graphify_python()

    assert result == "python3"
    assert len(calls) == 1
    # the probe must be a real multi-element argv, never one "env python3" token
    assert calls[0] == ["/usr/bin/env", "python3", "-c", "import graphify"]
    assert " " not in calls[0][0]  # argv[0] itself must be a single executable


def test_scope_ranks_relevant_skill_first(tmp_path):
    _write_skill(tmp_path, "deploy", "Deploy applications to production with rollback and releases.")
    _write_skill(tmp_path, "bake", "Bake cakes and pastries with flour, sugar, and eggs.")
    skills = discover([tmp_path])
    ext = build_extraction(skills)
    # feed the raw extraction straight into SkillGraph (it accepts "edges" too)
    g = SkillGraph(ext)
    results = g.scope("deploy my application to production", min_ratio=0.0)
    assert results, "expected at least one scoped skill"
    assert results[0].name == "deploy"
    # baking should score far lower or be scoped out
    scores = {r.name: r.score for r in results}
    assert scores.get("deploy", 0) > scores.get("bake", 0)


def test_scope_unrelated_returns_nothing(tmp_path):
    _write_skill(tmp_path, "deploy", "Deploy applications to production servers.")
    skills = discover([tmp_path])
    g = SkillGraph(build_extraction(skills))
    results = g.scope("quantum chromodynamics lattice gauge theory")
    assert results == []


def test_stem_singular_plural_match(tmp_path):
    # regression: the "es" suffix rule used to fire before "s", truncating
    # vowel-e + "s" plurals (files -> fil, failures -> failur, releases ->
    # releas) so they could never equal their singular's stem.
    pairs = [
        ("file", "files"),
        ("failure", "failures"),
        ("release", "releases"),
        ("pipeline", "pipelines"),
        ("box", "boxes"),
        ("graph", "graphs"),
    ]
    for singular, plural in pairs:
        assert _stem(singular) == _stem(plural), f"{singular!r} vs {plural!r}"


def test_scope_matches_plural_query_to_singular_concept(tmp_path):
    # A concept mined as singular ("file") must still be found by a
    # work context that only uses the plural ("files").
    ext = {
        "nodes": [
            {"id": "skill_editor", "skillmap_kind": "skill", "label": "editor",
             "skillmap_description": "Edit a file on disk.", "skillmap_triggers": []},
            {"id": "concept_file", "skillmap_kind": "concept", "label": "file"},
        ],
        "edges": [
            {"source": "skill_editor", "target": "concept_file",
             "relation": "references", "confidence": "high", "confidence_score": 1.0},
        ],
    }
    g = SkillGraph(ext)
    results = g.scope("clean up some old files", min_ratio=0.0)
    assert results and results[0].name == "editor"


def _make_project(tmp_path: Path) -> Path:
    """A fake project: a dir with .git, like find_project_root looks for."""
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    return proj


def test_find_project_root(tmp_path):
    proj = _make_project(tmp_path)
    nested = proj / "src" / "deep"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == proj.resolve()
    assert find_project_root(proj) == proj.resolve()
    assert project_skills_root(proj) == proj / ".claude" / "skills"


def test_add_skill_roundtrip(tmp_path):
    proj = _make_project(tmp_path)
    ref = tmp_path / "notes.md"
    ref.write_text("# deep detail\n", encoding="utf-8")
    skill_md = write_skill(
        project_skills_root(proj), "deploy-staging",
        "Deploy this project to the staging cluster with the canary rollout steps.",
        body="## Steps\n1. build image\n2. push\n3. rollout",
        reference_files=[ref],
    )
    assert skill_md == project_skills_root(proj) / "deploy-staging" / "SKILL.md"
    s = parse_skill(skill_md)
    assert s is not None
    assert s.name == "deploy-staging"
    assert "staging cluster" in s.description
    assert s.references == ["notes.md"]
    assert "Steps" in s.headings
    # and discovery over the project root finds it
    found = discover([project_skills_root(proj)])
    assert [x.name for x in found] == ["deploy-staging"]


def test_add_skill_validation(tmp_path):
    root = project_skills_root(_make_project(tmp_path))
    for bad_name in ("Bad_Name", "-x", "über", ""):
        try:
            write_skill(root, bad_name, "A perfectly reasonable description here.")
            raise AssertionError(f"accepted bad name {bad_name!r}")
        except ValueError:
            pass
    try:
        write_skill(root, "ok-name", "too short")
        raise AssertionError("accepted weak description")
    except ValueError:
        pass
    try:
        write_skill(root, "ok-name", "A perfectly reasonable description here.",
                    body="\n".join(f"line {i}" for i in range(500)))
        raise AssertionError("accepted oversized body")
    except ValueError as e:
        assert "references/" in str(e)  # the error routes the agent to references
    assert not (root / "ok-name").exists()  # nothing half-written


def test_add_skill_refuses_overwrite(tmp_path):
    root = project_skills_root(_make_project(tmp_path))
    desc = "Run the project's integration test suite against the docker fixture."
    write_skill(root, "run-itests", desc, body="v1")
    try:
        write_skill(root, "run-itests", desc, body="v2")
        raise AssertionError("expected SkillExistsError")
    except SkillExistsError as e:
        assert "merge" in str(e)  # route -> merge -> refactor guidance
    assert "v1" in (root / "run-itests" / "SKILL.md").read_text(encoding="utf-8")
    write_skill(root, "run-itests", desc, body="v2 merged", force=True)
    assert "v2 merged" in (root / "run-itests" / "SKILL.md").read_text(encoding="utf-8")


def test_refresh_graph_incremental(tmp_path):
    """add-skill's refresh merges into an existing graph.json, preserving
    graphify-added attributes (community) and giving new nodes a neighbor one."""
    skills_root = project_skills_root(_make_project(tmp_path))
    out = tmp_path / "out"
    write_skill(skills_root, "deploy",
                "Deploy applications to production servers with rollback.")
    # simulate a graphify-built graph.json: extraction + community labels
    ext = build_extraction(discover([skills_root]))
    graph = {"directed": False, "multigraph": False, "graph": {},
             "nodes": [dict(n, community=7) for n in ext["nodes"]],
             "links": [dict(e) for e in ext["edges"]]}
    out.mkdir()
    (out / "graph.json").write_text(json.dumps(graph), encoding="utf-8")

    write_skill(skills_root, "monitor",
                "Monitor production servers and alert on deploy failures.")
    summary = refresh_graph([skills_root], out)
    assert summary["mode"] == "incremental"
    assert summary["added_nodes"] > 0 and summary["removed_nodes"] == 0

    data = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    by_id = {n["id"]: n for n in data["nodes"]}
    assert by_id["skill_deploy"]["community"] == 7  # old attr preserved
    assert "skill_monitor" in by_id
    # new skill shares concepts with deploy -> inherits its neighborhood's community
    assert by_id["skill_monitor"].get("community") == 7
    # and the merged graph scopes the NEW skill
    results = SkillGraph(data).scope("alert on a production monitoring failure")
    assert results and results[0].name == "monitor"
    # removing a skill from disk consolidates it out on the next refresh
    (skills_root / "deploy" / "SKILL.md").unlink()
    summary2 = refresh_graph([skills_root], out)
    assert summary2["removed_nodes"] > 0
    data2 = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    assert "skill_deploy" not in {n["id"] for n in data2["nodes"]}


def test_cli_add_skill_and_scope(tmp_path):
    """End-to-end through the CLI, graphify-free: add-skill -> scope recalls it."""
    proj = _make_project(tmp_path)
    out = tmp_path / "out"
    rc = cli.main([
        "add-skill", "fix-flaky-ci", "--description",
        "Diagnose and fix flaky CI failures in this repo's integration pipeline.",
        "--body", "## Procedure\nRerun with -x, bisect the fixture.",
        "--project-dir", str(proj), "--project-only", "--out", str(out), "--json",
    ])
    assert rc == 0
    assert (project_skills_root(proj) / "fix-flaky-ci" / "SKILL.md").exists()
    # no graph.json (graphify-free) -> scope falls back to the raw extraction
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["scope", "our integration pipeline CI is flaky again",
                       "--out", str(out), "--json"])
    assert rc == 0
    results = json.loads(buf.getvalue())
    assert results and results[0]["name"] == "fix-flaky-ci"
    assert results[0]["origin"] == "project"
    assert results[0]["path"].endswith("SKILL.md")
    # unrelated context surfaces nothing
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["scope", "quantum chromodynamics lattice gauge theory",
                       "--out", str(out), "--json"])
    assert rc == 0 and json.loads(buf.getvalue()) == []
    # duplicate add exits 3 with merge guidance
    rc = cli.main([
        "add-skill", "fix-flaky-ci", "--description",
        "Diagnose and fix flaky CI failures in this repo's integration pipeline.",
        "--project-dir", str(proj), "--project-only", "--out", str(out),
    ])
    assert rc == 3


def test_read_commands_accept_project_flags(tmp_path):
    """scope/query/show take --project-dir/--project-only like the other
    commands (no argparse error), and --project-dir anchors the default --out
    at <project>/skillmap-out so reads find that project's graph."""
    proj = _make_project(tmp_path)
    rc = cli.main([  # no --out: lands in <proj>/skillmap-out via --project-dir
        "add-skill", "tune-query-cache", "--description",
        "Tune this repo's query cache: adjust TTLs and shard counts for hot keys.",
        "--project-dir", str(proj), "--project-only",
    ])
    assert rc == 0
    assert (proj.resolve() / "skillmap-out" / ".skillmap_extract.json").exists()
    for argv in (
        ["scope", "tune the query cache TTL", "--project-dir", str(proj),
         "--project-only", "--json"],
        ["show", "--project-dir", str(proj), "--project-only"],
        ["query", "cache", "--project-dir", str(proj), "--project-only"],
    ):
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = cli.main(argv)
        except SystemExit as e:  # argparse rejection would exit 2 here
            raise AssertionError(f"{argv[0]} rejected project flags: {e}")
        if argv[0] == "scope":
            assert rc == 0
            results = json.loads(buf.getvalue())
            assert [r["name"] for r in results] == ["tune-query-cache"]
        elif argv[0] == "show":
            assert rc == 0
            assert "tune-query-cache" in buf.getvalue()
        else:  # query needs graph.json (graphify-built); absent here -> exit 1
            assert rc == 1


def test_scope_project_only_filters_origin(tmp_path):
    """`scope --project-only` drops global-origin skills from the results."""
    out = tmp_path / "out"
    out.mkdir()
    ext = {"nodes": [], "edges": [], "hyperedges": [],
           "input_tokens": 0, "output_tokens": 0}
    for name, origin in (("proj-deploy", "project"), ("global-deploy", "global")):
        ext["nodes"].append({
            "id": f"skill_{name.replace('-', '_')}", "label": name,
            "file_type": "document", "skillmap_kind": "skill",
            "skillmap_description": "Deploy applications to production servers.",
            "skillmap_triggers": [], "skillmap_origin": origin,
            "source_file": f"/x/{name}/SKILL.md",
        })
    (out / ".skillmap_extract.json").write_text(json.dumps(ext), encoding="utf-8")

    def scope_names(argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert cli.main(argv) == 0
        return [r["name"] for r in json.loads(buf.getvalue())]

    both = scope_names(["scope", "deploy to production", "--out", str(out), "--json"])
    assert set(both) == {"proj-deploy", "global-deploy"}
    only = scope_names(["scope", "deploy to production", "--out", str(out),
                        "--json", "--project-only"])
    assert only == ["proj-deploy"]


def test_scope_project_only_survives_topk_and_min_ratio_cutoff(tmp_path):
    """Regression: --project-only must filter origin BEFORE top_k/min_ratio are
    applied. With many strong-matching global skills crowding out the top_k=8
    / min_ratio=0.1 window, a single matching project skill must still surface
    instead of being truncated away before the origin filter ever runs."""
    out = tmp_path / "out"
    out.mkdir()
    ext = {"nodes": [], "edges": [], "hyperedges": [],
           "input_tokens": 0, "output_tokens": 0}
    # Nine strongly-matching global skills — enough to fill (and exceed) the
    # default top_k=8 on their own — plus one matching project skill.
    for i in range(9):
        name = f"global-deploy-{i}"
        ext["nodes"].append({
            "id": f"skill_{name.replace('-', '_')}", "label": name,
            "file_type": "document", "skillmap_kind": "skill",
            "skillmap_description": "Deploy applications to production servers.",
            "skillmap_triggers": [], "skillmap_origin": "global",
            "source_file": f"/x/{name}/SKILL.md",
        })
    ext["nodes"].append({
        "id": "skill_proj_deploy", "label": "proj-deploy",
        "file_type": "document", "skillmap_kind": "skill",
        "skillmap_description": "Deploy applications to production servers.",
        "skillmap_triggers": [], "skillmap_origin": "project",
        "source_file": "/x/proj-deploy/SKILL.md",
    })
    (out / ".skillmap_extract.json").write_text(json.dumps(ext), encoding="utf-8")

    def scope_names(argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert cli.main(argv) == 0
        return [r["name"] for r in json.loads(buf.getvalue())]

    only = scope_names(["scope", "deploy to production", "--out", str(out),
                        "--json", "--project-only"])
    assert only == ["proj-deploy"], only


def test_hint_install_idempotent(tmp_path):
    proj = _make_project(tmp_path)
    assert cli.main(["hint", "--install", "--project-dir", str(proj)]) == 0
    once = (proj / "CLAUDE.md").read_text(encoding="utf-8")
    assert "skillmap scope" in once and "add-skill" in once
    assert cli.main(["hint", "--install", "--project-dir", str(proj)]) == 0
    assert (proj / "CLAUDE.md").read_text(encoding="utf-8") == once


if __name__ == "__main__":
    import tempfile
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(Path(td))
                print(f"  PASS {fn.__name__}")
                passed += 1
            except Exception:
                print(f"  FAIL {fn.__name__}")
                traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
