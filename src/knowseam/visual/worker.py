"""Durable visual work: explicit retries, bounded inference, no host/model mutation."""

import asyncio
from .assets import render_assets
from ..domain import NotFound


class VisualWorker:
    def __init__(self, backend, encoder, company_id):
        if encoder.fingerprint != backend.config.fingerprint:
            raise ValueError("visual_fingerprint_mismatch")
        self.backend, self.encoder, self.company_id = backend, encoder, company_id

    async def step(self):
        job = await asyncio.to_thread(self.backend.claim, self.company_id)
        if job is None:
            return {"status": "idle"}
        try:
            data = await asyncio.to_thread(self.backend.load, job)
            assets = await asyncio.to_thread(render_assets, data, job["requests"])
            vectors = []
            for asset in assets:
                if not await asyncio.to_thread(self.backend.renew, job):
                    raise NotFound("visual_job_obsolete")
                batch = await asyncio.wait_for(
                    self.encoder.encode_assets([asset]), self.backend.config.timeout_seconds
                )
                if len(batch) != 1:
                    raise ValueError("visual_vector_count")
                vectors.extend(batch)
            done = await asyncio.to_thread(self.backend.commit, job, assets, vectors)
            if not done:
                await asyncio.to_thread(self.backend.finish, job)
            return {"status": "done" if done else "obsolete"}
        except NotFound:
            await asyncio.to_thread(self.backend.finish, job)
            return {"status": "obsolete"}
        except Exception as error:
            await asyncio.to_thread(self.backend.finish, job, type(error).__name__)
            return {
                "status": "failed" if job["attempts"] >= self.backend.config.max_attempts else "retry",
                "error_code": type(error).__name__,
            }
