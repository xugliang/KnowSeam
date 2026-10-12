import asyncio
import time
import pytest


def hanging_model(pipe, config):
    pipe.send({'ready': True})
    pipe.recv()
    time.sleep(30)


@pytest.mark.asyncio
async def test_hung_inference_process_is_terminated_and_next_request_can_restart(monkeypatch):
    from knowseam.visual import supervisor
    from knowseam.visual.domain import VisualConfig
    monkeypatch.setattr(supervisor, '_model_process', hanging_model)
    encoder = supervisor.ProcessVisualEncoder(VisualConfig(timeout_seconds=1))
    try:
        await encoder.start()
        old = encoder.process
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(encoder.encode_queries(['figure']), 6)
        assert not old.is_alive()
        await encoder.start()
        assert encoder.process.pid != old.pid
    finally:
        await encoder.close()


@pytest.mark.asyncio
async def test_cancelled_request_stops_only_its_owned_model_process(monkeypatch):
    from knowseam.visual import supervisor
    from knowseam.visual.domain import VisualConfig
    monkeypatch.setattr(supervisor, '_model_process', hanging_model)
    encoder = supervisor.ProcessVisualEncoder(VisualConfig(timeout_seconds=20))
    await encoder.start()
    process = encoder.process
    task = asyncio.create_task(encoder.encode_queries(['figure']))
    await asyncio.sleep(.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not process.is_alive()
    await encoder.close()
