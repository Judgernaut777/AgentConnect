"""The standard-library baseline rules. Each module exposes `find(text)`.

A lightweight floor, not enterprise-grade detection. See `engines/baseline.py`.
"""

from . import encoding, prompt_injection, secrets, tool_instructions

__all__ = ["encoding", "prompt_injection", "secrets", "tool_instructions"]
