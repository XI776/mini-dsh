# Duration parsing

Scheduler configuration accepts short duration strings instead of raw second
counts. `duration.parse.parse_duration(value)` must accept:

- a bare nonnegative integer, meaning seconds (`"45"` -> `45`),
- one or more segments, each a nonnegative integer followed by a unit letter:
  `s` (seconds), `m` (minutes), `h` (hours), `d` (days),
- concatenated segments, which add up (`"1h30m"` -> `5400`,
  `"1h30m15s"` -> `5415`, `"1d12h"` -> `129600`); every segment after the
  first must carry its own unit, so `"1h30"` is invalid,
- surrounding whitespace around the whole value (`"  2m "` -> `120`).

Anything else raises `ValueError`: a non-string input, an empty or
whitespace-only string, an unknown or uppercase unit letter, a segment without
a unit, or a negative number. Units are case sensitive and only lowercase
letters are valid.
