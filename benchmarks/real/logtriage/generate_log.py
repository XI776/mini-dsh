"""Regenerate benchmarks/real/logtriage/baseline/data/events.log deterministically.

The fixture is checked in so that holdout counts stay stable. Run this script
only when the fixture must be rebuilt, and update the holdout expectations in
benchmarks/real/logtriage.json afterwards.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEVELS = ["DEBUG", "INFO", "INFO", "INFO", "WARN", "ERROR", "ERROR", "CRITICAL"]
COMPONENTS = [
    "api", "api", "api", "worker", "worker", "scheduler", "storage", "storage", "auth",
]
MESSAGES = {
    "api": ["request completed", "connection reset by peer", "upstream returned 503",
            "payload rejected", "slow request took 4.2s"],
    "worker": ["job finished", "job retried", "lease expired", "queue depth above threshold"],
    "scheduler": ["tick", "no work available", "lock contention", "cron entry skipped"],
    "storage": ["segment flushed", "disk usage above 85 percent", "checksum mismatch",
                "compaction finished"],
    "auth": ["token validated", "token expired", "signature mismatch"],
}


def main() -> None:
    rng = random.Random(20261015)
    start = datetime(2026, 10, 14, 0, 0, 0, tzinfo=timezone.utc)
    lines: list[str] = []
    for index in range(620):
        stamp = (start + timedelta(seconds=index * 7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        level = rng.choice(LEVELS)
        component = rng.choice(COMPONENTS)
        message = rng.choice(MESSAGES[component])
        lines.append(f"{stamp} {level} {component} {message}")
        if index % 97 == 41:
            lines.append("")
        if index % 113 == 17:
            lines.append(f"{stamp} TRACE {component} unknown level was logged")
        if index % 151 == 29:
            lines.append("  continuation of the previous entry, indented text")
        if index % 89 == 5:
            lines.append(f"{stamp} WARN {component}")
    target = Path(__file__).parent / "baseline" / "data" / "events.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {target} ({target.stat().st_size} bytes, {len(lines)} lines)")


if __name__ == "__main__":
    main()
