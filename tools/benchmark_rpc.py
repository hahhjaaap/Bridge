"""Reproducible stdio JSON-RPC microbenchmark using a disposable Python child.

This does not benchmark ChatGPT tunnel latency, Windows UI work, or real tools.
It validates concurrent response routing and reports in-process RPC latency.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from udb.transport import StdioChild

WORKER = """import json
import sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    reply = {"jsonrpc": "2.0", "id": request["id"], "result":
             {"value": request.get("params", {}).get("value")}}
    print(json.dumps(reply, separators=(",", ":")), flush=True)
"""


def run(calls: int = 100, parallel: int = 4) -> dict:
    if not 1 <= calls <= 10000 or not 1 <= parallel <= 10:
        raise ValueError("calls 1..10000 and parallel 1..10 required")
    with tempfile.TemporaryDirectory(prefix="udb-rpc-bench-") as folder:
        child = StdioChild(
            "benchmark", [sys.executable, "-u", "-c", WORKER], folder, lambda _: None
        )
        child.start()
        try:
            warmup = child.request("echo", {"value": -1}, timeout=10)
            if warmup.get("result", {}).get("value") != -1:
                raise RuntimeError("RPC benchmark warmup failed")

            def request(value: int):
                start = time.perf_counter()
                reply = child.request("echo", {"value": value}, timeout=10)
                elapsed = (time.perf_counter() - start) * 1000
                if reply.get("result", {}).get("value") != value:
                    raise RuntimeError("RPC replies were misrouted")
                return elapsed

            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=parallel) as workers:
                durations = list(workers.map(request, range(calls)))
            wall = time.perf_counter() - started
        finally:
            child.close()
    ordered = sorted(durations)
    return {
        "calls": calls,
        "parallel": parallel,
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[int(0.95 * (calls - 1))], 3),
        "throughput_calls_per_second": round(calls / max(wall, 1e-9), 1),
        "backend": "local_disposable_python_echo",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calls", type=int, default=100)
    parser.add_argument("--parallel", type=int, default=4)
    args = parser.parse_args()
    print(json.dumps(run(args.calls, args.parallel), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
