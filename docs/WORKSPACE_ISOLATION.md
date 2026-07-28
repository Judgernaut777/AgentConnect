# Workspace isolation

**AgentConnect manages workspace isolation through pluggable enforcement providers; it is not
itself a container or virtualization implementation.**

That sentence is the canonical statement of what this plane does about isolation. It resolves
an older framing in these docs that read as *"AgentConnect is not a sandbox, full stop"* —
which was accurate about the host-shell mode that ships today but wrongly implied AgentConnect
rejects workspace isolation as a concept. It does not. It **owns the workspace lifecycle and
isolation policy**, and **delegates enforcement** to a provider.

> **Status: the model is design direction; the shipped tier is Level 0–1.** Today AgentConnect
> ships managed execution (Levels 0–1 below): managed directories, environment, credentials,
> and tools, with the `agentconnect shell` `--container` seam **designed and deliberately
> unbuilt**. Container and microVM enforcement (Levels 2–3) are the target, not current
> runtime. Per this repo's honesty rule, that is stated here rather than implied by omission.
> This document describes the architecture the plane is converging on; it does not claim
> Level 2–3 enforcement exists.

---

## The split of responsibilities

Isolation is not one component's job. Three authorities meet at the workspace:

| Authority | Owns |
|---|---|
| **AgentConnect** (Work plane) | Workspace **lifecycle** and isolation **policy coordination** — creating, configuring, tracking, and tearing down workspaces; declaring the required isolation level; recording what ran |
| **The isolation provider** (pluggable) | **Enforcement** of filesystem, process, credential, and network boundaries |
| **ToolConnect** (Capability plane) | **Capability authorization** — which tool a principal may call, bound to final arguments ([TOOLCONNECT_CONTRACT.md](TOOLCONNECT_CONTRACT.md)) |

AgentConnect says *what the boundary must be*; the provider *is* the boundary; ToolConnect
decides *which capabilities cross it*. AgentConnect never becomes the container runtime, just
as it never becomes the coding harness or the inference engine.

## Pluggable enforcement providers

A workspace's isolation is enforced by a provider chosen for the required level. Providers may
use established, maintained isolation technologies rather than a bespoke sandbox runtime:

- process and filesystem boundaries
- containers
- Dev Containers
- Podman
- Docker
- microVMs
- remote isolated workers
- third-party sandbox providers

This is the [adapters-over-forks](../README.md) discipline applied to isolation: AgentConnect
defines the narrow interface a workspace needs enforced and implements it against a proven
engine; it does not build a proprietary sandbox runtime.

## Isolation levels

| Level | Enforcement | Status |
|---|---|---|
| **Level 0** | Unmanaged execution | Available (run an agent directly) |
| **Level 1** | Managed directories, environment, credentials, and tools | **Ships today** — the current compliance-and-control layer |
| **Level 2** | Container isolation | **Design direction** — the `--container` seam is designed, unbuilt |
| **Level 3** | Strong isolation via microVMs or isolated remote workers | **Design direction** |

The level that ships today (Level 1) makes AgentConnect the normal path and makes bypasses
visible; it **records what a cooperative agent did**. It does **not** contain a hostile process
— direct SQLite, filesystem, or environment tampering is outside a Level-1 boundary and needs
a Level-2/3 provider (a container, a microVM, a separate user). That is why the shipped
statement "AgentConnect is not a sandbox" is true *of the current tier*: the containment
property arrives with a Level-2/3 enforcement provider, which is design direction.

## Why the older "not a sandbox" wording stays — reframed

The pervasive "not a sandbox" caveats in these docs are kept, because they remain true about
the **Level 0–1** mode that ships. What changes is the frame: they no longer say AgentConnect
*rejects* isolation; they say AgentConnect *is not itself the isolation runtime* and *the tier
that ships today does not yet contain a hostile process*. When a Level-2/3 provider lands, the
containment property it adds will be described where a reader configures it, not smoothed over.

## Relationship to setup

The workspace-isolation choice is made during onboarding — stage 11 of the
[human-guided setup flow](https://github.com/Judgernaut777/Connect/blob/main/docs/SETUP_HUMAN_GUIDED.md)
— and enforced here by the selected provider. Organization-aware setup may require a minimum
isolation level per workspace, project, or department; see
[ORGANIZATION_AWARE_SETUP.md](ORGANIZATION_AWARE_SETUP.md) and
[SETUP_INTEGRATION.md](SETUP_INTEGRATION.md).

## See also

- [BACKPLANE.md](BACKPLANE.md) — the compliance-wrapper contract and the `--container` seam.
- [STATUS.md](STATUS.md) — what ships today, including the deferred isolation work.
- [TOOLCONNECT_CONTRACT.md](TOOLCONNECT_CONTRACT.md) — capability authorization at the boundary.
- [Connect PRODUCT_THESIS.md](https://github.com/Judgernaut777/Connect/blob/main/PRODUCT_THESIS.md) — where the Work plane sits in the ecosystem.
