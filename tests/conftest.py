"""
conftest.py — pytest configuration and shared fixtures.

docktail.py calls _find_docker() at module level to resolve the docker binary
path.  This conftest patches shutil.which before the first import so the module
loads cleanly in CI environments that don't have Docker installed.  The patch
only needs to be in place during the import itself; after that _DOCKER is set
and the real shutil.which is restored.
"""
import os
import sys
from unittest.mock import patch

# Ensure the repo root is on sys.path so 'import docktail' works from any
# working directory that pytest might be invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

with patch("shutil.which", return_value="/usr/bin/docker"):
    import docktail  # noqa: F401, E402
