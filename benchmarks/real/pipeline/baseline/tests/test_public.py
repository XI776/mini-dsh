import unittest

import pipeline


class ChunkTests(unittest.TestCase):
    def test_chunk_keeps_a_partial_final_chunk(self):
        self.assertEqual(pipeline.chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])

    def test_chunk_exact_multiple(self):
        self.assertEqual(pipeline.chunk([1, 2, 3, 4], 2), [[1, 2], [3, 4]])

    def test_chunk_rejects_bad_size(self):
        with self.assertRaises(ValueError):
            pipeline.chunk([1], 0)


class DedupeTests(unittest.TestCase):
    def test_duplicate_ids_collapse(self):
        records = [{"id": 1, "source": "a", "count": 1}, {"id": 1, "source": "a", "count": 1}]
        self.assertEqual(len(pipeline.dedupe(records)), 1)


class MergeTests(unittest.TestCase):
    def test_merge_sums_counts_per_id(self):
        self.assertEqual(pipeline.merge({1: 3, 2: 1}, {2: 4, 3: 2}), {1: 3, 2: 5, 3: 2})


class RunTests(unittest.TestCase):
    def test_run_reports_every_record(self):
        records = [
            {"id": 1, "source": "a", "count": 2},
            {"id": 2, "source": "b", "count": 3},
            {"id": 3, "source": "a", "count": 5},
        ]
        result = pipeline.run(records, size=2)
        self.assertEqual(result["records"], 3)
        self.assertEqual(result["total_count"], 10)


if __name__ == "__main__":
    unittest.main()
