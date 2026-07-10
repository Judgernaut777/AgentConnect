"""The stable entry points: `scan_text` and `scan_items`.

Callers name a surface and get a normalized result. They do not know which engines
ran, whether any of them was a subprocess, or whether one was missing — the
`SafetyResult` carries all of that, and the decision already accounts for it.

The default pipeline is built once, on first use, from `default_config()`. Which is
the baseline engine and nothing else: a default install stays lightweight and
offline. A deployment that configures engines constructs its own `SafetyPipeline`
and hands it to `AgentConnectService`.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .configuration import SafetyConfig
from .models import SafetyBatchResult, SafetyItem, SafetyResult
from .pipeline import SafetyPipeline

_DEFAULT: Optional[SafetyPipeline] = None


def default_config() -> SafetyConfig:
    return SafetyConfig()


def default_pipeline() -> SafetyPipeline:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = SafetyPipeline(default_config())
    return _DEFAULT


def reset_default_pipeline() -> None:
    """Test seam. Configuration is process-wide; a test that changes it says so."""
    global _DEFAULT
    _DEFAULT = None


def scan_text(content: str, *, surface: str, policy: str,
              pipeline: Optional[SafetyPipeline] = None) -> SafetyResult:
    """Scan one piece of text under a named policy.

    `surface` is recorded for the caller's logs and warnings; `policy` selects the
    decision table and the engine set. They are separate because a future surface may
    reuse an existing policy, and conflating them would force a new table per caller.
    """
    return (pipeline or default_pipeline()).scan_text(
        content, surface=surface, policy=policy)


def scan_items(items: Iterable[SafetyItem], *, policy: str,
               pipeline: Optional[SafetyPipeline] = None) -> SafetyBatchResult:
    """Scan many items, preserving each caller-supplied id.

    Identity is the point: the caller has to know *which* item was withheld, or the
    only honest thing it can report is that the pack got shorter.
    """
    return (pipeline or default_pipeline()).scan_items(items, policy=policy)
