"""Vercel's entrypoint for the web demo (docs/adr/0005). The app itself is rcm_ai.web:app.

Vercel looks for the entrypoint's module file from the project root, so it can't find a module
under src/. This shim puts src/ on the path in case the build didn't install the package.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rcm_ai.web import app  # noqa: E402

__all__ = ["app"]
