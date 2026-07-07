"""Hand the skillmap extraction to graphify's real engine.

skillmap does not reimplement graph building — it locates the graphify install
(via its uv/pipx shebang, exactly like the graphify skill does) and calls
graphify.build.build_from_json + graphify.cluster + graphify.export.to_json in
that interpreter as a subprocess. That keeps skillmap stdlib-only while the
graph layer stays 100% graphify.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def find_graphify_python() -> str | None:
    """Locate the Python interpreter that can `import graphify`.

    Mirrors the graphify skill's Step 1 detection: read the shebang of the
    `graphify` binary (uv tool / pipx installs), else try `python3`.
    """
    # 1. shebang of the graphify launcher
    binpath = shutil.which("graphify")
    if binpath:
        try:
            first = Path(binpath).read_text(errors="replace").splitlines()[0]
        except OSError:
            first = ""
        if first.startswith("#!"):
            interp = first[2:].strip()
            if _can_import_graphify(interp):
                return interp
    # 2. uv tool run
    if shutil.which("uv"):
        try:
            out = subprocess.run(
                ["uv", "tool", "run", "graphifyy", "python", "-c",
                 "import sys; print(sys.executable)"],
                capture_output=True, text=True, timeout=60,
            )
            cand = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
            if cand and _can_import_graphify(cand):
                return cand
        except (subprocess.SubprocessError, OSError):
            pass
    # 3. plain python3
    if _can_import_graphify("python3"):
        return "python3"
    return None


def _can_import_graphify(interp: str) -> bool:
    try:
        r = subprocess.run(
            [interp, "-c", "import graphify"],
            capture_output=True, timeout=60,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


# Runs inside the graphify interpreter. Reads the extraction, builds + clusters,
# writes graph.json (+ community labels), and prints a one-line summary.
_BUILD_SRC = r"""
import json, sys
from pathlib import Path
from graphify.build import build_from_json
from graphify.cluster import cluster
from graphify.export import to_json, to_html

extract_path = sys.argv[1]
out_path = sys.argv[2]
html_path = sys.argv[3] if len(sys.argv) > 3 else ""
extraction = json.loads(Path(extract_path).read_text(encoding="utf-8"))
G = build_from_json(extraction, directed=False)
if G.number_of_nodes() == 0:
    print("ERROR: empty graph"); sys.exit(2)
communities = cluster(G)
# Force-write: skillmap always rebuilds from the full skill set, so the #479
# shrink-guard would spuriously refuse legitimate rebuilds.
to_json(G, communities, out_path, force=True)
if html_path:
    try:
        to_html(G, communities, html_path)
    except Exception as exc:  # viz is best-effort; never fail the build on it
        print("WARN: html generation failed:", exc, file=sys.stderr)
        html_path = ""
print(json.dumps({
    "nodes": G.number_of_nodes(),
    "edges": G.number_of_edges(),
    "communities": len(communities),
    "html": bool(html_path),
}))
"""


def build_graph(extraction: dict, graphify_python: str, out_dir: Path) -> dict:
    """Build graph.json from an extraction dict using graphify. Returns summary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    extract_path = out_dir / ".skillmap_extract.json"
    graph_path = out_dir / "graph.json"
    html_path = out_dir / "graph.html"
    extract_path.write_text(json.dumps(extraction, ensure_ascii=False), encoding="utf-8")

    r = subprocess.run(
        [graphify_python, "-c", _BUILD_SRC, str(extract_path), str(graph_path), str(html_path)],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"graphify build failed (exit {r.returncode}):\n{r.stderr.strip()}\n{r.stdout.strip()}"
        )
    line = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "{}"
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"raw": r.stdout.strip()}


def graphify_query(graphify_python: str, graph_path: Path, question: str,
                   budget: int = 2000, dfs: bool = False) -> str:
    """Run `graphify query` against the built graph via the graphify CLI."""
    cmd = ["graphify", "query", question, "--graph", str(graph_path),
           "--budget", str(budget)]
    if dfs:
        cmd.append("--dfs")
    env = dict(os.environ)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except (subprocess.SubprocessError, OSError) as e:
        return f"(graphify query unavailable: {e})"
    return (r.stdout or r.stderr).strip()
