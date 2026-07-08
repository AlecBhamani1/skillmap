"""Semantic concept enrichment: the drop-in LLM pass on top of extraction.

Frequency mining (extract.py) can only produce concepts an author literally
wrote, often. This module layers *semantic* concepts on the same
graphify-native extraction: synonym/abstraction concept nodes per skill
("spreadsheet" for a skill that only ever says "xlsx"), plus
concept<->concept `conceptually_related_to` edges — the third graphify verb,
which is what makes scope's BFS bridge a query onto vocabulary no SKILL.md
contains.

Two entry routes produce the same **payload**, so the apply/merge path is
identical and testable without a network:

  zero-key (host agent):  skillmap enrich-prompt  ->  agent answers  ->
                          skillmap build --concepts-file answer.json
  direct API:             skillmap build --enrich  (ANTHROPIC_API_KEY /
                          ANTHROPIC_AUTH_TOKEN; stdlib urllib — the package
                          stays dependency-free, same reason graphify is
                          subprocess-only)

Payload shape (what the LLM / agent produces, and what --concepts-file reads):

  {
    "skills":  [{"slug": "<skill slug>", "concepts": ["phrase", ...]}, ...],
    "related": [{"a": "<concept label>", "b": "<concept label>"}, ...]
  }

Validated payloads are cached per SKILL.md content hash
(<out>/.skillmap_enrich.json), so `add-skill`'s incremental refresh re-applies
enrichment for unchanged skills without any network call, and a changed skill
simply falls back to deterministic mining until the next enrichment pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .discover import Skill
from .extract import _concept_id

CACHE_NAME = ".skillmap_enrich.json"
ENRICH_SOURCE = "skillmap://enrichment"
MAX_CONCEPTS_PER_SKILL = 12
MAX_RELATED_PAIRS = 60

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"
MODEL_ENV = "SKILLMAP_ENRICH_MODEL"

# Structured-outputs schema for the payload. Dynamic keys aren't allowed
# (every object needs additionalProperties: false), hence the array-of-
# {slug, concepts} form rather than a slug-keyed mapping.
PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "skills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "concepts": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["slug", "concepts"],
                "additionalProperties": False,
            },
        },
        "related": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                "required": ["a", "b"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["skills", "related"],
    "additionalProperties": False,
}


class EnrichmentError(RuntimeError):
    """The enrichment pass failed (bad payload, API error, refusal)."""


class EnrichmentUnavailable(EnrichmentError):
    """No credentials for the direct-API route; use enrich-prompt instead."""


def _norm_concept(text: str) -> str:
    return " ".join(text.lower().split())


def skill_hash(skill: Skill) -> str:
    h = hashlib.sha256()
    h.update(f"{skill.name}\n{skill.description}\n{skill.body}".encode("utf-8"))
    return h.hexdigest()


def validate_payload(payload: dict, skills: list[Skill]) -> tuple[dict, list[str]]:
    """Clamp a raw payload to what apply_enrichment will accept.

    Returns (normalized, warnings): normalized is {"skills": {slug: [concepts]},
    "related": [(a, b), ...]} with unknown slugs dropped, concepts lowercased/
    deduped/capped, and self- or duplicate pairs removed. Bad payloads never
    reach the graph — they degrade to warnings.
    """
    warnings: list[str] = []
    known = {s.slug for s in skills}
    by_slug: dict[str, list[str]] = {}
    for entry in payload.get("skills", []) or []:
        slug = str(entry.get("slug", "")).strip()
        if slug not in known:
            warnings.append(f"unknown skill slug in payload: {slug!r} (dropped)")
            continue
        seen: set[str] = set(by_slug.get(slug, []))
        out = by_slug.setdefault(slug, [])
        for c in entry.get("concepts", []) or []:
            c = _norm_concept(str(c))
            if not (3 <= len(c) <= 60) or c in seen:
                continue
            seen.add(c)
            out.append(c)
        if len(out) > MAX_CONCEPTS_PER_SKILL:
            warnings.append(f"{slug}: {len(out)} concepts capped to {MAX_CONCEPTS_PER_SKILL}")
            del out[MAX_CONCEPTS_PER_SKILL:]

    related: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for entry in payload.get("related", []) or []:
        a = _norm_concept(str(entry.get("a", "")))
        b = _norm_concept(str(entry.get("b", "")))
        if not a or not b or a == b:
            continue
        pair = tuple(sorted((a, b)))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        related.append(pair)
    if len(related) > MAX_RELATED_PAIRS:
        warnings.append(f"{len(related)} related pairs capped to {MAX_RELATED_PAIRS}")
        related = related[:MAX_RELATED_PAIRS]

    return {"skills": by_slug, "related": related}, warnings


def apply_enrichment(extraction: dict, normalized: dict) -> dict:
    """Layer a validated payload onto a deterministic extraction, in place.

    Adds concept nodes (marked skillmap_source="enrichment"), skill->concept
    `references` edges (INFERRED), and concept<->concept
    `conceptually_related_to` edges. A related pair is only applied when at
    least one endpoint concept already exists in the graph, so every enriched
    node stays within BFS reach of a skill. Returns a summary dict.
    """
    nodes_by_id = {n["id"]: n for n in extraction["nodes"]}
    skill_ids = {n["id"] for n in extraction["nodes"] if n.get("skillmap_kind") == "skill"}
    edge_set = {(e["source"], e["target"], e["relation"]) for e in extraction["edges"]}
    added_nodes = added_edges = 0

    def ensure_concept(label: str, anchor_file: str) -> str:
        nonlocal added_nodes
        cid = _concept_id(label)
        if cid not in nodes_by_id:
            node = {
                "id": cid,
                "label": label,
                "file_type": "concept",
                "source_file": anchor_file,
                "source_location": None,
                "source_url": None,
                "captured_at": None,
                "author": None,
                "contributor": "skillmap-enrich",
                "skillmap_kind": "concept",
                "skillmap_source": "enrichment",
            }
            extraction["nodes"].append(node)
            nodes_by_id[cid] = node
            added_nodes += 1
        return cid

    def add_edge(src: str, tgt: str, relation: str, score: float) -> None:
        nonlocal added_edges
        if (src, tgt, relation) in edge_set or (tgt, src, relation) in edge_set:
            return
        extraction["edges"].append({
            "source": src,
            "target": tgt,
            "relation": relation,
            "confidence": "INFERRED",
            "confidence_score": score,
            "source_file": ENRICH_SOURCE,
            "source_location": None,
            "weight": 1.0,
        })
        edge_set.add((src, tgt, relation))
        added_edges += 1

    for slug, concepts in normalized.get("skills", {}).items():
        sid = "skill_" + slug
        if sid not in skill_ids:
            continue
        anchor = nodes_by_id[sid].get("source_file", "")
        for label in concepts:
            cid = ensure_concept(label, anchor)
            add_edge(sid, cid, "references", 0.85)

    for a, b in normalized.get("related", []):
        aid, bid = _concept_id(a), _concept_id(b)
        # Require an existing anchor so a pair can't create a floating island.
        if aid not in nodes_by_id and bid not in nodes_by_id:
            continue
        anchor = nodes_by_id.get(aid, nodes_by_id.get(bid, {})).get("source_file", "")
        aid = ensure_concept(a, anchor)
        bid = ensure_concept(b, anchor)
        add_edge(aid, bid, "conceptually_related_to", 0.75)

    return {"added_nodes": added_nodes, "added_edges": added_edges}


# ---------------------------------------------------------------------------
# Cache: validated payload keyed by SKILL.md content hash.
# ---------------------------------------------------------------------------

def load_cache(out_dir: Path) -> dict:
    p = Path(out_dir) / CACHE_NAME
    if not p.exists():
        return {"version": 1, "skills": {}, "related": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": 1, "skills": {}, "related": []}
    data.setdefault("skills", {})
    data.setdefault("related", [])
    return data


def save_cache(out_dir: Path, cache: dict) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / CACHE_NAME
    p.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def merge_into_cache(cache: dict, normalized: dict, skills: list[Skill]) -> dict:
    """Record a validated payload against the current content hash per skill."""
    hashes = {s.slug: skill_hash(s) for s in skills}
    for slug, concepts in normalized.get("skills", {}).items():
        if slug in hashes:
            cache["skills"][slug] = {"hash": hashes[slug], "concepts": concepts}
    existing = {tuple(sorted(p)) for p in cache.get("related", [])}
    for pair in normalized.get("related", []):
        if tuple(sorted(pair)) not in existing:
            cache["related"].append(list(pair))
    return cache


def cached_payload(cache: dict, skills: list[Skill]) -> tuple[dict, list[str]]:
    """The cache filtered to skills whose SKILL.md is unchanged since caching.

    A stale entry (edited skill) is skipped — that skill runs on deterministic
    mining alone until the next enrichment pass refreshes it.
    """
    warnings: list[str] = []
    by_slug: dict[str, list[str]] = {}
    hashes = {s.slug: skill_hash(s) for s in skills}
    for slug, entry in cache.get("skills", {}).items():
        if slug not in hashes:
            continue  # skill deleted — silently drops, same as extraction
        if entry.get("hash") != hashes[slug]:
            warnings.append(f"{slug}: SKILL.md changed since enrichment (stale entry skipped)")
            continue
        by_slug[slug] = list(entry.get("concepts", []))
    related = [tuple(p) for p in cache.get("related", []) if len(p) == 2]
    return {"skills": by_slug, "related": related}, warnings


def apply_cached(extraction: dict, skills: list[Skill], out_dir: Path) -> dict:
    """One-call form used by build/refresh: load cache, filter, apply.

    Never raises — enrichment is an overlay; the deterministic graph must
    build even when the cache is missing, stale, or corrupt.
    """
    cache = load_cache(out_dir)
    if not cache.get("skills") and not cache.get("related"):
        return {"added_nodes": 0, "added_edges": 0, "warnings": []}
    normalized, warnings = cached_payload(cache, skills)
    summary = apply_enrichment(extraction, normalized)
    summary["warnings"] = warnings
    return summary


# ---------------------------------------------------------------------------
# Prompt (zero-key host-agent route) and direct API call.
# ---------------------------------------------------------------------------

def build_enrich_prompt(skills: list[Skill], body_chars: int = 700) -> str:
    lines = [
        "You are enriching a knowledge graph of installed agent skills with",
        "SEMANTIC concepts that frequency mining cannot produce.",
        "",
        "For each skill below, list 5-10 canonical concepts it is ABOUT:",
        "lowercase phrases (1-3 words), including synonyms and abstractions a",
        "user might say even though the SKILL.md never uses those words",
        '(e.g. "spreadsheet" for a skill that only says "xlsx").',
        "Then list cross-skill related concept pairs: two concept labels that",
        "mean similar or tightly-coupled things (synonym bridges).",
        "",
        "Answer with ONLY this JSON (no prose, no code fences):",
        json.dumps({"skills": [{"slug": "<slug>", "concepts": ["<phrase>", "..."]}],
                    "related": [{"a": "<concept>", "b": "<concept>"}]}, indent=2),
        "",
        "Skills:",
    ]
    for s in skills:
        lines.append(f"\n--- slug: {s.slug}")
        lines.append(f"name: {s.name}")
        lines.append(f"description: {s.description}")
        if s.headings:
            lines.append(f"headings: {'; '.join(s.headings[:12])}")
        body = " ".join(s.body.split())[:body_chars]
        if body:
            lines.append(f"body (excerpt): {body}")
    return "\n".join(lines)


def _credentials() -> tuple[str, str]:
    """(header_name, value) for whichever Anthropic credential is present."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return "x-api-key", key
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if token:
        return "Authorization", f"Bearer {token}"
    raise EnrichmentUnavailable(
        "No ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN in the environment. "
        "Use the zero-key route instead: `skillmap enrich-prompt`, answer it, "
        "then `skillmap build --concepts-file <answer.json>`.")


