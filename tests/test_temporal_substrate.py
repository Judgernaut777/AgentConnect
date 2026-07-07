"""The OPTIONAL Temporal fork: durable execution of one agentic task, with the
SAME privacy×tier rule as the SQLite queue reused verbatim.

Run against Temporal's in-process time-skipping test server (no external server,
no network beyond the one-time test-server binary). Skipped when the [temporal]
extra is absent.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("temporalio")

from temporalio.testing import WorkflowEnvironment  # noqa: E402

from agentconnect.common.config import load_routing  # noqa: E402
from agentconnect.common.schemas import WorkerResult  # noqa: E402
from agentconnect.temporal import (  # noqa: E402
    AgentTaskParams,
    TemporalSubstrate,
    build_worker,
    start_agent_task,
)


class _CompletingRuntime:
    def run(self, submission, task_id="t"):
        return WorkerResult(
            status="completed", summary=f"ran:{submission.task}",
            confidence=0.9, changed_artifacts=["a.txt"],
        )


async def _run(substrate: TemporalSubstrate, params: AgentTaskParams, task_queue="tq") -> dict:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with build_worker(env.client, substrate, task_queue=task_queue):
            return await start_agent_task(
                env.client, params, task_queue=task_queue, workflow_id=f"wf-{params.task_id}"
            )


# --------------------------------------------------------------------------- #
def test_admitted_task_runs_through_the_runtime():
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return _CompletingRuntime()

    sub = TemporalSubstrate(load_routing(), factory)
    p = AgentTaskParams(
        task="refactor the private module", privacy_class="repo_sensitive",
        attested_tier="local_only", task_id="ok1",
    )
    out = asyncio.run(_run(sub, p))

    assert out["status"] == "completed"
    assert "refactor the private module" in out["summary"]
    assert calls["n"] == 1


def test_denied_tier_never_reaches_the_runtime():
    # external tier + repo_sensitive class -> the admission activity denies it, so the
    # runtime is NEVER constructed or run: privacy is enforced in the Temporal path too.
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return _CompletingRuntime()

    sub = TemporalSubstrate(load_routing(), factory)
    p = AgentTaskParams(
        task="peek at the private module", privacy_class="repo_sensitive",
        attested_tier="external", task_id="deny1",
    )
    out = asyncio.run(_run(sub, p))

    assert out["status"] == "rejected"
    assert "privacy_tier_denied" in out["risks"]
    assert calls["n"] == 0


def test_flaky_execution_is_retried_to_success():
    # Temporal's native retry policy replaces the hand-rolled attempts/max_attempts:
    # a transient activity failure is retried (backoff auto-skipped by the test env).
    attempts = {"n": 0}

    class _FlakyRuntime:
        def run(self, submission, task_id="t"):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient boom")
            return WorkerResult(status="completed", summary="recovered", confidence=0.8)

    sub = TemporalSubstrate(load_routing(), lambda: _FlakyRuntime())
    p = AgentTaskParams(
        task="do it", privacy_class="public", attested_tier="external",
        task_id="retry1", max_attempts=3,
    )
    out = asyncio.run(_run(sub, p))

    assert out["status"] == "completed" and out["summary"] == "recovered"
    assert attempts["n"] == 2  # failed once, retried, succeeded
