"""draftwatch — review an AI agent's edits to your writing as a real git word-diff.

The diff is produced by git on your machine, never by the AI vendor: independent
verification of what the agent changed, with hunk-by-hunk keep/revert and a
commit loop.
"""

from .app import *                  # noqa: F401,F403  (re-export the engine for tests/embedding)
from .app import main, __version__  # noqa: F401  (version lives in app.py, single source)
