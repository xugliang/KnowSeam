"""Explicit opt-in: real offline visual inference, never auto-download weights."""
import math
import os

import pytest


@pytest.mark.visual_real
@pytest.mark.asyncio
async def test_real_colqwen_query_image_multivectors():
    path = os.environ.get("KNOWSEAM_VISUAL_TEST_CONFIG")
    if not path:
        pytest.skip("real visual inference needs approved hardware and KNOWSEAM_VISUAL_TEST_CONFIG")
    from knowseam.visual.runtime import load_runtime_config
    from knowseam.visual.supervisor import ProcessVisualEncoder
    from knowseam.visual.domain import VisualAsset, maxsim
    from test_visual_assets import png

    config = load_runtime_config(path)
    encoder = ProcessVisualEncoder(config)
    try:
        queries = await encoder.encode_queries(["图片中的主要颜色是什么？"])
        images = await encoder.encode_assets([VisualAsset(png((128, 128)), 128, 128, 1, "image")])
        assert queries[0].dimensions == images[0].dimensions == 128
        assert math.isfinite(maxsim(queries[0], images[0]))
    finally:
        await encoder.close()
