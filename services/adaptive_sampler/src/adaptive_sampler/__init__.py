"""Adaptive (rate-limited) head sampler for scenario E.

Keeps every trace while traffic is low and caps the kept traces at about
`cap` per second when traffic is high, so the cost of tracing stays bounded:

    probability = min(1, cap / current_rate)

`current_rate` is the number of new traces (root spans) per second, measured
over a tumbling window of `window_s` seconds. The probability computed at the
end of a window is used for the whole next window, so the sampler lags traffic
changes by one window (the experiment's warm-up covers that). Whether a trace is
kept is decided from its trace ID, the same way as `TraceIdRatioBased`.

The root sampler is wrapped in `ParentBased`: the gateway decides, and the
downstream services follow the decision in `traceparent`, so a trace is kept or
dropped as a whole. Only root spans reach `AdaptiveSampler`, so it counts traces.

Selected through the SDK's `opentelemetry_traces_sampler` entry point:

    OTEL_TRACES_SAMPLER=adaptive
    OTEL_TRACES_SAMPLER_ARG=15     # cap, traces per second

Based on the windowed sampler in OneUptime's "Adaptive sampling that adjusts to
traffic volume" (2026). Unlike that version it needs no background thread: the
window rolls over lazily inside `should_sample`.
"""

import threading
import time
from collections.abc import Callable, Sequence

from opentelemetry.context import Context
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import Link, SpanKind, TraceState, get_current_span
from opentelemetry.util.types import Attributes

__all__ = ["AdaptiveSampler", "create_sampler"]

DEFAULT_WINDOW_S = 5.0


class AdaptiveSampler(Sampler):
    """Root sampler that keeps about `cap` traces per second at most."""

    def __init__(
        self,
        cap: float,
        window_s: float = DEFAULT_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not cap > 0:
            raise ValueError(f"cap must be a positive number of traces/s, got {cap!r}")
        if not window_s > 0:
            raise ValueError(f"window_s must be positive, got {window_s!r}")
        self._cap = cap
        self._window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._window_start = clock()
        self._count = 0
        # Nothing has been measured yet, so the first window keeps everything.
        self._probability = 1.0

    @property
    def probability(self) -> float:
        with self._lock:
            return self._probability

    def _tick(self) -> float:
        """Count one new trace and return the probability to sample it with."""
        with self._lock:
            now = self._clock()
            elapsed = now - self._window_start
            if elapsed >= self._window_s:
                # Elapsed, not window_s: after an idle gap the rate is low.
                rate = self._count / elapsed
                self._probability = min(1.0, self._cap / rate) if rate > 0 else 1.0
                self._window_start = now
                self._count = 0
            self._count += 1
            return self._probability

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        probability = self._tick()
        bound = TraceIdRatioBased.get_bound_for_rate(probability)
        sampled = trace_id & TraceIdRatioBased.TRACE_ID_LIMIT < bound
        return SamplingResult(
            Decision.RECORD_AND_SAMPLE if sampled else Decision.DROP,
            attributes if sampled else None,
            get_current_span(parent_context).get_span_context().trace_state,
        )

    def get_description(self) -> str:
        return f"AdaptiveSampler{{cap={self._cap:g}/s, window={self._window_s:g}s}}"


def create_sampler(arg: str | None) -> Sampler:
    """Entry point factory: `arg` is OTEL_TRACES_SAMPLER_ARG, the cap in traces/s."""
    try:
        cap = float(arg or "")
    except ValueError:
        raise ValueError(f"OTEL_TRACES_SAMPLER_ARG must be the cap in traces/s, got {arg!r}") from None
    return ParentBased(root=AdaptiveSampler(cap))
