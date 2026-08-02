from schema_retrieval_benchmark import run_benchmark


def test_new_retriever_benchmark_is_measurably_compact_and_precise(tmp_path):
    report = run_benchmark(tmp_path)
    summary = report["summary"]

    assert summary["top1_accuracy"]["new"] >= summary["top1_accuracy"]["legacy"]
    assert summary["top3_accuracy"]["new"] >= summary["top3_accuracy"]["legacy"]
    assert summary["precision"]["new"] > summary["precision"]["legacy"]
    assert summary["recall"]["new"] >= summary["recall"]["legacy"]
    assert summary["average_context_chars"]["new"] < summary["average_context_chars"]["legacy"]
    assert summary["average_latency_ms"]["new"] < 100
