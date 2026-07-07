"""Temporal-backed durable execution of one agentic task.

**Topology.** AgentConnect's federation is PULL + mTLS-identity→tier; Temporal is
PUSH + server-assigned task queues. Privacy is enforced two ways here, both keeping
the authorization rule OURS:

  1. an **admission activity** that gates on ``common.privacy.admits(routing, tier,
     class)`` — the exact fail-closed predicate the SQLite ``WorkQueue`` uses; and
  2. **per-tier task queues** (deployment convention) so only appropriately-trusted
     workers poll a given class of work.

**What Temporal owns (the commodity mechanics we rent):** durable execution across a
worker crash, the retry policy (``maximum_attempts`` == our ``attempts``/``max_attempts``),
activity heartbeats/timeouts (== our lease renew / reaper), and — via child workflows —
the dependency DAG. **What stays ours:** the privacy predicate above and the
``AgentRuntime`` that actually executes the task (the same one the default path runs).

Nothing in the default path imports this module; it lives behind the ``[temporal]``
extra and is only loaded by a Temporal worker process.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable, Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

# Our own modules are deterministic to the workflow but must be passed through the
# workflow sandbox unchanged (they are used inside activities, which run OUTSIDE the
# sandbox, and for the params dataclass).
with workflow.unsafe.imports_passed_through():
    from agentconnect.common.config import RoutingConfig
    from agentconnect.common.privacy import admits
    from agentconnect.common.schemas import TaskConstraints, TaskSubmission, WorkerResult


@dataclass
class AgentTaskParams:
    """The serializable input to :class:`AgentTaskWorkflow` (Temporal's data
    converter handles dataclasses). ``attested_tier`` is the worker identity the
    admission gate checks — in a real deployment it is bound by which per-tier task
    queue served the workflow, not taken from an untrusted caller."""

    task: str
    privacy_class: str
    attested_tier: str
    agent_type: str = "worker"
    task_id: str = "temporal-task"
    max_attempts: int = 3


class TemporalSubstrate:
    """Binds the policy config + an ``AgentRuntime`` factory to Temporal activities.

    Register an instance's activities on a Worker for a per-tier task queue:
    ``build_worker(client, substrate, task_queue=...)``. ``runtime_factory`` returns a
    ready ``AgentRuntime`` (e.g. the built-in ``LangGraphAgentRuntime`` bound to a
    ``ModelSource``, or a bring-your-own runtime) — the SAME seam the router uses.
    """

    def __init__(self, routing: RoutingConfig, runtime_factory: Callable[[], Any]):
        self._routing = routing
        self._runtime_factory = runtime_factory

    @activity.defn(name="agentconnect_admit")
    async def admit(self, p: AgentTaskParams) -> bool:
        """Fail-closed tier×class authorization — the single shared rule."""
        return admits(self._routing, p.attested_tier, p.privacy_class)

    @activity.defn(name="agentconnect_execute")
    def execute(self, p: AgentTaskParams) -> dict:
        """Run the task through an ``AgentRuntime`` and return the ``WorkerResult`` as
        a dict. SYNC (``runtime.run`` blocks) so Temporal runs it in the worker's
        ``activity_executor`` thread pool rather than blocking the event loop. Temporal
        re-invokes this on retry; the runtime's own mid-run resumability
        (``checkpoint_root``) makes a re-invocation resume rather than restart."""
        submission = TaskSubmission(
            task=p.task,
            agent_type=p.agent_type,
            constraints=TaskConstraints(privacy_class=p.privacy_class),
        )
        runtime = self._runtime_factory()
        result: WorkerResult = runtime.run(submission, task_id=p.task_id)
        return result.model_dump(mode="json")


def _denied(p: "AgentTaskParams") -> dict:
    return {
        "status": "rejected",
        "summary": f"privacy_tier_denied: tier '{p.attested_tier}' may not handle "
        f"class '{p.privacy_class}'",
        "confidence": 0.0,
        "changed_artifacts": [],
        "evidence_refs": [],
        "risks": ["privacy_tier_denied"],
        "recommended_next_action": None,
        "usage": None,
    }


@workflow.defn
class AgentTaskWorkflow:
    """admit → (deny | execute-with-retry). The whole thing is durable: a worker
    crash mid-execute resumes the workflow and Temporal re-drives the activity under
    its retry policy."""

    @workflow.run
    async def run(self, p: AgentTaskParams) -> dict:
        admitted = await workflow.execute_activity_method(
            TemporalSubstrate.admit,
            p,
            start_to_close_timeout=timedelta(seconds=10),
        )
        if not admitted:
            # No execution — the task never reaches the runtime, so nothing leaks.
            return _denied(p)
        return await workflow.execute_activity_method(
            TemporalSubstrate.execute,
            p,
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=p.max_attempts),
        )


# --- deployment helpers (lazy Temporal-worker imports) ----------------------

def build_worker(
    client: Any,
    substrate: TemporalSubstrate,
    *,
    task_queue: str,
    activity_executor: Optional[Any] = None,
    max_workers: int = 8,
):
    """A Temporal ``Worker`` serving :class:`AgentTaskWorkflow` and ``substrate``'s
    activities on ``task_queue`` (use one queue per trusted tier). Provides a thread
    pool for the sync ``execute`` activity if none is supplied."""
    from concurrent.futures import ThreadPoolExecutor

    from temporalio.worker import Worker

    return Worker(
        client,
        task_queue=task_queue,
        workflows=[AgentTaskWorkflow],
        activities=[substrate.admit, substrate.execute],
        activity_executor=activity_executor or ThreadPoolExecutor(max_workers=max_workers),
    )


async def start_agent_task(
    client: Any, params: AgentTaskParams, *, task_queue: str, workflow_id: str
) -> dict:
    """Start (and await) one agentic task workflow. Returns the WorkerResult dict."""
    return await client.execute_workflow(
        AgentTaskWorkflow.run, params, id=workflow_id, task_queue=task_queue
    )
