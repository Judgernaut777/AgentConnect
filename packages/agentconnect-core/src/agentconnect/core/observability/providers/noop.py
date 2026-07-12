"""The provider that observes nothing (production handoff Part II).

It is the default. A standalone AgentConnect install configures no providers and
gets this one, so every emission site can call the emitter unconditionally and a
zero-infra deployment pays nothing for observability it did not ask for.
"""

from __future__ import annotations

from ..provider import AgentObservabilityProvider


class NoopObservabilityProvider(AgentObservabilityProvider):
    name = "noop"
