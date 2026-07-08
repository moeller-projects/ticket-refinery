"""metrics.MetricsCollector: counters, timers, snapshot independence."""
import time

import pytest

import metrics as metrics_mod


def test_increment_starts_at_one_by_default():
    c = metrics_mod.MetricsCollector()
    c.increment("foo")
    assert c.snapshot().counters == {"foo": 1}


def test_increment_with_explicit_value():
    c = metrics_mod.MetricsCollector()
    c.increment("bar", value=5)
    c.increment("bar", value=2)
    assert c.snapshot().counters["bar"] == 7


def test_independent_counter_names():
    c = metrics_mod.MetricsCollector()
    c.increment("a")
    c.increment("b")
    snap = c.snapshot()
    assert snap.counters == {"a": 1, "b": 1}


def test_timer_records_elapsed_ms():
    c = metrics_mod.MetricsCollector()
    with c.timer("phase"):
        time.sleep(0.01)
    snap = c.snapshot()
    assert "phase" in snap.timings_ms
    assert len(snap.timings_ms["phase"]) == 1
    assert snap.timings_ms["phase"][0] > 0


def test_timer_records_multiple_samples():
    c = metrics_mod.MetricsCollector()
    for _ in range(3):
        with c.timer("x"):
            time.sleep(0.001)
    snap = c.snapshot()
    assert len(snap.timings_ms["x"]) == 3


def test_timer_still_records_on_exception():
    c = metrics_mod.MetricsCollector()
    with pytest.raises(RuntimeError):
        with c.timer("phase"):
            raise RuntimeError("boom")
    snap = c.snapshot()
    assert "phase" in snap.timings_ms  # the `finally` still ran


def test_snapshot_is_immutable_copy():
    c = metrics_mod.MetricsCollector()
    c.increment("a", value=1)
    snap = c.snapshot()
    c.increment("a", value=10)
    # Snapshot taken before should not reflect later increments.
    assert snap.counters == {"a": 1}


def test_format_summary_returns_human_readable_strings():
    c = metrics_mod.MetricsCollector()
    c.increment("queued", value=3)
    with c.timer("t"):
        time.sleep(0.001)
    out = c.format_summary()
    assert "counters" in out and "queued=3" in out
    assert "timings_ms" in out and "t=" in out


def test_empty_collector_summary_is_well_formed():
    out = metrics_mod.MetricsCollector().format_summary()
    assert "no metrics" in out.lower()


def test_metrics_independent_from_logging():
    """Adding a counter does not require logging setup."""
    c = metrics_mod.MetricsCollector()
    c.increment("only_metric")
    snap = c.snapshot()
    # No log handler touched — purely in-memory.
    assert snap.counters == {"only_metric": 1}
