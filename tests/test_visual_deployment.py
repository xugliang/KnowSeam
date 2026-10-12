import json
from pathlib import Path
import tomllib

import pytest


def test_visual_runtime_config_has_no_database_or_source_credentials(tmp_path):
    from knowseam.visual.runtime import load_runtime_config
    path = tmp_path / "runtime.json"
    config = dict(enabled=True, model_revision="immutable-model-commit",
        model_path="/models/visual", socket_path="/run/knowseam/visual.sock",
        model_sha256="a"*64, processor_sha256="b"*64, dependency_sha256="c"*64)
    path.write_text(json.dumps(config))
    assert load_runtime_config(path).model_path == Path("/models/visual")
    for extra in ({"dsn": "postgresql://private"}, {"company_id": "company"},
                  {"model_path": "relative"}, {"socket_path": "/tmp/../bad"}):
        path.write_text(json.dumps({**config, **extra}))
        with pytest.raises(ValueError):
            load_runtime_config(path)


def test_visual_render_extra_is_separate_from_model_environment():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    requirements = project["project"]["optional-dependencies"]["visual-render"]
    assert any(r.startswith("pypdfium2") for r in requirements)
    assert not any("torch" in r or "colpali" in r for r in requirements)
    assert not any("colpali" in r for r in project["project"]["dependencies"])


def test_visual_deployment_has_no_network_or_secrets_in_inference_container():
    import yaml
    root = Path(__file__).resolve().parents[1]
    deployment = yaml.safe_load((root / "deployment/compose.visual.yaml").read_text())
    runtime = deployment["services"]["visual-runtime"]
    assert runtime["network_mode"] == "none"
    assert runtime["cap_drop"] == ["ALL"] and runtime["read_only"]
    assert not runtime.get("ports") and not runtime.get("privileged")
    assert not any("core.json" in v or "docker.sock" in v for v in runtime["volumes"])
    assert runtime["mem_limit"] and runtime["cpus"] and runtime["pids_limit"]


def test_visual_offline_build_inputs_are_in_allowlisted_context():
    root = Path(__file__).resolve().parents[1]
    rules = set((root / ".dockerignore").read_text().splitlines())
    # The build context denies everything by default. Without these narrow
    # exceptions Docker cannot COPY the hash lock or offline wheels.
    assert "**" in rules
    assert "!deployment/requirements-visual.lock" in rules
    assert "!.build-visual/" in rules
    assert "!.build-visual/wheelhouse/" in rules
    assert "!.build-visual/wheelhouse/*.whl" in rules
    assert "!.build-visual/**" not in rules
    assert "!secrets/**" not in rules
