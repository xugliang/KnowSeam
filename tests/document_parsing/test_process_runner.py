import sys

import pytest

from knowseam.domain import SourceContent, UnsupportedDocument


def test_timeout_then_following_request_works(tmp_path):
    from knowseam.document_parsing.process_runner import ProcessRunner
    from knowseam.document_parsing.protocol import DoclingOptions
    runner = ProcessRunner(tmp_path, command=(sys.executable, '-c', 'import time; time.sleep(10)'))
    with pytest.raises(UnsupportedDocument, match='timeout'):
        runner.run(SourceContent(b'abc', 'a.txt', 'text/plain'), DoclingOptions(timeout_seconds=1))
    runner.command = (sys.executable, '-c', 'print(\'{"sections":[{"text":"ok","locator":{},"heading":""}],"warnings":[],"parser_version":"test"}\')')
    assert runner.run(SourceContent(b'abc', 'a.txt', 'text/plain'), DoclingOptions()).sections[0].text == 'ok'


def test_child_environment_has_no_credentials(tmp_path, monkeypatch):
    from knowseam.document_parsing.process_runner import ProcessRunner
    from knowseam.document_parsing.protocol import DoclingOptions
    monkeypatch.setenv('FEISHU_SECRET', 'never-expose')
    monkeypatch.setenv('DATABASE_URL', 'never-expose')
    code = "import os; assert 'FEISHU_SECRET' not in os.environ and 'DATABASE_URL' not in os.environ; print('{\"sections\":[{\"text\":\"safe\",\"locator\":{},\"heading\":\"\"}],\"warnings\":[],\"parser_version\":\"test\"}')"
    runner = ProcessRunner(tmp_path, command=(sys.executable, '-c', code))
    assert runner.run(SourceContent(b'a', 'a.txt', 'text/plain'), DoclingOptions()).sections[0].text == 'safe'
