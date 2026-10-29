#!/usr/bin/env python3
"""Offline paired summary of three 120-second workload-C collector pilots.

Each --pair takes TELEMETRY_OFF_DIR TELEMETRY_ON_DIR, in that semantic order
regardless of chronological execution order. Both runs must have CMD-P off.
Consumes existing validated reports; never launches loads or changes inputs.
Python 3.6+, standard library only.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import statistics
import sys


PERCENTILES = ("p90", "p99", "p99.9", "p99.99", "p99.999")


def read(path):
    with open(str(path)) as source:
        return json.load(source)


def source_hash(path):
    h = hashlib.sha256()
    with open(str(path), "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return {"path": str(path), "sha256": h.hexdigest(), "size_bytes": path.stat().st_size}


def spread(values):
    values = [value for value in values if value is not None]
    return {"n": len(values), "median": statistics.median(values) if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None}


def run_summary(path, expected_telemetry):
    path = Path(path)
    manifest = read(path / "run.json")
    report = read(path / "report.json")
    timeline = read(path / "timeline.json")
    receipt = read(path / "receipt.json")
    restore = read(path / "restore.json")
    command = read(path / "command.json")
    errors = []
    if not receipt.get("passed"):
        errors.append("Run receipt is not passed")
    if not report.get("validation", {}).get("passed"):
        errors.append("Run report is not valid: %s" % report.get("validation", {}).get("errors"))
    for name, expected in (("workload", "C"), ("mode", "off"), ("workers", 8),
                           ("target_qps", 70000), ("warmup_seconds", 20),
                           ("measure_seconds", 120), ("telemetry", expected_telemetry)):
        if manifest.get(name) != expected:
            errors.append("%s=%r, expected %r" % (name, manifest.get(name), expected))
    if timeline["end_ms"] - timeline["start_ms"] != 120000:
        errors.append("Latency window is not exactly 120 seconds")
    frequency = report.get("frequency")
    if expected_telemetry:
        if not frequency or not frequency.get("validation", {}).get("passed"):
            errors.append("Telemetry-on run lacks valid frequency report")
    elif frequency is not None:
        errors.append("Telemetry-off run unexpectedly has frequency telemetry")
    for key in ("arm_events", "wp_events", "flush_calls"):
        if report["metrics"].get(key) != 0:
            errors.append("Unexpected CMD-P %s in collector pilot" % key)
    metrics = {"achieved_qps": report["metrics"]["achieved_qps"],
               "target_shortfall_pct": report["metrics"]["target_shortfall_pct"],
               "redis_cpu_percent": report["metrics"]["redis_cpu_percent"],
               "client_cpu_percent_of_eight_cores": report["metrics"]["client_cpu_percent_of_eight_cores"]}
    sample_counts = {}
    tails = {}
    for kind in ("actual", "intended"):
        latency = report["latency"]["combined-" + kind]
        metrics[kind + "_mean_us"] = latency.get("mean_us")
        sample_counts[kind] = latency["count"]
        tails[kind] = latency.get("tail", {})
        for percentile in PERCENTILES:
            metrics[kind + "_" + percentile + "_us"] = latency["percentiles_us"][percentile]
    common_properties = {key: value for key, value in command["properties"].items()
                         if key != "measurement.raw.output_file"}
    signature = {"workload": manifest["workload"], "mode": manifest["mode"],
                 "workers": manifest["workers"], "target_qps": manifest["target_qps"],
                 "selected_khz": manifest["selected_khz"],
                 "frequency_contract": manifest.get("frequency_contract", "locked-clock"),
                 "nominal_base_khz": manifest.get("nominal_base_khz"),
                 "warmup_seconds": manifest["warmup_seconds"],
                 "measure_seconds": manifest["measure_seconds"],
                 "policy": manifest.get("policy"),
                 "source_hashes": manifest.get("source_hashes"),
                 "rdb_sha256": restore.get("rdb_sha256"),
                 "ycsb_properties": common_properties}
    # Verify the report and configuration against the completed-run receipt.
    # The large raw file is deliberately not re-read: its analyzer SHA and the
    # campaign's receipt provide provenance without another multi-GB scan.
    sources = {}
    for name in ("run.json", "report.json", "timeline.json", "restore.json", "command.json"):
        sources[name] = source_hash(path / name)
        if receipt.get("files_sha256", {}).get(name) != sources[name]["sha256"]:
            errors.append("Receipt hash missing or mismatched for " + name)
    sources["receipt.json"] = source_hash(path / "receipt.json")
    return {"directory": str(path), "run_name": path.name, "telemetry": expected_telemetry,
            "query_epoch_ms": timeline.get("query_epoch_ms", timeline["start_ms"] - 20000),
            "metrics": metrics, "sample_counts": sample_counts, "tail_counts": tails,
            "matched_signature": signature, "sources": sources,
            "validation": {"passed": not errors, "errors": errors}}


def compare(pairs):
    errors = []
    if len(pairs) != 3:
        errors.append("Expected exactly three matched pilot pairs")
    reports = []
    all_paths = []
    global_signature = None
    for index, paths in enumerate(pairs, 1):
        off, on = (run_summary(paths[0], False), run_summary(paths[1], True))
        all_paths.extend(str(Path(path).resolve()) for path in paths)
        pair_errors = []
        for run in (off, on):
            pair_errors.extend(run["run_name"] + ": " + error for error in run["validation"]["errors"])
        if off["matched_signature"] != on["matched_signature"]:
            pair_errors.append("Off/on configuration, database, or source hashes differ")
        if global_signature is None:
            global_signature = off["matched_signature"]
        for run in (off, on):
            if run["matched_signature"] != global_signature:
                pair_errors.append("Configuration differs from the first pilot pair")
        metrics = {}
        for name, off_value in off["metrics"].items():
            on_value = on["metrics"].get(name)
            ratio = on_value / off_value if off_value not in (None, 0) and on_value is not None else None
            metrics[name] = {"off": off_value, "on": on_value, "on_over_off": ratio,
                             "delta_on_minus_off": on_value - off_value
                             if on_value is not None and off_value is not None else None,
                             "change_pct": (ratio - 1) * 100 if ratio is not None else None}
        qps_ratio = metrics["achieved_qps"]["on_over_off"]
        reports.append({"pair": index, "off": off, "on": on, "metrics": metrics,
                        "execution_order": "off then on" if off["query_epoch_ms"] < on["query_epoch_ms"] else "on then off",
                        "throughput_reduction_pct": (1 - qps_ratio) * 100 if qps_ratio is not None else None,
                        "validation": {"passed": not pair_errors, "errors": pair_errors}})
        errors.extend("Pair %d: %s" % (index, error) for error in pair_errors)
    if len(all_paths) != len(set(all_paths)):
        errors.append("A run directory was reused across pilot pairs")
    valid = [pair for pair in reports if pair["validation"]["passed"]]
    metric_names = sorted(set(name for pair in valid for name in pair["metrics"]))
    summary = {name: {field: spread([pair["metrics"][name][field] for pair in valid])
                     for field in ("off", "on", "on_over_off", "delta_on_minus_off", "change_pct")}
               for name in metric_names}
    return {"schema_version": 1, "experiment": "120-second workload-C frequency-collector pilot",
            "comparison": "Telemetry on versus off; CMD-P off in both; 8 workers, 70k aggregate target",
            "validation": {"passed": not errors, "errors": errors},
            "pairs": reports, "valid_pairs": len(valid), "metric_summary": summary,
            "throughput_reduction_pct": spread([pair["throughput_reduction_pct"] for pair in valid]),
            "uncertainty": [
                "Three 120-second pairs are a collector pilot, not the 600-second main campaign.",
                "Median/min/max summarize paired observations; they are not confidence intervals or statistical significance tests.",
                "Small on/off differences may reflect run-to-run variability; do not subtract a pilot estimate from the main results.",
                "Extreme percentile estimates use few tail observations; exact retained counts and actual tail populations remain attached.",
                "Intended latency includes accumulated delay against the target schedule and can grow when 70k QPS is unsustainable.",
                "Telemetry-off runs use the same verified frequency policy and prior qualification but have no contemporaneous APERF/MPERF evidence.",
                "Raw input is not re-hashed here; validated raw-analysis provenance and completed-run receipt are retained.",
            ]}


def fmt(value, digits=3):
    return "—" if value is None else ("{:." + str(digits) + "f}").format(value)


def markdown(result):
    lines = ["# Frequency collector pilot", "",
             "Workload C, CMD-P off, 8 workers, 70k QPS target; 20 seconds excluded, then **120 seconds measured**.",
             "The comparison changes only frequency telemetry. Ratios are **on/off**; throughput reduction is positive when telemetry lowers QPS.", "",
             "Validation: **%s**; %d valid matched pairs." %
             ("passed" if result["validation"]["passed"] else "failed or incomplete", result["valid_pairs"]), "",
             "| Pair | Order | QPS off | QPS on | On/off | Throughput reduction % | Actual p99.999 off/on (µs) | Intended mean off/on (µs) |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    for pair in result["pairs"]:
        metrics = pair["metrics"]
        qps, tail, intended = metrics["achieved_qps"], metrics["actual_p99.999_us"], metrics["intended_mean_us"]
        lines.append("| %d | %s | %s | %s | %s | %s | %s / %s | %s / %s |" %
                     (pair["pair"], pair["execution_order"], fmt(qps["off"], 1), fmt(qps["on"], 1),
                      fmt(qps["on_over_off"], 5), fmt(pair["throughput_reduction_pct"]),
                      fmt(tail["off"]), fmt(tail["on"]), fmt(intended["off"]), fmt(intended["on"])))
    lines += ["", "Median and observed range across valid pairs:", "",
              "| Metric | Off median | On median | Paired on/off median [min, max] | Paired change % median [min, max] |",
              "|---|---:|---:|---:|---:|"]
    ordered = ["achieved_qps", "actual_mean_us"] + ["actual_" + p + "_us" for p in PERCENTILES] + \
              ["intended_mean_us"] + ["intended_" + p + "_us" for p in PERCENTILES]
    for name in ordered:
        row = result["metric_summary"].get(name)
        if row is None:
            continue
        ratio, change = row["on_over_off"], row["change_pct"]
        lines.append("| %s | %s | %s | %s [%s, %s] | %s [%s, %s] |" %
                     (name, fmt(row["off"]["median"]), fmt(row["on"]["median"]),
                      fmt(ratio["median"], 5), fmt(ratio["min"], 5), fmt(ratio["max"], 5),
                      fmt(change["median"]), fmt(change["min"]), fmt(change["max"])))
    if result["validation"]["errors"]:
        lines += ["", "Validation findings:", ""] + ["- " + error for error in result["validation"]["errors"]]
    lines += ["", "Interpretation:", ""] + ["- " + point for point in result["uncertainty"]]
    return "\n".join(lines) + "\n"


def write_outputs(directory, result):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "collector-pilot-summary.json").open("w") as output:
        json.dump(result, output, sort_keys=True, indent=2, allow_nan=False)
        output.write("\n")
    (directory / "collector-pilot-summary.md").write_text(markdown(result))
    with (directory / "collector-pilot-pairs.csv").open("w", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(("pair", "valid", "metric", "off", "on", "on_over_off", "delta_on_minus_off", "change_pct"))
        for pair in result["pairs"]:
            for name, values in sorted(pair["metrics"].items()):
                writer.writerow([pair["pair"], pair["validation"]["passed"], name] +
                                [values[key] for key in ("off", "on", "on_over_off", "delta_on_minus_off", "change_pct")])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair", nargs=2, action="append", required=True,
                        metavar=("TELEMETRY_OFF_DIR", "TELEMETRY_ON_DIR"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = compare(args.pair)
        write_outputs(args.output_dir, result)
        print(json.dumps({"output_dir": str(args.output_dir), "validation": result["validation"],
                          "throughput_reduction_pct": result["throughput_reduction_pct"]}))
        return 0 if result["validation"]["passed"] else 2
    except (OSError, KeyError, ValueError) as exc:
        print(json.dumps({"validation": {"passed": False, "errors": [str(exc)]}}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
