"""Steady load for the tracing-tax stand.

Every simulated user places one order per second, so the number of users equals
the request rate (RPS). That holds while responses stay well under 1 s (they
take 0.1-0.25 s at the levels below). Load levels used in the experiment:

    low:  10 users = 10 RPS   (below the adaptive sampler cap)
    high: 30 users = 30 RPS   (above it)

Above ~30 RPS the orders service congests (median latency 240 ms at 30 RPS,
380 ms at 50 RPS, independent of tracing), which would bury tracing effects.

Extra options used by scripts/run_experiment.py (both optional):

    --warmup-s N          reset all stats N s after the start; only requests
                          after that are logged
    --requests-json PATH  write the measured window (Unix times) and every
                          request in it as [start, latency ms, failed, order id]

Run via docker compose, see README "Load generation".
"""

import itertools
import json
import random
import time

import gevent
from locust import HttpUser, constant_throughput, events, task


# Unique order id per request (within a run), so the runner can find the trace
# of every logged request in Jaeger by its URL.
_order_ids = itertools.count(random.randint(10**8, 9 * 10**8))


class Shopper(HttpUser):
    wait_time = constant_throughput(1)  # at most 1 request per second per user

    def on_start(self) -> None:
        # Random phase: without it all users fire at the same moment every
        # second, and the single-threaded services queue a burst of N requests
        # (at 50 RPS that inflated the median latency from ~100 ms to ~390 ms).
        gevent.sleep(random.random())

    @task
    def place_order(self) -> None:
        # Unique id, but one stats row for all of them. 5xx responses (fault
        # injection) are counted as failures automatically.
        order_id = next(_order_ids)
        self.client.get(f"/order/{order_id}", name="/order/{id}", context={"order_id": order_id})


# --- Per-request log of the measured window ------------------------------------
# Locust's --csv files are rewritten once per second and miss the last second,
# and they count requests by completion. The runner instead counts requests by
# start time, from this log.

_log: dict = {"measure_start": 0.0, "measure_end": 0.0, "requests": []}


def _start_measuring() -> None:
    _log.update(measure_start=time.time(), requests=[])


@events.init_command_line_parser.add_listener
def _add_options(parser) -> None:
    parser.add_argument("--warmup-s", type=float, default=0.0, help="Reset stats after this many seconds")
    parser.add_argument("--requests-json", default="", help="Write the per-request log here")


@events.test_start.add_listener
def _on_test_start(environment, **_kwargs) -> None:
    _start_measuring()
    warmup_s = environment.parsed_options.warmup_s
    if warmup_s > 0:

        def end_warmup() -> None:
            gevent.sleep(warmup_s)
            environment.stats.reset_all()  # console summary = measured window only
            _start_measuring()

        gevent.spawn(end_warmup)


@events.request.add_listener
def _on_request(start_time, response_time, exception, context, **_kwargs) -> None:
    # [start (Unix s), latency (ms), failed (0/1), order id]
    failed = int(exception is not None)
    _log["requests"].append([round(start_time, 4), round(response_time, 2), failed, context.get("order_id")])


@events.test_stop.add_listener
def _on_test_stop(environment, **_kwargs) -> None:
    _log["measure_end"] = time.time()
    if path := environment.parsed_options.requests_json:
        with open(path, "w") as f:
            json.dump(_log, f, separators=(",", ":"))
