# agentconnect-model-manager

The **Local Model Manager** — AgentConnect's local inference control plane, built to
be an appliance. Owns model residency, admission control, and generation, exposed
over a **mutually-authenticated (mTLS)** HTTP API. It knows nothing about global
routing policy, quota, cloud providers, or secrets.

An optional satellite in the Router's ecosystem: the **same** package runs on your
owned GPU box *or* on an ephemeral rented GPU node — the Router reaches both over
the same mTLS transport.

```
pip install agentconnect-model-manager
agentconnect-model-manager                  # serves https://0.0.0.0:8443 (mTLS)
```

See the [repository README](../../README.md) and
[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md).
