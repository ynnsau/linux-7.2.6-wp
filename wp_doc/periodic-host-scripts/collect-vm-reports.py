#!/usr/bin/env python3
"""Extract and validate a completed CMD-P VM serial log, locally or on bmp.

Examples:
  collect-vm-reports.py vm-debug.log --out reports-debug
  collect-vm-reports.py vm-debug.log --out reports-debug --scripts-dir \
      /home/yans3/cmdp-v2/linux/tools/testing/selftests/cmdp/scripts

No output directory is created until the VM completion marker and all block
boundaries have been checked. A completed failing run is still extracted for
diagnosis, but the command exits nonzero. An incomplete log is refused.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

BEGIN = re.compile(r"^CMDP_REPORT_BEGIN ([a-zA-Z0-9_-]+)$")
END = re.compile(r"^CMDP_REPORT_END ([a-zA-Z0-9_-]+)$")
RESULT = re.compile(r"^CMDP_VM_RESULT=([0-9]+)$")
WARNINGS = re.compile(r"BUG:|WARNING:|Oops:|KASAN:|Kernel panic|possible circular locking|inconsistent lock state|general protection fault")
STAT = re.compile(r"^(arms|publishes|revokes|waiters|rejects|entries|managed|isolated|stale|arm_ns|revoke_ns)\s+([0-9]+)$")


def parse_complete_log(text):
    lines = text.splitlines()
    results = []
    for index, line in enumerate(lines):
        match = RESULT.fullmatch(line.strip())
        if match:
            results.append((index, int(match.group(1))))
    if len(results) != 1:
        raise ValueError("Refusing incomplete/ambiguous log: expected exactly one CMDP_VM_RESULT marker")
    finish, result = results[0]
    if sum(line.strip() == "CMDP_VM_START" for line in lines) != 1:
        raise ValueError("Expected exactly one CMDP_VM_START marker")
    blocks = {}
    active = None
    for index, line in enumerate(lines):
        value = line.strip()
        begin, end = BEGIN.fullmatch(value), END.fullmatch(value)
        if begin:
            name = begin.group(1)
            if active is not None or name in blocks or index >= finish:
                raise ValueError(f"Nested, duplicate, or post-completion report block: {name}")
            active = name
            blocks[name] = {"begin": index, "json_lines": []}
        elif end:
            name = end.group(1)
            if active != name or index >= finish:
                raise ValueError(f"Unmatched report END: {name}")
            blocks[name]["end"] = index
            active = None
        elif active is not None:
            blocks[active]["json_lines"].append(line)
        elif value.startswith("CMDP_REPORT_"):
            raise ValueError(f"Malformed report marker on line {index + 1}: {value}")
    if active is not None:
        raise ValueError(f"Refusing incomplete report block: {active}")
    if not blocks:
        raise ValueError("No completed report blocks were found")
    ordered = list(blocks.values())
    for pos, block in enumerate(ordered):
        following = ordered[pos + 1]["begin"] if pos + 1 < len(ordered) else finish
        target_lines = lines[block["end"] + 1:following]
        # build-vm.sh prints final global statistics after the last workload.
        if pos + 1 == len(ordered):
            for i, line in enumerate(target_lines):
                if STAT.fullmatch(line.strip()):
                    target_lines = target_lines[:i]
                    break
        block["target_lines"] = target_lines
    stats = {}
    for line in lines[ordered[-1]["end"] + 1:finish]:
        match = STAT.fullmatch(line.strip())
        if match:
            stats[match.group(1)] = int(match.group(2))
    return lines, result, blocks, stats


def tap_results(lines):
    segments = []
    active = None
    for line in lines:
        value = line.strip()
        if BEGIN.fullmatch(value):
            break
        if value == "TAP version 13":
            active = {"plans": [], "results": [], "failures": []}
            segments.append(active)
        elif active is not None:
            plan = re.fullmatch(r"1\.\.([0-9]+)(?:\s+#.*)?", value)
            check = re.match(r"^(not ok|ok)\s+([0-9]+)\b", value)
            if plan:
                active["plans"].append(int(plan.group(1)))
            if check:
                active["results"].append(int(check.group(2)))
                if check.group(1) == "not ok":
                    active["failures"].append(value)
    for segment in segments:
        plans = segment["plans"]
        count = plans[0] if len(plans) == 1 else None
        segment["pass"] = (count is not None and count > 0 and
                           segment["results"] == list(range(1, count + 1)) and
                           not segment["failures"])
        segment["planned_checks"] = count
        segment["observed_checks"] = len(segment.pop("results"))
    return segments


def find_scripts(explicit):
    candidates = [Path(explicit)] if explicit else [
        Path(os.environ.get("CMDP_SCRIPTS_DIR", "/nonexistent")),
        Path("/tmp/cmdp-periodic-src/tools/testing/selftests/cmdp/scripts"),
        Path("/home/yans3/cmdp-v2/linux/tools/testing/selftests/cmdp/scripts"),
    ]
    for candidate in candidates:
        if all((candidate / name).is_file() for name in ("validate-run.py", "report-run.py")):
            return candidate.resolve()
    raise ValueError("Cannot locate validator/report scripts; supply --scripts-dir")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="New directory; existing paths are refused")
    parser.add_argument("--scripts-dir")
    parser.add_argument("--expected", default="hotcold,concurrent,churn,bfs",
                        help="Required comma-separated block names; omit bfs explicitly for an image without BFS")
    parser.add_argument("--min-sweeps", type=int, default=1)
    args = parser.parse_args()
    try:
        if args.min_sweeps < 1:
            raise ValueError("--min-sweeps must be positive")
        raw = args.log.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        lines, result, blocks, stats = parse_complete_log(text)
        scripts = find_scripts(args.scripts_dir)
        if args.out.exists():
            raise ValueError("Output path already exists; choose a new directory")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    expected = [name.strip() for name in args.expected.split(",") if name.strip()]
    errors = []
    if result:
        errors.append(f"VM reported CMDP_VM_RESULT={result}")
    missing = sorted(set(expected) - set(blocks))
    if missing:
        errors.append("Missing required report blocks: " + ", ".join(missing))
    warnings = [line for line in lines if WARNINGS.search(line)]
    if warnings:
        errors.append("Kernel warning/oops/panic signatures found in VM log")
    taps = tap_results(lines)
    if len(taps) != 2 or not all(t["pass"] for t in taps):
        errors.append("Expected two complete passing TAP suites")
    if not taps or taps[0]["planned_checks"] != 18:
        errors.append("Legacy 18-check regression suite is not established")
    for key in ("entries", "managed", "isolated"):
        if stats.get(key) != 0:
            errors.append(f"Final global {key} is not recorded as zero")

    args.out.mkdir(parents=True, exist_ok=False)
    runs = {}
    for name, block in blocks.items():
        destination = args.out / name
        destination.mkdir()
        (destination / "monitor.jsonl").write_text("\n".join(block["json_lines"]) + "\n")
        target_text = "\n".join(block["target_lines"]) + "\n"
        (destination / "target.log").write_text(target_text)
        run_errors = []
        validator = subprocess.run([sys.executable, str(scripts / "validate-run.py"),
                                    str(destination), "--min-sweeps", str(args.min_sweeps)],
                                   universal_newlines=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, check=False)
        (destination / "validator.log").write_text(validator.stdout + validator.stderr)
        if validator.returncode:
            run_errors.append(f"Counter/state validator exited {validator.returncode}")
        validation = {}
        try:
            validation = json.loads((destination / "validation.json").read_text())
        except (OSError, ValueError) as exc:
            run_errors.append("No readable validation result: " + str(exc))
        if name == "bfs":
            verifications = re.findall(r"Verification:\s*(PASS|FAIL)", target_text)
            trials = re.findall(r"Trial Time:\s*[0-9.eE+-]+", target_text)
            if not verifications or any(v != "PASS" for v in verifications) or len(verifications) != len(trials):
                run_errors.append("BFS must report one PASS verification for every observed trial")
            content = {"verification_passes": verifications.count("PASS"),
                       "verification_failures": verifications.count("FAIL"), "trial_count": len(trials)}
        elif name in ("hotcold", "concurrent", "churn"):
            marks = re.findall(r"fixture_validation=(PASS|FAIL)", target_text)
            if marks != ["PASS"]:
                run_errors.append("Fixture did not emit exactly one PASS result")
            if name == "hotcold" and "fixture_phase=cold" not in target_text:
                run_errors.append("Hot/cold fixture did not enter its cold phase")
            content = {"fixture_results": marks}
        else:
            content = {"note": "No workload-specific output oracle is defined for this block"}
        if validation:
            reporter = subprocess.run([sys.executable, str(scripts / "report-run.py"), str(destination)],
                                      universal_newlines=True, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, check=False)
            (destination / "reporter.log").write_text(reporter.stdout + reporter.stderr)
            if reporter.returncode:
                run_errors.append(f"Report generator exited {reporter.returncode}")
        else:
            (destination / "report.md").write_text("Validation failed; see validator.log and extraction-validation.json.\n")
        record = {"pass": not run_errors and bool(validation.get("pass")), "errors": run_errors,
                  "source_begin_line": block["begin"] + 1, "source_end_line": block["end"] + 1,
                  "completed_sweeps": validation.get("completed_sweeps", 0), "workload": content}
        workload_report = "\nVM extraction and workload validation: " + ("PASS" if record["pass"] else "FAIL") + "\n"
        workload_report += "".join("ERROR: " + error + "\n" for error in run_errors)
        report_path = destination / "report.md"
        with report_path.open("a") as report_file:
            report_file.write(workload_report)
        (destination / "extraction-validation.json").write_text(json.dumps(record, indent=2) + "\n")
        runs[name] = record
        if not record["pass"]:
            errors.append(f"Run {name} failed validation")

    summary = {"pass": not errors, "errors": errors, "vm_result": result,
               "source_log": str(args.log.resolve()), "source_bytes": len(raw),
               "source_sha256": hashlib.sha256(raw).hexdigest(),
               "collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "scripts_dir": str(scripts), "expected_blocks": expected,
               "tap_suites": taps, "final_global_stats": stats, "kernel_warnings": warnings, "runs": runs}
    encoded_summary = json.dumps(summary, indent=2) + "\n"
    (args.out / "vm-validation.json").write_text(encoded_summary)
    (args.out / "collection.json").write_text(encoded_summary)
    report = ["VM collection: " + ("PASS" if summary["pass"] else "FAIL"), "",
              f"Source: `{summary['source_log']}`", f"SHA256: `{summary['source_sha256']}`", "",
              "| Workload | Validation | Completed sweeps | Report |", "|---|---|---:|---|"]
    for name, record in runs.items():
        report.append(f"| {name} | {'PASS' if record['pass'] else 'FAIL'} | {record['completed_sweeps']} | [{name}]({name}/report.md) |")
    report += ["", "Global cleanup: " + ", ".join(f"{k}={stats.get(k, 'missing')}" for k in ("entries", "managed", "isolated"))]
    report += ["ERROR: " + error for error in errors]
    (args.out / "report.md").write_text("\n".join(report) + "\n")
    print(json.dumps({"pass": summary["pass"], "out": str(args.out), "errors": errors}, indent=2))
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
