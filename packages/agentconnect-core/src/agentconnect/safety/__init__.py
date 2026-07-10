"""AgentConnect-local safety scanning.

    AgentConnect-controlled surface
      -> AgentConnect safety policy
        -> configured safety engines
          -> normalized findings
            -> AgentConnect enforcement

AgentConnect owns when scanning happens, which engines run where, how findings
combine, what is persisted, what reaches an agent, and what the audit sees. Engines
own detection. A third-party engine never decides task state, artifact persistence,
audit success, or context-pack inclusion.

The default install is the standard-library `baseline` engine: deterministic,
offline, dependency-free, and a lightweight floor rather than enterprise-grade
detection. Maintained engines — detect-secrets, TruffleHog, Gitleaks, Presidio,
GLiNER, a pinned injection classifier — are opt-in and independently configurable.

**It is not a sandbox.** It reads content; it does not contain a process. It does not
stop direct SQLite access, filesystem writes, or environment tampering, and it does
not prove that scanned content is true.
"""

from .configuration import DEFAULT_ENGINES, EngineConfig, SafetyConfig
from .engines.base import BaseEngine, EngineScanRequest, ExternalToolEngine, SafetyEngine
from .models import (
    CATEGORY_OF_CAPABILITY,
    POLICY_VERSION,
    Capability,
    Category,
    Decision,
    EngineOutcome,
    EngineStatus,
    Finding,
    RiskLevel,
    SafetyBatchResult,
    SafetyItem,
    SafetyResult,
)
from .pipeline import SafetyPipeline, aggregate
from .policies import (
    ARTIFACT_INGEST,
    ATTEMPT_DECISION_NOTES,
    CONTEXT_OUTPUT,
    POLICIES,
    REPOSITORY_SCAN,
    REVIEW_INPUT,
    SUBTASK_INSTRUCTION,
    Policy,
    policy,
)
from .redaction import MARKER, redact
from .registry import KNOWN_ENGINES, EngineRegistry, build_engine
from .scanner import (
    default_config,
    default_pipeline,
    reset_default_pipeline,
    scan_items,
    scan_text,
)

__all__ = [
    "ARTIFACT_INGEST", "ATTEMPT_DECISION_NOTES", "CATEGORY_OF_CAPABILITY",
    "CONTEXT_OUTPUT", "DEFAULT_ENGINES", "KNOWN_ENGINES", "MARKER", "POLICIES",
    "POLICY_VERSION", "REPOSITORY_SCAN", "REVIEW_INPUT", "SUBTASK_INSTRUCTION",
    "BaseEngine", "Capability", "Category", "Decision", "EngineConfig",
    "EngineOutcome", "EngineRegistry", "EngineScanRequest", "EngineStatus",
    "ExternalToolEngine", "Finding", "Policy", "RiskLevel", "SafetyBatchResult",
    "SafetyConfig", "SafetyEngine", "SafetyItem", "SafetyPipeline", "SafetyResult",
    "aggregate", "build_engine", "default_config", "default_pipeline", "policy",
    "redact", "reset_default_pipeline", "scan_items", "scan_text",
]