def build_request(skills: list[Skill], model: str | None = None) -> urllib.request.Request:
    """The Messages API request for the enrichment pass (pure; testable)."""
    header, value = _credentials()
    headers = {
        "content-type": "application/json",
        "anthropic-version": API_VERSION,
        header: value,
    }
    if header == "Authorization":
        headers["anthropic-beta"] = "oauth-2025-04-20"
    body = {
        "model": model or os.environ.get(MODEL_ENV, DEFAULT_MODEL),
        "max_tokens": 8192,
        "output_config": {"format": {"type": "json_schema", "schema": PAYLOAD_SCHEMA}},
        "messages": [{"role": "user", "content": build_enrich_prompt(skills)}],
    }
    return urllib.request.Request(
        API_URL, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")


def request_enrichment(skills: list[Skill], model: str | None = None,
                       timeout: float = 180.0) -> dict:
    """Call the Anthropic Messages API and return the raw payload dict."""
    req = build_request(skills, model=model)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise EnrichmentError(f"Anthropic API error {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise EnrichmentError(f"Anthropic API unreachable: {e}") from e

    if data.get("stop_reason") == "refusal":
        raise EnrichmentError("Anthropic API declined the enrichment request (refusal).")
    text = next((b.get("text", "") for b in data.get("content", [])
                 if b.get("type") == "text"), "")
    if not text:
        raise EnrichmentError("Anthropic API returned no text content.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise EnrichmentError(f"Enrichment response was not valid JSON: {e}") from e
