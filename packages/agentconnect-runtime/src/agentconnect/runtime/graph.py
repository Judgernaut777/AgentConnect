"""The LangGraph execution graph: act -> tool -> act ... -> finalize.

* ``act``      — send the transcript to the model, parse its reply into an Action.
* ``tool``     — execute the action in the workspace, append an OBSERVATION message.
* ``finalize`` — fold the finish action (or the max-steps cutoff) into result fields.

The graph enforces worker-local policy only (step limit, shell/tests/browser
gates, workspace confinement). Global policy — privacy, budget, provider
selection — stays in the router.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from agentconnect.common.schemas import GenerateRequest

from langgraph.graph import END, START, StateGraph

from .actions import parse_action
from .agent import ModelSource, RuntimeConfig
from .state import RuntimeState
from .tools import fetch_url, list_dir, read_file, run_shell, run_tests, write_file
from .tools.browser import Fetcher, Resolver
from .workspace import Workspace

if TYPE_CHECKING:
    from .memory import MemorySink
    from agentconnect.core.execution_records import ExecutionRecord
    from agentconnect.core.toolconnect_client import ToolGovernor

_log = logging.getLogger(__name__)


def _utc_now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_execrec_id() -> str:
    from agentconnect.core import ids  # local: keeps the import graph shallow

    return ids.new_id(ids.EXECREC)


@dataclass(frozen=True)
class GovernanceLinkage:
    """R6 wiring: the governance context under which this run's tool calls
    execute (ADR-048 vertical slice).

    When bound to ``build_execution_graph``, the act/tool loop redeems the
    carried **Connect-Governance execution grant** at ToolConnect's
    point-of-effect route (``POST /redemptions``, R5) immediately before every
    side-effecting tool call, and emits an **Execution Record** —
    ``sink(record)`` — for every governed call: ``succeeded``/``failed`` after
    execution, ``refused`` when redemption denied or the capability is absent.
    Fail-closed holds end to end: no successful redemption → no execution →
    and (enforced again inside ``build_execution_record``) no "succeeded"
    record.

    The linkage ids (``work_request_id`` / ``decision_record_id`` /
    ``grant_id`` / ``correlation_id``) are read from the SIGNED grant payload
    at emission time — they are the issuer's attestation, not the runtime's
    say-so — with the redemption's echoes as fallback.

    ``sink`` is a plain callable (the same seam idiom as ``memory_sink``):
    tests pass ``list.append``; production wiring passes
    ``ExecutionRecordLedger(storage).record``. Sink failures are logged and
    never mask the tool result — the audit path must not destroy work that
    already ran, exactly like ToolConnect's best-effort outcome recording.
    """

    grant: Mapping[str, Any]
    sink: Callable[["ExecutionRecord"], None]
    executor_id: str = "agentconnect-runtime"
    executor_kind: str = "worker"
    harness: str = "agentconnect-runtime"
    subtask_id: Optional[str] = None
    #: Instant presented to the provider for the validity window (RFC 3339).
    #: None → the provider judges against its own request time. Tests pin it.
    at: Optional[str] = None
    #: Wall-clock seam for the record's started/finished timestamps; the runtime
    #: is not a purity-bound component, so the default is the real UTC clock.
    clock: Callable[[], str] = _utc_now_rfc3339
    #: Record-id minter (uuid-backed by default; tests pin a counter).
    record_id_factory: Callable[[], str] = _new_execrec_id



def build_execution_graph(
    config: RuntimeConfig,
    model_source: ModelSource,
    workspace: Workspace,
    *,
    fetcher: Fetcher | None = None,
    url_resolver: Resolver | None = None,
    memory_sink: "MemorySink | None" = None,
    provenance: dict | None = None,
    checkpointer: Any = None,
    tool_governor: "ToolGovernor | None" = None,
    governed_principal: Optional[Mapping[str, Any]] = None,
    governed_source_id: str = "agentconnect-runtime",
    governance: Optional[GovernanceLinkage] = None,
) -> Any:
    """Build and compile the worker graph bound to one workspace. When a
    ``checkpointer`` is supplied (a LangGraph ``BaseCheckpointSaver``), the graph
    persists state after each super-step so a crashed run can resume from the pending
    node; without one it runs in-memory (the default ephemeral behavior).

    ``tool_governor`` is the final-invocation-boundary enforcement seam (ADR 0009):
    when bound, every side-effecting tool call in ``run_tool`` below is authorized
    AND redeemed against its exact final arguments immediately before it executes —
    not the model's *declared* tool set (that's the cheap early gate the router/core
    layer already does before a worker even spawns), but the literal args about to
    run. ``None`` (the default) preserves today's ungoverned behavior byte-for-byte;
    every existing runtime test passes with no fixture changes required.

    ``governance`` (R6) layers the ADR-048 slice on top of that seam: when a
    :class:`GovernanceLinkage` is bound, the final-boundary check becomes a
    governance-grant redemption (ToolConnect ``POST /redemptions``) instead of
    the contract-1.1 authorize+redeem pair, and every governed call leaves an
    Execution Record on the linkage's sink. The two paths never mix on one
    call: a governance-linked run executes under the signed grant or not at
    all."""

    def act(state: RuntimeState) -> dict[str, Any]:
        req = GenerateRequest(
            request_id=f"req_{state['task_id']}_{state['iteration']}",
            task_id=state["task_id"],
            model_id=config.model_id,
            messages=state["messages"],
            max_output_tokens=config.max_output_tokens,
            temperature=config.temperature,
        )
        resp = model_source.generate(req)
        action = parse_action(resp.output_text)
        return {
            "messages": state["messages"] + [{"role": "assistant", "content": resp.output_text}],
            "last_action": {"kind": action.kind, "args": action.args, "freeform": action.freeform},
            # Accumulate token usage across steps (last-value-wins channel, so add
            # to the running total). Surfaced as WorkerResult.usage for metering.
            "input_tokens": state.get("input_tokens", 0) + resp.input_tokens,
            "output_tokens": state.get("output_tokens", 0) + resp.output_tokens,
            "model_id": resp.model_id or config.model_id,
        }

    def emit_execution_record(
        linkage: GovernanceLinkage, state: RuntimeState, tool_name: str,
        *, outcome: str, redemption: Any = None, refusal_reason: Optional[str] = None,
        started_at: str = "", finished_at: str = "",
    ) -> None:
        """Build and sink one Execution Record. Never raises: a broken audit
        sink must not crash a run whose tool already executed (or whose
        refusal is itself the evidence)."""
        from agentconnect.core.execution_records import (
            ExecutorIdentity, ProviderEnforcementRef, ToolIdentity,
            build_execution_record,
        )

        payload = linkage.grant.get("payload") if isinstance(linkage.grant, Mapping) else None
        payload = payload if isinstance(payload, Mapping) else {}

        def _link(field_name: str) -> str:
            echoed = str(getattr(redemption, field_name, "") or "")
            return echoed or str(payload.get(field_name) or "")

        reason = str(getattr(redemption, "reason", "") or "")
        enforcement = ProviderEnforcementRef(
            provider_id=str(payload.get("provider_id") or ""),
            grant_id=_link("grant_id"),
            redemption_outcome=("redeemed" if getattr(redemption, "redeemed", False)
                                else f"denied:{reason or 'unavailable'}"),
            # What the Harness positively knows: a redemption that succeeded
            # passed verification; a denial's `verified` nuance lives in the
            # provider's own audit record (reachable via grant_id).
            verified=bool(getattr(redemption, "redeemed", False)),
            enforced_at=str(linkage.at or ""),
        )
        try:
            record = build_execution_record(
                execution_record_id=linkage.record_id_factory(),
                work_request_id=str(payload.get("work_request_id") or ""),
                task_id=state["task_id"],
                subtask_id=linkage.subtask_id,
                decision_record_id=_link("decision_record_id"),
                grant_id=_link("grant_id"),
                correlation_id=_link("correlation_id"),
                provider_enforcement=enforcement,
                executor=ExecutorIdentity(
                    executor_id=linkage.executor_id,
                    executor_kind=linkage.executor_kind,
                    harness=linkage.harness,
                ),
                tool=ToolIdentity(source_id=governed_source_id, name=tool_name),
                outcome=outcome,
                refusal_reason=refusal_reason,
                started_at=started_at,
                finished_at=finished_at,
            )
            linkage.sink(record)
        except Exception:  # noqa: BLE001 — the audit path never masks execution
            _log.exception("execution-record emission failed for %s", tool_name)

    def governed(state: RuntimeState, tool_name: str, final_args: Mapping[str, Any],
                 execute: Callable[[Mapping[str, Any]], str]) -> str:
        """Final-invocation-boundary gate (ADR 0009): authorize the EXACT final
        args, redeem the one-use grant, THEN execute — immediately before, for
        every side-effecting tool call.

        No governor bound => today's ungoverned behavior, unchanged (the common
        case; the pre-spawn declared-tool-set check at dispatch time remains the
        only gate). A bound governor is all-or-nothing: any missing grant/redeem
        capability, any deny, any redemption failure, or any raised exception
        refuses — there is deliberately no decision-only downgrade, since that
        would recreate the exact enforcement gap this exists to close. A refusal
        is a soft in-loop stop: an ERROR observation fed back to the model, the
        same idiom the static allow_* gates already use.
        """
        if tool_governor is None:
            if governance is not None:
                # Fail closed: a governance-linked run with NO governor must not
                # silently degrade into ungoverned execution — that would be the
                # enforcement gap the grant exists to close. Refuse, and record
                # the refusal (never a success record without a redemption).
                emit_execution_record(
                    governance, state, tool_name, outcome="refused",
                    refusal_reason="no governor bound for governance-linked run")
                return (f"ERROR: {tool_name} requires a governance-grant redemption "
                        "but no governor is bound; action refused.")
            return execute(dict(final_args))
        principal = dict(governed_principal or {
            "id": "agentconnect-runtime", "kind": "agent", "privacy_tier": "local",
        })
        context = {"task_id": state["task_id"], "iteration": state["iteration"]}
        # The ONE mapping that is hashed, redeemed, AND executed: `execute` receives
        # this frozen copy rather than re-reading the model's mutable action dict, so
        # nothing that mutates the original between the two blocking governor
        # round-trips and the actual call can desynchronize what was authorized from
        # what runs (TOCTOU mitigation M5, mirroring ToolConnect's governed_invoke).
        frozen_args = dict(final_args)
        if governance is not None:
            # R6 path: the signed Connect-Governance grant IS the authorization
            # — redeem it at the point of effect, then execute, then record.
            # No 1.1 authorize call happens on this path; a governance-linked
            # run does not get a second, weaker gate substituted for the grant.
            redeem_gov = getattr(tool_governor, "redeem_governance_grant", None)
            if not callable(redeem_gov):
                emit_execution_record(
                    governance, state, tool_name, outcome="refused",
                    refusal_reason="governor cannot redeem governance grants")
                return (f"ERROR: {tool_name} requires a governance-grant redemption "
                        "but the bound governor cannot redeem one; action refused.")
            started = governance.clock()
            try:
                redemption = redeem_gov(
                    governance.grant, principal, governed_source_id, tool_name,
                    frozen_args, at=governance.at)
            except Exception as exc:  # noqa: BLE001 — a raising governor is an outage
                _log.warning("governor raised redeeming governance grant for %s: %s",
                             tool_name, exc)
                emit_execution_record(
                    governance, state, tool_name, outcome="refused",
                    refusal_reason=f"governor raised: {exc}",
                    started_at=started, finished_at=governance.clock())
                return (f"ERROR: {tool_name} governance redemption unavailable; "
                        f"action refused ({exc}).")
            # Identity-echo check, same doctrine as the 1.1 path: the echoed
            # stored identity must name the tool about to run.
            echoed_sid = str(getattr(redemption, "source_id", "") or "")
            echoed_name = str(getattr(redemption, "name", "") or "")
            identity_mismatch = (
                (echoed_sid and echoed_sid != governed_source_id)
                or (echoed_name and echoed_name != tool_name))
            if not getattr(redemption, "redeemed", False) or identity_mismatch:
                why = ("redeemed grant is for "
                       f"{echoed_sid}:{echoed_name}, expected "
                       f"{governed_source_id}:{tool_name}" if identity_mismatch
                       else str(getattr(redemption, "reason", "") or "unknown"))
                emit_execution_record(
                    governance, state, tool_name, outcome="refused",
                    redemption=redemption, refusal_reason=why,
                    started_at=started, finished_at=governance.clock())
                return f"ERROR: {tool_name} governance grant not redeemed ({why}); action refused."
            obs = execute(frozen_args)
            emit_execution_record(
                governance, state, tool_name,
                outcome="failed" if obs.startswith("ERROR:") else "succeeded",
                redemption=redemption,
                started_at=started, finished_at=governance.clock())
            return obs
        try:
            decision = tool_governor.authorize(
                principal, governed_source_id, tool_name, context, args=frozen_args)
        except Exception as exc:  # noqa: BLE001 — a raising governor is an outage, not an allow
            _log.warning("tool governor raised authorizing %s: %s", tool_name, exc)
            return f"ERROR: tool governor unavailable for {tool_name}; action refused ({exc})."
        if not decision.allowed:
            return f"ERROR: {tool_name} denied by policy: {decision.reason}"
        grant = getattr(decision, "grant", None)
        redeem = getattr(tool_governor, "redeem", None)
        if grant is None or not callable(redeem):
            return (f"ERROR: {tool_name} allowed but no argument-bound grant available; "
                    "refusing ungoverned execution.")
        try:
            redemption = redeem(grant.grant_id, principal, frozen_args)
        except Exception as exc:  # noqa: BLE001 — same outage posture as authorize
            _log.warning("tool governor raised redeeming %s: %s", tool_name, exc)
            return f"ERROR: {tool_name} grant redemption failed; action refused ({exc})."
        if not getattr(redemption, "redeemed", False):
            return (f"ERROR: {tool_name} grant not redeemed "
                    f"({getattr(redemption, 'reason', 'unknown')}); action refused.")
        # Identity-echo check (defense-in-depth, mirroring ToolConnect's
        # governed_invoke): the redeem response echoes the STORED grant identity —
        # if it names a different tool/source than the one about to execute, some
        # layer redeemed the wrong grant (collision, tracking bug, compromised
        # server). Refuse rather than execute on a mismatched redemption.
        echoed_sid = str(getattr(redemption, "source_id", "") or "")
        echoed_name = str(getattr(redemption, "name", "") or "")
        if (echoed_sid and echoed_sid != governed_source_id) or (
                echoed_name and echoed_name != tool_name):
            return (f"ERROR: {tool_name} redeemed grant is for "
                    f"{echoed_sid}:{echoed_name}, expected "
                    f"{governed_source_id}:{tool_name}; action refused.")
        obs = execute(frozen_args)
        if getattr(decision, "decision_id", ""):
            try:  # best-effort loop closure; the audit trail never gates execution
                tool_governor.record(
                    decision.decision_id,
                    "error" if obs.startswith("ERROR:") else "executed",
                    {"grant_id": grant.grant_id, "tool": tool_name},
                    grant_id=grant.grant_id,
                )
            except Exception:  # noqa: BLE001
                pass
        return obs

    def run_tool(state: RuntimeState) -> dict[str, Any]:
        action = state["last_action"] or {}
        kind, args = action.get("kind"), action.get("args", {})
        evidence = state["evidence_refs"]
        subtasks = state.get("subtasks", [])
        # Every execute closure below takes the frozen mapping `governed()` hashed and
        # redeemed and reads its values from THAT — never re-indexing the model's
        # mutable `args` dict after the governor round-trips (TOCTOU mitigation M5).
        if kind == "read_file":
            obs = governed(state, "read_file", {"path": args["path"]}, lambda a: read_file(
                workspace, a["path"], max_chars=config.observation_max_chars))
            if not obs.startswith("ERROR:"):
                evidence = evidence + [f"read_file:{args['path']}"]
        elif kind == "write_file":
            obs = governed(state, "write_file", {"path": args["path"], "content": args["content"]},
                           lambda a: write_file(workspace, a["path"], a["content"]))
        elif kind == "list_dir":
            obs = governed(state, "list_dir", {"path": args.get("path", ".")},
                           lambda a: list_dir(workspace, a["path"]))
        elif kind == "shell":
            if config.allow_shell:
                obs = governed(state, "shell", {"command": args["command"]}, lambda a: run_shell(
                    workspace, a["command"], timeout=config.shell_timeout_seconds))
                if not obs.startswith("ERROR:"):
                    evidence = evidence + [f"shell:{args['command'][:120]}"]
            else:
                obs = "ERROR: the shell action is disabled for this task."
        elif kind == "run_tests":
            # args are deliberately ignored: the command is operator config,
            # never model input. But run_tests still executes workspace code —
            # `pytest` imports every test_*.py under the root and write_file is
            # ungated, so the model can drop a test file whose module-level code
            # runs on import. With no OS sandbox on this worker, allow_shell is
            # the only isolation boundary; run_tests is an equivalent
            # code-execution primitive and must honour it, or allow_shell=False
            # is silently defeated. The governed final_args bind the OPERATOR's
            # command (never the model's ignored args) — that's the value about
            # to actually execute.
            if config.allow_tests and config.allow_shell:
                obs = governed(state, "run_tests", {"command": config.test_command},
                               lambda a: run_tests(workspace, a["command"],
                                                   timeout=config.test_timeout_seconds))
                if not obs.startswith("ERROR:"):
                    evidence = evidence + [f"run_tests:{config.test_command[:120]}"]
            else:
                obs = "ERROR: the run_tests action is disabled for this task."
        elif kind == "fetch_url":
            if config.allow_browser:
                obs = governed(state, "fetch_url", {"url": args["url"]}, lambda a: fetch_url(
                    a["url"],
                    timeout=config.browser_timeout_seconds,
                    max_bytes=config.browser_max_response_bytes,
                    max_redirects=config.browser_max_redirects,
                    fetcher=fetcher,
                    resolver=url_resolver,
                ))
                if not obs.startswith("ERROR:"):
                    evidence = evidence + [f"fetch_url:{args['url'][:120]}"]
            else:
                obs = "ERROR: the browser action is disabled for this task."
        elif kind == "remember":
            # Write-only durable memory. Gated on allow_memory AND an injected sink;
            # the worker can never read memory back (there is no recall action).
            if config.allow_memory and memory_sink is not None:
                prov = {**(provenance or {}), "task_id": state["task_id"]}
                obs = governed(state, "remember", {"text": args["text"]}, lambda a: memory_sink.capture(
                    a["text"], provenance=prov))
                if not obs.startswith("ERROR:"):
                    evidence = evidence + [f"remember:{args['text'][:120]}"]
            else:
                obs = "ERROR: the remember action is disabled for this task."
        elif kind == "delegate":
            # Not gated through the tool governor here: delegate executes nothing
            # external itself, it only records a sub-task for the router to run as
            # a child — that child subtask hits the pre-spawn declared-set gate
            # (site #2) through the router like any other dispatch. Omitted by
            # deliberate decision, not oversight.
            # Hierarchical decomposition (Track 4): record a sub-task for the router
            # to run as a child. Bounded — disabled past the depth limit and capped
            # per run — so recursion cannot run away. The worker never waits here; it
            # keeps its own context small and gets a synthesized summary from the router.
            if not config.allow_delegation:
                obs = "ERROR: the delegate action is disabled for this task."
            elif config.delegation_depth >= config.max_delegation_depth:
                obs = (
                    f"ERROR: delegation depth limit ({config.max_delegation_depth}) reached "
                    "— do this work directly instead of delegating."
                )
            elif len(subtasks) >= config.max_subtasks:
                obs = (
                    f"ERROR: subtask limit ({config.max_subtasks}) reached — finish with the "
                    "sub-tasks already delegated."
                )
            else:
                at, pc = args.get("agent_type"), args.get("privacy_class")
                subtasks = subtasks + [{
                    "task": args["task"],
                    "agent_type": at if isinstance(at, str) and at else None,
                    "privacy_class": pc if isinstance(pc, str) and pc else None,
                }]
                obs = (
                    f"Recorded sub-task #{len(subtasks)} for delegation (the router runs it as a "
                    "child and hands you back a synthesized summary). Continue or finish."
                )
                evidence = evidence + [f"delegate:{args['task'][:120]}"]
        else:  # "invalid"
            obs = f"ERROR: {args.get('error', 'invalid action')} — reply with one valid JSON action."
        if len(obs) > config.observation_max_chars:
            obs = obs[: config.observation_max_chars] + "\n[observation truncated]"
        return {
            "messages": state["messages"] + [{"role": "user", "content": f"OBSERVATION:\n{obs}"}],
            "iteration": state["iteration"] + 1,
            "changed_artifacts": list(workspace.changed_files),
            "evidence_refs": evidence,
            "subtasks": subtasks,
        }

    def finalize(state: RuntimeState) -> dict[str, Any]:
        action = state.get("last_action") or {}
        args = action.get("args", {})
        if action.get("kind") == "finish":
            # The finish payload is model output: coerce every field rather than
            # crash the run on a shape deviation (string risks, list next-action,
            # numeric-string confidence, ...).
            try:
                confidence = min(max(float(args.get("confidence", 0.0)), 0.0), 1.0)
            except (TypeError, ValueError):
                confidence = 0.0
            raw_risks = args.get("risks") or []
            if isinstance(raw_risks, str):
                raw_risks = [raw_risks]
            elif not isinstance(raw_risks, (list, tuple)):
                raw_risks = [raw_risks]
            next_action = args.get("recommended_next_action")
            return {
                "done": True,
                "status": "completed",
                "summary": str(args.get("summary", "")),
                "confidence": confidence,
                "risks": state["risks"] + [str(r) for r in raw_risks if r],
                "recommended_next_action": str(next_action) if next_action is not None else None,
                "changed_artifacts": list(workspace.changed_files),
            }
        return {
            "done": False,
            "status": "incomplete",
            "summary": f"Stopped after {state['iteration']} steps without a finish action.",
            "confidence": 0.0,
            "risks": state["risks"] + ["max_steps_reached_before_finish"],
            "recommended_next_action": "Retry with a higher step limit or a narrower task.",
            "changed_artifacts": list(workspace.changed_files),
        }

    def route_after_act(state: RuntimeState) -> str:
        return "finalize" if (state["last_action"] or {}).get("kind") == "finish" else "tool"

    def route_after_tool(state: RuntimeState) -> str:
        return "finalize" if state["iteration"] >= config.max_steps else "act"

    graph = StateGraph(RuntimeState)
    graph.add_node("act", act)
    graph.add_node("tool", run_tool)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "act")
    graph.add_conditional_edges("act", route_after_act, {"tool": "tool", "finalize": "finalize"})
    graph.add_conditional_edges("tool", route_after_tool, {"act": "act", "finalize": "finalize"})
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)
