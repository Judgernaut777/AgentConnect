"""Typed errors for the backplane.

Adapters map these onto their own vocabulary (HTTP status, MCP error text, CLI
exit code) — the service never raises protocol-specific errors.
"""

from __future__ import annotations


class AgentConnectError(Exception):
    """Base class for every error the service raises deliberately."""

    code = "agentconnect_error"


class NotFound(AgentConnectError):
    code = "not_found"


class Conflict(AgentConnectError):
    """The request is well-formed but loses a race or violates an invariant
    (e.g. a second primary_manager claim on a task that already has one)."""

    code = "conflict"


class Unauthenticated(AgentConnectError):
    """The credential is missing, unknown, expired, or revoked.

    Distinct from `PolicyViolation` on purpose. *Who are you* and *you may not do
    that* are different failures with different fixes: one is a token to re-mint,
    the other is a permission the principal was never meant to have. Collapsing
    them tells an operator to go looking in the wrong place.
    """

    code = "unauthenticated"


class PolicyViolation(AgentConnectError):
    """Refused by policy: privacy, authority, or approval — never by a bug."""

    code = "policy_violation"


class InvalidRequest(AgentConnectError):
    code = "invalid_request"
