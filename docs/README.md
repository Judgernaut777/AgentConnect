# AgentConnect docs

AgentConnect is the **Work plane** of the [Connect ecosystem](https://github.com/Judgernaut777/Connect):
it owns tasks, assignments, delegation, attempts, execution records, artifacts, review, worker
and harness coordination, workspace lifecycle, budget enforcement against work, and
organizational workflow.

Read [STATUS.md](STATUS.md) before trusting any capability described elsewhere — it is the
authority on what ships today.

## Start here

| Document | Read it for |
|---|---|
| [../README.md](../README.md) | Identity, the managed-agent loop, install |
| [OPERATOR_GUIDE.md](OPERATOR_GUIDE.md) | Walking the loop end to end |
| [STATUS.md](STATUS.md) | What is true today; deferred work; open defects |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Design notes and the phase/section map |

## Model & policy (target — design direction)

These describe where the Work plane is going. Each marks what ships today vs. what is design
direction in its opening lines.

| Document | Read it for |
|---|---|
| [BUDGET_MODEL.md](BUDGET_MODEL.md) | The generalized budget model: arbitrary amounts, intervals, overlap, scopes, delegation (shipped budget is a single global cap) |
| [WORKSPACE_ISOLATION.md](WORKSPACE_ISOLATION.md) | Workspace isolation via pluggable enforcement providers; Levels 0–3 (0–1 ship today) |
| [ORGANIZATION_AWARE_SETUP.md](ORGANIZATION_AWARE_SETUP.md) | The Work plane's part in organization-aware onboarding |
| [SETUP_INTEGRATION.md](SETUP_INTEGRATION.md) | How the Connect control plane configures the Work plane during setup |
| [MULTI_HARNESS.md](MULTI_HARNESS.md) | Interchangeable manager harnesses — coordinate, never replace |
| [HIERARCHICAL_DELEGATION.md](HIERARCHICAL_DELEGATION.md) | Delegation primitives |

## Contracts & integrations

| Document | Read it for |
|---|---|
| [TOOLCONNECT_CONTRACT.md](TOOLCONNECT_CONTRACT.md) | Capability authorization at the final invocation boundary |
| [COMPUTECONNECT_CONTRACT.md](COMPUTECONNECT_CONTRACT.md) | Local-compute provider integration |
| [EVENT_BUS.md](EVENT_BUS.md) | The cross-product event bus (projection, never a system of record) |
| [MEMORY_FEDERATION.md](MEMORY_FEDERATION.md) | Optional trusted-memory integration with BrainConnect |

## Deeper reference

Agent runtime ([AGENT_RUNTIME.md](AGENT_RUNTIME.md)), model adapters
([MODEL_ADAPTERS.md](MODEL_ADAPTERS.md)), remote dispatch ([REMOTE_DISPATCH.md](REMOTE_DISPATCH.md)),
safety ([SAFETY.md](SAFETY.md)), work queue ([WORK_QUEUE.md](WORK_QUEUE.md)), backplane
([BACKPLANE.md](BACKPLANE.md) and the `BACKPLANE_SPEC*` set), operations
([OPERATIONS.md](OPERATIONS.md)), troubleshooting ([TROUBLESHOOTING.md](TROUBLESHOOTING.md)),
and the decision records under [adr/](adr/).
