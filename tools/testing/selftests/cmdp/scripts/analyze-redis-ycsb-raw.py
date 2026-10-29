#!/usr/bin/env python3
"""Parse all YCSB raw latency records using exact, bounded-memory histograms.

The raw file contains one section per measurement, not chronological rows.
Timestamps are completion-time epoch milliseconds. Filtering is start-inclusive
and end-exclusive. Percentiles use the linear interpolation/type-7 definition.

Use --ycsb-log and --require-complete to detect exports truncated between valid
rows: a syntactically valid raw file alone cannot establish export completeness.
Python 3.6+, standard library only. Raw input is never changed.
"""
import argparse
import collections
import contextlib
import gzip
import hashlib
import heapq
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile


PERCENTILES = (("p90", 90, 100), ("p99", 99, 100),
               ("p99.9", 999, 1000), ("p99.99", 9999, 10000),
               ("p99.999", 99999, 100000))
RECORD = struct.Struct("<qQ")
MAX_MERGE_FILES = 48
HEADER = re.compile(br"^([A-Za-z][A-Za-z0-9_-]*) latency raw data: op, timestamp\(ms\), latency\(us\)$")
STREAM_NAME = re.compile(br"^[A-Za-z][A-Za-z0-9_-]*$")
EXPORT_COUNT = re.compile(r"^\[([^\]]+)\],\s*Total Operations,\s*(\d+)\s*$")
EXPORT_STATUS = re.compile(r"^\[([^\]]+)\],\s*Return=([^,]+),\s*(\d+)\s*$")
FAILURE_EXAMPLES_LIMIT = 20


class HistogramBudget:
    """Bound the number of live sparse-histogram bins across all streams."""
    def __init__(self, directory, limit=250000):
        if limit < 1:
            raise ValueError("max histogram bins must be positive")
        self.directory = Path(directory)
        self.limit = limit
        self.live_bins = 0
        self.peak_bins = 0
        self.histograms = []
        self.next_file = 0
        self.spill_count = 0
        self.spill_bytes = 0

    def new_file(self):
        self.next_file += 1
        return self.directory / ("hist-%08d.bin" % self.next_file)

    def check(self):
        self.peak_bins = max(self.peak_bins, self.live_bins)
        if self.live_bins > self.limit:
            max(self.histograms, key=lambda h: len(h.bins)).spill()


def binary_items(stream):
    while True:
        block = stream.read(RECORD.size * 4096)
        if not block:
            break
        if len(block) % RECORD.size:
            raise ValueError("truncated temporary histogram")
        for item in RECORD.iter_unpack(block):
            yield item


def merge_histograms(paths):
    """Merge sorted histogram files with exact addition of equal bins."""
    with contextlib.ExitStack() as stack:
        streams = [binary_items(stack.enter_context(open(str(p), "rb")))
                   for p in paths]
        last_value = None
        last_count = 0
        for value, count in heapq.merge(*streams):
            if value == last_value:
                last_count += count
            else:
                if last_value is not None:
                    yield last_value, last_count
                last_value, last_count = value, count
        if last_value is not None:
            yield last_value, last_count


def write_histogram(path, items):
    with open(str(path), "wb") as out:
        for value, count in items:
            out.write(RECORD.pack(value, count))


class Histogram:
    def __init__(self, budget):
        self.budget = budget
        self.bins = {}
        self.paths = []
        self.count = 0
        self.total_us = 0
        budget.histograms.append(self)

    def add(self, value):
        previous = self.bins.get(value, 0)
        if not previous:
            self.budget.live_bins += 1
        self.bins[value] = previous + 1
        self.count += 1
        self.total_us += value
        if not previous:
            self.budget.check()

    def spill(self):
        if not self.bins:
            return
        path = self.budget.new_file()
        write_histogram(path, sorted(self.bins.items()))
        self.paths.append(path)
        self.budget.spill_count += 1
        self.budget.spill_bytes += len(self.bins) * RECORD.size
        self.budget.live_bins -= len(self.bins)
        self.bins.clear()

    def finish(self):
        self.spill()
        if not self.paths:
            return None
        while len(self.paths) > 1:
            next_paths = []
            for offset in range(0, len(self.paths), MAX_MERGE_FILES):
                group = self.paths[offset:offset + MAX_MERGE_FILES]
                if len(group) == 1:
                    next_paths.extend(group)
                    continue
                path = self.budget.new_file()
                write_histogram(path, merge_histograms(group))
                for original in group:
                    original.unlink()
                next_paths.append(path)
            self.paths = next_paths
        return self.paths[0]


