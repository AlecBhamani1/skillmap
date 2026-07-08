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
from collections import Counter, defaultdict

from .discover import DEFAULT_ROOTS, Skill


def _origin(skill: Skill) -> str:
    """"global" if the skill lives under a global root, else "project"."""
    for root in DEFAULT_ROOTS:
        try:
            if skill.path.is_relative_to(root.resolve()):
                return "global"
        except OSError:
            continue
    return "project"

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


def _bigrams(text: str) -> list[str]:
    """Adjacent non-stopword word pairs, e.g. "knowledge graph" from "...the
    knowledge graph of...".

    Adjacency is checked on the *original* word order (no CamelCase split,
    which would blur which words were really next to each other) so that
    "knowledge" and "graph" mentioned in unrelated sentences never fabricate a
    "knowledge graph" phrase concept -- only a genuine adjacent phrase does.
    """
    pairs: list[str] = []
    prev: str | None = None
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9-]+", text):
        w = raw.lower()
        keep = 3 <= len(w) <= 30 and w not in _STOPWORDS and not w.isdigit()
        if keep and prev is not None:
            pairs.append(f"{prev} {w}")
        prev = w if keep else None
    return pairs


# ---------------------------------------------------------------------------
# Redirection detection (P1): a skill's description often disclaims work that
# belongs to another skill ("Use `orca-cli` instead for terminal control...").
# Those spans must not seed the *mentioning* skill's own concepts/scoring --
# and when the named skill is installed, the disclaimer becomes a routing
# signal that credits the span to the skill it actually names.
# ---------------------------------------------------------------------------

# "instead for" is an explicit hand-off marker, so it's trusted regardless of
# casing/backticks. The bare "use X for" forms are more prone to false
# positives ("use this skill for...") so they're only trusted when X is
# clearly a proper name: backticked, or capitalized.
_REDIRECT_INSTEAD_RE = re.compile(
    r"\buse\s+`?(?P<name>[A-Za-z][\w /-]{0,40}?)`?\s+instead\s+for\s+(?P<rest>[^.]+)",
    re.IGNORECASE)
_REDIRECT_FOR_BACKTICK_RE = re.compile(
    r"\buse\s+`(?P<name>[^`]{1,40})`\s+for\s+(?P<rest>[^.]+)", re.IGNORECASE)
_REDIRECT_FOR_PROPER_RE = re.compile(
    r"\b[Uu]se\s+(?P<name>[A-Z][\w -]{0,40}?)\s+for\s+(?P<rest>[^.]+)")
_REDIRECT_PREFER_RE = re.compile(
    r"\bprefer\s+`?(?P<name>[A-Za-z][\w /-]{0,40}?)`?\s+over\s+(?P<rest>[^.]+)",
    re.IGNORECASE)
_REDIRECT_BETTER_RE = re.compile(
    r"`?(?P<name>[A-Za-z][\w /-]{0,40}?)`?\s+is\s+better\s+for\s+(?P<rest>[^.]+)",
    re.IGNORECASE)
