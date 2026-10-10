import pytest
from pydantic import ValidationError


def test_default_limits_and_disabled_engine():
    from knowseam.document_parsing.config import ParsingConfig, DoclingConfig
    config = ParsingConfig()
    assert config.docling.enabled is False
    assert (config.docling.timeout_seconds, config.docling.max_pages, config.docling.max_sections) == (120, 200, 50000)
    for bad in (True, '2', 0, -1):
        with pytest.raises(ValidationError):
            DoclingConfig(timeout_seconds=bad)


@pytest.mark.parametrize('values', [{'routes': {'exe': 'docling'}}, {'routes': {'pdf': 'other'}},
    {'docling': {'enabled': 'true'}}, {'docling': {'socket_path': 'https://bad'}},
    {'docling': {'model_path': '/tmp/../secret'}}, {'unexpected': 1}])
def test_invalid_configuration(values):
    from knowseam.document_parsing.config import ParsingConfig
    with pytest.raises(ValidationError):
        ParsingConfig.model_validate(values)
