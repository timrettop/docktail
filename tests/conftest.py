"""
conftest.py — pytest configuration.

Ensures the repo root is on sys.path so test files can do
'import core' and 'import docktail' without installation.

core.py has no module-level side effects so it imports cleanly.
docktail.py calls _find_docker() at module level; that import is
patched here for any test that needs it, but parser/timestamp tests
only import from core and need no patching at all.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
