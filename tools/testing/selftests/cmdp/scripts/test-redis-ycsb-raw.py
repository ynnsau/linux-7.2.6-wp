#!/usr/bin/env python3
"""Deterministic validation of the exact, bounded-memory YCSB raw analyzer."""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import tracemalloc
import unittest


SCRIPT = Path(__file__).with_name("analyze-redis-ycsb-raw.py")
SPEC = importlib.util.spec_from_file_location("raw_analysis", str(SCRIPT))
RAW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RAW)


def write_raw(path, sections, final_newline=True):
    with path.open("w") as out:
        for name, records in sections:
            out.write(name + " latency raw data: op, timestamp(ms), latency(us)\n")
            for timestamp, latency in records:
                out.write("%s,%d,%d\n" % (name, timestamp, latency))
    if not final_newline:
        path.write_bytes(path.read_bytes().rstrip(b"\n"))


def oracle(values, numerator, denominator):
    values = sorted(values)
    lower, remainder = divmod((len(values) - 1) * numerator, denominator)
    upper = lower + bool(remainder)
    return values[lower] + (values[upper] - values[lower]) * remainder / denominator


class RawAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-ycsb-raw-")
        self.directory = Path(self.temp.name)
        self.raw = self.directory / "raw.log"
        self.log = self.directory / "run.log"

    def tearDown(self):
        self.temp.cleanup()

    def run_sections(self, sections, start=1000, end=3000, **kwargs):
        write_raw(self.raw, sections)
        with self.log.open("w") as output:
            for name, records in sections:
                output.write("[%s], Total Operations, %d\n" % (name, len(records)))
        return RAW.analyze(self.raw, start, end, ycsb_log=self.log,
                           require_complete=True, **kwargs)

    def assert_valid(self, result):
        self.assertTrue(result["validation"]["passed"], result["validation"])
        self.assertTrue(result["validation"]["complete_export_verified"])

    def test_grouped_sections_boundary_filter_counts_and_cleanup(self):
        actual = [(999, 500), (1000, 10), (1999, 20), (2000, 30), (2999, 40), (3000, 600)]
        # Intended record timestamps may fall on the next millisecond. Keep
        # the legitimate retained-window count difference, not an invented pair.
        intended = [(t + 1, value + 100) for t, value in actual]
        update = [(1000, 50), (2500, 60)]
        sections = [("UPDATE", update), ("CLEANUP", [(2500, 999999)]),
                    ("READ", actual), ("Intended-READ", intended),
                    ("Intended-CLEANUP", [(2500, 999999)]),
                    ("Intended-UPDATE", update)]
        result = self.run_sections(sections)
        self.assert_valid(result)
        self.assertEqual(result["streams"]["READ"]["count_all"], 6)
        self.assertEqual(result["streams"]["READ"]["count_retained"], 4)
        self.assertEqual(result["streams"]["READ"]["count_before_window"], 1)
        self.assertEqual(result["streams"]["READ"]["count_after_window"], 1)
        self.assertEqual(result["combined"]["actual"]["count"], 6)
        self.assertEqual(result["combined"]["actual"]["max_us"], 60)
        self.assertEqual(result["combined"]["actual"]["qps"], 3)
        self.assertEqual([x["successful_count"] for x in result["per_second"]], [3, 3])
        self.assertEqual(result["per_second"][0]["latency_by_stream"]["READ"]["mean_us"], 15)
        self.assertEqual(result["per_second"][1]["latency_by_stream"]["Intended-READ"]["max_us"], 130)
        self.assertEqual(result["input"]["sha256"], hashlib.sha256(self.raw.read_bytes()).hexdigest())
        self.assertEqual(result["streams"]["READ"]["latency"]["sum_us"], 100)

    def test_percentiles_against_independent_oracle_spills_and_ties(self):
        random.seed(871)
        values = [random.randrange(100) for unused in range(733)] + [1000000]
        records = [(1000 + index, value) for index, value in enumerate(values)]
        result = self.run_sections([("READ", records), ("Intended-READ", records)],
                                   max_histogram_bins=3, histogram_dir=self.directory / "hist")
        self.assert_valid(result)
        self.assertGreater(result["method"]["histogram_spills"], RAW.MAX_MERGE_FILES)
        self.assertLessEqual(result["method"]["peak_live_histogram_bins"], 4)
        measured = result["combined"]["actual"]
        for name, numerator, denominator in RAW.PERCENTILES:
            expected = oracle(values, numerator, denominator)
            self.assertAlmostEqual(measured["percentiles_us"][name], expected)
            self.assertEqual(measured["tail"][name]["observations_strictly_above_estimate"],
                             sum(value > expected for value in values))
        with gzip.open(measured["histogram_csv_gz"], "rt") as source:
            rows = source.read().splitlines()
        self.assertEqual(rows[0], "latency_us,count")
        self.assertEqual(sum(int(row.split(",")[1]) for row in rows[1:]), len(values))

    def test_workload_c_without_update_and_partial_final_second(self):
        records = [(1000, 1), (2000, 100), (2499, 1000)]
        result = self.run_sections([("READ", records), ("Intended-READ", records)], end=2500)
        self.assert_valid(result)
        self.assertNotIn("UPDATE", result["streams"])
        self.assertEqual(result["combined"]["actual"]["qps"], 2)
        self.assertEqual(result["per_second"][1]["duration_ms"], 500)
        self.assertEqual(result["per_second"][1]["successful_qps"], 4)

    def test_empty_stream_and_singleton_percentile(self):
        result = self.run_sections([("READ", [(1000, 17)]), ("UPDATE", []),
                                   ("Intended-READ", [(1000, 19)]), ("Intended-UPDATE", [])])
        self.assert_valid(result)
        self.assertIsNone(result["streams"]["UPDATE"]["latency"]["percentiles_us"]["p99"])
        self.assertEqual(result["combined"]["actual"]["percentiles_us"]["p99.999"], 17)
        self.assertEqual(result["combined"]["actual"]["tail"]["p99.999"]
                         ["observations_strictly_above_estimate"], 0)

    def test_truncation_detected_even_at_valid_row_boundary(self):
        result = self.run_sections([("READ", [(1000, 5)]), ("Intended-READ", [(1000, 8)])])
        self.assert_valid(result)
        with self.log.open("a") as output:
            output.write("[UPDATE], Total Operations, 123\n")
        result = RAW.analyze(self.raw, 1000, 3000, ycsb_log=self.log, require_complete=True)
        self.assertFalse(result["validation"]["passed"])
        self.assertFalse(result["validation"]["complete_export_verified"])
        self.assertTrue(any("UPDATE" in error for error in result["validation"]["errors"]))

    def test_syntax_errors_are_retained_and_not_silently_dropped(self):
        write_raw(self.raw, [("READ", [(1000, 5)]), ("Intended-READ", [(1000, 8)])])
        with self.raw.open("ab") as output:
            output.write(b"READ,1050,12\nIntended-READ,1050,-1\nIntended-READ,x,3\n")
            output.write(b"Intended-READ,1050,2147483648\nIntended-READ,1050")
        result = RAW.analyze(self.raw, 1000, 3000)
        self.assertFalse(result["validation"]["passed"])
        self.assertEqual(result["validation"]["malformed_line_count"], 5)
        self.assertFalse(result["validation"]["final_newline"])
        self.assertEqual(result["combined"]["actual"]["count"], 1)

    def test_failed_streams_retained_but_excluded_from_successful_qps(self):
        sections = [("READ", [(1000, 5)]), ("READ-FAILED", [(1001, 200)]),
                    ("Intended-READ", [(1000, 8)]),
                    ("Intended-READ-FAILED", [(1001, 300)])]
        result = self.run_sections(sections)
        self.assertFalse(result["validation"]["passed"])
        self.assertEqual(result["streams"]["READ-FAILED"]["count_retained"], 1)
        self.assertEqual(result["combined"]["actual"]["count"], 1)
        self.assertEqual(result["per_second"][0]["attempted_count"], 2)
        self.assertEqual(result["per_second"][0]["successful_count"], 1)

    def test_actual_intended_pairs_missing_and_window_difference(self):
        result = self.run_sections([("READ", [(999, 5), (1000, 5)]),
                                   ("Intended-READ", [(1000, 8), (1001, 8)])])
        self.assert_valid(result)
        self.assertEqual(result["actual_intended_pairs"]["READ"]
                         ["retained_delta_intended_minus_actual"], 1)
        result = self.run_sections([("READ", [(1000, 5)])])
        self.assertFalse(result["validation"]["passed"])
        self.assertIn("Missing intended latency stream for READ", result["validation"]["errors"])

    def test_export_count_file_cli_and_return_statuses(self):
        sections = [("READ", [(1000, 5)]), ("Intended-READ", [(1000, 8)])]
        write_raw(self.raw, sections)
        expected = self.directory / "counts.json"
        expected.write_text(json.dumps({"READ": 1, "Intended-READ": 1}))
        output = self.directory / "analysis.json"
        command = [sys.executable, str(SCRIPT), "--input", str(self.raw),
                   "--start-ms", "1000", "--end-ms", "3000", "--output", str(output),
                   "--expected-counts", str(expected), "--require-complete"]
        process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assert_valid(json.loads(output.read_text()))
        self.log.write_text("[READ], Total Operations, 1\n[Intended-READ], Total Operations, 1\n"
                            "[READ], Return=ERROR, 1\n")
        result = RAW.analyze(self.raw, 1000, 3000, ycsb_log=self.log, require_complete=True)
        self.assertFalse(result["validation"]["passed"])
        self.assertEqual(result["export_return_counts"]["READ"]["ERROR"], 1)

    def test_requires_metadata_to_claim_completeness(self):
        write_raw(self.raw, [("READ", [(1000, 5)]), ("Intended-READ", [(1000, 8)])])
        result = RAW.analyze(self.raw, 1000, 3000, require_complete=True)
        self.assertFalse(result["validation"]["passed"])
        result = RAW.analyze(self.raw, 1000, 3000)
        self.assertTrue(result["validation"]["passed"])
        self.assertFalse(result["validation"]["complete_export_verified"])
        self.assertTrue(result["validation"]["warnings"])

    def test_cli_refuses_to_overwrite_raw_evidence(self):
        write_raw(self.raw, [("READ", [(1000, 5)]), ("Intended-READ", [(1000, 8)])])
        original = self.raw.read_bytes()
        process = subprocess.run([sys.executable, str(SCRIPT), "--input", str(self.raw),
                                  "--start-ms", "1000", "--end-ms", "3000",
                                  "--output", str(self.raw)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(self.raw.read_bytes(), original)

    def test_high_cardinality_retention_is_bounded(self):
        count = 20000
        with self.raw.open("w") as output:
            for name in ("READ", "Intended-READ"):
                output.write(name + " latency raw data: op, timestamp(ms), latency(us)\n")
                for index in range(count):
                    output.write("%s,%d,%d\n" % (name, 1000 + index % 1000, index * 997))
        counts = self.directory / "counts.json"
        counts.write_text(json.dumps({"READ": count, "Intended-READ": count}))
        tracemalloc.start()
        result = RAW.analyze(self.raw, 1000, 3000, expected_counts=counts,
                             require_complete=True, max_histogram_bins=128)
        unused, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assert_valid(result)
        self.assertLessEqual(result["method"]["peak_live_histogram_bins"], 129)
        # Includes bounded merge buffers, JSON result, and Python runtime data;
        # no Python list of the 40,000 records is retained.
        self.assertLess(peak, 32 * 1024 * 1024)
        self.assertEqual(result["combined"]["actual"]["distinct_latency_values"], count)


if __name__ == "__main__":
    unittest.main(verbosity=2)
