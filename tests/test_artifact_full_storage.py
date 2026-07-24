"""Regression for REVIEW finding 3: worker output is stored in FULL in shared
memory (no write-side clamp at hard_max_chars), and the tail past the MCP inline
cap is recoverable by paging read_artifact_chunk. Inline reads stay bounded by
mcp_output_policy; only the storage side changed."""

from agentconnect.common.memory import SharedMemory
from agentconnect.common.schemas import TaskConstraints, TaskState, TaskSubmission
from agentconnect.model_manager.residency import ResidencyManager
from agentconnect.router.gateway import GatewayResult
from agentconnect.router.local_client import InProcessLocalClient
from agentconnect.router.service import RouterService


def test_oversized_output_stored_in_full_and_recovered_via_chunks():
    svc = RouterService.create(
        memory=SharedMemory(), local_client=InProcessLocalClient(ResidencyManager())
    )
    hard = int(svc.routing_cfg.mcp_output_policy.get("hard_max_chars", 12000))
    # Well past the inline hard cap; numbered lines so a lost tail is detectable.
    big = "".join(f"line-{i:06d}\n" for i in range(3000))
    assert len(big) > hard

    def fake_call(cfg, gen_req):
        return GatewayResult(
            output_text=big, input_tokens=10, output_tokens=10,
            provider=cfg.provider_id, model=gen_req.model_id,
        )

    svc.gateway.call = fake_call  # type: ignore[method-assign]

    summary = svc.submit_task(
        TaskSubmission(
            task="Produce the full migration log for this private repo.",
            agent_type="patch_worker",
            constraints=TaskConstraints(privacy_class="repo_sensitive"),
        )
    )
    assert summary.status == TaskState.COMPLETE
    artifact_id = summary.artifacts["output"]

    # Stored artifact is the FULL output, not a 12k-truncated copy.
    first = svc.read_artifact_chunk(artifact_id)
    assert first["total_size"] == len(big)
    # Each inline chunk still respects the MCP payload cap...
    assert len(first["content"]) <= hard

    # ...and sequential paging recovers every char, including the tail
    # beyond hard_max_chars that the old write-side clamp used to drop.
    content, offset = "", 0
    while True:
        chunk = svc.read_artifact_chunk(artifact_id, offset, hard)
        content += chunk["content"]
        if chunk["next_offset"] is None:
            break
        offset = chunk["next_offset"]
    assert content == big
