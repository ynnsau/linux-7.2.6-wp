#!/usr/bin/env python3
"""Offline frequency validation and reports for fixed-frequency Redis/YCSB.

Standard library only for analysis. --plots imports matplotlib only when asked.
No experiment, process, frequency-policy, or raw-evidence mutations are made.
"""
import argparse
import collections
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys


GIB = 1024 ** 3
NSEC = 1000000000
PERCENTILES = ("p90", "p99", "p99.9", "p99.99", "p99.999")
WORKLOADS = ("A", "B", "C")
MODES = ("off", "sampling", "arm")


def read_json(path):
    with open(str(path)) as source:
        return json.load(source)


def read_jsonl(path):
    with open(str(path)) as source:
        for number, line in enumerate(source, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError as exc:
                    raise ValueError("%s:%d: %s" % (path, number, exc))


def digest(path):
    h = hashlib.sha256()
    with open(str(path), "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return {"path": str(path), "size_bytes": Path(path).stat().st_size,
            "sha256": h.hexdigest()}


def save(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(str(temporary), "w") as output:
        json.dump(result, output, sort_keys=True, indent=2, allow_nan=False)
        output.write("\n")
    temporary.replace(path)


def cpulist(value):
    cpus = set()
    for part in value.split(","):
        if not part:
            continue
        ends = [int(n) for n in part.split("-")]
        if len(ends) > 2 or ends[-1] < ends[0] or ends[0] < 0:
            raise ValueError("invalid CPU list")
        cpus.update(range(ends[0], ends[-1] + 1))
    if not cpus:
        raise ValueError("empty CPU list")
    return sorted(cpus)


def spread(values):
    values = [value for value in values if value is not None]
    if not values:
        return {"n": 0, "median": None, "min": None, "max": None}
    return {"n": len(values), "median": statistics.median(values),
            "min": min(values), "max": max(values)}


def analyze_frequency(path, start_ns, end_ns, selected_khz, expected_cpus=None,
                      min_busy_fraction=0.05, tolerance_fraction=0.01,
                      required_good_fraction=0.99, require_boundary_labels=False,
                      min_interval_seconds=1.0):
    if end_ns <= start_ns or selected_khz <= 0:
        raise ValueError("invalid frequency window or target")
    if min_interval_seconds <= 0:
        raise ValueError("minimum frequency interval must be positive")
    rows = list(read_jsonl(path))
    errors = []
    warnings = []
    configs = [row for row in rows if row.get("event") == "frequency_config"]
    if len(configs) != 1:
        errors.append("Expected exactly one frequency_config")
    config = configs[0] if configs else {}
    configured_cpus = cpulist(config["online_cpus"]) if config.get("online_cpus") else []
    expected_cpus = sorted(expected_cpus if expected_cpus is not None else configured_cpus)
    if not expected_cpus:
        errors.append("No expected CPUs configured")
    if configured_cpus != expected_cpus:
        errors.append("Collector CPU set differs from expected CPU set")
    if config.get("cpu_count") != len(expected_cpus):
        errors.append("Collector cpu_count differs from expected CPU count")
    exits = [row for row in rows if row.get("event") == "frequency_exit"]
    if len(exits) != 1 or exits[0].get("exit_code") != 0:
        errors.append("Collector lacks a unique successful exit")
    for row in rows:
        if row.get("event") == "boundary_end" and row.get("ok") is False:
            errors.append("Collector boundary failed: %s" % row.get("label"))
    groups = collections.defaultdict(dict)
    by_cpu = collections.defaultdict(list)
    labels = collections.defaultdict(dict)
    for row in rows:
        if row.get("event") != "frequency":
            continue
        cpu, seq = int(row["cpu"]), int(row["seq"])
        if cpu in groups[seq]:
            errors.append("Duplicate CPU%d in frequency sequence %d" % (cpu, seq))
        groups[seq][cpu] = row
        by_cpu[cpu].append(row)
        label = row.get("label")
        if label in ("measurement_start", "measurement_end"):
            if cpu in labels[label]:
                errors.append("Repeated %s sample for CPU%d" % (label, cpu))
            labels[label][cpu] = row
        if row["read_end_ns"] < row["monotonic_ns"]:
            errors.append("Negative counter-read duration on CPU%d sequence %d" % (cpu, seq))
        if row["time_enabled"] != row["time_running"] or row["time_running"] <= 0:
            errors.append("Multiplexed or unscheduled counters on CPU%d sequence %d" % (cpu, seq))
    for seq, group in sorted(groups.items()):
        if sorted(group) != expected_cpus:
            errors.append("Frequency sequence %d is missing or adds CPUs" % seq)
        if len(set(row.get("label") for row in group.values())) != 1:
            errors.append("Frequency sequence %d has inconsistent labels" % seq)
    if require_boundary_labels and any(sorted(labels[label]) != expected_cpus
            for label in ("measurement_start", "measurement_end")):
        errors.append("Measurement boundary counter samples are incomplete")
    if not groups:
        errors.append("No frequency samples")

    target_mhz = selected_khz / 1000
    cpu_reports = {}
    all_intervals = []
    for cpu in expected_cpus:
        samples = by_cpu.get(cpu, [])
        begin = labels["measurement_start"].get(cpu)
        end = labels["measurement_end"].get(cpu)
        explicit = begin is not None and end is not None
        if explicit:
            lower = (begin["monotonic_ns"] + begin["read_end_ns"]) / 2
            upper = (end["monotonic_ns"] + end["read_end_ns"]) / 2
            if upper <= lower:
                errors.append("Nonpositive boundary interval on CPU%d" % cpu)
        else:
            lower, upper = start_ns, end_ns
        intervals = []
        excluded = 0
        invalid = 0
        for previous, current in zip(samples, samples[1:]):
            a = (previous["monotonic_ns"] + previous["read_end_ns"]) / 2
            z = (current["monotonic_ns"] + current["read_end_ns"]) / 2
            if current["seq"] <= previous["seq"] or z <= a:
                errors.append("Nonmonotonic samples on CPU%d" % cpu)
                invalid += 1
                continue
            # Explicit boundary snapshots define the measured counter window.
            # Without them, require both complete reads inside the nominal window.
            inside = (a >= lower and z <= upper) if explicit else (
                previous["monotonic_ns"] >= lower and current["read_end_ns"] <= upper)
            if not inside:
                excluded += 1
                continue
            dt = (z - a) / NSEC
            delta = {key: current[key] - previous[key]
                     for key in ("aperf", "mperf", "tsc", "time_enabled", "time_running")}
            if (any(value < 0 for value in delta.values()) or delta["tsc"] <= 0 or
                    delta["time_running"] <= 0 or
                    delta["time_enabled"] != delta["time_running"]):
                errors.append("Invalid/multiplexed counter delta on CPU%d sequence %d" %
                              (cpu, current["seq"]))
                invalid += 1
                continue
            busy = delta["mperf"] / delta["tsc"]
            mhz = (delta["tsc"] / dt) * (delta["aperf"] / delta["mperf"]) / 1e6 \
                if delta["mperf"] else None
            long_enough = dt >= min_interval_seconds
            eligible = long_enough and busy >= min_busy_fraction and mhz is not None
            observed_in_band = abs(mhz / target_mhz - 1) <= tolerance_fraction if mhz is not None else None
            in_band = observed_in_band if eligible else None
            exclusion_reason = None
            if not long_enough:
                exclusion_reason = "interval shorter than %.3g seconds; counter-read timing uncertainty" % min_interval_seconds
            elif busy < min_busy_fraction or mhz is None:
                exclusion_reason = "idle or below %.3g%% busy" % (min_busy_fraction * 100)
            if busy > 1.05:
                errors.append("Implausible busy fraction on CPU%d sequence %d" % (cpu, current["seq"]))
                invalid += 1
            interval = {"cpu": cpu, "start_seq": previous["seq"], "end_seq": current["seq"],
                        "start_ns": a, "end_ns": z, "seconds": dt,
                        "relative_end_seconds": (z - start_ns) / NSEC,
                        "busy_fraction": busy, "busy_mhz": mhz,
                        "tsc_mhz": delta["tsc"] / dt / 1e6,
                        "eligible": eligible, "in_band": in_band,
                        "observed_in_band": observed_in_band,
                        "short_interval": not long_enough,
                        "stability_exclusion_reason": exclusion_reason,
                        "aperf_delta": delta["aperf"], "mperf_delta": delta["mperf"]}
            intervals.append(interval)
            all_intervals.append(interval)
        eligible = [row for row in intervals if row["eligible"]]
        bad = [row for row in eligible if not row["in_band"]]
        short = [row for row in intervals if row["short_interval"]]
        good_fraction = (len(eligible) - len(bad)) / len(eligible) if eligible else None
        eligible_duration = sum(row["seconds"] for row in eligible)
        bad_duration = sum(row["seconds"] for row in bad)
        frequency_good = good_fraction is None or good_fraction >= required_good_fraction
        if not frequency_good:
            errors.append("CPU%d frequency outside band in %d/%d eligible intervals" %
                          (cpu, len(bad), len(eligible)))
        if not intervals:
            errors.append("CPU%d has no valid intervals inside measurement window" % cpu)
        cpu_reports[str(cpu)] = {
            "boundary_source": "explicit per-CPU snapshots" if explicit else "nominal conservative window",
            "start_offset_from_nominal_ms": (lower - start_ns) / 1e6,
            "end_offset_from_nominal_ms": (upper - end_ns) / 1e6,
            "effective_start_ns": lower, "effective_end_ns": upper,
            "intervals": len(intervals), "excluded_intervals": excluded, "invalid_intervals": invalid,
            "short_intervals": len(short),
            "short_interval_seconds": sum(row["seconds"] for row in short),
            "short_out_of_band_intervals_diagnostic_only": sum(row["observed_in_band"] is False for row in short),
            "idle_or_low_busy_intervals": len(intervals) - len(eligible) - len(short),
            "eligible_intervals": len(eligible), "out_of_band_intervals": len(bad),
            "good_fraction": good_fraction,
            "eligible_seconds": eligible_duration, "out_of_band_seconds": bad_duration,
            "good_duration_fraction": (1 - bad_duration / eligible_duration) if eligible_duration else None,
            "eligible_busy_mhz": spread([row["busy_mhz"] for row in eligible]),
            "observed_busy_fraction": spread([row["busy_fraction"] for row in intervals]),
            "frequency_within_requirement": frequency_good if eligible else None,
            "assessment": "eligible" if eligible else "no sufficiently long, busy interval; no run-time frequency assertion",
        }
    unexercised = [int(cpu) for cpu, report in cpu_reports.items() if not report["eligible_intervals"]]
    if unexercised:
        warnings.append("No eligible busy intervals on CPUs %s; all-core qualification supplies their loaded evidence" %
                        ",".join(map(str, unexercised)))
    errors = list(dict.fromkeys(errors))
    return {"schema_version": 1, "input": digest(path), "selected_khz": selected_khz,
            "window_ns": [start_ns, end_ns], "expected_cpus": expected_cpus,
            "method": {"busy_mhz": "delta_TSC / delta_midpoint_time_seconds * delta_APERF / delta_MPERF / 1e6",
                       "busy_fraction": "delta_MPERF / delta_TSC", "minimum_busy_fraction": min_busy_fraction,
                       "minimum_interval_seconds": min_interval_seconds,
                       "short_intervals_retained_but_excluded_from_stability_decision": True,
                       "frequency_tolerance_fraction": tolerance_fraction,
                       "required_good_interval_fraction_per_cpu": required_good_fraction,
                       "explicit_boundaries_report_their_offset_from_latency_window": True,
                       "idle_intervals_are_not_frequency_failures": True,
                       "counter_multiplexing_allowed": False},
            "validation": {"passed": not errors, "errors": errors, "warnings": warnings},
            "cpus": cpu_reports, "intervals": all_intervals,
            "short_intervals": sum(row["short_intervals"] for row in cpu_reports.values()),
            "eligible_intervals": sum(row["eligible_intervals"] for row in cpu_reports.values()),
            "out_of_band_intervals": sum(row["out_of_band_intervals"] for row in cpu_reports.values())}


def process_summary(observations, process):
    if len(observations) < 2:
        return {"samples": len(observations), "cpu_percent_one_core": None,
                "cpu_percent_eight_core_pool": None, "sampled_wall_seconds": None,
                "rss_gib": spread([row[process]["rss_bytes"] / GIB for row in observations])}
    first, last = observations[0], observations[-1]
    seconds = (last["clock"]["monotonic_ns"] - first["clock"]["monotonic_ns"]) / NSEC
    cpu_seconds = last[process]["cpu_s"] - first[process]["cpu_s"]
    percent = cpu_seconds * 100 / seconds if seconds > 0 else None
    return {"samples": len(observations), "sampled_wall_seconds": seconds,
            "first_monotonic_ns": first["clock"]["monotonic_ns"],
            "last_monotonic_ns": last["clock"]["monotonic_ns"],
            "cpu_seconds": cpu_seconds, "cpu_percent_one_core": percent,
            "cpu_percent_eight_core_pool": percent / 8 if percent is not None else None,
            "rss_gib": spread([row[process]["rss_bytes"] / GIB for row in observations]),
            "rss_gib_mean_sampled": statistics.mean([row[process]["rss_bytes"] / GIB for row in observations]),
            "minor_faults": last[process]["minor_faults"] - first[process]["minor_faults"],
            "major_faults": last[process]["major_faults"] - first[process]["major_faults"]}


def report_run(directory, expected_cpus=None):
    directory = Path(directory)
    capture = read_json(directory / "capture-summary.json")
    manifest = read_json(directory / "run.json")
    timeline = read_json(directory / "timeline.json")
    counters = read_json(directory / "counters.json")
    latency = read_json(directory / "latency.json")
    errors = []
    warnings = []
    if not latency["validation"]["passed"]:
        errors.extend("Latency: " + error for error in latency["validation"]["errors"])
    if not latency["validation"].get("complete_export_verified"):
        errors.append("Latency raw export completeness was not verified")
    start_ns = int(round(timeline["start_monotonic"] * NSEC))
    end_ns = int(round(timeline["end_monotonic"] * NSEC))
    observations = [row for row in read_jsonl(directory / "observations.jsonl")
                    if start_ns <= row["clock"]["monotonic_ns"] < end_ns]
    observations.sort(key=lambda row: row["clock"]["monotonic_ns"])
    if len(observations) < 2:
        errors.append("Fewer than two process observations in measurement window")
    for row in observations:
        if row.get("selected_khz") != manifest["selected_khz"]:
            errors.append("Frequency policy changed in process observation")
        if any(row[process].get("vm_swap_kib", 0) for process in ("redis", "client")):
            errors.append("Process swapped during measurement window")
    processes = {name: process_summary(observations, name) for name in ("redis", "client")}
    for name, row in processes.items():
        if row.get("cpu_seconds", 0) < 0:
            errors.append("Negative %s CPU-time delta" % name)
    if expected_cpus is None:
        expected_cpus = [int(cpu) for cpu in manifest.get("policy", {}).get("governors", {})] or list(range(52))
    frequency = None
    if capture.get("frequency_telemetry", manifest.get("telemetry", True)):
        frequency = analyze_frequency(directory / "frequency.jsonl", start_ns, end_ns,
                                      manifest["selected_khz"], expected_cpus,
                                      require_boundary_labels=True)
        errors.extend("Frequency: " + error for error in frequency["validation"]["errors"])
        warnings.extend(frequency["validation"]["warnings"])
    else:
        warnings.append("Frequency telemetry disabled for this explicit collector-overhead pilot")
    mode = manifest["mode"]
    arm = {"page_size_bytes": 4096, "exposure_gib_seconds": 0,
           "live_gib_mean": 0, "live_gib_start": 0, "live_gib_end": 0,
           "status_window_seconds": (end_ns - start_ns) / NSEC,
           "status_window_ns": [start_ns, end_ns], "coverage_of_redis_rss_mean": 0}
    if mode != "off":
        status_start, status_end = counters["status_window_ns"]
        duration = (status_end - status_start) / NSEC
        if duration <= 0:
            errors.append("Nonpositive CMD-P status window")
        exposure = counters["protected_byte_ms"] / (GIB * 1000)
        mean_gib = exposure / duration if duration > 0 else None
        rss_mean = processes["redis"].get("rss_gib_mean_sampled")
        arm.update({"exposure_gib_seconds": exposure, "live_gib_mean": mean_gib,
                    "live_gib_start": counters["active_start_pages"] * 4096 / GIB,
                    "live_gib_end": counters["active_end_pages"] * 4096 / GIB,
                    "status_window_seconds": duration,
                    "status_window_ns": counters["status_window_ns"],
                    "status_start_offset_from_latency_ms": (status_start - start_ns) / 1e6,
                    "status_end_offset_from_latency_ms": (status_end - end_ns) / 1e6,
                    "coverage_of_redis_rss_mean": mean_gib / rss_mean if rss_mean else None})
        if counters["flush_calls"] != sum(counters.get(key, 0) for key in
                ("shared_flush_calls", "dirty_only_flush_calls", "wp_only_flush_calls")):
            errors.append("Shared/dirty-only/ARM-only flush accounting does not sum")
        if counters["arm_events"] != counters["first_arms"] + counters["rearms"]:
            errors.append("First-ARM/re-ARM event accounting does not sum")
    for key in ("flush_calls", "arm_events", "wp_events", "protected_byte_ms",
                "write_calls", "write_ns", "resource_failures", "reports_dropped"):
        if counters.get(key, 0) < 0:
            errors.append("Negative %s counter delta" % key)
    if counters.get("resource_failures", 0) or counters.get("reports_dropped", 0):
        errors.append("CMD-P resource failures or dropped reports")
    if mode in ("off", "sampling") and any(counters.get(key, 0) for key in
                                           ("arm_events", "wp_events", "active_end_pages")):
        errors.append("ARM/WP activity occurred outside ARM mode")
    arm_series = []
    if (directory / "cmdp.jsonl").exists():
        for row in read_jsonl(directory / "cmdp.jsonl"):
            if ("active" in row and "terminals" in row and
                    start_ns <= row.get("now_ns", 0) <= end_ns):
                arm_series.append({"seconds": (row["now_ns"] - start_ns) / NSEC,
                                   "live_arm_gib": row["active"] * 4096 / GIB,
                                   "first_arms": row.get("first_arms"), "rearms": row.get("rearms"),
                                   "wp_events_cumulative": row["terminals"]["write"]})
    actual_qps = latency["combined"]["actual"]["qps"]
    target = manifest["target_qps"]
    latency_summary = {}
    for name, stream in latency["streams"].items():
        if stream["successful_workload_stream"]:
            latency_summary[name] = stream["latency"]
    latency_summary["combined-actual"] = latency["combined"]["actual"]
    latency_summary["combined-intended"] = latency["combined"]["intended"]
    metric = {"achieved_qps": actual_qps,
              "target_shortfall_pct": max(0, 100 * (target - actual_qps) / target),
              "target_attainment_pct": actual_qps * 100 / target,
              "redis_cpu_percent": processes["redis"]["cpu_percent_one_core"],
              "client_cpu_percent_of_eight_cores": processes["client"]["cpu_percent_eight_core_pool"],
              "redis_rss_gib_peak": processes["redis"]["rss_gib"]["max"],
              "client_rss_gib_peak": processes["client"]["rss_gib"]["max"],
              "arm_live_gib_mean": arm["live_gib_mean"], "arm_live_gib_end": arm["live_gib_end"],
              "arm_exposure_gib_seconds": arm["exposure_gib_seconds"],
              "arm_coverage_of_redis_rss_mean": arm["coverage_of_redis_rss_mean"],
              "first_arms": counters.get("first_arms", 0), "rearms": counters.get("rearms", 0),
              "arm_events": counters["arm_events"], "wp_events": counters["wp_events"],
              "flush_calls": counters["flush_calls"],
              "flush_seconds": counters.get("flush_ns", 0) / NSEC,
              "cmdp_write_hook_calls": counters.get("write_calls", 0),
              "cmdp_write_hook_seconds": counters.get("write_ns", 0) / NSEC,
              "cmdp_write_hook_mean_us": counters.get("write_ns", 0) /
                  (1000 * counters["write_calls"]) if counters.get("write_calls") else None,
              "tlb_shootdown_ipis_redis_cpu1": counters["tlb_shootdown_interrupts"]["redis_cpu1"],
              "tlb_shootdown_ipis_host": counters["tlb_shootdown_interrupts"]["all_host"],
              "passes_completed": counters["passes_completed"],
              "frequency_eligible_intervals": frequency["eligible_intervals"] if frequency else None,
              "frequency_short_intervals": frequency["short_intervals"] if frequency else None,
              "frequency_out_of_band_intervals": frequency["out_of_band_intervals"] if frequency else None}
    sources = ("run.json", "timeline.json", "capture-summary.json", "latency.json",
               "counters.json", "observations.jsonl")
    return {"schema_version": 1, "directory": str(directory), "run_name": directory.name,
            "workload": manifest["workload"], "mode": mode, "workers": manifest["workers"],
            "target_qps": target, "selected_khz": manifest["selected_khz"],
            "timeline": timeline, "metrics": metric, "latency": latency_summary,
            "processes": processes, "arm": arm, "counters": counters,
            "frequency": frequency, "arm_time_series": arm_series,
            "latency_time_series": latency["per_second"],
            "measurement_notes": ["Percentiles use every retained raw record, not a sampled latency stream.",
                                  "Intended latency includes delay against YCSB's bounded-worker request schedule.",
                                  "CPU percentages use first/last process observations inside the window, with actual wall time.",
                                  "ARM exposure integrates active pages in the kernel; its exact status-window mean is reported.",
                                  "WP events count write-terminated ARM generations; write-hook time is not complete page-fault time.",
                                  "Scanner flush calls and host TLB shootdown interrupts are distinct counters.",
                                  "Frequency sampling validates intervals at least 1 second long and at least 5% busy; shorter intervals remain diagnostic.",
                                  "Idle-core loaded frequency behavior comes from the separate all-core qualification."],
            "sources": {name: digest(directory / name) for name in sources},
            "validation": {"passed": not errors, "errors": list(dict.fromkeys(errors)),
                           "warnings": list(dict.fromkeys(warnings))}}


def aggregate_campaign(directory, expected_repetitions=3):
    directory = Path(directory)
    paths = sorted(directory.glob("*/report.json"))
    reports = [read_json(path) for path in paths]
    errors = []
    groups = []
    for report in reports:
        if not report["validation"]["passed"]:
            errors.append("Invalid run retained: %s" % report["run_name"])
    for workload in WORKLOADS:
        for mode in MODES:
            selected = [report for report in reports
                        if report["workload"] == workload and report["mode"] == mode]
            valid = [report for report in selected if report["validation"]["passed"]]
            if len(valid) != expected_repetitions:
                errors.append("%s/%s: %d valid runs, expected %d" %
                              (workload, mode, len(valid), expected_repetitions))
            keys = set(key for report in valid for key in report["metrics"])
            streams = set(key for report in valid for key in report["latency"])
            metrics = {key: spread([report["metrics"].get(key) for report in valid]) for key in keys}
            latency = {}
            for stream in sorted(streams):
                latency[stream] = {percentile: spread([report["latency"].get(stream, {})
                    .get("percentiles_us", {}).get(percentile) for report in valid])
                    for percentile in PERCENTILES}
                latency[stream]["sample_count"] = spread([report["latency"].get(stream, {})
                                                          .get("count") for report in valid])
            groups.append({"workload": workload, "mode": mode,
                           "valid_runs": [report["run_name"] for report in valid],
                           "excluded_invalid_runs": [report["run_name"] for report in selected
                                                     if not report["validation"]["passed"]],
                           "metrics": metrics, "latency_us": latency})
    if len(reports) != len(WORKLOADS) * len(MODES) * expected_repetitions:
        errors.append("Campaign has %d report files, expected %d" %
                      (len(reports), len(WORKLOADS) * len(MODES) * expected_repetitions))
    configurations = {(report["workers"], report["target_qps"], report["selected_khz"])
                      for report in reports}
    if len(configurations) != 1:
        errors.append("Campaign reports do not share one worker/target/frequency configuration")
    return {"schema_version": 1, "directory": str(directory),
            "expected_repetitions": expected_repetitions,
            "aggregation": "Median and min/max of per-run values; no averaging of percentile populations",
            "validation": {"passed": not errors, "errors": errors},
            "groups": groups, "runs": reports}


def number(value, decimals=2):
    return "—" if value is None else ("{:." + str(decimals) + "f}").format(value)


def markdown_report(campaign):
    lines = ["# Fixed-frequency Redis/YCSB results", "",
             "Eight workers, a 70,000 QPS aggregate target, 20 seconds excluded, then 600 seconds measured.",
             "Each summary is the median of the valid repetitions; individual runs and ranges remain in the JSON.", "",
             "Validation: **%s**." % ("passed" if campaign["validation"]["passed"] else "incomplete or failed")]
    if campaign["validation"]["errors"]:
        lines += ["", "Validation findings:", ""] + ["- " + error for error in campaign["validation"]["errors"]]
    lines += ["", "| Workload | Mode | Runs | QPS | Shortfall % | Live ARM mean GiB | ARM events | WP events | Scanner flush calls | Redis TLB IPIs |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for group in campaign["groups"]:
        metric = group["metrics"]
        def med(name):
            return metric.get(name, {}).get("median")
        lines.append("| %s | %s | %d | %s | %s | %s | %s | %s | %s | %s |" % (
            group["workload"], group["mode"], len(group["valid_runs"]), number(med("achieved_qps"), 0),
            number(med("target_shortfall_pct")), number(med("arm_live_gib_mean")),
            number(med("arm_events"), 0), number(med("wp_events"), 0),
            number(med("flush_calls"), 0), number(med("tlb_shootdown_ipis_redis_cpu1"), 0)))
    for stream in ("combined-actual", "combined-intended", "READ", "UPDATE", "Intended-READ", "Intended-UPDATE"):
        lines += ["", "## %s latency (microseconds)" % stream, "",
                  "| Workload | Mode | Samples/run | p90 | p99 | p99.9 | p99.99 | p99.999 |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
        for group in campaign["groups"]:
            lat = group["latency_us"].get(stream)
            if lat is None:
                continue
            values = [number(lat["sample_count"]["median"], 0)] + [number(lat[p]["median"]) for p in PERCENTILES]
            lines.append("| %s | %s | %s |" % (group["workload"], group["mode"], " | ".join(values)))
    lines += ["", "Counts and interpretation:", "",
              "- ARM events include first ARM and re-ARM; they are not counts of unique pages.",
              "- WP counts are write-terminated ARM generations. Hook timing excludes other page-fault work.",
              "- Scanner flush calls count shared dirty/ARM flushes once. TLB IPIs also include other kernel activity.",
              "- Intended latency reveals delay against the target schedule when eight workers cannot sustain 70k QPS.",
              "- Very high percentiles depend on few tail observations, especially workload B updates; inspect per-run sample counts and tail ranks.",
              "- Invalid runs are retained and excluded explicitly; an incomplete campaign is never presented as a complete three-repetition result.", ""]
    return "\n".join(lines)


def plot_campaign(campaign, directory):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    directory = Path(directory)
    colors = {"off": "#3e6a91", "sampling": "#ce8b27", "arm": "#397d58"}
    groups = {(group["workload"], group["mode"]): group for group in campaign["groups"]}
    output = []
    for stream, title in (("combined-actual", "Database operation latency"),
                          ("combined-intended", "Latency from intended request start")):
        figure, axes = plt.subplots(1, 3, figsize=(12.6, 4.0), sharey=True)
        for ax, workload in zip(axes, WORKLOADS):
            for mode in MODES:
                latency = groups[(workload, mode)]["latency_us"].get(stream)
                if not latency:
                    continue
                values = [latency[p]["median"] / 1000 for p in PERCENTILES]
                lower = [(latency[p]["median"] - latency[p]["min"]) / 1000 for p in PERCENTILES]
                upper = [(latency[p]["max"] - latency[p]["median"]) / 1000 for p in PERCENTILES]
                ax.errorbar(range(5), values, yerr=[lower, upper], color=colors[mode],
                            marker="o", capsize=3, label=mode)
            ax.set_xticks(range(5))
            ax.set_xticklabels(PERCENTILES, rotation=30)
            ax.set_title("Workload " + workload)
            ax.set_yscale("log")
            ax.grid(axis="y", alpha=.25)
        axes[0].set_ylabel("Latency (ms, logarithmic scale)")
        axes[-1].legend(frameon=False)
        figure.suptitle(title + " — median and range across repetitions")
        figure.tight_layout(rect=(0, 0, 1, .94))
        for extension in ("png", "svg"):
            path = directory / (stream + "." + extension)
            figure.savefig(str(path), dpi=180, bbox_inches="tight")
            output.append(str(path))
        plt.close(figure)
    figure, axes = plt.subplots(1, 3, figsize=(12.6, 4.0))
    for axis, metric, label, divisor in ((axes[0], "achieved_qps", "Achieved kQPS", 1000),
                                        (axes[1], "arm_live_gib_mean", "Mean live ARM (GiB)", 1),
                                        (axes[2], "wp_events", "WP events", 1)):
        for index, mode in enumerate(MODES):
            samples = [groups[(w, mode)]["metrics"].get(metric) for w in WORKLOADS]
            values = [sample["median"] / divisor if sample else float("nan") for sample in samples]
            axis.bar([x + (index - 1) * .25 for x in range(3)], values, .24,
                     label=mode, color=colors[mode])
        axis.set_xticks(range(3))
        axis.set_xticklabels(WORKLOADS)
        axis.set_xlabel("Workload")
        axis.set_ylabel(label)
        axis.grid(axis="y", alpha=.2)
    axes[0].axhline(70, color="#555555", linestyle="--", linewidth=1, label="70k target")
    axes[0].legend(frameon=False, fontsize=8)
    figure.suptitle("Throughput, protected memory, and write faults")
    figure.tight_layout(rect=(0, 0, 1, .94))
    for extension in ("png", "svg"):
        path = directory / ("throughput-arm-wp." + extension)
        figure.savefig(str(path), dpi=180, bbox_inches="tight")
        output.append(str(path))
    plt.close(figure)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run")
    run.add_argument("--directory", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--expected-cpus", default=None)
    frequency = commands.add_parser("frequency")
    frequency.add_argument("--input", type=Path, required=True)
    frequency.add_argument("--start-ns", type=int, required=True)
    frequency.add_argument("--end-ns", type=int, required=True)
    frequency.add_argument("--selected-khz", type=int, required=True)
    frequency.add_argument("--expected-cpus", default=None)
    frequency.add_argument("--require-boundaries", action="store_true")
    frequency.add_argument("--output", type=Path, required=True)
    campaign = commands.add_parser("campaign")
    campaign.add_argument("--directory", type=Path, required=True)
    campaign.add_argument("--output-dir", type=Path, required=True)
    campaign.add_argument("--expected-repetitions", type=int, default=3)
    campaign.add_argument("--plots", action="store_true")
    args = parser.parse_args()
    if not args.command:
        parser.error("select run, frequency, or campaign")
    try:
        if args.command == "run":
            result = report_run(args.directory, cpulist(args.expected_cpus) if args.expected_cpus else None)
            save(args.output, result)
        elif args.command == "frequency":
            result = analyze_frequency(args.input, args.start_ns, args.end_ns, args.selected_khz,
                cpulist(args.expected_cpus) if args.expected_cpus else None,
                require_boundary_labels=args.require_boundaries)
            save(args.output, result)
        else:
            result = aggregate_campaign(args.directory, args.expected_repetitions)
            args.output_dir.mkdir(parents=True, exist_ok=True)
            save(args.output_dir / "campaign-summary.json", result)
            (args.output_dir / "report.md").write_text(markdown_report(result))
            if args.plots:
                print(json.dumps({"plots": plot_campaign(result, args.output_dir)}))
        print(json.dumps({"validation": result["validation"]}))
        return 0 if result["validation"]["passed"] else 2
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"validation": {"passed": False, "errors": [str(exc)]}}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
