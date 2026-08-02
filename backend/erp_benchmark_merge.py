import argparse
import json
from pathlib import Path
from typing import Any

from erp_accuracy_benchmark import benchmark_summary, load_benchmark
from erp_benchmark_runner import _aggregate


def merge_benchmark_reports(
    inputs: list[str],
    output: str,
    *,
    expected_split: str | None = None,
) -> dict[str, Any]:
    reports = [
        json.loads(Path(path).read_text(encoding="utf-8"))
        for path in inputs
    ]
    if not reports:
        raise ValueError("At least one benchmark report is required.")
    databases = {report.get("database") for report in reports}
    fingerprints = {report.get("schema_fingerprint") for report in reports}
    models = {
        json.dumps(report.get("production_model"), sort_keys=True)
        for report in reports
    }
    if len(databases) != 1 or len(fingerprints) != 1 or len(models) != 1:
        raise ValueError(
            "Benchmark shards must use the same database, schema, and model."
        )

    records = []
    seen_ids = set()
    for report in reports:
        if report.get("status") != "complete":
            raise ValueError(
                f"Incomplete benchmark shard: {report.get('trace_output')}"
            )
        for record in report.get("records", []):
            if record["id"] in seen_ids:
                raise ValueError(f"Duplicate benchmark record: {record['id']}")
            seen_ids.add(record["id"])
            records.append(record)
    records.sort(key=lambda record: record["id"])

    expected_items = [
        item
        for item in load_benchmark()
        if expected_split is None or item.split == expected_split
    ]
    expected_ids = {item.id for item in expected_items}
    if seen_ids != expected_ids:
        missing = sorted(expected_ids - seen_ids)
        unexpected = sorted(seen_ids - expected_ids)
        raise ValueError(
            f"Merged benchmark coverage mismatch. "
            f"Missing={missing[:10]} unexpected={unexpected[:10]}"
        )

    merged = {
        **_aggregate(records),
        "benchmark": benchmark_summary(expected_items),
        "database": reports[0]["database"],
        "schema_fingerprint": reports[0]["schema_fingerprint"],
        "production_model": reports[0]["production_model"],
        "records": records,
        "status": "complete",
        "completed": len(records),
        "total": len(expected_items),
        "source_traces": inputs,
        "trace_output": output,
    }
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(merged, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge deterministic ERP benchmark shards."
    )
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--split", choices=["development", "holdout"])
    args = parser.parse_args()
    report = merge_benchmark_reports(
        args.input,
        args.output,
        expected_split=args.split,
    )
    print(
        json.dumps(
            {
                "output": report["trace_output"],
                "completed": report["completed"],
                "summary": report["summary"],
                "latency": report["latency"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
