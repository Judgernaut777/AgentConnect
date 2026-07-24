"""Compute config surface (ComputeConnect integration, Part IX item 7).

`compute_worker_from_env` must wire an external local-compute manager in as a routable
worker *from config*, with the exact memory-backend discipline: env overrides file, file
overrides nothing, absence is off, and a malformed block degrades to off. These are the
regressions the ComputeConnect contract (docs/AGENTCONNECT_INTEGRATION.md) asks for.
"""

import pytest

from agentconnect.core import bootstrap
from agentconnect.core.local_compute import (
    HttpLocalComputeProvider,
    LocalModelManagerWorkerAdapter,
)


@pytest.fixture(autouse=True)
def _clean_compute_env(monkeypatch, tmp_path):
    # Isolate every test from the repo's real config/compute.yaml and any ambient env.
    for var in (
        "AGENTCONNECT_COMPUTE_URL", "AGENTCONNECT_COMPUTE_TIMEOUT",
        "AGENTCONNECT_COMPUTE_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    # Point the config path at a file that does not exist, so "no env" means "off".
    monkeypatch.setenv(bootstrap.COMPUTE_CONFIG_PATH, str(tmp_path / "absent.yaml"))


def test_env_wiring_builds_compute_worker(monkeypatch):
    monkeypatch.setenv("AGENTCONNECT_COMPUTE_URL", "http://127.0.0.1:8090/")
    monkeypatch.setenv("AGENTCONNECT_COMPUTE_TIMEOUT", "12.5")
    monkeypatch.setenv("AGENTCONNECT_COMPUTE_TOKEN", "Bearer sekret")

    worker = bootstrap.compute_worker_from_env()

    assert isinstance(worker, LocalModelManagerWorkerAdapter)
    provider = worker.provider
    assert isinstance(provider, HttpLocalComputeProvider)
    assert provider.base_url == "http://127.0.0.1:8090"  # trailing slash stripped
    assert provider._timeout == 12.5
    assert provider._token == "Bearer sekret"  # honored, and never logged
    assert worker.capabilities().harness == "local_model_manager"


def test_absent_config_means_no_worker():
    # The autouse fixture cleared env and pointed the config at a missing file.
    assert bootstrap.compute_worker_from_env() is None


def test_malformed_yaml_degrades_to_off(monkeypatch, tmp_path):
    bad = tmp_path / "compute.yaml"
    bad.write_text("compute: [this: is not, valid: mapping\n", encoding="utf-8")
    monkeypatch.setenv(bootstrap.COMPUTE_CONFIG_PATH, str(bad))

    # No crash, no worker — a missing compute plane is smaller than a wrong one.
    assert bootstrap.compute_worker_from_env() is None


def test_non_mapping_compute_block_degrades_to_off(monkeypatch, tmp_path):
    # Well-formed YAML of the wrong shape (`compute: notamapping`) must degrade to
    # off with a warning exactly like an unparseable file, not crash startup.
    bad = tmp_path / "compute.yaml"
    bad.write_text("compute: notamapping\n", encoding="utf-8")
    monkeypatch.setenv(bootstrap.COMPUTE_CONFIG_PATH, str(bad))

    assert bootstrap.compute_worker_from_env() is None


def test_list_rooted_compute_document_degrades_to_off(monkeypatch, tmp_path):
    bad = tmp_path / "compute.yaml"
    bad.write_text("- compute\n- oops\n", encoding="utf-8")
    monkeypatch.setenv(bootstrap.COMPUTE_CONFIG_PATH, str(bad))

    assert bootstrap.compute_worker_from_env() is None


def test_non_mapping_memory_backend_entry_is_ignored(monkeypatch, tmp_path):
    # One level deeper than the block guards: `wikibrain: true` is a natural
    # operator shorthand for enabling a backend, and it must be ignored with a
    # warning — not crash startup with AttributeError on `spec.get`.
    bad = tmp_path / "memory.yaml"
    bad.write_text("memory:\n  backends:\n    wikibrain: true\n", encoding="utf-8")
    monkeypatch.setenv(bootstrap.MEMORY_CONFIG_PATH, str(bad))
    for var in ("WIKIBRAIN_URL", "BRAINCONNECT_URL", "COGNEE_URL", "GRAPHITI_URL"):
        monkeypatch.delenv(var, raising=False)

    adapters, _config = bootstrap.memory_from_env()
    assert adapters == {}


def test_yaml_base_url_and_knobs_honored(monkeypatch, tmp_path):
    cfg = tmp_path / "compute.yaml"
    cfg.write_text(
        "compute:\n"
        "  enabled: true\n"
        "  base_url: http://127.0.0.1:8099\n"
        "  timeout: 7\n"
        "  worker_id: my-gpu\n"
        "  task_type: code\n"
        "  max_output_tokens: 512\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(bootstrap.COMPUTE_CONFIG_PATH, str(cfg))

    worker = bootstrap.compute_worker_from_env()

    assert isinstance(worker, LocalModelManagerWorkerAdapter)
    assert worker.worker_id == "my-gpu"
    assert worker.provider.base_url == "http://127.0.0.1:8099"
    assert worker.provider._timeout == 7.0
    assert worker._max_output_tokens == 512


def test_env_url_overrides_yaml_base_url(monkeypatch, tmp_path):
    cfg = tmp_path / "compute.yaml"
    cfg.write_text("compute:\n  enabled: true\n  base_url: http://from-file:1\n",
                   encoding="utf-8")
    monkeypatch.setenv(bootstrap.COMPUTE_CONFIG_PATH, str(cfg))
    monkeypatch.setenv("AGENTCONNECT_COMPUTE_URL", "http://from-env:2")

    worker = bootstrap.compute_worker_from_env()
    assert worker is not None
    assert worker.provider.base_url == "http://from-env:2"


def test_yaml_enabled_false_means_off(monkeypatch, tmp_path):
    cfg = tmp_path / "compute.yaml"
    cfg.write_text("compute:\n  enabled: false\n  base_url: http://x:1\n", encoding="utf-8")
    monkeypatch.setenv(bootstrap.COMPUTE_CONFIG_PATH, str(cfg))
    assert bootstrap.compute_worker_from_env() is None


def test_service_from_env_appends_compute_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTCONNECT_COMPUTE_URL", "http://127.0.0.1:8090")
    monkeypatch.setenv("AGENTCONNECT_DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("AGENTCONNECT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AGENTCONNECT_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("AGENTCONNECT_WORKERS", "echo")
    # No memory / toolconnect configured for this test.
    monkeypatch.setenv(bootstrap.TOOLCONNECT_CONFIG_PATH, str(tmp_path / "absent-tc.yaml"))
    monkeypatch.setenv(bootstrap.MEMORY_CONFIG_PATH, str(tmp_path / "absent-mem.yaml"))

    service = bootstrap.service_from_env()
    harnesses = {w.capabilities().harness for w in service.registry.all()}
    worker_ids = {w.worker_id for w in service.registry.all()}
    assert "local_model_manager" in harnesses
    assert "echo_worker" in worker_ids  # the built-in is still there


def test_service_from_env_without_compute_is_unchanged(monkeypatch, tmp_path):
    # No compute env, config path missing -> no local_model_manager worker.
    monkeypatch.setenv("AGENTCONNECT_DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("AGENTCONNECT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AGENTCONNECT_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("AGENTCONNECT_WORKERS", "echo")
    monkeypatch.setenv(bootstrap.TOOLCONNECT_CONFIG_PATH, str(tmp_path / "absent-tc.yaml"))
    monkeypatch.setenv(bootstrap.MEMORY_CONFIG_PATH, str(tmp_path / "absent-mem.yaml"))

    service = bootstrap.service_from_env()
    harnesses = {w.capabilities().harness for w in service.registry.all()}
    assert "local_model_manager" not in harnesses
    assert service.tool_governor is None
