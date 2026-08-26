"""The ledger-backed :class:`RoutingFactsResolver` (ADR 0010, docs/CONTROL_SHADOW.md).

:mod:`agentconnect.core.control_shadow` deliberately holds no ledger: it defines
what a resolver must return and stops there, so the evaluator stays a pure
function of its inputs and its tests never open a database. This module is the
other half — the one place that reads the ledger and hands shadow mode the facts
about a routing that already happened.

**The bus event is a pointer; the ledger is the authority.** A `compute.placed`
payload does carry the placement class, and reading it would be one line shorter.
The ledger is read instead because that is where the routing decision actually
lives: `Subtask.route_reason` is the persisted `RouteExplanation` the router
itself wrote. A bus payload is a projection of that, and EVENT_BUS.md §0 is
explicit that the bus is "never authoritative for anything."

That distinction is not academic here. Until the fix that accompanies this
module, `compute.placed` reported `location: "local"` for *every* route,
including cloud and rented ones — the emit read a field that did not exist on
`RouteExplanation` and fell through to a literal default. A resolver trusting
that payload would have scored every cloud route as a disagreement and quietly
understated the model. Reading the explanation the router wrote is the habit
that survives that class of bug.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from .control_projection import tier_to_control_privacy
from .control_shadow import NormalizedState, RouterFacts, ShadowInput
from .models import PrivacyTier
from .routing import RouteExplanation

log = logging.getLogger(__name__)


class LedgerRoutingFactsResolver:
    """Resolves a routing event into a :class:`ShadowInput` from the ledger.

    ``storage`` is anything exposing ``get_subtask(subtask_id)`` — the ledger's
    own storage handle does, and a fake does in tests.

    Every failure mode returns ``None`` rather than raising: a subtask that has
    been reaped, an event with no subtask id, a route that was never recorded.
    The consumer counts those as skips, which is the right outcome for an
    evaluator — a hole in a dataset, never an exception into a caller.
    """

    def __init__(self, storage: Any, *, policy_version: str = "unknown") -> None:
        self._storage = storage
        self._policy_version = policy_version

    def resolve(self, event: Mapping[str, Any]) -> Optional[ShadowInput]:
        subtask_id = event.get("subtask_id")
        if not subtask_id:
            return None

        subtask = self._storage.get_subtask(subtask_id)
        if subtask is None:
            return None

        explanation = self._explanation(subtask)
        if explanation is None or not explanation.selected_worker:
            # No recorded route, or a subtask that never reached a worker
            # (blocked, refused, still queued). Nothing to compare against.
            return None

        return ShadowInput(
            state=self._state(subtask),
            router=RouterFacts.from_worker_location(
                explanation.selected_location,
                decision="routed",
                worker=explanation.selected_worker,
                model=explanation.selected_model,
                policy_version=self._policy_version,
            ),
            task_id=event.get("task_id") or getattr(subtask, "parent_task_id", None),
            subtask_id=subtask_id,
        )

    @staticmethod
    def _explanation(subtask: Any) -> Optional[RouteExplanation]:
        raw = getattr(subtask, "route_reason", None)
        if not raw:
            return None
        try:
            return RouteExplanation(**raw)
        except Exception as exc:  # a malformed stored route is a skip, not a crash
            log.debug("shadow: unparseable route_reason on %s: %s",
                      getattr(subtask, "id", "?"), exc)
            return None

    @staticmethod
    def _state(subtask: Any) -> NormalizedState:
        """Project the subtask into the model's vocabulary.

        Only fields the ledger genuinely holds are filled. `Subtask` carries no
        token estimates, no `allow_external`, and no redaction verdict, so those
        keep :class:`NormalizedState`'s defaults rather than being invented from
        adjacent values — a fabricated input would make a shadow record compare
        two decisions that never saw the same state.

        `allow_paid` is the one exception, and it is a fact rather than a guess:
        a recorded `approved_max_cost_usd` means a human approved spend on this
        subtask.

        `subtask.instructions` — the only free text on the record — is never
        read. `SHADOW_STATE_KEYS` would reject it downstream, but the honest
        place to not leak a prompt is to not pick it up.
        """
        tier = getattr(subtask, "privacy_tier", PrivacyTier.repo_sensitive)
        return NormalizedState(
            privacy=tier_to_control_privacy(tier),
            needed_capabilities=tuple(getattr(subtask, "required_capabilities", ()) or ()),
            allow_paid=getattr(subtask, "approved_max_cost_usd", None) is not None,
        )
