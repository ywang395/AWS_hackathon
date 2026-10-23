"""
conftest.py (wardline root) -- makes `import backend....` work from any
test file under this project, regardless of where pytest is invoked from.

`backend/` has no `__init__.py` files (it relies on Python's implicit
namespace packages, PEP 420), so the only thing missing is having the
`wardline/` directory itself on `sys.path`. This file adds it once, at
collection time, for every test in the suite.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))