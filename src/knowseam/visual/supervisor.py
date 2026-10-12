"""A task-owned inference process provides hard deadlines without killing the API."""
import asyncio
from contextlib import suppress
import multiprocessing

from .domain import MultiVector, VisualEncoder


def _model_process(pipe, config):
    # This private pipe has no network listener and carries only parent-validated inputs.
    from .colqwen import ColQwenEncoder
    try:
        encoder = ColQwenEncoder(config)
        asyncio.run(encoder.encode_queries(['visual readiness']))
        pipe.send({'ready': True})
        while True:
            operation, values = pipe.recv()
            if operation == 'queries':
                result = asyncio.run(encoder.encode_queries(values))
            elif operation == 'assets':
                result = asyncio.run(encoder.encode_assets(values))
            else:
                raise ValueError('visual_operation')
            pipe.send({'vectors': [v.model_dump() for v in result]})
    except (EOFError, BrokenPipeError):
        pass
    except Exception:
        with suppress(Exception):
            pipe.send({'error': 'visual_inference_failed'})
    finally:
        pipe.close()


class ProcessVisualEncoder(VisualEncoder):
    def __init__(self, config):
        self.config, self.fingerprint = config, config.fingerprint
        self.process = None
        self.pipe = None
        self._ready = False
        self.lock = asyncio.Lock()

    def _stop(self):
        self._ready = False
        process, pipe = self.process, self.pipe
        if process is not None:
            if process.is_alive():
                process.terminate()
            process.join(2)
            if process.is_alive():
                process.kill()
                process.join(2)
        if pipe is not None:
            pipe.close()
        self.pipe = None

    def _receive(self):
        if not self.pipe.poll(self.config.timeout_seconds):
            raise TimeoutError('visual_inference_timeout')
        value = self.pipe.recv()
        if not isinstance(value, dict) or 'error' in value:
            raise ValueError('visual_inference_failed')
        return value

    async def _wait(self):
        task = asyncio.create_task(asyncio.to_thread(self._receive))
        try:
            return await asyncio.shield(task)
        except BaseException:
            # Kill only the exact child created here; closing its peer also unblocks poll/recv.
            self._stop()
            with suppress(Exception):
                await task
            raise

    async def _start(self):
        if self.process is not None and self.process.is_alive() and self.pipe is not None:
            return
        self._stop()
        ctx = multiprocessing.get_context('spawn')
        self.pipe, child = ctx.Pipe()
        self.process = ctx.Process(target=_model_process, args=(child, self.config), daemon=True)
        self.process.start()
        child.close()
        if (await self._wait()).get('ready') is not True:
            self._stop()
            raise ValueError('visual_runtime_not_ready')
        self._ready = True

    async def start(self):
        async with self.lock:
            await self._start()

    async def ready(self):
        return bool(self._ready and self.process is not None and self.process.is_alive() and self.pipe is not None)

    async def _encode(self, operation, values):
        async with self.lock:
            await self._start()
            try:
                self.pipe.send((operation, values))
                result = await self._wait()
                vectors = [MultiVector.model_validate(v) for v in result['vectors']]
                if len(vectors) != 1 or vectors[0].dimensions != self.config.dimensions:
                    raise ValueError('visual_vector_shape')
                return vectors
            except BaseException:
                self._stop()
                raise

    async def encode_queries(self, texts):
        if len(texts) != 1 or not isinstance(texts[0], str) or not texts[0].strip() or len(texts[0]) > 4096:
            raise ValueError('visual_query_limit')
        return await self._encode('queries', texts)

    async def encode_assets(self, assets):
        if len(assets) != 1 or len(assets[0].data) > 10 * 1024 * 1024:
            raise ValueError('visual_asset_limit')
        return await self._encode('assets', assets)

    async def close(self):
        async with self.lock:
            self._stop()
