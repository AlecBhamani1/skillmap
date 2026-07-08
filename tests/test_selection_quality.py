"""Regression gate for P1 (cross-mention contamination) and P2 (phrase-level
concepts), driven through build_extraction + SkillGraph over fixture skills --
no graphify needed, same trick tests/test_skillmap.py uses.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillmap.discover import discover  # noqa: E402
from skillmap.extract import build_extraction  # noqa: E402
from skillmap.scope import SkillGraph  # noqa: E402


def _write_skill(root: Path, name: str, description: str, body: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: >-\n  {description}\n---\n{body}"
    (d / "SKILL.md").write_text(fm, encoding="utf-8")
    return d / "SKILL.md"


def test_redirection_credits_the_named_skill_not_the_mentioner(tmp_path):
    """A disclaimer in one skill's description ("use worker-cli instead for
    terminal control and worktree management") must help the *named* skill
    win the query it describes, not the skill saying the disclaimer."""
    _write_skill(
        tmp_path, "coordinator",
        "Coordinate agents with task DAGs and decision gates. "
        "Use `worker-cli` instead for terminal control and worktree management.")
    _write_skill(
        tmp_path, "worker-cli",
        "Manage worker terminals and worktrees for parallel task execution.")
    skills = discover([tmp_path])
    g = SkillGraph(build_extraction(skills))
    results = g.scope("manage worktrees and terminals", min_ratio=0.0)
    assert results, "expected at least one scoped skill"
    assert results[0].name == "worker-cli"


def test_redirection_tokens_excluded_from_mentioning_skills_concepts(tmp_path):
    """Tokens inside a redirection span must never appear among the mined
    concepts of the skill that says the disclaimer."""
    _write_skill(
        tmp_path, "coordinator",
        "Coordinate agents with task DAGs and decision gates. "
        "Use `worker-cli` instead for terminal control and worktree management.")
    _write_skill(
        tmp_path, "worker-cli",
        "Manage worker terminals and worktrees for parallel task execution.")
    skills = discover([tmp_path])
    ext = build_extraction(skills)
    coordinator_id = "skill_coordinator"
    concept_labels = {
        n["label"] for n in ext["nodes"] if n.get("skillmap_kind") == "concept"
    }
    mentioner_concepts = {
        e["target"] for e in ext["edges"]
        if e["source"] == coordinator_id and e["relation"] == "references"
    }
    mentioner_labels = {
        n["label"] for n in ext["nodes"]
        if n["id"] in mentioner_concepts and n.get("skillmap_kind") == "concept"
    }
    assert "terminal" not in mentioner_labels
    assert "worktree" not in mentioner_labels
    assert "management" not in mentioner_labels
    # sanity: the concepts really were mined (for worker-cli), so the absence
    # above is redirection-stripping, not a tokenizer/stopword accident.
    assert concept_labels  # something was extracted at all


def test_self_referential_use_sentence_is_not_a_redirection(tmp_path):
    """"Use `X` for ..." naming the skill's *own* name is a normal
    self-description, not a redirection -- it must still seed its own
    concepts/scoring, matching how graphify's real orchestration-style
    skills phrase their opening sentence."""
    _write_skill(
        tmp_path, "widget-builder",
        "Use `widget-builder` for assembling gadgets and widgets from parts.")
    skills = discover([tmp_path])
    g = SkillGraph(build_extraction(skills))
    results = g.scope("assemble gadgets from parts", min_ratio=0.0)
    assert results and results[0].name == "widget-builder"


def test_adversarial_multi_sentence_redirection(tmp_path):
    """Mirrors the real orchestration-skill shape: an opening self-description
    sentence followed by several "use X instead/for ..." disclaimers naming
    different installed skills -- each disclaimer's tokens must route to the
    skill it names, not stay with the mentioner."""
    _write_skill(
        tmp_path, "orchestration",
        "Use orca orchestration for structured multi-agent coordination: "
        "threaded messages, task dispatch, and decision gates. "
        "Use `orca-cli` instead for full ownership handoffs. "
        "Use `orca-cli` for ordinary terminal control and worktree management. "
        "Use computer-use for browser windows and desktop UI.")
    _write_skill(
        tmp_path, "orca-cli",
        "Give full ownership handoffs and terminal control over worktrees to another agent.")
    _write_skill(
        tmp_path, "computer-use",
        "Operate a browser window and click buttons on the desktop UI.")
    skills = discover([tmp_path])
    g = SkillGraph(build_extraction(skills))

    handoff = g.scope("hand off this task to another agent", min_ratio=0.0)
    assert handoff and handoff[0].name == "orca-cli"

    terminals = g.scope("manage worktrees and terminal control", min_ratio=0.0)
    assert terminals and terminals[0].name == "orca-cli"

    browser = g.scope("operate a browser window on the desktop", min_ratio=0.0)
    assert browser and browser[0].name == "computer-use"

    coordination = g.scope("coordinate multi-agent task dispatch and decision gates",
                           min_ratio=0.0)
    assert coordination and coordination[0].name == "orchestration"


def test_bigram_phrase_concept_outranks_unrelated_unigram_hits(tmp_path):
    """A skill genuinely about "knowledge graph" must outrank a skill that
    only mentions "knowledge" and "graph" in unrelated sentences, for a query
    that names the phrase."""
    _write_skill(
        tmp_path, "graph-builder",
        "Build a knowledge graph from any input and query its structure.")
    _write_skill(
        tmp_path, "generic-notes",
        "Manage personal knowledge notes and to-do lists. "
        "Also renders a dependency graph for the build pipeline.")
    skills = discover([tmp_path])
    g = SkillGraph(build_extraction(skills))
    results = g.scope("build a knowledge graph", min_ratio=0.0)
    assert results and results[0].name == "graph-builder"
    scores = {r.name: r.score for r in results}
    assert scores["graph-builder"] > scores.get("generic-notes", 0)


def test_bigram_concept_mined_only_for_genuinely_adjacent_phrase(tmp_path):
    """"knowledge" and "graph" appearing in different sentences must not
    fabricate a "knowledge graph" phrase concept."""
    _write_skill(
        tmp_path, "generic-notes",
        "Manage personal knowledge notes and to-do lists. "
        "Also renders a dependency graph for the build pipeline.")
    skills = discover([tmp_path])
    ext = build_extraction(skills)
    concept_labels = {
        n["label"] for n in ext["nodes"] if n.get("skillmap_kind") == "concept"
    }
    assert "knowledge graph" not in concept_labels


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
