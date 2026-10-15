# Log triage

`data/events.log` is a copy of the production event log exported by the
platform team. Implement `logtool.summary.summarize_errors` so the on-call
dashboard can render it without shipping the whole file around.

## Log format

A **well-formed** line is exactly:

```
<timestamp> <LEVEL> <component> <message>
```

- `<timestamp>` matches `YYYY-MM-DDTHH:MM:SSZ`.
- `<LEVEL>` is one of `DEBUG`, `INFO`, `WARN`, `ERROR`, `CRITICAL` (uppercase).
- `<component>` is one or more lowercase letters.
- `<message>` is everything after the component and is never empty; it may
  contain spaces, digits, dots and percent signs.

Every non-empty line that does not match this shape is **malformed**.
Blank lines are ignored entirely and never counted as malformed.

## Required result

`summarize_errors(path)` reads the file at `path` and returns a dict:

- `"by_level"`: counts per level over well-formed lines only.
- `"by_component"`: counts per component over well-formed lines whose level is
  `ERROR` or `CRITICAL` only.
- `"total_errors"`: number of well-formed `ERROR` plus `CRITICAL` lines.
- `"malformed"`: number of malformed lines.
- `"first_error"`: the message of the first well-formed `ERROR` or `CRITICAL`
  line in file order, or `None` when there is none.

Levels and components are reported with their original spelling.
