"""skillmap — graph-scoped retrieval for Claude skills.

Links skills to the work they apply to so a session surfaces only the relevant
neighborhood of skills instead of every installed one competing for selection.

The graph layer is graphify: skillmap discovers installed skills, stages them
into a graphify corpus, extracts a skill/concept graph, and hands it to
graphify's engine (build -> cluster -> export + query).
"""

__version__ = "0.1.0"
