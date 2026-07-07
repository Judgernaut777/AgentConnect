import sys
from pathlib import Path

# Make the four packages importable from a source checkout without installing.
# PEP 420 namespace packages: all `agentconnect/` dirs on sys.path merge into
# one `agentconnect` namespace, so `agentconnect.common`, `agentconnect.router`,
# `agentconnect.model_manager`, and `agentconnect.runtime` all resolve.
ROOT = Path(__file__).resolve().parents[1]
for _pkg in (
    "agentconnect-core",
    "agentconnect-router",
    "agentconnect-model-manager",
    "agentconnect-runtime",
    # Optional Temporal fork — only importable when its tests run (guarded by
    # importorskip("temporalio")); never touched by the default path.
    "agentconnect-temporal",
):
    _src = ROOT / "packages" / _pkg / "src"
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
