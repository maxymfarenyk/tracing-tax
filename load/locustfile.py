"""Steady load for the tracing-tax stand.

Every simulated user places one order per second, so the number of users equals
the request rate (RPS). That holds while responses stay well under 1 s (they
take 0.1-0.25 s at the levels below). Load levels used in the experiment:

    low:  10 users = 10 RPS   (below the adaptive sampler cap)
    high: 30 users = 30 RPS   (above it)

Above ~30 RPS the orders service congests (median latency 240 ms at 30 RPS,
380 ms at 50 RPS, independent of tracing), which would bury tracing effects.

Run via docker compose, see README "Load generation".
"""

import random

import gevent
from locust import HttpUser, constant_throughput, task


class Shopper(HttpUser):
    wait_time = constant_throughput(1)  # at most 1 request per second per user

    def on_start(self) -> None:
        # Random phase: without it all users fire at the same moment every
        # second, and the single-threaded services queue a burst of N requests
        # (at 50 RPS that inflated the median latency from ~100 ms to ~390 ms).
        gevent.sleep(random.random())

    @task
    def place_order(self) -> None:
        # Random id, but one stats row for all of them. 5xx responses (fault
        # injection) are counted as failures automatically.
        self.client.get(f"/order/{random.randint(1, 1_000_000)}", name="/order/{id}")
