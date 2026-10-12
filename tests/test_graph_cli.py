# ruff: noqa: F811 -- imported pytest fixtures intentionally match parameter names.
import argparse
import json
import sys

import pytest

from test_cli import settings, runtime, Store  # noqa: F401


def test_default_settings_do_not_activate_graph(settings):
    assert settings.graph.enabled is False


@pytest.mark.asyncio
async def test_status_off_mode_does_not_import_driver(settings, runtime, capsys):
    from knowseam import cli
    sys.modules.pop("neo4j", None)
    assert await cli.execute(argparse.Namespace(command="status"), settings) == 0
    assert "neo4j" not in sys.modules
    assert json.loads(capsys.readouterr().out)["database_ready"] is True


def test_unknown_graph_collection_is_rejected(settings):
    from knowseam.config import Settings
    from pydantic import ValidationError
    values = settings.model_dump()
    values["graph"] = {"enabled": True, "password": "synthetic-test-only", "collections": {
        "unknown": {"entity_types": ["item"], "relation_types": ["supports"]}}, "extractor": {
        "base_url": "http://127.0.0.1:8999", "model_id": "test-model"}}
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_cli_exposes_rebuild_and_projection_worker():
    from knowseam import cli
    for command in ("graph-worker", "graph-rebuild", "graph-status", "init-graph"):
        assert cli.parser().parse_args(["--config", "private.json", command]).command == command
