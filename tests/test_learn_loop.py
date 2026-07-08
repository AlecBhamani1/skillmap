"""Tests for the P3 "close the learn loop" behavior: `learn`/`add-skill`
parity, the near-duplicate guard, and the graph refresh they both drive.

Pure-Python, no graphify required (same trick as tests/test_skillmap.py: a
raw extraction dict loads straight into SkillGraph, and CLI runs are pointed
at a temp project via --project-dir/--root so they never touch the real
~/.claude/skills or ~/.agents/skills on the machine running the tests).
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillmap import cli  # noqa: E402
from skillmap.author import (  # noqa: E402
    NearDuplicateSkillError,
    SkillExistsError,
    find_near_duplicate,
    write_skill,
)
from skillmap.discover import discover, parse_skill, project_skills_root  # noqa: E402


def _write_skill(root: Path, name: str, description: str, body: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: >-\n  {description}\n---\n{body}"
    (d / "SKILL.md").write_text(fm, encoding="utf-8")
    return d / "SKILL.md"


def _make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    return proj


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# learn / add-skill parity
# ---------------------------------------------------------------------------

def test_learn_is_an_alias_of_add_skill_in_the_parser():
    parser = cli.build_parser()
    add_ns = parser.parse_args(
        ["add-skill", "x", "--description", "d"])
    learn_ns = parser.parse_args(
        ["learn", "x", "--description", "d"])
    assert add_ns.func is learn_ns.func is cli.cmd_add_skill


def test_learn_and_add_skill_produce_identical_results(tmp_path):
    # Two fully separate projects (not two skills in one project) so an
    # identical description doesn't legitimately trip the near-duplicate
    # guard against the other invocation -- the point here is command
    # parity, which the guard test below covers separately.
    proj_a, proj_b = _make_project(tmp_path / "a"), _make_project(tmp_path / "b")
    root_a, root_b = project_skills_root(proj_a), project_skills_root(proj_b)
    desc = "Run the project's integration test suite against the docker fixture data."

    rc1, out1, err1 = _run_cli([
        "add-skill", "run-tests", "--description", desc,
        "--project-dir", str(proj_a), "--root", str(root_a), "--json",
    ])
    rc2, out2, err2 = _run_cli([
        "learn", "run-tests", "--description", desc,
        "--project-dir", str(proj_b), "--root", str(root_b), "--json",
    ])
    assert rc1 == rc2 == 0, (err1, err2)

    payload1, payload2 = json.loads(out1), json.loads(out2)
    # Same shape / same graph-refresh mode for both invocations; the created
    # path legitimately differs (different project dirs), name doesn't.
    assert payload1["name"] == payload2["name"]
    assert payload1["graph"]["mode"] == payload2["graph"]["mode"]
    assert set(payload1) == set(payload2)

    s1 = parse_skill(root_a / "run-tests" / "SKILL.md")
    s2 = parse_skill(root_b / "run-tests" / "SKILL.md")
    assert s1 is not None and s2 is not None
    assert s1.description == s2.description == desc


# ---------------------------------------------------------------------------
# graph refresh in place (regression: attribute preservation)
# ---------------------------------------------------------------------------

def test_learn_refreshes_existing_graph_json_in_place(tmp_path):
    proj = _make_project(tmp_path)
    root = project_skills_root(proj)
    out_dir = proj / "skillmap-out"

    rc, _, err = _run_cli([
        "learn", "deploy-app",
        "--description", "Deploy applications to production servers with rollback and canary release steps.",
        "--project-dir", str(proj), "--root", str(root),
    ])
    assert rc == 0, err

    # Simulate a prior `skillmap build` having run graphify's clustering: hand
    # -craft a graph.json with a community label on the existing node, in the
    # networkx node-link shape graphify's export produces ("links", not
    # "edges").
    extract_path = out_dir / ".skillmap_extract.json"
    extraction = json.loads(extract_path.read_text(encoding="utf-8"))
    skill_node = next(n for n in extraction["nodes"] if n.get("skillmap_kind") == "skill")
    skill_node["community"] = 7
    graph_path = out_dir / "graph.json"
    graph_path.write_text(json.dumps({
        "nodes": extraction["nodes"],
        "links": extraction["edges"],
    }), encoding="utf-8")

    rc2, _, err2 = _run_cli([
        "learn", "run-tests",
        "--description", "Run the project's integration test suite against the docker fixture data.",
        "--project-dir", str(proj), "--root", str(root),
    ])
    assert rc2 == 0, err2

    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    deploy_node = next(n for n in graph["nodes"]
                       if n.get("skillmap_kind") == "skill" and n.get("label") == "deploy-app")
    assert deploy_node["community"] == 7  # preserved across the incremental refresh
    labels = {n.get("label") for n in graph["nodes"] if n.get("skillmap_kind") == "skill"}
    assert "run-tests" in labels  # the new skill was merged in


# ---------------------------------------------------------------------------
# near-duplicate guard
# ---------------------------------------------------------------------------

def test_near_duplicate_description_blocks_learn_and_names_the_merge_target(tmp_path):
    proj = _make_project(tmp_path)
    root = project_skills_root(proj)
    _write_skill(root, "deploy-app",
                "Deploy applications to production servers with rollback and canary release steps.")

    rc, out, err = _run_cli([
        "learn", "ship-app",
        "--description", "Ship applications to production with rollback and canary releases.",
        "--project-dir", str(proj), "--root", str(root),
    ])
    assert rc == 3
    assert "similar existing skill: deploy-app" in err
    assert "--force" in err
    assert not (root / "ship-app").exists()  # nothing half-written

    # --force bypasses the guard and writes anyway.
    rc2, out2, err2 = _run_cli([
        "learn", "ship-app",
        "--description", "Ship applications to production with rollback and canary releases.",
        "--project-dir", str(proj), "--root", str(root), "--force",
    ])
    assert rc2 == 0, err2
    assert (root / "ship-app" / "SKILL.md").exists()


def test_near_duplicate_guard_does_not_flag_a_merely_related_skill(tmp_path):
    # A staging deploy and a production deploy share vocabulary but are
    # distinct procedures -- the guard must not be so aggressive it blocks
    # legitimately separate skills.
    proj = _make_project(tmp_path)
    root = project_skills_root(proj)
    _write_skill(root, "deploy-app",
                "Deploy applications to production servers with rollback and canary release steps.")

    rc, out, err = _run_cli([
        "learn", "deploy-staging",
        "--description",
        "Deploy this project's app to the staging cluster with the canary rollout steps.",
        "--project-dir", str(proj), "--root", str(root),
    ])
    assert rc == 0, err
    assert (root / "deploy-staging" / "SKILL.md").exists()


def test_find_near_duplicate_direct(tmp_path):
    _write_skill(tmp_path, "bake-cookies",
                "Bake cookies and pastries with flour, sugar, and eggs for a party.")
    skills = discover([tmp_path])
    assert skills  # sanity: fixture actually parsed

    dup = find_near_duplicate(
        [tmp_path], "cookie-baker",
        "Bake a batch of chocolate chip cookies with butter and sugar.")
    assert dup is not None
    assert dup.name == "bake-cookies"

    no_dup = find_near_duplicate(
        [tmp_path], "totally-unrelated",
        "Paint a landscape watercolor with warm autumn colors.")
    assert no_dup is None


def test_write_skill_raises_near_duplicate_error_with_merge_target(tmp_path):
    existing_root = tmp_path / "existing"
    _write_skill(existing_root, "bake-cookies",
                "Bake cookies and pastries with flour, sugar, and eggs for a party.")
    new_root = tmp_path / "new"
    try:
        write_skill(
            new_root, "cookie-baker",
            "Bake a batch of chocolate chip cookies with butter and sugar.",
            dedup_roots=[existing_root],
        )
        raise AssertionError("expected NearDuplicateSkillError")
    except NearDuplicateSkillError as e:
        assert "bake-cookies" in str(e)
        assert "--force" in str(e)
    assert not (new_root / "cookie-baker").exists()

    # force bypasses it
    skill_md = write_skill(
        new_root, "cookie-baker",
        "Bake a batch of chocolate chip cookies with butter and sugar.",
        dedup_roots=[existing_root], force=True,
    )
    assert skill_md.exists()


# ---------------------------------------------------------------------------
# exact-name collision (unchanged contract)
# ---------------------------------------------------------------------------

def test_exact_name_collision_still_exits_3(tmp_path):
    proj = _make_project(tmp_path)
    root = project_skills_root(proj)
    desc = "Run the project's integration test suite against the docker fixture data."

    rc1, _, err1 = _run_cli([
        "learn", "run-itests", "--description", desc,
        "--project-dir", str(proj), "--root", str(root),
    ])
    assert rc1 == 0, err1

    rc2, _, err2 = _run_cli([
        "learn", "run-itests", "--description", desc,
        "--project-dir", str(proj), "--root", str(root),
    ])
    assert rc2 == 3
    assert "already exists" in err2

    rc3, _, err3 = _run_cli([
        "learn", "run-itests", "--description", desc, "--body", "v2",
        "--project-dir", str(proj), "--root", str(root), "--force",
    ])
    assert rc3 == 0, err3


def test_write_skill_exact_name_collision_direct(tmp_path):
    root = tmp_path / "skills"
    desc = "Run the project's integration test suite against the docker fixture."
    write_skill(root, "run-itests", desc, body="v1")
    try:
        write_skill(root, "run-itests", desc, body="v2")
        raise AssertionError("expected SkillExistsError")
    except SkillExistsError as e:
        assert "already exists" in str(e)


if __name__ == "__main__":
    # Lightweight self-runner mirroring tests/test_skillmap.py, for environments
    # without pytest.
    import inspect
    import tempfile
    import traceback

    failures = 0
    tests = [(n, f) for n, f in list(globals().items())
            if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            if inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            print(f"PASS {name}")
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
