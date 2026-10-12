"""Restart-safe projection worker; inference and graph writes are outside publication."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import logging

from .domain import ChunkExtraction
from .extraction import GRAPH_EXTRACTOR_VERSION, validate_extraction

LOGGER = logging.getLogger("knowseam.graph.worker")


class LeaseLost(RuntimeError):
    pass


class GraphWorker:
    def __init__(self, repository, graph_store, extractor, config, company_id, *, extraction_cache=None, fact_recorder=None):
        if not config.enabled and fact_recorder is None:
            raise ValueError("graph worker requires graph or fact capability")
        if graph_store is None and fact_recorder is None:
            raise ValueError('projection_or_fact_recorder_required')
        self.repository, self.graph_store, self.extractor = repository, graph_store, extractor
        self.config, self.company_id = config, company_id
        self.extraction_cache = extraction_cache
        self.fact_recorder = fact_recorder
        if config.extraction_cache and extraction_cache is None:
            raise ValueError('configured extraction cache missing')
        self.projections = {collection: config.projection_key(collection) for collection in config.collections}

    async def _renew(self, job):
        if not await asyncio.to_thread(self.repository.graph_renew, job, self.config.lease_seconds):
            raise LeaseLost("graph worker lease was lost")

    async def step(self):
        job = await asyncio.to_thread(self.repository.graph_claim, self.company_id, self.projections,
                                      lease_seconds=self.config.lease_seconds, max_attempts=self.config.max_attempts)
        if job is None:
            return None
        try:
            snapshot = await asyncio.to_thread(self.repository.graph_load, job)
            if snapshot is None:
                await asyncio.to_thread(self.repository.graph_complete, job, obsolete=True)
                return {"id": job["id"], "status": "obsolete"}
            if job["action"] == "delete":
                await self._renew(job)
                if self.graph_store is not None:
                    await asyncio.to_thread(self.graph_store.remove_document, snapshot.company_id,
                                            snapshot.collection_id, snapshot.document_id, snapshot.event_sequence)
            else:
                if len(snapshot.chunks) > 10000 or len(job["checkpoint"]) > len(snapshot.chunks):
                    raise ValueError("graph document or checkpoint exceeds bounds")
                ontology = self.config.collections[snapshot.collection_id]
                extractions = [validate_extraction(chunk, ontology, ChunkExtraction.model_validate(value))
                               for chunk, value in zip(snapshot.chunks, job["checkpoint"])]
                for ordinal in range(len(extractions), len(snapshot.chunks)):
                    await self._renew(job)
                    if not await asyncio.to_thread(self.repository.graph_is_current, job):
                        await asyncio.to_thread(self.repository.graph_complete, job, obsolete=True)
                        return {"id": job["id"], "status": "obsolete"}
                    # A configured LLM receives current source chunks, not a synthesized conversation.
                    extraction = (await asyncio.to_thread(self.extraction_cache.get, job, snapshot.chunks[ordinal], ontology)
                                  if self.extraction_cache is not None else None)
                    if extraction is None:
                        extraction = await self.extractor.extract(snapshot.chunks[ordinal], ontology)
                    extraction = validate_extraction(snapshot.chunks[ordinal], ontology, extraction)
                    await self._renew(job)
                    committed = (await asyncio.to_thread(self.extraction_cache.checkpoint, job, ordinal,
                                                        snapshot.chunks[ordinal], ontology, extraction)
                                 if self.extraction_cache is not None else
                                 await asyncio.to_thread(self.repository.graph_checkpoint, job, ordinal,
                                                         extraction.model_dump(mode='json')))
                    if not committed:
                        raise LeaseLost("graph checkpoint was not committed")
                    extractions.append(extraction)
                # Withdrawal/version change during inference invalidates the entire projection.
                current = await asyncio.to_thread(self.repository.graph_load, job)
                if current is None:
                    await asyncio.to_thread(self.repository.graph_complete, job, obsolete=True)
                    return {"id": job["id"], "status": "obsolete"}
                await self._renew(job)
                snapshot = replace(snapshot, extractor_version=GRAPH_EXTRACTOR_VERSION,
                                   model_id=self.config.extractor.model_id,
                                   model_revision=self.config.extractor.model_revision, extractions=tuple(extractions))
                if self.fact_recorder is not None:
                    if not await asyncio.to_thread(self.fact_recorder.record, job, snapshot, ontology):
                        raise LeaseLost('fact publication lease lost')
                if self.graph_store is not None:
                    await asyncio.to_thread(self.graph_store.replace_document, snapshot)
            committed = await asyncio.to_thread(self.repository.graph_complete, job)
            LOGGER.info("graph_projection_complete job=%s action=%s committed=%s", job["id"], job["action"], committed)
            return {"id": job["id"], "status": "done" if committed else "lease_lost"}
        except Exception as error:
            await asyncio.to_thread(self.repository.graph_fail, job, type(error).__name__,
                                    max_attempts=self.config.max_attempts,
                                    delay_seconds=min(300, 2 ** job["attempts"]))
            LOGGER.warning("graph_projection_failed job=%s attempt=%s category=%s",
                           job["id"], job["attempts"], type(error).__name__)
            return {"id": job["id"], "status": "failed_attempt", "attempt": job["attempts"],
                    "category": type(error).__name__}

    async def run(self):
        while True:
            try:
                outcome = await self.step()
            except Exception as error:
                LOGGER.warning("graph_outbox_unavailable category=%s", type(error).__name__)
                outcome = None
            if outcome is None:
                await asyncio.sleep(self.config.poll_seconds)
