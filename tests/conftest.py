"""Shared pytest fixtures and path setup.

Tests run straight from the repository checkout (no ``pip install -e .``),
so the project root is pushed onto ``sys.path`` before anything imports
``file_organizer``.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
