import sys
from pathlib import Path

# Make the three packages importable from a source checkout without installing.
# PEP 420 namespace packages: all three `agentconnect/` dirs on sys.path merge
# into one `agentconnect` namespace, so `agentconnect.common`, `agentconnect.router`,
# and `agentconnect.model_manager` all resolve.
ROOT = Path(__file__).resolve().parents[1]
for _pkg in ("agentconnect-core", "agentconnect-router", "agentconnect-model-manager"):
    _src = ROOT / "packages" / _pkg / "src"
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
