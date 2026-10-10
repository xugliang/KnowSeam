"""ReAct -> real Runner/schema -> real plugin -> mock HTTP. No employee messages."""
# ruff: noqa: E402 -- optional framework must be checked before adapter imports.
import asyncio
import json
import os
import sys
import httpx
import pytest

if source_path := os.environ.get("SUITEHARNESS_SOURCE_PATH"):
    sys.path.insert(0, source_path)
pytest.importorskip("suiteharness", reason="optional plugin tests require the real SuiteHarness source/package")

from knowseam.harness_plugin import KnowledgeHttpClient, KnowledgeTool, KnowledgePluginConfig, knowledge_tool_specs, CAPABILITY
from suiteharness.runtime import ScopePath, RequestScope
from suiteharness.agents import ReActWorkflow
from suiteharness.models import ModelStreamEvent, ModelStreamEventType, ModelToolCall, FinishReason
from suiteharness.execution import RunRequest
from suiteharness.execution.in_memory import (InMemoryToolRegistry, InMemoryCapabilityAuthority,
    InMemoryApprovalStore, InMemoryAuditJournal)
from suiteharness.execution.protocols import ExecutionStrategies
from suiteharness.execution.runner import ExecutionRunner

PATH = ScopePath.product('test-company','feature-assistant')
SCOPE = RequestScope(PATH,'test-employee',channel_id='feishu')

def config():
    return KnowledgePluginConfig(enabled=True,base_url='https://knowledge.internal',
        service_token='test-token',company_by_tenant={'test-company':'test-company'})


def test_invalid_limit_repaired_then_entire_document_paginated():
    async def check():
        pages, seen = [], []
        def http(request):
            assert request.headers['X-Actor-Id'] == SCOPE.principal_id
            assert request.url.path == '/v1/documents/doc-test'
            assert request.url.params['version_id'] == 'ver-test'
            offset, limit = int(request.url.params['offset']), int(request.url.params['limit'])
            assert 1 <= limit <= 100  # invalid request must never reach HTTP
            pages.append((offset, limit))
            rows = [{'chunk_id':f'c-{i}', 'text':str(i)} for i in range(offset,min(offset+limit,230))]
            return httpx.Response(200,json={'document_id':'doc-test','version_id':'ver-test',
                'sections':rows,'citations':[], 'next_offset':offset+limit if offset+limit<230 else None})

        class RepairingModel:
            calls = 0
            async def stream(self, route, request):
                self.calls += 1
                if self.calls == 1:
                    args = {'document_id':'doc-test','version_id':'ver-test','limit':200}
                else:
                    observation = json.loads(request.messages[-1].content[0].text)
                    if self.calls == 2:
                        assert observation['failure']['code'] == 'tool_input_invalid'
                        hint = observation['failure']['message']
                        assert '"validation_path":"limit"' in hint
                        assert '"validation_constraint":100' in hint
                        args = {'document_id':'doc-test','version_id':'ver-test','limit':100}
                    else:
                        result = observation['result']
                        seen.extend(x['chunk_id'] for x in result['sections'])
                        if result['next_offset'] is None:
                            yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA,text_delta='全文读取完成')
                            yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END,finish_reason=FinishReason.STOP)
                            return
                        args = {'document_id':'doc-test','version_id':'ver-test','limit':100,'offset':result['next_offset']}
                yield ModelStreamEvent(type=ModelStreamEventType.TOOL_CALL_DELTA,
                    tool_call=ModelToolCall(call_id=f'call-{self.calls}',name='knowledge.read',arguments=args))
                yield ModelStreamEvent(type=ModelStreamEventType.MESSAGE_END,finish_reason=FinishReason.TOOL_CALLS)

        client = KnowledgeHttpClient(config(),PATH,transport=httpx.MockTransport(http))
        try:
            spec = next(s for s in knowledge_tool_specs() if s.name=='knowledge.read')
            registry = InMemoryToolRegistry()
            registry.register(PATH,spec,KnowledgeTool(client,spec))
            authority = InMemoryCapabilityAuthority()
            grant = await authority.issue(PATH,tool_identities=[registry.resolve(SCOPE,spec.name).identity],
                capabilities=[CAPABILITY],principal_id=SCOPE.principal_id)
            journal = InMemoryAuditJournal()
            runner = ExecutionRunner(tools=registry,capabilities=authority,approvals=InMemoryApprovalStore(),journal=journal)
            result = await runner.run(RunRequest(run_id='repair-e2e',scope=SCOPE,grant_id=grant.grant_id,
                input='读取完整文档',read_only=True),ExecutionStrategies(workflow=ReActWorkflow(RepairingModel(),'test')))
            assert result.status.value == 'succeeded'
            assert result.output == '全文读取完成'
            assert pages == [(0,100),(100,100),(200,100)]
            assert seen == [f'c-{i}' for i in range(230)]
            assert result.observations[0].failure.code.value == 'tool_input_invalid'
        finally:
            await client.close()
    asyncio.run(check())
