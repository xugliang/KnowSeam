"""Private UDS runtime; intentionally not mounted as a public HTTP route."""

import asyncio
import base64
import os
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from ..document_parsing.protocol import load_json
from .domain import VisualAsset


def load_runtime_config(path):
    """Inference only: deliberately reject core credentials/source configuration."""
    from pathlib import Path
    from .domain import VisualConfig
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("visual_config_limit")
        config = VisualConfig.model_validate(load_json(raw))
        paths = (config.model_path, config.socket_path)
        if not config.enabled or any(
            p is None or not p.is_absolute() or ".." in p.parts or p == Path(p.anchor) for p in paths
        ):
            raise ValueError("visual_config_paths")
        return config
    except (OSError, ValueError, TypeError):
        raise ValueError("invalid_visual_runtime_config") from None


def create_runtime(encoder):
    busy = asyncio.Lock()

    async def status(request):
        ready = await encoder.ready()
        return JSONResponse({"ready": ready, "fingerprint": encoder.fingerprint})

    async def encode(request):
        try:
            data = bytearray()
            async with asyncio.timeout(30):
                async for part in request.stream():
                    if len(data) + len(part) > 14 * 1024 * 1024:
                        return JSONResponse({"error": "request_limit"}, 413)
                    data.extend(part)
            value = load_json(bytes(data))
            if (
                not isinstance(value, dict)
                or value.get("fingerprint") != encoder.fingerprint
                or set(value) not in ({"fingerprint", "query"}, {"fingerprint", "image"})
            ):
                return JSONResponse({"error": "invalid_request"}, 400)
            if busy.locked():
                return JSONResponse({"error": "busy"}, 429)
            async with busy:
                if "query" in value:
                    if (
                        not isinstance(value["query"], str)
                        or not value["query"].strip()
                        or len(value["query"]) > 4096
                    ):
                        return JSONResponse({"error": "invalid_query"}, 400)
                    vectors = await encoder.encode_queries([value["query"]])
                else:
                    from .assets import _image

                    raw = base64.b64decode(value["image"], validate=True)
                    if len(raw) > 10 * 1024 * 1024:
                        return JSONResponse({"error": "request_limit"}, 413)
                    image = _image(raw)
                    try:
                        if max(image.size) > 2048:
                            return JSONResponse({"error": "image_limit"}, 400)
                        asset = VisualAsset(raw, image.width, image.height, 1, "image")
                    finally:
                        image.close()
                    vectors = await encoder.encode_assets([asset])
            return JSONResponse(
                {"fingerprint": encoder.fingerprint, "vectors": [v.model_dump() for v in vectors]}
            )
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "invalid_request"}, 400)
        except Exception:
            return JSONResponse({"error": "runtime_unavailable"}, 503)

    return Starlette(routes=[Route("/v1/encode", encode, methods=["POST"]),
                            Route("/v1/status", status, methods=["GET"])])


async def serve_runtime(config):
    from .supervisor import ProcessVisualEncoder
    import uvicorn

    # No pre-existing arbitrary socket/file is removed. Supervisor owns stale socket cleanup.
    path = config.socket_path
    if not path.is_absolute() or ".." in path.parts or path.exists() or path.is_symlink():
        raise ValueError("visual_socket_path_unavailable")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    encoder = ProcessVisualEncoder(config)
    try:
        await encoder.start()
        await uvicorn.Server(
            uvicorn.Config(create_runtime(encoder), uds=str(path), access_log=False, log_level="warning")
        ).serve()
    finally:
        await encoder.close()
        # Only unlink the socket created by this invocation, never an arbitrary existing file.
        import stat

        if path.exists() and stat.S_ISSOCK(path.lstat().st_mode):
            path.unlink()


def main(argv=None):
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Offline visual runtime; no knowledge DB credentials")
    parser.add_argument("--config", required=True)
    parser.add_argument("--inspect", action="store_true", help="Print local weight/package fingerprints only")
    args = parser.parse_args(argv)
    try:
        config = load_runtime_config(args.config)
        if args.inspect:
            from .colqwen import runtime_identity
            _, identity = runtime_identity(config)
            print(json.dumps(identity))
        else:
            asyncio.run(serve_runtime(config))
        return 0
    except Exception as error:
        print(json.dumps({"error": "visual_runtime_failed", "category": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
