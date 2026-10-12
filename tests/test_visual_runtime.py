import subprocess
import sys
import pytest
from pydantic import ValidationError


def test_visual_off_imports_no_heavy_dependency():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import knowseam.visual.domain, knowseam.config; import sys; "
            'assert not {"torch","transformers","colpali_engine","pypdfium2"} & sys.modules.keys()',
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_vector_shape_and_model_fingerprint():
    from knowseam.visual.domain import MultiVector, VisualConfig, maxsim

    value = MultiVector(vectors=[[1.0, 0.0], [0.0, 1.0]], dimensions=2)
    assert maxsim(value, MultiVector(vectors=[[1.0, 0.0]], dimensions=2)) == 1.0
    for vectors in ([[float("nan"), 0.0]], [[0.0, 0.0]], [[1.0]], []):
        with pytest.raises(ValueError):
            MultiVector(vectors=vectors, dimensions=2)
    assert MultiVector(vectors=[[1e-200, 0.0]], dimensions=2).normalized() == [[1.0, 0.0]]
    config = dict(
        model_revision="pinned-commit",
        model_sha256="1" * 64,
        processor_sha256="2" * 64,
        dependency_sha256="3" * 64,
    )
    first = VisualConfig(**config)
    assert first.fingerprint != VisualConfig(**{**config, "processor_sha256": "4" * 64}).fingerprint
    with pytest.raises(ValidationError):
        VisualConfig(allow_online=True)
    with pytest.raises(ValidationError):
        VisualConfig(enabled=True)


def test_arbitrary_path_and_external_url_rejected():
    from knowseam.visual.domain import AssetRequest

    for extra in (
        {"path": "/etc/passwd"},
        {"url": "https://example.org/image.png"},
        {"page": 0},
        {"bbox": [0.9, 0.0, 0.1, 1.0]},
        {"source_sha256": "../x"},
    ):
        with pytest.raises(ValidationError):
            AssetRequest.model_validate({"source_sha256": "a" * 64, "media_type": "image/png", **extra})


def test_missing_local_weights_rejected_before_model_import(tmp_path):
    from knowseam.visual.colqwen import validate_runtime
    from knowseam.visual.domain import VisualConfig

    config = VisualConfig(model_path=tmp_path / "missing")
    with pytest.raises(ValueError, match="weights"):
        validate_runtime(config)


@pytest.mark.asyncio
async def test_visual_http_client_checks_fingerprint_and_vector_dimensions(tmp_path):
    import httpx
    from knowseam.visual.encoder import VisualClient
    from knowseam.visual.domain import VisualConfig

    config = VisualConfig(
        model_revision="pinned",
        model_sha256="a" * 64,
        processor_sha256="b" * 64,
        dependency_sha256="c" * 64,
        dimensions=2,
    )

    async def wrong(request):
        return httpx.Response(
            200, json={"fingerprint": "wrong", "vectors": [{"dimensions": 2, "vectors": [[1.0, 0.0]]}]}
        )

    client = VisualClient(config, transport=httpx.MockTransport(wrong))
    with pytest.raises(ValueError, match="fingerprint"):
        await client.encode_queries(["figure"])
    await client.close()


def test_visual_configuration_requires_explicit_permission_worker():
    from knowseam.config import Settings
    from test_config import valid

    values = valid()
    values["parsing"] = {"docling": {"socket_path": "/run/knowseam/parser.sock"}}
    values["features"] = {"visual": True}
    with pytest.raises(ValidationError):
        Settings.model_validate(values)
    values["visual"] = {
        "enabled": True,
        "model_revision": "pinned",
        "model_sha256": "a" * 64,
        "processor_sha256": "b" * 64,
        "dependency_sha256": "c" * 64,
    }
    with pytest.raises(ValidationError):
        Settings.model_validate(values)
    values["permissions"] = {"enabled": True}
    assert Settings.model_validate(values).visual.enabled


@pytest.mark.asyncio
async def test_runtime_rejects_wrong_fingerprint_and_paths_without_inference():
    import httpx
    from knowseam.visual.runtime import create_runtime

    class Encoder:
        fingerprint = "f" * 64

        async def encode_queries(self, queries):
            raise AssertionError("untrusted request reached model")

    app = create_runtime(Encoder())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://runtime") as http:
        for body in (
            {"fingerprint": "wrong", "query": "test"},
            {"fingerprint": "f" * 64, "path": "/etc/passwd"},
        ):
            assert (await http.post("/v1/encode", json=body)).status_code == 400
