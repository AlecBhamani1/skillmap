"""Skill scoping: given a work context, return the relevant neighborhood of skills.

This is skillmap's reason for existing. Instead of every installed skill
competing for selection, a query returns only the skills connected to the
current work context in the graph.

Algorithm (pure, no LLM needed):
  1. Tokenize the work context; match tokens against concept- and skill-node
     labels + skill descriptions/triggers (case-folded substring, IDF-weighted).
  2. Seed scores on matched nodes, then BFS-propagate a decaying score across
     graph edges (weighted by edge weight/confidence).
  3. Collect skill nodes by accumulated score -> the relevant neighborhood.

This mirrors graphify's own query traversal (label-match seeds -> BFS
neighborhood) but post-filters to skill nodes and returns a ranked list, which
is exactly the scoping primitive the README calls for.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]+")

_STOP = set(
    """the a an and or of to for in on at by with from into is are be as it this
    that you your we our i how do does when what which help me please want need
    working work fix add build make change update using use""".split()
)


def _stem(tok: str) -> str:
    """Cheap suffix stem so plural/gerund query words match concept labels."""
    for suf in ("ing", "ies", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            return tok[: -len(suf)] + ("y" if suf == "ies" else "")
    return tok


def _tokens(text: str) -> list[str]:
    out = []
    for raw in _TOKEN_RE.findall(text.lower()):
        if len(raw) >= 3 and raw not in _STOP:
            out.append(_stem(raw))
    return out


@dataclass
class ScopedSkill:
    name: str
    score: float
    matched_concepts: list[str]
    description: str
    triggers: list[str]
    path: str = ""  # SKILL.md location, so a caller can load the body
    origin: str = ""  # "project" or "global"


class SkillGraph:
    """Loaded graph.json with scoping over it."""

    def __init__(self, data: dict):
        self.nodes = {n["id"]: n for n in data.get("nodes", [])}
        self.adj: dict[str, list[tuple[str, float]]] = defaultdict(list)
        # graphify's to_json emits networkx node-link format: edges live under
        # "links". Accept "edges" too so a raw extraction dict also loads.
        for e in data.get("links", data.get("edges", [])):
            s, t = e.get("source"), e.get("target")
            if s in self.nodes and t in self.nodes:
                w = float(e.get("weight", 1.0)) * float(e.get("confidence_score", 1.0))
                self.adj[s].append((t, w))
                self.adj[t].append((s, w))
        # IDF over concept labels: rarer concepts are more discriminating.
        self._concept_df: dict[str, int] = defaultdict(int)
        n_skills = sum(1 for n in self.nodes.values() if n.get("skillmap_kind") == "skill")
        self._n_skills = max(n_skills, 1)
        for cid, node in self.nodes.items():
            if node.get("skillmap_kind") == "concept":
                self._concept_df[cid] = len(self.adj.get(cid, []))

    @classmethod
    def load(cls, graph_path: Path) -> "SkillGraph":
        return cls(json.loads(Path(graph_path).read_text(encoding="utf-8")))

    def _idf(self, cid: str) -> float:
        df = max(self._concept_df.get(cid, 1), 1)
        return math.log(1 + self._n_skills / df)

    def _seed_scores(self, query_tokens: set[str]) -> dict[str, float]:
        """Score nodes whose label/description/triggers match query tokens."""
        seeds: dict[str, float] = defaultdict(float)
        for nid, node in self.nodes.items():
            kind = node.get("skillmap_kind")
            label_toks = set(_tokens(node.get("label", "")))
            if kind == "concept":
                if label_toks & query_tokens:
                    seeds[nid] += 2.0 * self._idf(nid)
            elif kind == "skill":
                # direct hits on skill name / description / triggers
                hay = " ".join([
                    node.get("label", ""),
                    node.get("skillmap_description", ""),
                    " ".join(node.get("skillmap_triggers", []) or []),
                ])
                overlap = query_tokens & set(_tokens(hay))
                if overlap:
                    seeds[nid] += 1.5 * len(overlap)
        return seeds

    def scope(self, work_context: str, top_k: int = 8,
              hops: int = 2, decay: float = 0.5,
              min_ratio: float = 0.1) -> list[ScopedSkill]:
        """Return the ranked neighborhood of skills relevant to work_context.

        min_ratio drops skills scoring below that fraction of the top score, so
        the result is a scoped *neighborhood* rather than a ranked list of every
        installed skill. Set to 0 to keep the full gradient.
        """
        qtok = set(_tokens(work_context))
        seeds = self._seed_scores(qtok)
        # BFS score propagation with decay.
        scores: dict[str, float] = defaultdict(float)
        for nid, s in seeds.items():
            scores[nid] += s
        frontier = dict(seeds)
        for _ in range(hops):
            nxt: dict[str, float] = defaultdict(float)
            for nid, s in frontier.items():
                neigh = self.adj.get(nid, [])
                total_w = sum(w for _n, w in neigh) or 1.0
                for tgt, w in neigh:
                    contrib = s * decay * (w / total_w)
                    if contrib > 0.01:
                        nxt[tgt] += contrib
            for nid, s in nxt.items():
                scores[nid] += s
            frontier = nxt

        # Collect skill nodes; record which matched concepts fed each one.
        results: list[ScopedSkill] = []
        for nid, node in self.nodes.items():
            if node.get("skillmap_kind") != "skill":
                continue
            sc = scores.get(nid, 0.0)
            if sc <= 0:
                continue
            # Explain the surface: direct concept seeds first, then any peer
            # skill it links to (so a skill pulled in purely by a skill↔skill
            # edge still shows *why* it appeared, never a bare score).
            matched: list[str] = []
            peer_skills: list[str] = []
            for tgt, _w in self.adj.get(nid, []):
                tn = self.nodes.get(tgt, {})
                kind = tn.get("skillmap_kind")
                if kind == "concept" and tgt in seeds:
                    matched.append(tn.get("label", tgt))
                elif kind == "skill" and scores.get(tgt, 0) > 0:
                    peer_skills.append(tn.get("label", tgt))
            if not matched and peer_skills:
                matched = ["→ " + p for p in sorted(set(peer_skills))]
            results.append(ScopedSkill(
                name=node.get("label", nid),
                score=round(sc, 4),
                matched_concepts=sorted(set(matched)),
                description=node.get("skillmap_description", ""),
                triggers=node.get("skillmap_triggers", []) or [],
                path=node.get("source_file", "") or "",
                origin=node.get("skillmap_origin", "") or "",
            ))
        results.sort(key=lambda r: -r.score)
        if results and min_ratio > 0:
            cutoff = results[0].score * min_ratio
            results = [r for r in results if r.score >= cutoff]
        return results[:top_k]
