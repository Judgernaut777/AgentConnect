"""Node provisioning for rented GPU inference (handoff Goal 4).

A rented GPU box runs the **same** ``agentconnect-model-manager`` as an owned node
and is reached over the **same mutual-TLS transport**. The only new machinery is
lifecycle: rent -> wait-for-ready -> (use) -> drain -> terminate.

This module defines the provisioner interface plus a deterministic
:class:`StubProvisioner` so the rented-node path is testable offline. Real vendor
adapters (RunPod / Lambda / Vast) implement :class:`NodeProvisioner` and read the
vendor's control-plane API key from the secrets manager via
``RentalConfig.secret_ref`` — that key is the ONLY secret involved, and it is used
purely to rent/terminate the box, never for inference traffic.
"""

from __future__ import annotations

import abc
from typing import Optional

from ..common.config import ProviderConfig
from ..common.schemas import NodeHandle, NodeSpec, NodeState, NodeTrust


def spec_from_provider(cfg: ProviderConfig, model_id: Optional[str] = None) -> NodeSpec:
    """Build a :class:`NodeSpec` from a rented provider's config entry."""
    rental = cfg.rental
    trust = NodeTrust(**(rental.trust if rental and rental.trust else {}))
    return NodeSpec(
        provider_id=cfg.provider_id,
        vendor=rental.vendor if rental else "generic",
        instance_type=rental.instance_type if rental else None,
        model_id=model_id,
        min_rental_seconds=rental.min_rental_seconds if rental else 900,
        max_hourly_usd=rental.max_hourly_usd if rental else 0.0,
        trust=trust,
    )


class NodeProvisioner(abc.ABC):
    """Lifecycle control for an inference node. Owned nodes are always-on and do not
    need a provisioner; rented nodes do."""

    @abc.abstractmethod
    def provision(self, spec: NodeSpec) -> NodeHandle: ...

    @abc.abstractmethod
    def wait_ready(self, handle: NodeHandle, timeout_seconds: int = 600) -> NodeHandle: ...

    @abc.abstractmethod
    def drain(self, handle: NodeHandle) -> NodeHandle: ...

    @abc.abstractmethod
    def terminate(self, handle: NodeHandle) -> NodeHandle: ...


class StubProvisioner(NodeProvisioner):
    """Deterministic in-memory provisioner for development and tests.

    ``provision`` returns a ``ready`` handle immediately (no real box, no network,
    no randomness). ``clock`` is injectable so ``started_at`` is deterministic in
    tests; it defaults to a fixed epoch rather than wall-clock time.
    """

    def __init__(self, endpoint_template: str = "https://rented-{pid}.local:8443", clock: float = 0.0):
        self._endpoint_template = endpoint_template
        self._clock = clock
        self._counter = 0

    def provision(self, spec: NodeSpec) -> NodeHandle:
        self._counter += 1
        node_id = f"{spec.provider_id}-rented-{self._counter:03d}"
        return NodeHandle(
            node_id=node_id,
            provider_id=spec.provider_id,
            state=NodeState.ready,
            manager_endpoint=self._endpoint_template.format(pid=spec.provider_id),
            started_at=self._clock,
            hourly_usd=spec.max_hourly_usd,
            trust=spec.trust,
        )

    def wait_ready(self, handle: NodeHandle, timeout_seconds: int = 600) -> NodeHandle:
        return handle.model_copy(update={"state": NodeState.ready})

    def drain(self, handle: NodeHandle) -> NodeHandle:
        return handle.model_copy(update={"state": NodeState.draining})

    def terminate(self, handle: NodeHandle) -> NodeHandle:
        return handle.model_copy(update={"state": NodeState.terminated})
