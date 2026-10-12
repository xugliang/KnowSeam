"""Local-only ColQwen2 adapter; import only in the dedicated visual runtime."""

import asyncio
from hashlib import sha256
from importlib.metadata import version
import os

from ..chunking import canonical, digest
from .domain import MultiVector, VisualEncoder

PACKAGES = ("colpali-engine", "torch", "transformers", "peft", "Pillow", "numpy", "torchvision")


def _fingerprint(root, paths):
    hashes = []
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("visual_weights_symlink")
        h = sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
        hashes.append((str(path.relative_to(root)), h.hexdigest()))
    if not hashes:
        raise ValueError("visual_weights_missing")
    return digest(canonical(hashes))


def runtime_identity(config):
    root = config.model_path
    if root is None or not root.is_dir():
        raise ValueError("visual_weights_missing")
    root = root.resolve()
    # Require a complete merged local checkpoint, not an adapter referring to a remote base model.
    if (root / "adapter_config.json").exists() or not (root / "config.json").is_file():
        raise ValueError("visual_weights_require_merged_local_checkpoint")
    weights = list(root.glob("*.safetensors")) + [root / "config.json"]
    processors = [
        p for p in root.iterdir() if p.suffix in {".json", ".txt", ".model"} and p.name != "config.json"
    ]
    if not any(p.suffix == ".safetensors" for p in weights):
        raise ValueError("visual_weights_missing")
    actual = dict(
        model_sha256=_fingerprint(root, weights),
        processor_sha256=_fingerprint(root, processors),
        dependency_sha256=digest(canonical({p: version(p) for p in PACKAGES})),
    )
    return root, actual


def validate_runtime(config):
    root, actual = runtime_identity(config)
    if any(actual[k] != getattr(config, k) for k in actual):
        raise ValueError("visual_runtime_fingerprint_mismatch")
    if version("colpali-engine") != "0.3.13":
        raise ValueError("visual_processor_version_mismatch")
    return root


class ColQwenEncoder(VisualEncoder):
    def __init__(self, config):
        root = validate_runtime(config)
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        import torch
        from colpali_engine.models import ColQwen2, ColQwen2Processor

        self.config, self.fingerprint = config, config.fingerprint
        self.model = ColQwen2.from_pretrained(
            str(root),
            local_files_only=True,
            trust_remote_code=False,
            torch_dtype=getattr(torch, config.dtype),
            device_map=config.device,
        ).eval()
        self.processor = ColQwen2Processor.from_pretrained(
            str(root), local_files_only=True, trust_remote_code=False, max_num_visual_tokens=1024
        )
        self.processor.query_prefix = config.query_prefix
        self.lock = asyncio.Lock()

    def _infer(self, values, images):
        import torch

        batch = (
            self.processor.process_images(values) if images else self.processor.process_queries(values)
        ).to(self.model.device)
        if batch["input_ids"].shape[1] > 2048:
            raise ValueError("visual_token_limit")
        with torch.inference_mode():
            output = self.model(**batch)
        # Single inputs avoid padding tokens; still remove masked rows explicitly.
        rows = output[0][batch["attention_mask"][0].bool()].float().cpu().tolist()
        return [MultiVector(dimensions=self.config.dimensions, vectors=rows)]

    async def encode_queries(self, texts):
        if len(texts) != 1 or not texts[0].strip() or len(texts[0]) > 4096:
            raise ValueError("visual_query_limit")
        return await self._locked_inference(list(texts), False)

    async def _locked_inference(self, values, images):
        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(self._infer, values, images))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Cancellation must not release the model lock while its thread still uses the device.
                await task
                raise

    async def encode_assets(self, assets):
        from .assets import _image

        if len(assets) != 1:
            raise ValueError("visual_batch_limit")
        image = _image(assets[0].data)
        try:
            return await self._locked_inference([image], True)
        finally:
            image.close()
