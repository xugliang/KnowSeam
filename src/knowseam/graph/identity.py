"""Shared stable entity identity, independent of either database driver."""
import hashlib
import json
from .domain import normalized_name


def entity_id(company, collection, kind, name, qualifier):
    return hashlib.sha256(json.dumps([company,collection,kind,normalized_name(name),normalized_name(qualifier)],
                                    ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
