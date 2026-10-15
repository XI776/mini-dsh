"""Row importer used by the nightly load."""

from util.text import normalize


def import_row(row):
    """Normalize one row from the upstream export."""
    return {"name": normalize(row["name"]), "qty": int(row["qty"])}
