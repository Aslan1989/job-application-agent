"""Normalized schema, cross-source deduplication and the ATS board adapters."""
from __future__ import annotations

import pytest

from job_agent.collectors.ats import GreenhouseSource, LeverSource
from job_agent.sources import (NormalizedJob, apply_track_quota, content_fingerprint,
                               deduplicate, interleave_by_source, location_matches,
                               normalize_url, strip_html, term_matches)


def job(**overrides) -> NormalizedJob:
    base = dict(source_key="ba", source_label="BA Jobsuche", external_id="10001-1234-S",
                company="ACME GmbH", role="Backend Developer",
                location="Frankfurt am Main",
                job_url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1234-S")
    return NormalizedJob(**{**base, **overrides})


class Response:
    def __init__(self, payload, error=None):
        self.payload = payload
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise RuntimeError(self.error)

    def json(self):
        return self.payload


class Session:
    """Records every call so tests can assert no extra requests were made."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        for marker, payload in self.payloads.items():
            if marker in url:
                if isinstance(payload, Exception):
                    raise payload
                return Response(payload)
        return Response({}, error="404")


# --- normalization ---------------------------------------------------------


def test_url_normalization_collapses_tracking_noise():
    assert (normalize_url("https://WWW.Example.com/jobs/7/?utm_source=x#apply")
            == "https://example.com/jobs/7")
    assert (normalize_url("http://example.com/jobs/7")
            == normalize_url("https://www.example.com/jobs/7/"))
    # A posting id in the query is identity, not tracking, so it is preserved.
    assert "gh_jid=55" in normalize_url("https://boards.greenhouse.io/x?gh_jid=55&utm_medium=y")
    for bad in ("", "not-a-url", "ftp://example.com/x", "javascript:alert(1)"):
        assert normalize_url(bad) == ""


def test_normalized_job_rejects_malformed_records():
    with pytest.raises(ValueError, match="job_url"):
        job(job_url="not-a-url")
    with pytest.raises(ValueError, match="company"):
        job(company="   ")
    with pytest.raises(ValueError, match="Invalid source key"):
        job(source_key="Bad Key")
    assert job().job_id == "ba:10001-1234-S"


def test_fingerprint_ignores_case_and_accents():
    assert (content_fingerprint("ACME GmbH", "Backend Developer", "Frankfurt am Main")
            == content_fingerprint("acme  gmbh", "backend developer", "FRANKFURT AM MAIN"))
    assert content_fingerprint("", "role", "city") == ""


def test_html_stripping_removes_scripts_and_double_escaping():
    assert "evil" not in strip_html("<p>Hello<script>evil()</script></p>")
    # Greenhouse ships HTML inside an escaped string.
    assert "Aufgaben" in strip_html("&lt;h2&gt;Aufgaben&lt;/h2&gt;&lt;p&gt;PHP&lt;/p&gt;")


def test_location_and_term_matching():
    assert location_matches("Frankfurt, Germany", ["Frankfurt am Main"])
    assert location_matches("Stuttgart", ["Frankfurt am Main", "Stuttgart"])
    # Frankfurt (Oder) is a different city ~500 km away.
    assert not location_matches("Frankfurt (Oder)", ["Frankfurt am Main"])
    assert not location_matches("Berlin", ["Frankfurt am Main"])
    assert location_matches("Remote - Germany", ["Frankfurt am Main"],
                            allow_remote_germany=True)
    assert not location_matches("Remote - Germany", ["Frankfurt am Main"])
    assert term_matches("Senior Python Developer", "python developer")
    assert not term_matches("Scala Developer", "c developer")


# --- cross-source deduplication -------------------------------------------


def test_cross_source_deduplication_by_url_and_fingerprint():
    """The same advert on three boards must create exactly one row."""
    canonical = job()
    same_url_other_source = job(
        source_key="arbeitnow", source_label="Arbeitnow", external_id="acme-backend-dev",
        job_url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1234-S?utm_source=x")
    same_posting_other_url = job(
        source_key="lever", source_label="Lever", external_id="acme-9f2",
        job_url="https://jobs.lever.co/acme/9f2")
    different = job(source_key="greenhouse", source_label="Greenhouse",
                    external_id="acme-77", role="Data Engineer",
                    job_url="https://boards.greenhouse.io/acme/jobs/77")

    result = deduplicate([canonical, same_url_other_source,
                          same_posting_other_url, different])
    assert [item.job_id for item in result.unique] == ["ba:10001-1234-S", "greenhouse:acme-77"]
    assert result.duplicates == 2
    assert result.by_source == {"ba": 1, "greenhouse": 1}


def test_deduplication_respects_rows_already_in_the_sheet():
    known = job()
    result = deduplicate([known], known_ids=["ba:10001-1234-S"])
    assert result.unique == [] and result.duplicates == 1
    by_url = deduplicate([known], known_urls=["https://arbeitsagentur.de/jobsuche/jobdetail/10001-1234-S/"])
    assert by_url.unique == [] and by_url.duplicates == 1


def test_track_quota_prevents_one_category_dominating():
    jobs = [job(external_id=f"10001-{index:04d}-S", track="graduate_entry",
                job_url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-{index:04d}-S")
            for index in range(5)]
    jobs.append(job(external_id="10001-9999-S", track="ai_engineering",
                    job_url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-9999-S"))
    kept = apply_track_quota(jobs, 2)
    assert [item.track for item in kept] == ["graduate_entry", "graduate_entry", "ai_engineering"]


# --- Greenhouse ------------------------------------------------------------


GREENHOUSE_PAYLOAD = {"jobs": [
    {"id": 4567, "title": "AI Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/4567",
     "location": {"name": "Frankfurt am Main"},
     "content": "&lt;p&gt;Build RAG pipelines with Python and embeddings.&lt;/p&gt;"},
    {"id": 4568, "title": "Senior Director Sales", "absolute_url": "https://boards.greenhouse.io/acme/jobs/4568",
     "location": {"name": "Berlin"}, "content": "&lt;p&gt;Sales leadership.&lt;/p&gt;"},
    {"id": None, "title": "Broken", "absolute_url": "", "location": {}, "content": ""},
]}


def test_greenhouse_adapter_normalizes_and_filters_locally():
    session = Session({"boards-api.greenhouse.io": GREENHOUSE_PAYLOAD})
    source = GreenhouseSource(["acme"], session=session, attempts=1)
    found = source.discover(term="AI Engineer", city="Frankfurt am Main", size=10)
    assert [item.job_id for item in found] == ["greenhouse:acme-4567"]
    assert found[0].company == "acme"
    assert "RAG pipelines" in source.describe(found[0])
    # Second query must not trigger a second HTTP request.
    source.discover(term="AI Engineer", city="Frankfurt am Main", size=10)
    assert len(session.calls) == 1
    assert session.calls[0][1]["params"] == {"content": "true"}


def test_greenhouse_requires_an_explicit_board_and_rejects_junk_tokens():
    assert GreenhouseSource([]).configured is False
    assert GreenhouseSource([]).discover(term="x", city="Frankfurt am Main", size=5) == []
    with pytest.raises(ValueError, match="Invalid greenhouse board token"):
        GreenhouseSource(["../etc/passwd"])


def test_greenhouse_schema_change_is_recorded_not_silently_empty():
    session = Session({"boards-api.greenhouse.io": {"unexpected": []}})
    source = GreenhouseSource(["acme"], session=session, attempts=1)
    assert source.discover(term="AI Engineer", city="Frankfurt am Main", size=5) == []
    assert source.errors and "greenhouse:acme unavailable" in source.errors[0]


# --- Lever -----------------------------------------------------------------


LEVER_PAYLOAD = [
    {"id": "9f2a", "text": "Backend Developer", "hostedUrl": "https://jobs.lever.co/acme/9f2a",
     "categories": {"location": "Stuttgart", "commitment": "Full-time"},
     "descriptionPlain": "Work on Java Spring Boot services.",
     "lists": [{"text": "Requirements", "content": "<ul><li>SQL</li></ul>"}],
     "additionalPlain": "We offer training."},
    {"id": "", "text": "Broken", "hostedUrl": "", "categories": {}},
]


def test_lever_adapter_keeps_every_description_block():
    session = Session({"api.lever.co": LEVER_PAYLOAD})
    source = LeverSource(["acme"], session=session, attempts=1)
    found = source.discover(term="Backend Developer", city="Stuttgart", size=10)
    assert [item.job_id for item in found] == ["lever:acme-9f2a"]
    body = source.describe(found[0])
    # Mandatory requirements often live in a separate list block.
    assert "Spring Boot" in body and "Requirements" in body and "SQL" in body
    assert "training" in body


def test_board_source_cannot_describe_a_foreign_posting():
    source = LeverSource(["acme"], session=Session({"api.lever.co": LEVER_PAYLOAD}), attempts=1)
    with pytest.raises(ValueError, match="foreign posting"):
        source.describe(job())


def test_one_failing_board_does_not_break_the_other():
    session = Session({"api.lever.co": LEVER_PAYLOAD})
    source = LeverSource(["broken", "acme"], session=session, attempts=1)
    # "broken" resolves to the same stub URL, so force a failure for it only.
    original = session.get

    def failing(url, **kwargs):
        if "broken" in url:
            raise RuntimeError("boom")
        return original(url, **kwargs)

    session.get = failing
    found = source.discover(term="Backend Developer", city="Stuttgart", size=5)
    assert [item.job_id for item in found] == ["lever:acme-9f2a"]
    assert any("lever:broken unavailable" in error for error in source.errors)


def test_track_quota_respects_priority_weights():
    """A prioritised track keeps more of what it found, not a flat share."""
    jobs = []
    for index in range(4):
        jobs.append(job(external_id=f"10001-{index:04d}-S", track="ai_engineering",
                        role=f"AI Engineer {index}",
                        job_url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-{index:04d}-S"))
    for index in range(4, 8):
        jobs.append(job(external_id=f"10001-{index:04d}-S", track="sap_abap",
                        role=f"SAP Berater {index}",
                        job_url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-{index:04d}-S"))
    kept = apply_track_quota(jobs, 1, weights={"ai_engineering": 3, "sap_abap": 1})
    tracks = [item.track for item in kept]
    assert tracks.count("ai_engineering") == 3
    assert tracks.count("sap_abap") == 1
    # Without weights the cap stays flat, as before.
    flat = apply_track_quota(jobs, 1)
    assert [item.track for item in flat].count("ai_engineering") == 1


def test_interleave_by_source_stops_one_feed_crowding_out_the_others():
    """A large feed used to fill every max_new slot purely by query order."""
    big = [job(external_id=f"10001-{i:04d}-S", role=f"BA Role {i}",
               job_url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-{i:04d}-S")
           for i in range(5)]
    small = [job(source_key="ashby", source_label="Ashby", external_id=f"acme-{i}",
                 role=f"AI Engineer {i}",
                 job_url=f"https://jobs.ashbyhq.com/acme/{i}") for i in range(2)]
    merged = interleave_by_source(big + small)
    assert [item.source_key for item in merged[:4]] == ["ba", "ashby", "ba", "ashby"]
    assert len(merged) == 7, "nothing is lost, only reordered"
    # Each source keeps its own internal order.
    assert [item.role for item in merged if item.source_key == "ashby"] == [
        "AI Engineer 0", "AI Engineer 1"]
    assert [item.role for item in merged if item.source_key == "ba"] == [
        f"BA Role {i}" for i in range(5)]


# --- v0.11: per-source fairness -------------------------------------------


def many(source_key: str, count: int, host: str):
    return [job(source_key=source_key, source_label=source_key,
                external_id=f"{source_key}-{index}", role=f"{source_key} role {index}",
                job_url=f"https://{host}/{index}") for index in range(count)]


def test_round_robin_alone_lets_the_largest_feed_take_the_budget():
    """The reported 28/1/1 split: interleaving is not a cap."""
    from collections import Counter

    pool = interleave_by_source(
        many("ba", 40, "www.arbeitsagentur.de/jobsuche/jobdetail")
        + many("arbeitnow", 1, "www.arbeitnow.com/jobs")
        + many("ashby", 1, "jobs.ashbyhq.com/acme"))
    assert dict(Counter(item.source_key for item in pool[:30])) == {
        "ba": 28, "arbeitnow": 1, "ashby": 1}


def test_source_quota_caps_the_largest_feed():
    from collections import Counter

    from job_agent.sources import apply_source_quota

    pool = interleave_by_source(
        many("ba", 40, "www.arbeitsagentur.de/jobsuche/jobdetail")
        + many("arbeitnow", 1, "www.arbeitnow.com/jobs")
        + many("ashby", 1, "jobs.ashbyhq.com/acme"))
    capped = apply_source_quota(pool, 30, default_share=0.5)
    assert dict(Counter(item.source_key for item in capped)) == {
        "ba": 15, "arbeitnow": 1, "ashby": 1}
    # Per-source overrides win over the default.
    tight = apply_source_quota(pool, 30, shares={"ba": 0.2}, default_share=1.0)
    assert Counter(item.source_key for item in tight)["ba"] == 6


def test_quota_shares_the_budget_when_every_source_has_supply():
    from collections import Counter

    from job_agent.sources import apply_source_quota

    pool = interleave_by_source(
        many("ba", 20, "www.arbeitsagentur.de/jobsuche/jobdetail")
        + many("arbeitnow", 12, "www.arbeitnow.com/jobs")
        + many("ashby", 9, "jobs.ashbyhq.com/acme"))
    share = Counter(item.source_key for item in apply_source_quota(
        pool, 30, default_share=0.5))
    assert share["ba"] <= 15
    assert share["arbeitnow"] >= 9 and share["ashby"] >= 9
    assert sum(share.values()) == 30


def test_a_lone_source_is_never_capped():
    """Capping the only contributor would discard candidates for no benefit."""
    from job_agent.sources import apply_source_quota

    pool = many("ba", 40, "www.arbeitsagentur.de/jobsuche/jobdetail")
    assert len(apply_source_quota(pool, 30, default_share=0.5)) == 30


def test_fill_unused_capacity_trades_the_cap_for_a_full_budget():
    from collections import Counter

    from job_agent.sources import apply_source_quota

    pool = interleave_by_source(
        many("ba", 40, "www.arbeitsagentur.de/jobsuche/jobdetail")
        + many("ashby", 1, "jobs.ashbyhq.com/acme"))
    filled = apply_source_quota(pool, 30, default_share=0.5, fill_leftover=True)
    assert len(filled) == 30
    assert Counter(item.source_key for item in filled)["ashby"] == 1


def test_a_cap_never_silences_a_source_completely():
    from collections import Counter

    from job_agent.sources import apply_source_quota

    pool = interleave_by_source(
        many("ba", 10, "www.arbeitsagentur.de/jobsuche/jobdetail")
        + many("ashby", 10, "jobs.ashbyhq.com/acme"))
    share = Counter(item.source_key for item in apply_source_quota(
        pool, 4, shares={"ashby": 0.0}, default_share=1.0))
    assert share["ashby"] >= 1, "a zero share must still leave one slot"
