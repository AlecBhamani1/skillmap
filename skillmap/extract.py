"""Turn discovered skills into a graphify-compatible extraction JSON.

Output schema matches graphify's extractor exactly (see graphify
references/extraction-spec.md): {nodes, edges, hyperedges, input_tokens,
output_tokens}. graphify's own build_from_json / cluster / to_json then consume
it unchanged — skillmap owns extraction, graphify owns the graph.

Two node tiers, per the design:
  - skill nodes   (file_type="document"): one per SKILL.md, the scoping targets
  - concept nodes (file_type="concept"):  shared topics that link skills together

Edges use graphify's fixed relation vocabulary only:
  - skill --references--> concept        (skill covers this concept)   EXTRACTED
  - skill --semantically_similar_to--> skill   (shared concepts / trigger overlap)  INFERRED

Extraction is deterministic and key-free so the POC runs end to end. When a
Gemini key is present, enrich_with_llm() can layer richer semantic edges on top.
"""

from __future__ import annotations

import re
from collections import defaultdict

from .discover import Skill

# Words that are never useful concept anchors.
_STOPWORDS = set(
    """
    the a an and or of to for in on at by with from into over under is are be as
    it its this that these those you your yours we our they them their he she his
    her use used using when where what which who how why then than so if but not
    no yes do does did done can could should would may might must will shall have
    has had via per etc eg ie vs about above below after before between during
    without within across only just also more most less least any all each every
    some such other same one two three first second new run get set see show make
    build user users skill skills claude anthropic tool tools command commands
    file files step steps must always never note important example examples
    default value values path paths type types field fields output input args
    """.split()
)


def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens, splitting CamelCase and dropping stopwords/short."""
    words: list[str] = []
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9-]+", text):
        # split CamelCase -> "GraphRAG" -> graph, rag
        parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+", raw) or [raw]
        for p in parts:
            t = p.lower()
            if 3 <= len(t) <= 30 and t not in _STOPWORDS and not t.isdigit():
                words.append(t)
    return words


def _concept_id(concept: str) -> str:
    return "concept_" + re.sub(r"[^a-z0-9]+", "_", concept.lower()).strip("_")


def _skill_id(skill: Skill) -> str:
    return "skill_" + skill.slug


def _skill_node(skill: Skill) -> dict:
    return {
        "id": _skill_id(skill),
        "label": skill.name,
        "file_type": "document",
        "source_file": str(skill.path),
        "source_location": None,
        "source_url": str(skill.path),
        "captured_at": None,
        "author": skill.name,
        "contributor": "skillmap",
        # skillmap-specific metadata (graphify preserves unknown node keys).
        "skillmap_kind": "skill",
        "skillmap_description": skill.description,
        "skillmap_triggers": skill.triggers,
        "skillmap_references": skill.references,
    }


def _pick_concepts(skill: Skill, max_concepts: int) -> list[str]:
    """Rank candidate concepts for a skill by weighted frequency.

    Headings and the description carry more signal than the body, so weight them.
    """
    counts: dict[str, float] = defaultdict(float)
    for tok in _tokenize(skill.description):
        counts[tok] += 3.0
    for h in skill.headings:
        for tok in _tokenize(h):
            counts[tok] += 2.0
    for tok in _tokenize(skill.body):
        counts[tok] += 1.0
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [c for c, _w in ranked[:max_concepts]]


def build_extraction(skills: list[Skill], max_concepts_per_skill: int = 12) -> dict:
    """Build the graphify extraction dict from discovered skills."""
    nodes: list[dict] = []
    edges: list[dict] = []
    concept_nodes: dict[str, dict] = {}
    # concept -> set of skill ids that cover it (for shared-concept skill edges).
    concept_to_skills: dict[str, set[str]] = defaultdict(set)
    # trigger -> set of skill ids (for trigger-overlap edges).
    trigger_to_skills: dict[str, set[str]] = defaultdict(set)

    for skill in skills:
        nodes.append(_skill_node(skill))
        sid = _skill_id(skill)

        for trig in skill.triggers:
            trigger_to_skills[trig].add(sid)

        concepts = _pick_concepts(skill, max_concepts_per_skill)
        for rank, concept in enumerate(concepts):
            cid = _concept_id(concept)
            if cid not in concept_nodes:
                concept_nodes[cid] = {
                    "id": cid,
                    "label": concept,
                    "file_type": "concept",
                    "source_file": str(skill.path),
                    "source_location": None,
                    "source_url": None,
                    "captured_at": None,
                    "author": None,
                    "contributor": "skillmap",
                    "skillmap_kind": "concept",
                }
            concept_to_skills[concept].add(sid)
            # Higher-ranked concepts get higher weight/confidence.
            weight = round(1.0 - rank / (max_concepts_per_skill + 2), 3)
            edges.append(
                {
                    "source": sid,
                    "target": cid,
                    "relation": "references",
                    "confidence": "EXTRACTED",
                    "confidence_score": 1.0,
                    "source_file": str(skill.path),
                    "source_location": None,
                    "weight": max(weight, 0.3),
                }
            )

    nodes.extend(concept_nodes.values())

    # Skill<->skill edges from shared concepts. Confidence scales with overlap.
    pair_shared: dict[tuple[str, str], int] = defaultdict(int)
    for concept, sids in concept_to_skills.items():
        sids_l = sorted(sids)
        for i in range(len(sids_l)):
            for j in range(i + 1, len(sids_l)):
                pair_shared[(sids_l[i], sids_l[j])] += 1

    for (a, b), shared in pair_shared.items():
        if shared < 2:  # a single shared generic concept is too weak to link
            continue
        # 2 shared -> 0.65, 3 -> 0.75, 4+ -> 0.85 (graphify's discrete rubric)
        score = {2: 0.65, 3: 0.75}.get(shared, 0.85)
        edges.append(
            {
                "source": a,
                "target": b,
                "relation": "semantically_similar_to",
                "confidence": "INFERRED",
                "confidence_score": score,
                "source_file": "skillmap://shared-concepts",
                "source_location": f"{shared} shared concepts",
                "weight": float(shared),
            }
        )

    # Trigger-overlap edges: two skills answering to the same slash command.
    for trig, sids in trigger_to_skills.items():
        sids_l = sorted(sids)
        for i in range(len(sids_l)):
            for j in range(i + 1, len(sids_l)):
                edges.append(
                    {
                        "source": sids_l[i],
                        "target": sids_l[j],
                        "relation": "semantically_similar_to",
                        "confidence": "INFERRED",
                        "confidence_score": 0.9,
                        "source_file": "skillmap://trigger-overlap",
                        "source_location": f"shared trigger {trig}",
                        "weight": 2.0,
                    }
                )

    return {
        "nodes": nodes,
        "edges": edges,
        "hyperedges": [],
        "input_tokens": 0,
        "output_tokens": 0,
    }
