"""Tests for the semantic-enrichment layer (skillmap/enrich.py).

Pure-Python: fixture skills + hand-written payloads stand in for the LLM, so
the whole validate -> cache -> apply -> scope path runs with no network and no
graphify — same trick as the other suites.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from skillmap.discover import Skill  # noqa: E402
from skillmap.enrich import (  # noqa: E402
    EnrichmentUnavailable, PAYLOAD_SCHEMA, apply_cached, apply_enrichment,
    build_enrich_prompt, build_request, cached_payload, load_cache,
    merge_into_cache, save_cache, validate_payload,
)
from skillmap.extract import build_extraction  # noqa: E402
from skillmap.scope import SkillGraph  # noqa: E402

REQUIRED_EDGE_KEYS = {"source", "target", "relation", "confidence", "confidence_score"}
ALLOWED_RELATIONS = {"references", "semantically_similar_to", "conceptually_related_to"}


def _skill(name: str, description: str, body: str = "") -> Skill:
    return Skill(name=name, description=description,
                 path=Path(f"/tmp/{name}/SKILL.md"), body=body)


def _fixtures() -> list[Skill]:
    return [
        _skill("xlsx-tool", "Edit xlsx workbook files: read cells, write formulas.",
               body="## Usage\nOpen the xlsx workbook and edit cells with formulas."),
        _skill("note-taker", "Capture meeting notes and summaries into markdown.",
               body="## Usage\nWrite meeting notes as markdown summaries."),
    ]


def _payload() -> dict:
    return {
        "skills": [
            {"slug": "xlsx_tool", "concepts": ["spreadsheet", "tabular data"]},
            {"slug": "note_taker", "concepts": ["meeting minutes"]},
        ],
        "related": [{"a": "spreadsheet", "b": "workbook"}],
    }


def test_validate_drops_unknown_slug_and_normalizes():
    skills = _fixtures()
    payload = {
        "skills": [
            {"slug": "xlsx_tool", "concepts": ["  Spreadsheet ", "spreadsheet", "x"]},
            {"slug": "nope", "concepts": ["ghost"]},
        ],
        "related": [
            {"a": "Spreadsheet", "b": "spreadsheet"},   # self after normalize
            {"a": "spreadsheet", "b": "workbook"},
            {"a": "workbook", "b": "spreadsheet"},      # duplicate reversed
        ],
    }
    normalized, warnings = validate_payload(payload, skills)
    assert normalized["skills"] == {"xlsx_tool": ["spreadsheet"]}  # dedup, len>=3
    assert normalized["related"] == [("spreadsheet", "workbook")]
    assert any("nope" in w for w in warnings)


def test_apply_enrichment_stays_graphify_native():
    skills = _fixtures()
    ext = build_extraction(skills)
    normalized, _ = validate_payload(_payload(), skills)
    summary = apply_enrichment(ext, normalized)
    assert summary["added_nodes"] >= 3  # spreadsheet, tabular data, meeting minutes...
    assert summary["added_edges"] >= 4
    for e in ext["edges"]:
        assert REQUIRED_EDGE_KEYS <= set(e)
        assert e["relation"] in ALLOWED_RELATIONS
    enriched = [n for n in ext["nodes"] if n.get("skillmap_source") == "enrichment"]
    assert enriched and all(n["skillmap_kind"] == "concept" for n in enriched)
    assert any(e["relation"] == "conceptually_related_to" for e in ext["edges"])


def test_related_pair_with_no_anchor_is_skipped():
    skills = _fixtures()
    ext = build_extraction(skills)
    normalized = {"skills": {}, "related": [("floating", "island")]}
    summary = apply_enrichment(ext, normalized)
    assert summary["added_nodes"] == 0 and summary["added_edges"] == 0


def test_scope_bridges_synonym_via_enrichment():
    """The payoff: a query in vocabulary no SKILL.md contains finds the skill."""
    skills = _fixtures()
    ext = build_extraction(skills)
    names_before = [r.name for r in SkillGraph(ext).scope("organize tabular data in a spreadsheet")]
    assert "xlsx-tool" not in names_before  # fails by construction pre-enrichment

    ext = build_extraction(skills)
    normalized, _ = validate_payload(_payload(), skills)
    apply_enrichment(ext, normalized)
    results = SkillGraph(ext).scope("organize tabular data in a spreadsheet")
    assert results and results[0].name == "xlsx-tool"


def test_scope_bridges_one_hop_via_related_edge():
    """Query hits only the *related* concept; the edge carries it to the skill."""
    skills = _fixtures()
    ext = build_extraction(skills)
    # "spreadsheet" is enriched onto xlsx-tool; "grid of numbers" only exists
    # as the far end of a conceptually_related_to edge.
    normalized = {"skills": {"xlsx_tool": ["spreadsheet"]},
                  "related": [("spreadsheet", "numeric grid")]}
    apply_enrichment(ext, normalized)
    results = SkillGraph(ext).scope("numeric grid")
    assert any(r.name == "xlsx-tool" for r in results)


def test_cache_roundtrip_and_stale_hash(tmp_path):
    skills = _fixtures()
    normalized, _ = validate_payload(_payload(), skills)
    cache = merge_into_cache(load_cache(tmp_path), normalized, skills)
    save_cache(tmp_path, cache)

    reloaded = load_cache(tmp_path)
    filtered, warnings = cached_payload(reloaded, skills)
    assert filtered["skills"]["xlsx_tool"] == ["spreadsheet", "tabular data"]
    assert not warnings

    # Editing the skill invalidates its cache entry but not the others'.
    skills[0].body += "\n## Changed\nNew content."
    filtered, warnings = cached_payload(reloaded, skills)
    assert "xlsx_tool" not in filtered["skills"]
    assert filtered["skills"]["note_taker"] == ["meeting minutes"]
    assert any("stale" in w for w in warnings)


def test_apply_cached_is_total(tmp_path):
    """apply_cached never raises: missing, corrupt, or empty cache all no-op."""
    skills = _fixtures()
    ext = build_extraction(skills)
    assert apply_cached(ext, skills, tmp_path)["added_nodes"] == 0
    (tmp_path / ".skillmap_enrich.json").write_text("{not json", encoding="utf-8")
    assert apply_cached(ext, skills, tmp_path)["added_nodes"] == 0

    normalized, _ = validate_payload(_payload(), skills)
    save_cache(tmp_path, merge_into_cache(load_cache(tmp_path), normalized, skills))
    ext = build_extraction(skills)
    assert apply_cached(ext, skills, tmp_path)["added_nodes"] > 0


def test_refresh_graph_reapplies_enrichment(tmp_path):
    """add-skill's incremental refresh keeps enriched concepts without a network call."""
    from skillmap.author import refresh_graph

    root = tmp_path / "skills"
    d = root / "xlsx-tool"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: xlsx-tool\ndescription: >-\n  Edit xlsx workbook files: read "
        "cells, write formulas.\n---\n## Usage\nEdit cells.\n", encoding="utf-8")

    out = tmp_path / "out"
    from skillmap.discover import discover
    skills = discover([root])
    normalized, _ = validate_payload(
        {"skills": [{"slug": "xlsx_tool", "concepts": ["spreadsheet"]}], "related": []},
        skills)
    save_cache(out, merge_into_cache(load_cache(out), normalized, skills))

    refresh_graph([root], out)
    ext = json.loads((out / ".skillmap_extract.json").read_text(encoding="utf-8"))
    labels = {n["label"] for n in ext["nodes"] if n.get("skillmap_source") == "enrichment"}
    assert "spreadsheet" in labels


def test_prompt_names_every_skill_and_the_payload_shape():
    skills = _fixtures()
    prompt = build_enrich_prompt(skills)
    for s in skills:
        assert s.slug in prompt
    assert '"skills"' in prompt and '"related"' in prompt


def test_request_requires_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    try:
        build_request(_fixtures())
    except EnrichmentUnavailable as e:
        assert "enrich-prompt" in str(e)
    else:
        raise AssertionError("expected EnrichmentUnavailable")


def test_request_shape_with_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    req = build_request(_fixtures())
    assert req.full_url == "https://api.anthropic.com/v1/messages"
    assert req.get_header("X-api-key") == "sk-test"
    assert req.get_header("Anthropic-version") == "2023-06-01"
    body = json.loads(req.data.decode("utf-8"))
    assert body["output_config"]["format"]["schema"] == PAYLOAD_SCHEMA
    assert body["model"]  # default or env override, never empty


def test_request_shape_with_oauth_token(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oat-test")
    req = build_request(_fixtures())
    assert req.get_header("Authorization") == "Bearer oat-test"
    assert req.get_header("Anthropic-beta") == "oauth-2025-04-20"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
