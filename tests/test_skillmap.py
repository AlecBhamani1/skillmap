"""Tests for skillmap's pure-Python layer (discovery, extraction, scoping).

These do not require graphify — they exercise the parsing/extraction/scoping
logic directly. Run with: python -m pytest tests/ (or python tests/test_skillmap.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillmap.discover import Skill, discover, parse_skill  # noqa: E402
from skillmap.extract import build_extraction  # noqa: E402
from skillmap.scope import SkillGraph  # noqa: E402


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


def test_trigger_blocklist(tmp_path):
    p = _write_skill(tmp_path, "foo", "A foo skill.",
                     body="see `/tmp/x` and `/graphify-out` and `/abs` paths")
    s = parse_skill(p)
    # only the skill's own /foo should survive, not path-like slash tokens
    assert s.triggers == ["/foo"]


def test_discover_dedup(tmp_path):
    _write_skill(tmp_path, "alpha", "Alpha does alpha things.")
    _write_skill(tmp_path, "beta", "Beta does beta things.")
    skills = discover([tmp_path])
    names = {s.name for s in skills}
    assert names == {"alpha", "beta"}


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
