"""Nightly record aggregation pipeline.

Reference fix for the pipeline-tail task: every record must survive chunking
and dedupe keeps the first record per id.
"""

DEFAULT_CHUNK = 64


def _validate_records(records):
    """Reject records that do not carry the fields the stages rely on."""
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("records must be dicts")
        for field in ("id", "source", "count"):
            if field not in record:
                raise ValueError(f"record is missing {field!r}")


def chunk(records, size=DEFAULT_CHUNK):
    """Split records into consecutive chunks of at most `size` items."""
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        raise ValueError("size must be a positive integer")
    return [records[index:index + size] for index in range(0, len(records), size)]


def dedupe(records):
    """Keep one record per id and preserve the input order."""
    seen = {}
    for record in records:
        seen.setdefault(record["id"], record)
    return list(seen.values())


def merge(left, right):
    """Merge two summaries by summing their count fields per id."""
    merged = {}
    for summary in (left, right):
        for record_id, count in summary.items():
            merged[record_id] = merged.get(record_id, 0) + count
    return merged


def summarize(records):
    """Count records per source and report the aggregate numbers."""
    by_source = {}
    ids = set()
    total = 0
    for record in records:
        by_source[record["source"]] = by_source.get(record["source"], 0) + 1
        ids.add(record["id"])
        total += record["count"]
    return {
        "records": len(records),
        "distinct_ids": len(ids),
        "total_count": total,
        "by_source": by_source,
    }


def run(records, size=DEFAULT_CHUNK):
    """Prepare the nightly aggregation and return per-chunk summaries."""
    _validate_records(records)
    unique = dedupe(records)
    chunks = chunk(unique, size)
    summaries = [summarize(group) for group in chunks]
    return {
        "chunks": summaries,
        "records": sum(item["records"] for item in summaries),
        "total_count": sum(item["total_count"] for item in summaries),
    }
