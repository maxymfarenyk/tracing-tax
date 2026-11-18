"""Unit tests of the adaptive sampler's rate logic (stdlib unittest).

    cd services && uv run python -m unittest discover -s adaptive_sampler/tests -v
"""

import unittest

from adaptive_sampler import AdaptiveSampler, create_sampler
from opentelemetry.sdk.trace.sampling import Decision, ParentBased
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context

WINDOW_S = 5.0
N_IDS = 10_000
# Trace IDs whose low 64 bits are spread evenly over [0, 2**64).
EVEN_IDS = [(i * 2**64) // N_IDS for i in range(N_IDS)]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def feed(sampler: AdaptiveSampler, clock: FakeClock, rate: float, seconds: float) -> None:
    """Send `rate` root spans per second for `seconds`, evenly spaced."""
    for _ in range(round(rate * seconds)):
        sampler.should_sample(None, 1, "root")
        clock.now += 1 / rate


class AdaptiveSamplerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.sampler = AdaptiveSampler(15, window_s=WINDOW_S, clock=self.clock)

    def kept_share(self) -> float:
        """Share of EVEN_IDS kept, all within the current window."""
        kept = sum(
            self.sampler.should_sample(None, tid, "root").decision is Decision.RECORD_AND_SAMPLE
            for tid in EVEN_IDS
        )
        return kept / N_IDS

    def test_first_window_keeps_everything(self) -> None:
        self.assertEqual(self.sampler.probability, 1.0)
        self.assertEqual(self.kept_share(), 1.0)

    def test_below_cap_keeps_everything(self) -> None:
        feed(self.sampler, self.clock, rate=10, seconds=4 * WINDOW_S)
        self.assertEqual(self.sampler.probability, 1.0)

    def test_above_cap_limits_to_cap(self) -> None:
        feed(self.sampler, self.clock, rate=30, seconds=4 * WINDOW_S)
        self.assertAlmostEqual(self.sampler.probability, 0.5, places=2)
        self.assertAlmostEqual(self.kept_share(), 0.5, places=2)

    def test_recovers_when_load_drops(self) -> None:
        feed(self.sampler, self.clock, rate=60, seconds=2 * WINDOW_S)
        self.assertAlmostEqual(self.sampler.probability, 0.25, places=2)
        feed(self.sampler, self.clock, rate=5, seconds=2 * WINDOW_S)
        self.assertEqual(self.sampler.probability, 1.0)

    def test_recovers_after_idle_gap(self) -> None:
        feed(self.sampler, self.clock, rate=60, seconds=2 * WINDOW_S)
        # The current window still holds a full window of 60/s; after a long
        # silence its rate is measured over the whole gap.
        feed(self.sampler, self.clock, rate=60, seconds=WINDOW_S * 0.9)
        self.clock.now += 100
        self.sampler.should_sample(None, 1, "root")
        self.assertEqual(self.sampler.probability, 1.0)

    def test_invalid_cap(self) -> None:
        for cap in (0, -1, float("nan")):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                AdaptiveSampler(cap)


class FactoryTest(unittest.TestCase):
    def test_returns_parent_based(self) -> None:
        sampler = create_sampler("15")
        self.assertIsInstance(sampler, ParentBased)
        self.assertIn("AdaptiveSampler{cap=15/s", sampler.get_description())

    def test_rejects_bad_arg(self) -> None:
        for arg in (None, "", "abc", "0"):
            with self.subTest(arg=arg), self.assertRaises(ValueError):
                create_sampler(arg)

    def test_follows_sampled_remote_parent(self) -> None:
        clock = FakeClock()
        root = AdaptiveSampler(1, window_s=WINDOW_S, clock=clock)
        feed(root, clock, rate=100, seconds=2 * WINDOW_S)  # p = 0.01
        sampler = ParentBased(root=root)
        parent = SpanContext(trace_id=2**64 - 1, span_id=1, is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED))
        context = set_span_in_context(NonRecordingSpan(parent))
        result = sampler.should_sample(context, parent.trace_id, "child")
        self.assertIs(result.decision, Decision.RECORD_AND_SAMPLE)
        # Without the parent the same trace ID would be dropped.
        self.assertIs(sampler.should_sample(None, parent.trace_id, "root").decision, Decision.DROP)


if __name__ == "__main__":
    unittest.main()
