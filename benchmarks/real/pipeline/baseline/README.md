# Nightly record pipeline

`pipeline.run(records)` prepares the nightly aggregation. Records are plain
dicts with the keys `id`, `source`, `count` and `payload`.

The pipeline is defined as the composition of four stages:

1. `chunk(records, size)` splits records into **consecutive chunks of at most
   `size` items**. Every record must end up in exactly one chunk, so a partial
   final chunk is kept. `size` must be a positive integer.
2. `dedupe(records)` keeps, for each `id`, the **first** record in input order
   and drops later records with the same `id`. The surviving records stay in
   their original order.
3. `merge(left, right)` combines two summaries into one, summing the `count`
   field per `id`.
4. `summarize(records)` counts records per `source` and reports the number of
   records, the number of distinct ids, and the total of all `count` fields.

`run` applies `dedupe` first, then `chunk`, then returns the per-chunk
`summarize` results together with the overall total.
