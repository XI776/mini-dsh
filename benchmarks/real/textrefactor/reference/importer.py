"""Row importer used by the nightly load.

Reference fix for the text-refactor task: imported names keep their case.
"""

from util.text import normalize


def import_row(row):
    """Normalize one row from the upstream export."""
    return {"name": normalize(row["name"], keep_case=True), "qty": int(row["qty"])}
