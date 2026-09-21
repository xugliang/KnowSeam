"""Write a private client credential and server binding; never print token material."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets

from knowseam.bridge import BridgeConfig
from knowseam.mcp_server import Binding


def create(directory, *, url, actor, company, product, collections, allow_http=False):
    token = secrets.token_urlsafe(36)
    client = BridgeConfig(url=url, token=token, allow_insecure_http=allow_http)
    binding = Binding(token_sha256=hashlib.sha256(token.encode()).hexdigest(), actor_id=actor,
                      company_id=company, product_id=product, allowed_collections=tuple(collections))
    # A NEW private directory only: no overwriting an existing credential or config.
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    payloads = {"client.local.json": {"url": client.url, "token": token,
                                      "allow_insecure_http": client.allow_insecure_http},
                "binding.local.json": binding.model_dump(mode="json")}
    for name, payload in payloads.items():
        descriptor = os.open(directory / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
    return directory


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--collections", nargs="+", required=True)
    parser.add_argument("--allow-http", action="store_true")
    args = parser.parse_args()
    try:
        create(**vars(args))
        print("Private credential files created. Deliver client.local.json securely; install binding server-side.")
    except Exception:
        parser.exit(1, "Could not create credentials; use valid parameters and a new private directory.\n")
