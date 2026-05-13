"""Make scripts/ importable as a top-level package for tests.

`scripts/` holds Python modules invoked as CLI tools; they aren't packaged
under src/ because the runtime targets are LOQ via `python3 scripts/foo.py`,
not pip-installable distributions. Tests need to import the helper functions
directly, so we inject scripts/ at the front of sys.path here.
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
