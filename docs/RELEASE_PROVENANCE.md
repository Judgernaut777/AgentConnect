# Release-artifact provenance

AgentConnect ships as nine independently-installable wheels built from this
workspace. This document records how release artifacts are built, how their
integrity is verified, and the hash manifest for a build.

## Build (reproducible)

Wheels are built with `uv build` (no network, no `pip` needed). For byte-stable
output pin the timestamp:

```bash
export SOURCE_DATE_EPOCH=$(git -C /home/mini/mcp-agentconnect log -1 --format=%ct)
OUT=dist
rm -rf "$OUT"; mkdir -p "$OUT"
for pkg in agentconnect-core agentconnect-router agentconnect-model-manager \
           agentconnect-api agentconnect-cli agentconnect-mcp \
           agentconnect-runtime agentconnect-linear agentconnect-temporal; do
  uv build --wheel "packages/$pkg" -o "$OUT"
done
```

## Clean-install verification

Every release build is smoke-tested by installing the wheels into a *fresh* venv
(no source on path) and importing + running the CLI:

```bash
uv venv /tmp/ac-clean
uv pip install --python /tmp/ac-clean/bin/python \
  dist/agentconnect_core-0.1.0-py3-none-any.whl \
  dist/agentconnect_cli-0.1.0-py3-none-any.whl
/tmp/ac-clean/bin/python -c "import agentconnect.core, agentconnect.cli.main; \
  from agentconnect.cli.main import build_parser; build_parser().parse_args(['metrics'])"
```

This was run for the manifest below and passed (core + cli import clean, the CLI
parses, including the new `metrics`/`ready`/`backup`/`restore`/`sessions
reconcile` commands).

## Hash manifest

`sha256`, `agentconnect *==0.1.0`, built from the source tree at the recorded
commit. Regenerate + verify with `sha256sum -c` after a rebuild.

| Wheel | sha256 |
|-------|--------|
| agentconnect_core-0.1.0-py3-none-any.whl | 04d4aaf2c6f49d597e4cad5cb113688cf3893b82cb42f48817f85139fd9f8790 |
| agentconnect_router-0.1.0-py3-none-any.whl | bd0adacdacd2db62ce85e9314f4092db3507619831bb4e58c53b242b3b73d60f |
| agentconnect_model_manager-0.1.0-py3-none-any.whl | 74782ff87b4b29d712c4aa8e24565d9fd73391cb465f86c643c357ae76f0c0c0 |
| agentconnect_api-0.1.0-py3-none-any.whl | cac47a2e20c46207ced815df75f628dc15d15956f244e68615187b602889a97b |
| agentconnect_cli-0.1.0-py3-none-any.whl | cffe578458ae3b8cce48b51eb64f699eb21bcb10c54585f9eed12193d488e7dc |
| agentconnect_mcp-0.1.0-py3-none-any.whl | 24d5ea84bf043d6cfd8de7508c566780d557c5aaff7a3fe9800927bdb8a2beb6 |
| agentconnect_runtime-0.1.0-py3-none-any.whl | 8e8bb1c9bd774699c2c568fc87ac10d01f6f4e88f7d6cfef76a412754e605ab0 |
| agentconnect_linear-0.1.0-py3-none-any.whl | 82fb41422c886022dbdc0a32dd09dce9c821fcea31017f3cb6ddf5f985e6045c |
| agentconnect_temporal-0.1.0-py3-none-any.whl | 037ebf363d640d22ff5d636be2ba1e20a7fdc71e2c973f732c9c50c16f079392 |

> The hashes above are for a build of the tree at
> `4313c1f` (stage-1 head). Wheel bytes depend on the source tree and
> `SOURCE_DATE_EPOCH`; after any source change rebuild and refresh this manifest
> (the exact regeneration command is in the "Build" section). Treat the manifest
> as the integrity record for a specific release build, not a cross-commit
> constant.

## Provenance notes

- Source of truth for versions: each package's `pyproject.toml` (`0.1.0`).
- License: Apache-2.0 (repo `LICENSE`).
- No compiled extensions — all wheels are `py3-none-any`, so the same artifact
  installs on aarch64 and x86_64.
