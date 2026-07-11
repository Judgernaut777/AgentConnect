"""Clean-install packaging guarantees (found during the 0.1.0 release verification).

An installed `agentconnect-router` used to die at startup with
`FileNotFoundError: config/providers.yaml`: the wheel shipped no config, and the
upward search only ever found a source checkout. The core wheel now packages a
default config — EMPTY provider/profile registries plus the fail-closed routing
policy — and `_discover_config_dir` falls back to it when no checkout config exists.

The empty registries are the point: a clean install must be able to *start*, and
must not believe it has inference providers nobody gave it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from agentconnect.common import config as cfg


def test_packaged_default_config_ships_all_three_files():
    for name in ("providers.yaml", "profiles.yaml", "routing.yaml"):
        assert (cfg.PACKAGED_CONFIG_DIR / name).is_file(), f"missing packaged {name}"


def test_packaged_providers_registry_is_empty():
    data = yaml.safe_load((cfg.PACKAGED_CONFIG_DIR / "providers.yaml").read_text())
    assert data["policy_version"] == "packaged-default"
    assert data["providers"] == {}
    profiles = yaml.safe_load((cfg.PACKAGED_CONFIG_DIR / "profiles.yaml").read_text())
    assert profiles["profiles"] == {}


def test_packaged_routing_policy_matches_repo_policy():
    """The packaged routing.yaml is a copy of the repo policy — same parsed content.

    Compared as parsed YAML so the packaged header comment doesn't matter, and so
    a future edit to config/routing.yaml that forgets the packaged copy fails here
    instead of shipping a silently divergent default.
    """
    repo_root = Path(__file__).resolve().parents[1]
    repo = yaml.safe_load((repo_root / "config" / "routing.yaml").read_text())
    packaged = yaml.safe_load((cfg.PACKAGED_CONFIG_DIR / "routing.yaml").read_text())
    assert packaged == repo


def test_discover_falls_back_to_packaged_config(tmp_path, monkeypatch):
    """No env var, no config/ anywhere above cwd or the module: packaged wins."""
    monkeypatch.delenv("AGENTCONNECT_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    # The upward search from the module file would find the repo's config/ in a
    # source checkout; point it elsewhere to simulate an installed layout.
    monkeypatch.setattr(cfg, "__file__", str(tmp_path / "site-packages" / "config.py"))
    assert cfg._discover_config_dir() == cfg.PACKAGED_CONFIG_DIR


def test_discover_env_override_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCONNECT_CONFIG_DIR", str(tmp_path))
    assert cfg._discover_config_dir() == tmp_path


def test_router_service_starts_on_packaged_config():
    """`RouterService.create()` builds against the packaged (empty) config.

    Run in a subprocess because `CONFIG_DIR` is resolved at import time and
    `load_all` is cached — the running test process has already bound the repo
    config. This is the same path `agentconnect-router` takes at startup.
    """
    code = (
        "from agentconnect.router.service import RouterService\n"
        "svc = RouterService.create()\n"
        "assert svc is not None\n"
        "print('router-ok')\n"
    )
    repo_root = Path(__file__).resolve().parents[1]
    pythonpath = ":".join(
        str(repo_root / "packages" / p / "src")
        for p in ("agentconnect-core", "agentconnect-router",
                  "agentconnect-model-manager", "agentconnect-runtime")
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=120,
        env={
            "PYTHONPATH": pythonpath,
            "AGENTCONNECT_CONFIG_DIR": str(cfg.PACKAGED_CONFIG_DIR),
            "PATH": "/usr/bin:/bin",
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert "router-ok" in proc.stdout
