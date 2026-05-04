"""
tests/conftest.py — pytest 共享 fixture
=======================================
把项目根加到 sys.path, 让 `from agents.iter_planner import ...` 在 tests/ 下也能 import.
"""
import os
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
