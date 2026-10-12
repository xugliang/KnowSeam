from dataclasses import asdict, is_dataclass
from collections.abc import Mapping


def _matches(span, hits):
    intervals = []
    for raw in hits:
        hit = asdict(raw) if is_dataclass(raw) else raw
        if not isinstance(hit, Mapping) or (hit.get("document_id"), hit.get("version_id")) != (
                span.document_id, span.version_id):
            continue
        locator = hit.get("locator", {})
        if not isinstance(locator, Mapping):
            continue
        if span.table_id is not None:
            if (locator.get("table_id"), locator.get("row"), locator.get("column_id")) != (
                    span.table_id, span.row, span.column_id):
                continue
            if span.expected_value is not None and locator.get("value") != span.expected_value:
                continue
            if span.unit is not None and locator.get("unit") != span.unit:
                continue
            return True
        if locator.get("section_id") != span.section_id:
            continue
        start, end = locator.get("char_start"), locator.get("char_end")
        if type(start) is int and type(end) is int and start >= 0 and end > start:
            intervals.append((start, end))
    covered = span.start_char
    for start, end in sorted(intervals):
        if start > covered:
            break
        covered = max(covered, end)
        if covered >= span.end_char:
            return True
    return False


def evidence_counts(case, result, k, *, failed=False):
    groups = getattr(case, "required_evidence_groups", None)
    if groups is None:
        return {"evidence_group_count": None, "matched_evidence_groups": None}
    hits = [] if failed else (result.get("hits", []) if isinstance(result, Mapping) else result)
    hits = hits[:k]
    matched = sum(any(_matches(span, hits) for span in group.alternatives) for group in groups)
    return {"evidence_group_count": len(groups), "matched_evidence_groups": matched}


def aggregate_evidence(rows):
    labeled = [row for row in rows if row.get("evidence_group_count")]
    return {
        "evidence_group_recall": (sum(row["matched_evidence_groups"] / row["evidence_group_count"]
                                      for row in labeled) / len(labeled) if labeled else None),
        "complete_support_rate": (sum(row["matched_evidence_groups"] == row["evidence_group_count"]
                                      for row in labeled) / len(labeled) if labeled else None),
        "evidence_labeled_cases": len(labeled),
    }