def summarize_histogram(path, count, total_us, histogram_output=None):
    """Compute type-7 percentiles by rank; never expand histogram counts."""
    if not count:
        return {"count": 0, "sum_us": 0, "mean_us": None, "min_us": None, "max_us": None,
                "distinct_latency_values": 0,
                "percentiles_us": {p[0]: None for p in PERCENTILES},
                "tail": {p[0]: {"expected_count_above_percentile": 0,
                                  "observations_strictly_above_estimate": 0,
                                  "lower_order_statistic_1based": None,
                                  "upper_order_statistic_1based": None}
                         for p in PERCENTILES}}
    queries = {}
    wanted = set()
    for name, numerator, denominator in PERCENTILES:
        rank_numerator = (count - 1) * numerator
        lower, remainder = divmod(rank_numerator, denominator)
        upper = lower + bool(remainder)
        queries[name] = (lower, upper, remainder, denominator)
        wanted.add(lower)
        wanted.add(upper)
    wanted = sorted(wanted)
    rank_values = {}
    rank_index = 0
    cumulative = 0
    distinct = 0
    first = last = None
    with contextlib.ExitStack() as stack:
        source = stack.enter_context(open(str(path), "rb"))
        retained = None
        if histogram_output is not None:
            retained = stack.enter_context(gzip.open(str(histogram_output), "wt"))
            retained.write("latency_us,count\n")
        for value, frequency in binary_items(source):
            distinct += 1
            if first is None:
                first = value
            last = value
            cumulative += frequency
            while rank_index < len(wanted) and wanted[rank_index] < cumulative:
                rank_values[wanted[rank_index]] = value
                rank_index += 1
            if retained is not None:
                retained.write("%d,%d\n" % (value, frequency))
    if cumulative != count:
        raise ValueError("internal histogram count mismatch")
    percentiles = {}
    tails = {}
    for name, numerator, denominator in PERCENTILES:
        lower, upper, remainder, divisor = queries[name]
        lo, hi = rank_values[lower], rank_values[upper]
        value = lo + (hi - lo) * remainder / divisor
        percentiles[name] = value
        tails[name] = {
            "expected_count_above_percentile": count * (denominator - numerator) / denominator,
            "observations_strictly_above_estimate": 0,
            "lower_order_statistic_1based": lower + 1,
            "upper_order_statistic_1based": upper + 1,
        }
    # Only five thresholds: one second pass supplies the actual tail population,
    # which can be far smaller than nominal rank counts when latencies tie.
    sorted_thresholds = sorted((v, k) for k, v in percentiles.items())
    passed = []
    threshold_index = 0
    with open(str(path), "rb") as source:
        for value, frequency in binary_items(source):
            while (threshold_index < len(sorted_thresholds) and
                   sorted_thresholds[threshold_index][0] < value):
                passed.append(sorted_thresholds[threshold_index][1])
                threshold_index += 1
            for name in passed:
                tails[name]["observations_strictly_above_estimate"] += frequency
    result = {"count": count, "sum_us": total_us, "mean_us": total_us / count,
              "min_us": first, "max_us": last, "distinct_latency_values": distinct,
              "percentiles_us": percentiles, "tail": tails}
    if histogram_output is not None:
        result["histogram_csv_gz"] = str(histogram_output)
    return result


def stream_identity(name, operations):
    intended = name.startswith("Intended-")
    operation = name[len("Intended-"):] if intended else name
    success = operation in operations
    failure = any(operation.startswith(op + "-") for op in operations)
    base_operation = next((op for op in operations
                           if operation == op or operation.startswith(op + "-")), None)
    return {"latency_kind": "intended" if intended else "actual",
            "operation": operation, "base_operation": base_operation,
            "successful_workload_stream": success,
            "failed_workload_stream": failure}


def read_expected_counts(log_path=None, expected_path=None):
    counts = {}
    statuses = {}
    errors = []
    provenance = {}
    if log_path:
        digest = hashlib.sha256()
        with open(str(log_path), "rb") as source:
            for line in source:
                digest.update(line)
                text = line.decode("utf-8", errors="replace").strip()
                match = EXPORT_COUNT.match(text)
                if match:
                    name, count = match.group(1), int(match.group(2))
                    if name in counts:
                        errors.append("Repeated Total Operations metadata for %s" % name)
                    counts[name] = count
                match = EXPORT_STATUS.match(text)
                if match:
                    name, status, count = match.group(1), match.group(2), int(match.group(3))
                    statuses.setdefault(name, {})[status] = count
        provenance["ycsb_log"] = {"path": str(log_path), "sha256": digest.hexdigest(),
                                  "size_bytes": os.stat(str(log_path)).st_size}
    if expected_path:
        with open(str(expected_path), "rb") as source:
            raw = source.read()
        supplied = json.loads(raw.decode("utf-8"))
        if not isinstance(supplied, dict):
            raise ValueError("expected-counts must be a JSON object mapping stream names to counts")
        for name, count in supplied.items():
            if (not isinstance(name, str) or not isinstance(count, int) or
                    isinstance(count, bool) or count < 0):
                raise ValueError("invalid expected stream count")
            if name in counts and counts[name] != count:
                errors.append("Conflicting expected counts for %s" % name)
            counts[name] = count
        provenance["expected_counts"] = {"path": str(expected_path),
                                         "sha256": hashlib.sha256(raw).hexdigest()}
    return counts, statuses, errors, provenance


