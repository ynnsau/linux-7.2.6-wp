#!/usr/bin/env python3
"""Validate counter frequency math, aligned ARM exposure, and report admission."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("report-redis-ycsb-fixedfreq.py")
SPEC = importlib.util.spec_from_file_location("fixedfreq_report", str(SCRIPT))
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def frequency_rows(times, cpus=(0, 1), labels=None, busy=None, mhz=None):
    labels = labels or {}
    busy = busy or {cpu: 1.0 for cpu in cpus}
    mhz = mhz or {cpu: 2100 for cpu in cpus}
    rows = [{"event": "frequency_config", "online_cpus": ",".join(map(str, cpus)),
             "cpu_count": len(cpus), "interval_ms": 11000}]
    for seq, second in enumerate(times, 1):
        for cpu in cpus:
            rows.append({"event": "frequency", "seq": seq, "label": labels.get(second, "periodic"),
                         "cpu": cpu, "monotonic_ns": int(second * 1e9),
                         "read_end_ns": int(second * 1e9 + 100),
                         "realtime_ns": int((second + 1790000000) * 1e9),
                         "time_enabled": int((second + 1) * 1e9),
                         "time_running": int((second + 1) * 1e9),
                         "aperf": int((second + 1) * mhz[cpu] * 1e6 * busy[cpu]),
                         "mperf": int((second + 1) * 2100 * 1e6 * busy[cpu]),
                         "tsc": int((second + 1) * 2100 * 1e6)})
    rows.append({"event": "frequency_exit", "exit_code": 0})
    return rows


class FixedFrequencyReportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-fixedfreq-report-")
        self.directory = Path(self.temp.name)
        self.path = self.directory / "frequency.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def frequency(self, rows, start=10, end=30, **kwargs):
        write_jsonl(self.path, rows)
        return REPORT.analyze_frequency(self.path, int(start * 1e9), int(end * 1e9),
                                         2100000, expected_cpus=[0, 1], **kwargs)

    def test_busy_frequency_and_explicit_boundary_skew(self):
        rows = frequency_rows([0, 10.001, 20, 30.003, 40],
                              labels={10.001: "measurement_start", 30.003: "measurement_end"},
                              busy={0: .25, 1: 1.0})
        result = self.frequency(rows, require_boundary_labels=True)
        self.assertTrue(result["validation"]["passed"], result["validation"])
        self.assertEqual(result["eligible_intervals"], 4)
        for interval in result["intervals"]:
            self.assertAlmostEqual(interval["busy_mhz"], 2100)
        self.assertAlmostEqual(result["cpus"]["0"]["start_offset_from_nominal_ms"], 1.00005)
        self.assertAlmostEqual(result["cpus"]["1"]["end_offset_from_nominal_ms"], 3.00005)
        self.assertAlmostEqual(result["cpus"]["0"]["observed_busy_fraction"]["median"], .25)

    def test_conservative_window_excludes_crossing_intervals(self):
        result = self.frequency(frequency_rows([0, 9, 11, 20, 29, 31, 40]))
        self.assertTrue(result["validation"]["passed"], result["validation"])
        self.assertEqual(result["cpus"]["0"]["intervals"], 2)
        self.assertEqual(result["cpus"]["0"]["excluded_intervals"], 4)
        self.assertEqual(result["cpus"]["0"]["boundary_source"], "nominal conservative window")

    def test_idle_zero_counters_are_not_frequency_failures(self):
        result = self.frequency(frequency_rows([10, 20, 29], busy={0: 0, 1: .01},
                                               mhz={0: 700, 1: 999}))
        self.assertTrue(result["validation"]["passed"], result["validation"])
        self.assertEqual(result["eligible_intervals"], 0)
        self.assertIsNone(result["cpus"]["0"]["frequency_within_requirement"])
        self.assertTrue(result["validation"]["warnings"])
        self.assertIsNone(result["intervals"][0]["busy_mhz"])

    def test_out_of_band_multiplex_and_missing_cpu_are_errors(self):
        rows = frequency_rows([10, 20, 29], mhz={0: 2100, 1: 2200})
        result = self.frequency(rows)
        self.assertFalse(result["validation"]["passed"])
        self.assertEqual(result["cpus"]["1"]["out_of_band_intervals"], 2)
        rows = frequency_rows([10, 20, 29])
        rows[1]["time_running"] -= 1
        rows = [row for row in rows if not (row.get("seq") == 2 and row.get("cpu") == 1)]
        result = self.frequency(rows)
        self.assertFalse(result["validation"]["passed"])
        self.assertTrue(any("Multiplexed" in error for error in result["validation"]["errors"]))
        self.assertTrue(any("missing or adds CPUs" in error for error in result["validation"]["errors"]))

    def test_boundary_labels_and_export_exit_required(self):
        rows = frequency_rows([10, 20, 29])[:-1]
        result = self.frequency(rows, require_boundary_labels=True)
        self.assertFalse(result["validation"]["passed"])
        self.assertTrue(any("boundary" in error for error in result["validation"]["errors"]))
        self.assertTrue(any("successful exit" in error for error in result["validation"]["errors"]))

    def test_short_noisy_boundary_interval_retained_but_not_stability_failure(self):
        # A periodic sample is only 10 ms after the boundary. Inject an APERF
        # excess into this interval, then retain the offset so subsequent full
        # intervals recover exactly 2100 MHz. The short interval measures 2600
        # MHz but must not decide the >=1-second stability requirement.
        rows = frequency_rows([10, 10.01, 20, 30],
                              labels={10: "measurement_start", 30: "measurement_end"})
        for row in rows:
            if row.get("event") == "frequency" and row["seq"] >= 2:
                row["aperf"] += 5000000
        result = self.frequency(rows, require_boundary_labels=True)
        self.assertTrue(result["validation"]["passed"], result["validation"])
        self.assertEqual(result["short_intervals"], 2)
        self.assertEqual(result["eligible_intervals"], 4)
        self.assertEqual(result["out_of_band_intervals"], 0)
        short = [row for row in result["intervals"] if row["short_interval"]]
        self.assertEqual(len(short), 2)
        self.assertAlmostEqual(short[0]["busy_mhz"], 2600)
        self.assertFalse(short[0]["observed_in_band"])
        self.assertIsNone(short[0]["in_band"])
        self.assertFalse(short[0]["eligible"])
        self.assertIn("counter-read timing", short[0]["stability_exclusion_reason"])
        self.assertEqual(result["cpus"]["0"]["short_out_of_band_intervals_diagnostic_only"], 1)

    def test_one_second_real_drift_still_fails(self):
        rows = frequency_rows([10, 11, 20, 30],
                              labels={10: "measurement_start", 30: "measurement_end"})
        for row in rows:
            if row.get("event") == "frequency" and row["seq"] >= 2:
                row["aperf"] += 500000000
        result = self.frequency(rows, require_boundary_labels=True)
        self.assertFalse(result["validation"]["passed"])
        self.assertEqual(result["short_intervals"], 0)
        self.assertEqual(result["out_of_band_intervals"], 2)
        self.assertEqual(result["eligible_intervals"], 6)
        drift = next(row for row in result["intervals"] if row["end_seq"] == 2)
        self.assertEqual(drift["seconds"], 1)
        self.assertAlmostEqual(drift["busy_mhz"], 2600)
        self.assertTrue(drift["eligible"])
        self.assertFalse(drift["in_band"])

    def test_short_intervals_do_not_bypass_counter_integrity_checks(self):
        rows = frequency_rows([10, 10.01, 20, 30],
                              labels={10: "measurement_start", 30: "measurement_end"})
        next(row for row in rows if row.get("event") == "frequency" and
             row["seq"] == 2 and row["cpu"] == 0)["time_running"] -= 1
        result = self.frequency(rows, require_boundary_labels=True)
        self.assertFalse(result["validation"]["passed"])
        self.assertTrue(any("Multiplexed" in error for error in result["validation"]["errors"]))

    def test_fixed_policy_retains_outliers_but_cannot_claim_qualification(self):
        rows = frequency_rows([10, 20, 29], mhz={0: 2000, 1: 2100})
        strict = self.frequency(rows)
        self.assertFalse(strict["validation"]["passed"])
        self.assertTrue(strict["counter_integrity"]["passed"])
        self.assertFalse(strict["runtime_frequency"]["passed"])
        policy = self.frequency(rows, frequency_contract="fixed-policy")
        self.assertTrue(policy["validation"]["passed"], policy["validation"])
        self.assertFalse(policy["runtime_frequency"]["passed"])
        self.assertFalse(policy["runtime_frequency"]["affects_raw_admission"])
        self.assertFalse(policy["policy_and_qualification_verified"])
        self.assertEqual(policy["out_of_band_intervals"], strict["out_of_band_intervals"])
        self.assertEqual(policy["intervals"], strict["intervals"])
        self.assertTrue(any("outside band" in warning for warning in policy["validation"]["warnings"]))
        self.assertTrue(any("does not verify policy" in warning for warning in policy["validation"]["warnings"]))

    def test_fixed_policy_still_rejects_counter_corruption(self):
        rows = frequency_rows([10, 20, 29], mhz={0: 2000, 1: 2100})
        rows[1]["time_running"] -= 1
        result = self.frequency(rows, frequency_contract="fixed-policy")
        self.assertFalse(result["counter_integrity"]["passed"])
        self.assertFalse(result["validation"]["passed"])

    def test_nominal_hardware_base_is_independent_of_lower_policy_target(self):
        rows = frequency_rows([10, 20, 29], mhz={0: 2000, 1: 2000})
        # Deliberately distinguish measured TSC from hardware nominal base.
        for row in rows:
            if row.get("event") == "frequency":
                row["tsc"] = round(row["tsc"] * 2095 / 2100)
        write_jsonl(self.path, rows)
        result = REPORT.analyze_frequency(self.path, 10 * REPORT.NSEC, 30 * REPORT.NSEC,
                                         2000000, [0, 1], nominal_base_khz=2100000)
        self.assertTrue(result["validation"]["passed"], result["validation"])
        for interval in result["intervals"]:
            self.assertAlmostEqual(interval["busy_mhz"], 2000)
            self.assertAlmostEqual(interval["tsc_mhz"], 2095)
            self.assertAlmostEqual(interval["measured_tsc_busy_mhz"], 2095 * 2000 / 2100)
            self.assertAlmostEqual(interval["busy_fraction"], 2100 / 2095)
        self.assertEqual(result["selected_khz"], 2000000)
        self.assertEqual(result["nominal_base_khz"], 2100000)

    def make_run(self, path, mode="arm"):
        path.mkdir()
        start, end = 10, 30
        timeline = {"start_monotonic": start, "end_monotonic": end,
                    "start_ms": 10000, "end_ms": 30000}
        manifest = {"workload": "B", "mode": mode, "workers": 8, "target_qps": 70000,
                    "selected_khz": 2100000, "telemetry": True,
                    "policy": {"boot_id": "fixture-boot", "governors": {"0": "performance", "1": "performance"}}}
        capture = {"frequency_telemetry": True}
        counters = {"mode": mode, "first_arms": 5, "rearms": 2, "arm_events": 7,
                    "wp_events": 3, "passes_completed": 2,
                    "protected_byte_ms": 2 * REPORT.GIB * 20000,
                    "active_start_pages": 2 * REPORT.GIB // 4096,
                    "active_end_pages": 3 * REPORT.GIB // 4096,
                    "status_window_ns": [10 * REPORT.NSEC, 30 * REPORT.NSEC],
                    "flush_calls": 100, "shared_flush_calls": 20, "dirty_only_flush_calls": 70,
                    "wp_only_flush_calls": 10, "write_ns": 15000, "write_calls": 5,
                    "flush_ns": 250000000, "resource_failures": 0, "reports_dropped": 0,
                    "tlb_shootdown_interrupts": {"redis_cpu1": 80, "all_host": 100}}
        if mode == "off":
            counters.update({key: 0 for key in ("first_arms", "rearms", "arm_events", "wp_events",
                                                "active_start_pages", "active_end_pages", "flush_calls",
                                                "shared_flush_calls", "dirty_only_flush_calls", "wp_only_flush_calls")})
        lat = {"count": 100, "percentiles_us": {p: 20 for p in REPORT.PERCENTILES}, "qps": 65000}
        latency = {"validation": {"passed": True, "complete_export_verified": True},
                   "combined": {"actual": lat, "intended": lat},
                   "streams": {"READ": {"successful_workload_stream": True, "latency": lat}},
                   "per_second": []}
        for name, value in (("run.json", manifest), ("capture-summary.json", capture),
                            ("timeline.json", timeline), ("counters.json", counters), ("latency.json", latency)):
            REPORT.save(path / name, value)
        observations = []
        for second in (9, 10, 15, 20, 29, 30):
            row = {"clock": {"monotonic_ns": second * REPORT.NSEC}, "selected_khz": 2100000}
            for name, multiplier in (("redis", 1), ("client", 2)):
                row[name] = {"rss_bytes": 16 * REPORT.GIB, "cpu_s": second * multiplier,
                             "minor_faults": second * 10, "major_faults": 0, "vm_swap_kib": 0}
            observations.append(row)
        write_jsonl(path / "observations.jsonl", observations)
        write_jsonl(path / "frequency.jsonl", frequency_rows([0, 10, 20, 30, 40],
                    labels={10: "measurement_start", 30: "measurement_end"}))
        return path

    def add_fixed_policy_evidence(self, path):
        manifest = REPORT.read_json(path / "run.json")
        manifest.update({"frequency_contract": "fixed-policy", "nominal_base_khz": 2100000})
        REPORT.save(path / "run.json", manifest)
        capture = REPORT.read_json(path / "capture-summary.json")
        capture.update({"selected_khz": 2100000, "frequency_policy_verified_start": True,
                        "frequency_policy_verified_end": True})
        REPORT.save(path / "capture-summary.json", capture)
        profile = {"status": "qualified_locked", "selected_khz": 2100000, "boot_id": "fixture-boot",
                   "attempts": [{"status": "passed", "selected_khz": 2100000, "seconds": 1800,
                                 "failed_cpus": [], "per_cpu": {"0": {}, "1": {}}}]}
        REPORT.save(path / "frequency-profile.json", profile)

    def test_run_exact_kernel_exposure_hook_scope_and_sampled_cpu(self):
        path = self.make_run(self.directory / "B-arm-r1")
        result = REPORT.report_run(path)
        self.assertTrue(result["validation"]["passed"], result["validation"])
        self.assertEqual(result["arm"]["exposure_gib_seconds"], 40)
        self.assertEqual(result["arm"]["live_gib_mean"], 2)
        self.assertEqual(result["arm"]["live_gib_end"], 3)
        self.assertEqual(result["arm"]["coverage_of_redis_rss_mean"], 2 / 16)
        self.assertEqual(result["processes"]["redis"]["sampled_wall_seconds"], 19)
        self.assertEqual(result["metrics"]["redis_cpu_percent"], 100)
        self.assertEqual(result["metrics"]["client_cpu_percent_of_eight_cores"], 25)
        self.assertEqual(result["metrics"]["cmdp_write_hook_mean_us"], 3)
        self.assertAlmostEqual(result["metrics"]["target_shortfall_pct"], 100 / 14)

    def test_run_bad_flush_accounting_and_raw_validation_remain_invalid(self):
        path = self.make_run(self.directory / "B-arm-r1")
        counters = REPORT.read_json(path / "counters.json")
        counters["flush_calls"] += 1
        REPORT.save(path / "counters.json", counters)
        result = REPORT.report_run(path)
        self.assertFalse(result["validation"]["passed"])
        self.assertTrue(any("flush accounting" in error for error in result["validation"]["errors"]))

    def test_fixed_policy_run_requires_qualification_and_policy_attestations(self):
        path = self.make_run(self.directory / "B-arm-r1")
        self.add_fixed_policy_evidence(path)
        write_jsonl(path / "frequency.jsonl", frequency_rows([0, 10, 20, 30, 40],
                    labels={10: "measurement_start", 30: "measurement_end"}, mhz={0: 2000, 1: 2100}))
        result = REPORT.report_run(path)
        self.assertTrue(result["validation"]["passed"], result["validation"])
        self.assertTrue(result["frequency_contract_evidence"]["validation"]["passed"])
        self.assertFalse(result["frequency"]["runtime_frequency"]["passed"])
        self.assertEqual(result["frequency_contract_evidence"]["loaded_qualification"]["seconds"], 1800)
        (path / "frequency-profile.json").unlink()
        result = REPORT.report_run(path)
        self.assertFalse(result["validation"]["passed"])
        self.assertTrue(any("lacks copied frequency-profile" in error for error in result["validation"]["errors"]))

    def test_fixed_policy_rejects_wrong_boot_short_profile_or_missing_attestation(self):
        path = self.make_run(self.directory / "B-arm-r1")
        self.add_fixed_policy_evidence(path)
        original = REPORT.read_json(path / "frequency-profile.json")
        profile = copy.deepcopy(original)
        profile["boot_id"] = "different-boot"
        REPORT.save(path / "frequency-profile.json", profile)
        self.assertFalse(REPORT.report_run(path)["validation"]["passed"])
        profile = copy.deepcopy(original)
        profile["attempts"][0]["seconds"] = 1799
        REPORT.save(path / "frequency-profile.json", profile)
        self.assertFalse(REPORT.report_run(path)["validation"]["passed"])
        REPORT.save(path / "frequency-profile.json", original)
        capture = REPORT.read_json(path / "capture-summary.json")
        capture["frequency_policy_verified_end"] = False
        REPORT.save(path / "capture-summary.json", capture)
        result = REPORT.report_run(path)
        self.assertFalse(result["validation"]["passed"])
        self.assertTrue(any("verified_end" in error for error in result["validation"]["errors"]))

    def test_campaign_retains_invalid_attempt_and_requires_three_valid_repetitions(self):
        path = self.make_run(self.directory / "B-arm-r1")
        valid = REPORT.report_run(path)
        REPORT.save(path / "report.json", valid)
        invalid = copy.deepcopy(valid)
        invalid["run_name"] = "B-arm-r2"
        invalid["validation"] = {"passed": False, "errors": ["fixture failure"], "warnings": []}
        REPORT.save(self.directory / "B-arm-r2" / "report.json", invalid)
        campaign = REPORT.aggregate_campaign(self.directory)
        self.assertFalse(campaign["validation"]["passed"])
        group = next(group for group in campaign["groups"] if group["workload"] == "B" and group["mode"] == "arm")
        self.assertEqual(group["metrics"]["achieved_qps"]["n"], 1)
        self.assertEqual(group["excluded_invalid_runs"], ["B-arm-r2"])
        self.assertIn("incomplete or failed", REPORT.markdown_report(campaign))


if __name__ == "__main__":
    unittest.main(verbosity=2)
