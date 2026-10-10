from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_parser_container_has_no_source_credentials_or_network():
    service = yaml.safe_load((ROOT/'deployment/compose.docling.yaml').read_text())['services']['parser']
    assert service['network_mode'] == 'none' and not service.get('ports')
    assert service['read_only'] is True and service['user'] == '10001:10001'
    assert service['profiles'] == ['docling']
    assert service['cpus'] == 2 and service['mem_limit'] == '4g'
    assert service['pids_limit'] == 256 and service['cap_drop'] == ['ALL']
    assert set(service.get('environment', {})) <= {'HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'OMP_NUM_THREADS'}
    for item in service['volumes']:
        assert '/private' not in item and 'docker.sock' not in item and 'data' not in item
    assert not service.get('env_file')


def test_disabled_default_compose_has_no_parser_mount():
    compose = yaml.safe_load((ROOT/'deployment/compose.yaml').read_text())
    assert 'parser' not in compose['services']
    assert all('parser-socket' not in str(service) for service in compose['services'].values())


def test_online_wheel_builder_mounts_only_public_lock_not_repository():
    script = (ROOT/'scripts/prepare-parser-wheelhouse.sh').read_text()
    assert 'dst=/source' not in script
    assert 'src=$root/deployment/requirements-parser.lock,dst=/requirements-parser.lock,readonly' in script
