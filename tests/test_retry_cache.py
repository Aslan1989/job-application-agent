"""Retry/backoff behaviour and cache persistence. No real sleeping, no network."""
from __future__ import annotations

import json

import pytest

from job_agent.cache import DescriptionCache, profile_fingerprint
from job_agent.collectors.arbeitsagentur import ArbeitsagenturCollector
from job_agent.retry import RetryBudgetExceeded, is_retryable, retry_call


class Boom(Exception):
    def __init__(self, status=None):
        super().__init__(f"status={status}")
        self.response = type("R", (), {"status_code": status})() if status else None


def test_successful_call_never_sleeps():
    slept: list[float] = []
    assert retry_call(lambda: "ok", sleep=slept.append) == "ok"
    assert slept == []


def test_backoff_is_exponential_and_capped():
    slept: list[float] = []
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise Boom(503)
        return "recovered"

    result = retry_call(flaky, attempts=4, base_delay=1.0, max_delay=3.0,
                        sleep=slept.append, jitter=lambda ceiling: ceiling,
                        label="test")
    assert result == "recovered"
    # 1s, 2s, then capped at 3s instead of 4s.
    assert slept == [1.0, 2.0, 3.0]


def test_jitter_keeps_delays_within_the_ceiling():
    slept: list[float] = []
    with pytest.raises(RetryBudgetExceeded):
        retry_call(lambda: (_ for _ in ()).throw(Boom(429)), attempts=4,
                   base_delay=2.0, sleep=slept.append, label="jittered")
    assert len(slept) == 3
    for index, delay in enumerate(slept):
        assert 0 <= delay <= 2.0 * (2 ** index)


def test_permanent_errors_are_not_retried():
    """A 403 or 404 is a configuration problem; retrying only wastes quota."""
    for status in (400, 401, 403, 404):
        slept: list[float] = []
        attempts = {"n": 0}

        def failing():
            attempts["n"] += 1
            raise Boom(status)

        with pytest.raises(Boom):
            retry_call(failing, attempts=4, sleep=slept.append)
        assert attempts["n"] == 1, f"status {status} should not be retried"
        assert slept == []


def test_retryable_classification():
    assert is_retryable(Boom(429), retry_on=(Exception,))
    assert is_retryable(Boom(None), retry_on=(Exception,))  # timeout/connection
    assert not is_retryable(Boom(404), retry_on=(Exception,))
    assert not is_retryable(KeyboardInterrupt(), retry_on=(Exception,))


def test_exhausted_budget_message_carries_no_payload():
    with pytest.raises(RetryBudgetExceeded) as caught:
        retry_call(lambda: (_ for _ in ()).throw(Boom(500)), attempts=2,
                   sleep=lambda _: None, label="BA search")
    message = str(caught.value)
    assert "BA search failed after 2 attempts" in message
    assert "status=500" not in message, "the upstream body must not be echoed"


# --- collector integration -------------------------------------------------


class Response:
    def __init__(self, payload, status=None):
        self.payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status:
            raise Boom(self.status)

    def json(self):
        return self.payload


PAYLOAD = {"ergebnisliste": [{
    "referenznummer": "10001-12345678-S", "stellenangebotsTitel": "Backend Developer",
    "firma": "ACME GmbH", "stellenlokationen": [{"ort": "Frankfurt am Main"}]}]}


def test_ba_collector_recovers_from_a_transient_error():
    calls = {"n": 0}

    class Session:
        def get(self, url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return Response({}, status=503)
            return Response(PAYLOAD)

    collector = ArbeitsagenturCollector(session=Session(), attempts=3,
                                        sleep=lambda _: None)
    hits = collector.search(term="Backend Developer", city="Frankfurt am Main")
    assert [hit.job_id for hit in hits] == ["ba:10001-12345678-S"]
    assert calls["n"] == 2


def test_ba_collector_default_is_single_shot():
    """v0.7 behaviour is preserved unless retries are requested explicitly."""
    calls = {"n": 0}

    class Session:
        def get(self, url, **kwargs):
            calls["n"] += 1
            return Response({}, status=503)

    with pytest.raises(Boom):
        ArbeitsagenturCollector(session=Session()).search(
            term="x", city="Frankfurt am Main")
    assert calls["n"] == 1


# --- cache -----------------------------------------------------------------


def test_cache_round_trips_and_file_is_owner_only(tmp_path):
    path = tmp_path / "cache.json"
    cache = DescriptionCache(path)
    fingerprint = profile_fingerprint({"skills": []}, "PROMPT")
    cache.remember_assessment("ba:1", "advert text " * 20, fingerprint, {"score": 80})
    assert cache.save() is True
    assert cache.save() is False, "a clean cache must not rewrite the file"
    assert oct(path.stat().st_mode)[-3:] == "600"

    reloaded = DescriptionCache.load(path)
    hit = reloaded.assessment_for("ba:1", "advert text " * 20, fingerprint)
    assert hit is not None and hit.assessment["score"] == 80
    assert reloaded.description_for("ba:1").startswith("advert text")


def test_new_description_drops_the_stale_assessment(tmp_path):
    cache = DescriptionCache(tmp_path / "cache.json")
    fingerprint = profile_fingerprint({}, "PROMPT")
    cache.remember_assessment("ba:1", "old advert " * 20, fingerprint, {"score": 80})
    cache.remember_description("ba:1", "new advert " * 20)
    assert cache.assessment_for("ba:1", "new advert " * 20, fingerprint) is None
    assert cache.description_for("ba:1").startswith("new advert")


def test_unknown_schema_version_is_ignored(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"schema_version": "0.1", "entries": {"ba:1": {}}}),
                    encoding="utf-8")
    assert DescriptionCache.load(path).entries == {}


def test_pruning_keeps_the_most_recent_entries(tmp_path):
    cache = DescriptionCache(tmp_path / "cache.json", max_entries=2)
    fingerprint = profile_fingerprint({}, "PROMPT")
    for index in range(4):
        cache.remember_assessment(f"ba:{index}", f"advert {index} " * 20,
                                  fingerprint, {"score": 80})
        cache.entries[f"ba:{index}"]["scored_at"] = f"2026-11-0{index + 1}T00:00:00Z"
    cache.save()
    assert sorted(DescriptionCache.load(tmp_path / "cache.json").entries) == ["ba:2", "ba:3"]


def test_cache_rejects_absurd_limits():
    with pytest.raises(ValueError):
        DescriptionCache(max_entries=0)
