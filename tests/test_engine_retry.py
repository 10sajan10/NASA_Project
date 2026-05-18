"""Retry + dead-letter tests.

Covers the pure RetryPolicy/attempt_with_retry primitive and its
integration into PipelineRunner (StepResult.attempts / dead_letter).
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    Pipeline,
    PipelineRunner,
    ProducerRegistry,
    RetryPolicy,
    SerialBackend,
    ThreadBackend,
    attempt_with_retry,
    make_backend,
)


# --------------------------------------------------- pure function
def test_default_policy_is_one_shot():
    """No-arg RetryPolicy = single attempt, no retry."""
    pol = RetryPolicy()
    assert pol.max_attempts == 1


def test_invalid_policy_raises():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(backoff_seconds=-1.0)


def test_success_on_first_try_uses_one_attempt():
    pol = RetryPolicy(max_attempts=3)
    result, exc, attempts = attempt_with_retry(lambda: 42, policy=pol)
    assert result == 42 and exc is None and attempts == 1


def test_retries_until_success():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise IOError("transient")
        return "ok"

    pol = RetryPolicy(max_attempts=5)
    result, exc, attempts = attempt_with_retry(flaky, policy=pol)
    assert result == "ok" and exc is None and attempts == 3


def test_dead_letter_after_max_attempts():
    pol = RetryPolicy(max_attempts=3)

    def always_fail():
        raise IOError("permanent")

    result, exc, attempts = attempt_with_retry(always_fail, policy=pol)
    assert result is None
    assert isinstance(exc, IOError)
    assert attempts == 3


def test_non_retryable_exception_fails_fast():
    pol = RetryPolicy(max_attempts=5,
                       retryable_exceptions=(IOError,))

    def value_error():
        raise ValueError("bad config")

    result, exc, attempts = attempt_with_retry(value_error, policy=pol)
    assert isinstance(exc, ValueError)
    assert attempts == 1   # fast-fail, no retries used


def test_backoff_actually_sleeps():
    pol = RetryPolicy(max_attempts=3, backoff_seconds=0.05)
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise IOError("never works")

    t0 = time.monotonic()
    attempt_with_retry(always_fail, policy=pol)
    # 2 retries (between 3 attempts) at 0.05 * 1 + 0.05 * 2 = 0.15s
    assert time.monotonic() - t0 >= 0.10


# --------------------------------------------------- runner integration
@dataclass
class _CountingProducer:
    """Fails the first n_fails times, then succeeds."""
    name: str = "flaky"
    produces: tuple = ("x",)
    requires: tuple = ()
    n_fails: int = 0
    _calls: list = field(default_factory=list)

    def run(self, cube, request):
        self._calls.append(time.monotonic())
        if len(self._calls) <= self.n_fails:
            raise IOError(f"flake attempt {len(self._calls)}")
        return ["x"]


def test_runner_retries_until_success():
    reg = ProducerRegistry()
    prod = _CountingProducer(n_fails=2)
    reg.register(prod)
    runner = PipelineRunner(
        reg, backend=SerialBackend(),
        retry_policy=RetryPolicy(max_attempts=3), verbose=False)
    res = runner.run(object(), Pipeline().add("flaky"))
    assert res.ok
    sr = res.by_name()["flaky"]
    assert sr.status == "ok"
    assert sr.attempts == 3
    assert sr.dead_letter is False
    assert len(prod._calls) == 3


def test_runner_dead_letters_after_exhausting_retries():
    reg = ProducerRegistry()
    prod = _CountingProducer(n_fails=99)
    reg.register(prod)
    runner = PipelineRunner(
        reg, backend=SerialBackend(),
        retry_policy=RetryPolicy(max_attempts=3), verbose=False)
    res = runner.run(object(), Pipeline().add("flaky"))
    assert not res.ok
    sr = res.by_name()["flaky"]
    assert sr.status == "error"
    assert sr.attempts == 3
    assert sr.dead_letter is True


def test_runner_thread_backend_retries():
    reg = ProducerRegistry()
    p1 = _CountingProducer(name="p1", produces=("v1",), n_fails=1)
    p2 = _CountingProducer(name="p2", produces=("v2",), n_fails=2)
    reg.register(p1); reg.register(p2)
    pipeline = (Pipeline().add("p1").add("p2"))
    backend = ThreadBackend(max_workers=2)
    try:
        runner = PipelineRunner(
            reg, backend=backend,
            retry_policy=RetryPolicy(max_attempts=4), verbose=False)
        res = runner.run(object(), pipeline)
    finally:
        backend.shutdown()
    assert res.ok
    assert res.by_name()["p1"].attempts == 2
    assert res.by_name()["p2"].attempts == 3


def test_runner_default_policy_is_one_shot_backwards_compat():
    """Without retry_policy, a failing producer fails on attempt 1."""
    reg = ProducerRegistry()
    prod = _CountingProducer(n_fails=99)
    reg.register(prod)
    runner = PipelineRunner(reg, backend=SerialBackend(), verbose=False)
    res = runner.run(object(), Pipeline().add("flaky"))
    sr = res.by_name()["flaky"]
    assert sr.attempts == 1
    # max_attempts=1, attempts >= 1 -> dead_letter=True
    assert sr.dead_letter is True
