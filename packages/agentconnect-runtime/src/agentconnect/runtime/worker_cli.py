"""``agentconnect-worker`` — run a compute-contributor worker against a broker.

Launches a :class:`PullWorker` loop that claims work this worker's trust tier is
authorized for, runs it on THIS box's model, and reports the result back. The
broker never executes; the compute happens here. See docs/WORK_QUEUE.md.

Model (what actually runs the task):
  ``--dry-run``  a built-in echo model, no model server required — use it to
                 smoke-test broker connectivity + mTLS + the claim/report flow
                 before wiring a real model.
  otherwise      an OpenAI-compatible server (vLLM/llama.cpp/Ollama/…) via the
                 model-manager's ``backend_from_env`` (``MODEL_BACKEND_URL`` /
                 ``MODEL_BACKEND_API_KEY`` / ``MODEL_BACKEND_MODELS``); needs the
                 ``[worker]`` extra (agentconnect-model-manager).

Identity — and therefore which privacy classes this worker may claim — is the
mTLS client certificate (``--ca``/``--cert``/``--key``); ``--insecure-localhost``
is loopback-only dev. Run examples:

    agentconnect-worker --broker https://broker:8443 --dry-run --insecure-localhost
    agentconnect-worker --broker https://broker:8443 --capabilities coding,summarization \\
        --ca ca.pem --cert worker.pem --key worker.key
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Optional, Sequence

from agentconnect.common.schemas import GenerateRequest, GenerateResponse

from .agent import LangGraphAgentRuntime, RuntimeConfig
from .pull_worker import PullWorker


class _EchoModelSource:
    """Dependency-free model that finishes immediately, echoing the task. Used by
    ``--dry-run`` to prove claim → run → report works end to end without a model
    server or the model-manager package installed."""

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        last = ""
        for m in reversed(req.messages):
            if m.get("role") == "user":
                last = str(m.get("content", ""))
                break
        return GenerateResponse(
            request_id=req.request_id,
            model_id=req.model_id,
            output_text=f"[dry-run worker] received: {last}",
        )


def _tls_from_args(args: argparse.Namespace) -> Optional[Any]:
    from agentconnect.common.config import TlsClientConfig

    if args.insecure_localhost:
        return TlsClientConfig(mode="insecure_localhost")
    if args.ca or args.cert or args.key:
        return TlsClientConfig(
            mode="mutual", ca_cert=args.ca, client_cert=args.cert,
            client_key=args.key, server_name=args.server_name,
        )
    return None  # no TLS material — plain HTTP (loopback only)


def _model_source(args: argparse.Namespace) -> Any:
    if args.dry_run:
        return _EchoModelSource()
    try:
        from agentconnect.model_manager.backends import backend_from_env
    except ImportError as exc:
        raise SystemExit(
            f"A real model needs the [worker] extra (agentconnect-model-manager): {exc}. "
            "Configure MODEL_BACKEND_URL, or pass --dry-run to smoke-test connectivity."
        )
    return backend_from_env()


def build_worker(args: argparse.Namespace) -> PullWorker:
    """Construct a configured PullWorker from parsed args, without connecting."""
    runtime = LangGraphAgentRuntime(
        _model_source(args),
        RuntimeConfig(
            model_id=args.model_id, allow_shell=args.shell, allow_browser=args.browser
        ),
    )
    caps = [c.strip() for c in (args.capabilities or "").split(",") if c.strip()]
    # In real deployment identity is the mTLS client cert and no header is sent.
    # --identity sets X-Client-Cert-DN for the header-stripping-proxy topology
    # (broker started with trust_proxy_headers=True) and for local dev over
    # --insecure-localhost, where there is no cert to carry identity.
    identity_headers = {"X-Client-Cert-DN": args.identity} if args.identity else None
    return PullWorker(
        runtime,
        base_url=args.broker,
        tls=_tls_from_args(args),
        capabilities=caps,
        identity_headers=identity_headers,
        poll_interval=args.poll_interval,
        heartbeat_interval=args.heartbeat_interval,
    )


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="agentconnect-worker",
        description="Contribute compute to an AgentConnect broker's federated work-queue.",
    )
    p.add_argument("--broker", required=True, help="Broker base URL, e.g. https://broker:8443")
    p.add_argument("--capabilities", default="", help="Comma-separated skills this worker offers")
    p.add_argument("--model-id", default=RuntimeConfig().model_id, help="Model id sent to the backend")
    p.add_argument("--dry-run", action="store_true", help="Built-in echo model (connectivity smoke test)")
    p.add_argument("--shell", action=argparse.BooleanOptionalAction, default=True,
                   help="Allow the shell tool in this worker's runtime (default on)")
    p.add_argument("--browser", action=argparse.BooleanOptionalAction, default=False,
                   help="Allow the browser tool (default off)")
    # mTLS identity (identity == authorization on the pull surface).
    p.add_argument("--ca", help="CA cert to verify the broker")
    p.add_argument("--cert", help="This worker's client certificate")
    p.add_argument("--key", help="This worker's client private key")
    p.add_argument("--server-name", help="Expected broker server name (SNI/verification)")
    p.add_argument("--insecure-localhost", action="store_true", help="Plain HTTP loopback (dev only)")
    p.add_argument("--identity", help="Send X-Client-Cert-DN identity header (proxy-terminated "
                   "TLS or local dev; broker must run with trust_proxy_headers=True)")
    # Loop control.
    p.add_argument("--poll-interval", type=float, default=2.0, help="Seconds to wait when idle")
    p.add_argument("--heartbeat-interval", type=float, default=30.0,
                   help="Seconds between lease renewals while a task runs (0 disables)")
    p.add_argument("--once", action="store_true", help="Process at most one ticket, then exit")
    p.add_argument("--max-iterations", type=int, default=0, help="Claim attempts before exit (0 = forever)")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    worker = build_worker(args)
    try:
        if args.once:
            outcome = worker.run_once()
            print("processed 1 ticket" if outcome else "no work available")
        else:
            n = worker.run_forever(max_iterations=(args.max_iterations or None))
            print(f"processed {n} ticket(s)")
    except KeyboardInterrupt:
        print("worker stopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