def analyze(input_path, start_ms, end_ms, ycsb_log=None, expected_counts=None,
            require_complete=False, max_histogram_bins=250000,
            scratch_dir=None, histogram_dir=None, operations=("READ", "UPDATE")):
    if end_ms <= start_ms or start_ms < 0:
        raise ValueError("measurement window must have nonnegative start and end > start")
    if not operations or len(set(operations)) != len(operations):
        raise ValueError("operations must be nonempty and unique")
    expected, statuses, errors, metadata_provenance = read_expected_counts(
        ycsb_log, expected_counts)
    warnings = []
    input_path = Path(input_path)
    stat_before = input_path.stat()
    if histogram_dir is not None:
        histogram_dir = Path(histogram_dir)
        histogram_dir.mkdir(parents=True, exist_ok=True)
    duration_ms = end_ms - start_ms
    second_count = (duration_ms + 999) // 1000
    # This memory cost depends on the requested window and stream count, never
    # on the number of records. The campaign uses exactly 600 bins per stream.
    if second_count > 7 * 24 * 3600:
        raise ValueError("measurement windows longer than seven days are unsupported")
    malformed_count = 0
    malformed_examples = []
    digest = hashlib.sha256()
    bytes_read = 0
    line_number = 0
    active_stream = None
    raw_name_cache = {}
    streams = {}
    header_counts = collections.Counter()
    last_had_newline = True
    with tempfile.TemporaryDirectory(prefix="ycsb-raw-hist-", dir=scratch_dir) as temporary:
        budget = HistogramBudget(temporary, max_histogram_bins)

        def get_stream(name):
            if name not in streams:
                streams[name] = dict(stream_identity(name, operations),
                                     count_all=0, count_retained=0,
                                     count_before_window=0, count_after_window=0,
                                     min_timestamp_ms=None, max_timestamp_ms=None,
                                     histogram=Histogram(budget),
                                     per_second_counts=[0] * second_count,
                                     per_second_sum_us=[0] * second_count,
                                     per_second_max_us=[None] * second_count)
            return streams[name]

        with open(str(input_path), "rb") as source:
            for line_number, line in enumerate(source, 1):
                digest.update(line)
                bytes_read += len(line)
                last_had_newline = line.endswith(b"\n")
                row = line.strip()
                if not row:
                    continue
                try:
                    if b" latency raw data: " in row:
                        match = HEADER.match(row)
                        if not match:
                            raise ValueError("invalid raw section header")
                        name = match.group(1).decode("ascii")
                        header_counts[name] += 1
                        active_stream = name
                        get_stream(name)
                        continue
                    columns = row.split(b",")
                    if len(columns) != 3:
                        raise ValueError("expected exactly three raw columns")
                    raw_name, raw_timestamp, raw_latency = columns
                    entry = raw_name_cache.get(raw_name)
                    if entry is None:
                        if not STREAM_NAME.match(raw_name):
                            raise ValueError("invalid stream name")
                        name = raw_name.decode("ascii")
                        entry = (name, get_stream(name))
                        raw_name_cache[raw_name] = entry
                    name, stream = entry
                    if name != active_stream:
                        raise ValueError("record does not match current section header")
                    timestamp = int(raw_timestamp)
                    latency = int(raw_latency)
                    if timestamp < 0 or latency < 0 or latency > 2147483647:
                        raise ValueError("negative timestamp or invalid/overflowed latency")
                    stream["count_all"] += 1
                    if (stream["min_timestamp_ms"] is None or
                            timestamp < stream["min_timestamp_ms"]):
                        stream["min_timestamp_ms"] = timestamp
                    if (stream["max_timestamp_ms"] is None or
                            timestamp > stream["max_timestamp_ms"]):
                        stream["max_timestamp_ms"] = timestamp
                    if timestamp < start_ms:
                        stream["count_before_window"] += 1
                    elif timestamp >= end_ms:
                        stream["count_after_window"] += 1
                    else:
                        stream["count_retained"] += 1
                        stream["histogram"].add(latency)
                        second = (timestamp - start_ms) // 1000
                        stream["per_second_counts"][second] += 1
                        stream["per_second_sum_us"][second] += latency
                        previous_max = stream["per_second_max_us"][second]
                        if previous_max is None or latency > previous_max:
                            stream["per_second_max_us"][second] = latency
                except (ValueError, UnicodeDecodeError) as exc:
                    malformed_count += 1
                    if len(malformed_examples) < FAILURE_EXAMPLES_LIMIT:
                        malformed_examples.append({"line": line_number, "reason": str(exc),
                            "excerpt": row[:160].decode("ascii", errors="replace")})

        stat_after = input_path.stat()
        if (stat_before.st_size != stat_after.st_size or
                stat_before.st_mtime_ns != stat_after.st_mtime_ns or
                stat_before.st_ino != stat_after.st_ino or bytes_read != stat_after.st_size):
            errors.append("Raw input changed during parsing")
        if malformed_count:
            errors.append("%d malformed raw records or headers" % malformed_count)
        if not last_had_newline:
            errors.append("Raw file lacks final newline; export may be truncated")
        for name, count in sorted(header_counts.items()):
            if count != 1:
                errors.append("Expected one section header for %s, found %d" % (name, count))
        reconciliation = {}
        for name in sorted(set(expected) | set(streams)):
            actual = streams.get(name, {}).get("count_all", 0)
            required = expected.get(name)
            reconciliation[name] = {"raw_count": actual, "export_count": required,
                                    "matches": actual == required if required is not None else None}
            if required is not None and actual != required:
                errors.append("Raw/export count mismatch for %s: %d != %d" %
                              (name, actual, required))
            if required is not None and name not in header_counts:
                errors.append("Missing raw section for exported stream %s" % name)
        complete_verified = bool(expected) and all(
            r["matches"] is True for r in reconciliation.values())
        if not complete_verified:
            message = "Complete raw export cannot be verified against all stream totals"
            if require_complete:
                errors.append(message)
            else:
                warnings.append(message)
        for name, codes in sorted(statuses.items()):
            for code, count in sorted(codes.items()):
                if code != "OK" and count:
                    errors.append("YCSB returned %s %s %d times" % (name, code, count))
        paired = {}
        for name, stream in sorted(streams.items()):
            if stream["latency_kind"] != "actual":
                continue
            peer = streams.get("Intended-" + name)
            if stream["successful_workload_stream"] or stream["failed_workload_stream"]:
                if peer is None:
                    errors.append("Missing intended latency stream for %s" % name)
                elif stream["count_all"] != peer["count_all"]:
                    errors.append("Actual/intended whole-export count mismatch for %s" % name)
                if stream["failed_workload_stream"] and stream["count_all"]:
                    errors.append("Failed workload operations appear in raw stream %s" % name)
            if peer is not None:
                paired[name] = {"actual_all": stream["count_all"],
                                "intended_all": peer["count_all"],
                                "actual_retained": stream["count_retained"],
                                "intended_retained": peer["count_retained"],
                                "retained_delta_intended_minus_actual":
                                    peer["count_retained"] - stream["count_retained"]}
        for name, stream in sorted(streams.items()):
            if (stream["latency_kind"] == "intended" and
                    (stream["successful_workload_stream"] or stream["failed_workload_stream"]) and
                    name[len("Intended-"):] not in streams):
                errors.append("Missing actual latency stream for %s" % name)

        paths = {}
        for name, stream in sorted(streams.items()):
            histogram = stream.pop("histogram")
            path = histogram.finish()
            paths[name] = path
            hist_output = histogram_dir / (name + ".csv.gz") if histogram_dir else None
            stream["latency"] = summarize_histogram(path, histogram.count,
                                                    histogram.total_us, hist_output)
        combined = {}
        for kind in ("actual", "intended"):
            selected = [name for name, stream in streams.items()
                        if stream["latency_kind"] == kind and
                        stream["successful_workload_stream"]]
            histogram_paths = [paths[name] for name in selected if paths[name] is not None]
            path = budget.new_file()
            if histogram_paths:
                write_histogram(path, merge_histograms(histogram_paths))
            count = sum(streams[name]["count_retained"] for name in selected)
            total_us = sum(streams[name]["latency"]["sum_us"] for name in selected)
            hist_output = histogram_dir / ("combined-" + kind + ".csv.gz") if histogram_dir else None
            combined[kind] = summarize_histogram(path, count, total_us, hist_output)
            combined[kind]["included_streams"] = sorted(selected)
            combined[kind]["qps"] = count * 1000 / duration_ms
        if not combined["actual"]["count"]:
            errors.append("No successful workload operations in measurement window")
        per_second = []
        for index in range(second_count):
            bin_ms = min(1000, duration_ms - index * 1000)
            actual_counts = {name: stream["per_second_counts"][index]
                             for name, stream in sorted(streams.items())
                             if stream["latency_kind"] == "actual" and
                             (stream["successful_workload_stream"] or stream["failed_workload_stream"])}
            success = sum(count for name, count in actual_counts.items()
                          if streams[name]["successful_workload_stream"])
            attempted = sum(actual_counts.values())
            latency_bins = {}
            for name, stream in sorted(streams.items()):
                if not (stream["successful_workload_stream"] or stream["failed_workload_stream"]):
                    continue
                count = stream["per_second_counts"][index]
                latency_bins[name] = {"count": count,
                                     "mean_us": stream["per_second_sum_us"][index] / count if count else None,
                                     "max_us": stream["per_second_max_us"][index]}
            per_second.append({"index": index, "start_ms": start_ms + index * 1000,
                               "duration_ms": bin_ms,
                               "successful_count": success, "attempted_count": attempted,
                               "successful_qps": success * 1000 / bin_ms,
                               "attempted_qps": attempted * 1000 / bin_ms,
                               "actual_operation_counts": actual_counts,
                               "latency_by_stream": latency_bins})
        result = {
            "schema_version": 1,
            "input": {"path": str(input_path), "size_bytes": bytes_read,
                      "sha256": digest.hexdigest(), "lines": line_number},
            "window": {"start_ms": start_ms, "end_ms": end_ms,
                       "duration_seconds": duration_ms / 1000,
                       "timestamp_semantics": "YCSB record completion time; epoch milliseconds",
                       "inclusion": "start-inclusive,end-exclusive"},
            "method": {"percentiles": "linear interpolation (Hyndman-Fan type 7)",
                       "latency_units": "microseconds", "operations": list(operations),
                       "all_retained_samples_used": True,
                       "qps_uses_actual_streams_only": True,
                       "cleanup_and_failures_excluded_from_successful_combined": True,
                       "histogram_bin_limit": max_histogram_bins,
                       "peak_live_histogram_bins": budget.peak_bins,
                       "histogram_spills": budget.spill_count,
                       "histogram_initial_spill_bytes": budget.spill_bytes},
            "validation": {"passed": not errors, "errors": errors, "warnings": warnings,
                           "complete_export_verified": complete_verified and not malformed_count,
                           "malformed_line_count": malformed_count,
                           "malformed_examples": malformed_examples,
                           "final_newline": last_had_newline},
            "metadata_provenance": metadata_provenance,
            "export_return_counts": statuses,
            "count_reconciliation": reconciliation,
            "actual_intended_pairs": paired,
            "streams": streams,
            "combined": combined,
            "per_second": per_second,
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--start-ms", required=True, type=int)
    parser.add_argument("--end-ms", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ycsb-log", type=Path)
    parser.add_argument("--expected-counts", type=Path,
                        help="JSON object: exact raw stream name -> whole-export count")
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--max-histogram-bins", type=int, default=250000)
    parser.add_argument("--scratch-dir", type=Path)
    parser.add_argument("--histogram-dir", type=Path,
                        help="retain exact selected-window histograms as CSV.gz for later pooling")
    args = parser.parse_args()
    protected_inputs = [path.resolve() for path in
                        (args.input, args.ycsb_log, args.expected_counts) if path is not None]
    if args.output.resolve() in protected_inputs:
        parser.error("--output must not replace an input or metadata file")
    try:
        result = analyze(args.input, args.start_ms, args.end_ms, args.ycsb_log,
                         args.expected_counts, args.require_complete,
                         args.max_histogram_bins,
                         str(args.scratch_dir) if args.scratch_dir else None,
                         args.histogram_dir)
    except (OSError, ValueError) as exc:
        result = {"schema_version": 1, "validation": {"passed": False,
                  "complete_export_verified": False, "errors": [str(exc)], "warnings": []}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    with open(str(temporary), "w") as output:
        json.dump(result, output, indent=2, sort_keys=True, allow_nan=False)
        output.write("\n")
    os.replace(str(temporary), str(args.output))
    print(json.dumps({"output": str(args.output), "validation": result["validation"],
                      "actual_qps": result.get("combined", {}).get("actual", {}).get("qps")}))
    return 0 if result["validation"]["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
