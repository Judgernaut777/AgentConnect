"""AgentConnect-local baseline safety scanning.

Local, deterministic, dependency-light. No HTTP service, no LLM classifier, no
third-party guard package. It scans text at the surfaces AgentConnect controls —
in phase 1, artifact ingest and context-pack output — and redacts, withholds, or
labels risky content before it becomes evidence or reaches an agent.

**It is not a sandbox.** It reads content; it does not contain a process. It does
not stop direct SQLite access, filesystem writes, or environment tampering, and it
does not prove that scanned content is true.
"""

from .models import (
    POLICY_VERSION,
    Category,
    Decision,
    Finding,
    RiskLevel,
    SafetyBatchResult,
    SafetyItem,
    SafetyResult,
)
from .policies import (
    ARTIFACT_INGEST,
    ATTEMPT_DECISION_NOTES,
    CONTEXT_OUTPUT,
    POLICIES,
    REVIEW_INPUT,
    SUBTASK_INSTRUCTION,
    Policy,
    policy,
)
from .redaction import MARKER, redact
from .scanner import scan_items, scan_text

__all__ = [
    "ARTIFACT_INGEST", "ATTEMPT_DECISION_NOTES", "CONTEXT_OUTPUT", "MARKER",
    "POLICIES", "POLICY_VERSION", "REVIEW_INPUT", "SUBTASK_INSTRUCTION",
    "Category", "Decision", "Finding", "Policy", "RiskLevel",
    "SafetyBatchResult", "SafetyItem", "SafetyResult",
    "policy", "redact", "scan_items", "scan_text",
]
