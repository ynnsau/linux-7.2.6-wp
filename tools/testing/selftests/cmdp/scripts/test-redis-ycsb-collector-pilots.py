#!/usr/bin/env python3
"""Validate matching, receipt provenance, and paired collector-pilot summaries."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("compare-redis-ycsb-collector-pilots.py")
SPEC = importlib.util.spec_from_file_location("collector_pilots", str(SCRIPT))
PILOTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PILOTS)


def write(path, value):
    path.write_text(json.dumps(value))


def seal(path):
    write(path / "receipt.json", {"passed": True, "files_sha256": {
        name: PILOTS.source_hash(path / name)["sha256"] for name in
        ("run.json", "report.json", "timeline.json", "restore.json", "command.json")}})


def fixture(path, telemetry, qps, latency_scale=1):
    path.mkdir()
    manifest = {"workload": "C", "mode": "off", "workers": 8, "target_qps": 70000,
                "warmup_seconds": 20, "measure_seconds": 120, "selected_khz": 2100000,
                "telemetry": telemetry, "policy": {"boot_id": "fixture", "governors": {"1": "performance"}},
                "source_hashes": {"fixture.py": "same-source"}}
    report = {"validation": {"passed": True, "errors": []},
              "frequency": {"validation": {"passed": True}} if telemetry else None,
              "metrics": {"achieved_qps": qps, "target_shortfall_pct": 0,
                          "redis_cpu_percent": 99, "client_cpu_percent_of_eight_cores": 40,
                          "arm_events": 0, "wp_events": 0, "flush_calls": 0}, "latency": {}}
    for kind, multiplier in (("actual", 1), ("intended", 10)):
        report["latency"]["combined-" + kind] = {
            "count": 8000000, "mean_us": 50 * multiplier * latency_scale,
            "percentiles_us": {p: (index + 1) * 100 * multiplier * latency_scale
                               for index, p in enumerate(PILOTS.PERCENTILES)},
            "tail": {"p99.999": {"observations_strictly_above_estimate": 80}}}
    write(path / "run.json", manifest)
    write(path / "report.json", report)
    write(path / "timeline.json", {"start_ms": 20000, "end_ms": 140000,
                                    "query_epoch_ms": 1000 if telemetry else 0})
    write(path / "restore.json", {"rdb_sha256": "same-rdb"})
    write(path / "command.json", {"properties": {"measurement.interval": "both",
          "measurement.raw.output_file": str(path / "latency.raw"), "target": 70000}})
    seal(path)


class CollectorPilotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-collector-pilots-")
        self.root = Path(self.temp.name)
        self.pairs = []
        for rep, off_qps, on_qps in ((1, 100, 90), (2, 200, 220), (3, 400, 360)):
            off, on = self.root / ("off-r%d" % rep), self.root / ("on-r%d" % rep)
            fixture(off, False, off_qps)
            fixture(on, True, on_qps, 1 + rep * .05)
            self.pairs.append((off, on))

    def tearDown(self):
        self.temp.cleanup()

    def test_summary_uses_paired_ratios_not_ratio_of_unpaired_medians(self):
        result = PILOTS.compare(self.pairs)
        self.assertTrue(result["validation"]["passed"], result["validation"])
        summary = result["metric_summary"]["achieved_qps"]
        self.assertEqual(summary["on_over_off"]["median"], .9)
        self.assertEqual(summary["on_over_off"]["min"], .9)
        self.assertEqual(summary["on_over_off"]["max"], 1.1)
        self.assertNotEqual(summary["on_over_off"]["median"],
                            summary["on"]["median"] / summary["off"]["median"])
        self.assertAlmostEqual(result["throughput_reduction_pct"]["median"], 10)
        for kind in ("actual", "intended"):
            for percentile in PILOTS.PERCENTILES:
                self.assertAlmostEqual(result["metric_summary"][kind + "_" + percentile + "_us"]
                                       ["on_over_off"]["median"], 1.1)
        self.assertIn("120 seconds measured", PILOTS.markdown(result))
        self.assertTrue(any("not confidence intervals" in text for text in result["uncertainty"]))

    def test_modified_report_rejected_by_receipt(self):
        path = self.pairs[0][1] / "report.json"
        value = PILOTS.read(path)
        value["metrics"]["achieved_qps"] += 1
        write(path, value)
        result = PILOTS.compare(self.pairs)
        self.assertFalse(result["validation"]["passed"])
        self.assertEqual(result["valid_pairs"], 2)
        self.assertTrue(any("hash missing or mismatched for report.json" in error
                            for error in result["validation"]["errors"]))

    def test_mismatched_frequency_invalidates_pair_despite_valid_receipts(self):
        path = self.pairs[0][1]
        manifest = PILOTS.read(path / "run.json")
        manifest["selected_khz"] = 2000000
        write(path / "run.json", manifest)
        seal(path)
        result = PILOTS.compare(self.pairs)
        self.assertFalse(result["validation"]["passed"])
        self.assertEqual(result["valid_pairs"], 2)

    def test_only_two_pairs_is_not_a_complete_three_pair_result(self):
        result = PILOTS.compare(self.pairs[:2])
        self.assertFalse(result["validation"]["passed"])
        self.assertEqual(result["valid_pairs"], 2)

    def test_reusing_one_pair_cannot_impersonate_repetitions(self):
        result = PILOTS.compare([self.pairs[0]] * 3)
        self.assertFalse(result["validation"]["passed"])
        self.assertTrue(any("reused" in error for error in result["validation"]["errors"]))

    def test_protocol_requires_120_second_c_off_pilot(self):
        path = self.pairs[1][0]
        manifest = PILOTS.read(path / "run.json")
        manifest["measure_seconds"] = 600
        manifest["mode"] = "arm"
        write(path / "run.json", manifest)
        seal(path)
        result = PILOTS.compare(self.pairs)
        self.assertFalse(result["validation"]["passed"])
        self.assertTrue(any("measure_seconds" in error for error in result["validation"]["errors"]))

    def test_outputs_include_every_pair_and_full_latency_metrics(self):
        result = PILOTS.compare(self.pairs)
        output = self.root / "analysis"
        PILOTS.write_outputs(output, result)
        self.assertEqual(PILOTS.read(output / "collector-pilot-summary.json")["valid_pairs"], 3)
        self.assertIn("intended_p99.999_us", (output / "collector-pilot-pairs.csv").read_text())
        self.assertIn("actual_p90_us", (output / "collector-pilot-summary.md").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