_REDIRECT_PATTERNS = [
    _REDIRECT_INSTEAD_RE, _REDIRECT_FOR_BACKTICK_RE, _REDIRECT_FOR_PROPER_RE,
    _REDIRECT_PREFER_RE, _REDIRECT_BETTER_RE,
]
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _normalize_name(name: str) -> str:
    """Fold a skill/candidate name to bare lowercase words for fuzzy matching
    ("orca-cli", "Orca CLI" and "orca cli" all normalize the same way)."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _match_redirect(sentence: str) -> tuple[str, str] | None:
    """If `sentence` is a redirection sentence, return (named_text, rest_text)."""
    for pat in _REDIRECT_PATTERNS:
        m = pat.search(sentence)
        if m:
            return m.group("name").strip(), m.group("rest").strip()
    return None


def _resolve_named_skill(name_text: str, skills_by_norm: dict[str, str]) -> str | None:
    """Resolve free-text `name_text` to an installed skill id, else None.

    Matches when a skill's normalized name appears as a whole-word phrase
    inside `name_text` (so "Orca orchestration" resolves to "orchestration",
    "Computer Use" resolves to "computer-use"). The longest/most specific
    match wins when more than one skill's name appears.
    """
    norm = _normalize_name(name_text)
    if not norm:
        return None
    best: tuple[int, str] | None = None
    for skill_norm, sid in skills_by_norm.items():
        if skill_norm and re.search(rf"(?:^|\s){re.escape(skill_norm)}(?:\s|$)", norm):
            if best is None or len(skill_norm) > best[0]:
                best = (len(skill_norm), sid)
    return best[1] if best else None


def split_redirections(text: str, self_id: str,
                       skills_by_norm: dict[str, str]) -> tuple[str, dict[str, list[str]]]:
    """Split a skill's description into (clean_text, routed).

    clean_text: `text` with redirection sentences that name *another* skill
    removed -- a self-referential "Use Orca orchestration for ..." sentence
    describes this same skill, so it's not a redirection and stays in.
    routed: target skill id -> list of sentence fragments whose tokens should
    be credited to that skill instead of the one this text belongs to
    (sentences naming no installed skill are stripped but routed nowhere --
    they're still disclaimer language that must not seed the mentioning
    skill, even if we don't know who else it belongs to).
    """
    kept: list[str] = []
    routed: dict[str, list[str]] = defaultdict(list)
    for sentence in (s for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s):
        m = _match_redirect(sentence)
        if not m:
            kept.append(sentence)
            continue
        name_text, rest_text = m
        target = _resolve_named_skill(name_text, skills_by_norm)
        if target is None:
            continue
        if target == self_id:
            kept.append(sentence)
            continue
        routed[target].append(rest_text)
    return " ".join(kept), dict(routed)


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
        "skillmap_origin": _origin(skill),
    }


# A phrase concept needs at least this much weighted evidence to survive --
# one coincidental adjacent pair seen only once in the body (weight 1.0) is
# noise; one seen in the description, or twice in the body, is a real phrase.
_BIGRAM_FLOOR = 2.0

# A word repeated many times within one block (typically a body stuffed with
# copy-pasted CLI examples, each repeating the same command/flag names) is
# not many times more "about" that word than a handful of mentions -- past
# this many hits in a single block, extra repeats stop adding weight. Without
# this, boilerplate repetition in one skill's usage examples can outweigh a
# genuine, once-stated concept (or a redirected disclaimer) in another's.
_MAX_HITS_PER_BLOCK = 4

# Fragments routed in from another skill's redirection sentence get a flat
# weight higher than even the description tier: another skill's author
# explicitly naming you as the right destination for this topic is a
# stronger, more deliberate signal than your own incidental word frequency,
# and must not lose out to that skill's own boilerplate/body repetition.
_ROUTED_WEIGHT = 6.0


def _pick_concepts(blocks: list[tuple[str, float]], max_concepts: int) -> list[str]:
    """Rank candidate concepts (unigram + phrase) by weighted frequency across
    (text, weight) blocks -- description/headings/body weighted high to low,
    each already split by split_redirections (see build_extraction) so
    redirection spans naming another skill aren't in here, plus any fragments
    routed in from other skills' redirections at _ROUTED_WEIGHT.
    """
    counts: dict[str, float] = defaultdict(float)
    bigram_counts: dict[str, float] = defaultdict(float)
    for text, weight in blocks:
        for tok, hits in Counter(_tokenize(text)).items():
            counts[tok] += weight * min(hits, _MAX_HITS_PER_BLOCK)
        for bg, hits in Counter(_bigrams(text)).items():
            bigram_counts[bg] += weight * min(hits, _MAX_HITS_PER_BLOCK)

    # Cap how many phrase concepts compete for a slot, so a chatty body can't
    # crowd out unigram concepts entirely.
    bigram_cap = max(2, max_concepts // 3)
    qualifying = sorted(
        ((bg, w) for bg, w in bigram_counts.items() if w >= _BIGRAM_FLOOR),
        key=lambda kv: (-kv[1], kv[0]),
    )[:bigram_cap]
    for bg, w in qualifying:
        counts[bg] = w

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

    # Split every text block (description/headings/body) up front so
    # redirection sentences naming another skill (i) never seed the
    # mentioning skill's own concepts and (ii) instead credit the concepts of
    # the skill they actually name -- see split_redirections and
    # _ROUTED_WEIGHT.
    skills_by_norm = {_normalize_name(s.name): _skill_id(s) for s in skills}
    own_blocks: dict[str, list[tuple[str, float]]] = {}
    routed_in: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for skill in skills:
        sid = _skill_id(skill)
        raw_blocks = ([(skill.description, 3.0)] + [(h, 2.0) for h in skill.headings]
                      + [(skill.body, 1.0)])
        kept: list[tuple[str, float]] = []
        for text, weight in raw_blocks:
            clean, routed = split_redirections(text, sid, skills_by_norm)
            kept.append((clean, weight))
            for target_sid, fragments in routed.items():
                routed_in[target_sid].extend((frag, _ROUTED_WEIGHT) for frag in fragments)
        own_blocks[sid] = kept

    for skill in skills:
        nodes.append(_skill_node(skill))
        sid = _skill_id(skill)

        for trig in skill.triggers:
            trigger_to_skills[trig].add(sid)

        concepts = _pick_concepts(own_blocks[sid], max_concepts_per_skill)
        routed_fragments = routed_in.get(sid)
        if routed_fragments:
            # Guarantee routed-in concepts a place regardless of how much of
            # the target's own vocabulary already fills max_concepts_per_skill
            # -- a disclaimer explicitly naming this skill must always land as
            # a real edge here, not lose a frequency contest to this skill's
            # own boilerplate. Capped independently so many redirectors can't
            # blow the concept count out unboundedly.
            routed_cap = max(6, max_concepts_per_skill // 2)
            for concept in _pick_concepts(routed_fragments, routed_cap):
                if concept not in concepts:
                    concepts.append(concept)
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
