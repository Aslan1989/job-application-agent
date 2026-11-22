"""Console entrypoint for the human-approved Job Application Agent."""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from .candidate_profile import load_candidate_profile, public_projects, blocking_review_items
from .github_evidence import collect_curated_public_evidence, save_cache


def _csv(raw: str | None) -> list[str]:
    """Split a comma/semicolon separated CLI or sheet value."""
    return [part.strip() for part in str(raw or "").replace(";", ",").split(",")
            if part.strip()]


def _gap_items(row: dict, label: str) -> list[str]:
    """Pull one labelled group out of the row's Critical gaps text.

    `as_candidate` writes gaps as "Mandatory unmet: a, b; To verify: c".
    Reading them back lets a draft address the same points the assessment
    raised, without a second model call.
    """
    text = str(row.get("Critical gaps") or "")
    for chunk in text.split(";"):
        piece = chunk.strip()
        if piece.startswith(label):
            body = piece[len(label):].strip()
            return [item.strip() for item in body.split(",") if item.strip()]
    return []


def _track_for(row: dict) -> str:
    """Infer the career track from the role title, as the pipeline does."""
    from .career_tracks import infer_track
    return infer_track(str(row.get("Role") or ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Job Agent — daily targeted search, AI matching and Google Sheets")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Check access, schema and profile preferences")
    listing = commands.add_parser("list", help="Show tracked applications")
    listing.add_argument("--limit", type=int, default=20)
    commands.add_parser("approved", help="List applications with both approvals; no sending")
    ingest = commands.add_parser("ingest", help="Screen and upsert a local JSON candidate")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--write", action="store_true", help="Explicitly authorize Sheet write")
    collect = commands.add_parser("collect", help="Fetch Arbeitnow jobs; DRY RUN by default")
    collect.add_argument("--pages", type=int, default=2, help="Recent pages to scan (1-5)")
    collect.add_argument("--max-new", type=int, default=20, help="Max new Sheet rows (1-100)")
    collect.add_argument("--write", action="store_true", help="Authorize append-only Sheet write")
    collect.add_argument("--diagnose", action="store_true", help="Show why jobs fail the city/title filters")
    profile_cmd = commands.add_parser("profile", help="Inspect local CV + curated GitHub evidence (offline)")
    profile_cmd.add_argument("--file", type=Path, default=Path("data/candidate_profile.json"))
    gh = commands.add_parser("github-sync", help="Read public GitHub metadata for curated repositories")
    gh.add_argument("--file", type=Path, default=Path("data/candidate_profile.json"))
    gh.add_argument("--write", action="store_true", help="Write local metadata cache; no Google Sheets writes")
    match = commands.add_parser("match", help="LLM job-to-profile matching; dry-run by default")
    match.add_argument("file", type=Path, help="JSON with job metadata and full description")
    match.add_argument("--profile-file", type=Path, default=Path("data/candidate_profile.json"))
    match.add_argument("--provider", choices=["ollama", "gemini"], default=None)
    match.add_argument("--write", action="store_true", help="Explicitly save assessment to Sheets; never submit")
    commands.add_parser("migrate-schema", help="Add the optional Eligibility column; DRY RUN by default") \
        .add_argument("--write", action="store_true", help="Authorize the single header write to T1")
    emp = commands.add_parser("employers", help="Show the employer registry; --probe re-checks boards read-only")
    emp.add_argument("--probe", action="store_true", help="Call each board's public API (read-only, no writes)")
    emp.add_argument("--all", action="store_true", help="Probe disabled entries too")
    emp.add_argument("--file", type=Path, default=None, help="Registry path (default data/employers.json)")
    commands.add_parser("providers", help="Show LLM matching providers and their costs (offline)")
    commands.add_parser("dashboard", help="Tracker overview: Found/Review/Potential/Skip/errors")
    prep = commands.add_parser(
        "prepare", help="Draft a tailored CV and cover letter; DRY RUN by default")
    prep.add_argument("--job-id", default=None, help="A single Job ID from the tracker")
    prep.add_argument("--status", default="Review,Potential",
                      help="Statuses to draft for (default: Review,Potential)")
    prep.add_argument("--limit", type=int, default=3, help="Maximum vacancies (1..20)")
    prep.add_argument("--language", choices=["de", "en"], default=None,
                      help="Force the document language (default: detect from the advert)")
    prep.add_argument("--style", choices=["modern", "ats", "both"], default="modern",
                      help="CV layout: modern (polished, optional photo), ats "
                           "(plain single column), or both (default: modern)")
    photo_group = prep.add_mutually_exclusive_group()
    photo_group.add_argument("--photo", dest="photo", action="store_true", default=True,
                             help="Include data/profile_photo.jpg in the modern CV "
                                  "(default; stays local, never sent to a model)")
    photo_group.add_argument("--no-photo", dest="photo", action="store_false",
                             help="Render the modern CV without a Bewerbungsfoto")
    prep.add_argument("--max-projects", type=int, default=3,
                      help="Projects shown on the CV, most relevant first (1..6)")
    prep.add_argument("--no-ai", action="store_true",
                      help="Use the deterministic letter template; no model call")
    prep.add_argument("--provider", choices=["ollama", "gemini"], default=None)
    prep.add_argument("--output", type=Path, default=Path("output/applications"))
    prep.add_argument("--files", action="store_true",
                      help="Write DOCX/PDF files locally (no Google Sheets change)")
    prep.add_argument("--write", action="store_true",
                      help="Write files AND record references in Sheets columns O/P")
    prep.add_argument("--overwrite-links", action="store_true",
                      help="Replace existing CV/cover-letter cell values")
    prep.add_argument("--drive", action="store_true",
                      help="Upload to Google Drive (needs a new OAuth scope)")
    reband = commands.add_parser("reband", help="Re-band already-scored 65-79%% Skip rows as Potential; DRY RUN by default")
    reband.add_argument("--write", action="store_true", help="Apply the Status change (column M only)")
    reband.add_argument("--potential-min", type=int, default=65)
    sq = commands.add_parser("search-queries", help="Show the web-search queries built from your profile (offline)")
    sq.add_argument("--tracks", default=None, help="Comma-separated track keys")
    sq.add_argument("--limit", type=int, default=12, help="How many queries to build (1..120)")
    sq.add_argument("--cities", default=None, help="Comma-separated cities; defaults to the profile")
    tracks_cmd = commands.add_parser("tracks", help="Show career tracks and today's search plan (offline)")
    tracks_cmd.add_argument("--tracks", default=None, help="Comma-separated track keys to include")
    tracks_cmd.add_argument("--max-searches", type=int, default=33, help="Search budget to plan (1..120)")
    tracks_cmd.add_argument("--cities", default="Frankfurt am Main,Stuttgart,Heilbronn",
                            help="Comma-separated cities; the Profile tab wins in a real run")
    daily = commands.add_parser("daily", help="Skill-based multi-source search, optional LangGraph/Gemini matching; DRY RUN by default")
    daily.add_argument("--ai", action="store_true", help="Call LLM for up to --max-matches jobs (may cost tokens)")
    daily.add_argument("--write", action="store_true", help="Explicitly save found jobs and scores to Sheets; NEVER submits")
    daily.add_argument("--provider", choices=["ollama", "gemini"], default=None)
    daily.add_argument("--max-searches", type=int, default=33,
                       help="Search budget (1..120); default covers 11 tracks x 3 cities")
    daily.add_argument("--tracks", default=None,
                       help="Comma-separated career tracks (default: all; see 'job-agent tracks')")
    daily.add_argument("--sources", default=None,
                       help="Comma-separated sources to override JOB_SOURCES "
                            "(ba, arbeitnow, greenhouse, lever, ashby, career_page, brave)")
    daily.add_argument("--remote-germany", dest="remote_germany",
                       action="store_true", default=None,
                       help="Accept remote-Germany postings (default: on)")
    daily.add_argument("--no-remote-germany", dest="remote_germany",
                       action="store_false",
                       help="Restrict strictly to the Profile tab's cities")
    daily.add_argument("--source-share", type=float, default=None,
                       help="Max fraction of --max-new one source may take (0..1); "
                            "ignored when only one source has candidates")
    daily.add_argument("--fill-unused", action="store_true",
                       help="Hand capped-away slots back out, filling --max-new fully")
    daily.add_argument("--potential-min", type=int, default=None,
                       help="Lowest score saved as Potential instead of Skip (default 65)")
    daily.add_argument("--llm-attempts", type=int, default=3,
                       help="Attempts per assessment, retrying transient faults only (1..6)")
    daily.add_argument("--max-consecutive-failures", type=int, default=3,
                       help="Stop the matching stage after this many failures in a row")
    daily.add_argument("--max-detail-attempts", type=int, default=3,
                       help="Runs an unreadable advert is retried before it is closed")
    daily.add_argument("--no-cache", action="store_true",
                       help="Ignore the local description/assessment cache for this run")
    daily.add_argument("--verbose", action="store_true", help="Log each pipeline stage")
    daily.add_argument("--per-search", type=int, default=10, help="BA results requested per query (1..50)")
    daily.add_argument("--max-new", type=int, default=12, help="Maximum NEW jobs per run (1..50)")
    daily.add_argument("--max-matches", type=int, default=6, help="Maximum model calls per run (1..15)")
    daily.add_argument("--max-detail", type=int, default=20, help="Maximum BA description fetches per run (1..50)")
    args = parser.parse_args()

    if args.command == "providers":
        from .providers import DEFAULT_PROVIDER, available_providers
        from dotenv import load_dotenv

        load_dotenv()
        print(f"Default matching provider: {DEFAULT_PROVIDER} "
              "(override with MATCH_LLM_PROVIDER or --provider)")
        for info in available_providers():
            state = "ready" if info.available else "not configured"
            kind = "remote" if info.remote else "local"
            print(f"  {info.key:10} [{kind:6}] [{state:14}] {info.label}")
            if info.env_keys:
                print(f"  {'':10} needs: {', '.join(info.env_keys)}")
            if info.cost_note:
                print(f"  {'':10} cost: {info.cost_note}")
        print("Adding Anthropic or OpenAI means registering a factory in "
              "providers.py; no discovery code changes.")
        return

    if args.command == "employers":
        from .employers import load_registry, probe_counts, summarize

        book = load_registry(args.file or Path("data/employers.json"))
        if book.finding:
            print("Finding: " + book.finding)
        print(f"Employers: {len(book.employers)} "
              f"({len(book.enabled_employers())} enabled); "
              f"career pages: {len(book.career_pages)} "
              f"({len(book.enabled_career_pages())} enabled)")
        for line in summarize(book):
            print(line)
        if not args.probe:
            print("\nAdd --probe to re-check each board against its public API "
                  "(read-only; no Google Sheets access, no model calls).")
            return

        from .collectors.ats import AshbySource, GreenhouseSource, LeverSource
        from .ratelimit import RateLimiter

        classes = {"greenhouse": GreenhouseSource, "lever": LeverSource,
                   "ashby": AshbySource}

        def build(employer):
            maker = classes.get(employer.platform)
            if maker is None:
                return None
            return maker([employer.board], attempts=2,
                         companies={employer.board: employer.name},
                         limiter=RateLimiter.for_source(employer.platform))

        chosen = book.employers if args.all else book.enabled_employers()
        cities = _csv(args.cities) if getattr(args, "cities", None) else [
            "Frankfurt am Main", "Stuttgart", "Heilbronn"]
        print(f"\nProbing {len(chosen)} board(s) read-only against {', '.join(cities)}:")
        results = probe_counts(chosen, build_source=build, cities=cities)
        for board, data in results.items():
            if "error" in data:
                print(f"  {board:26} unavailable ({data['error']})")
                continue
            print(f"  {board:26} total={data['total']:4} "
                  f"in_cities={data['in_target_cities']:3} "
                  f"+remoteDE={data['with_remote_germany']:3}  {data['name']}")
            for note in data.get("errors", [])[:2]:
                print(f"  {'':26} note: {note}")
        print("No Google Sheets access and no model calls were made.")
        return

    if args.command == "search-queries":
        from .query_builder import (build_queries, classify_domain, describe_sources,
                                    resolve_query_tracks)

        candidate_profile = load_candidate_profile()
        if not 1 <= args.limit <= 120:
            parser.error("--limit must be between 1 and 120")
        facts = describe_sources(candidate_profile)
        cities = _csv(args.cities) or facts["cities"] or ["Frankfurt am Main"]
        print("Evidenced skill keywords used to build queries:")
        print("  " + ", ".join(facts["skill_terms"]))
        if facts["excluded_not_evidenced"]:
            print("Excluded as not evidenced: "
                  + ", ".join(facts["excluded_not_evidenced"]))
        selected = resolve_query_tracks(_csv(args.tracks) or None)
        queries = build_queries(candidate_profile, cities, args.limit, tracks=selected)
        print(f"\n{len(queries)} queries for today "
              f"({sum(1 for q in queries if q.kind == 'skill')} built from skills, "
              f"{sum(1 for q in queries if q.kind == 'title')} from job titles):")
        for query in queries:
            print(f"  [{query.kind:5}] {query.track:20} {query.text}")
        print("\nAggregators are ranked below employer-direct results, e.g. "
              f"stepstone.de -> {classify_domain('https://stepstone.de/x')}, "
              f"boards.greenhouse.io -> {classify_domain('https://boards.greenhouse.io/x')}, "
              f"acme.de -> {classify_domain('https://acme.de/karriere/1')}")
        print("Offline: no search API, job source or Google Sheets was contacted.")
        return

    if args.command == "tracks":
        from .career_tracks import resolve_tracks, search_plan, track_quota

        selected = resolve_tracks(_csv(args.tracks))
        cities = _csv(args.cities) or ["Frankfurt am Main"]
        print(f"Career tracks enabled: {len(selected)}")
        for track in selected:
            print(f"  {track.key:20} {track.label}")
            print(f"  {'':20} terms: " + ", ".join(track.terms))
            if track.note:
                print(f"  {'':20} note: {track.note}")
        plan = search_plan(cities, args.max_searches, tracks=selected)
        print(f"\nToday's plan: {len(plan)} queries; "
              f"per-track keep quota at --max-new 12: {track_quota(12, len(selected))}")
        for query in plan:
            print(f"  {query.track:20} {query.term!r} @ {query.city}")
        print("\nOffline: no job source or Google Sheets was contacted.")
        return

    if args.command == "daily":
        import logging

        from dotenv import load_dotenv

        from .ai_matching import make_provider
        from .auth import make_sheets_service
        from .cache import DescriptionCache
        from .collectors import build_sources
        from .config import Settings
        from .employers import load_registry
        from .daily_pipeline import run_daily
        from .sheets import GoogleSheetsRepository

        if not 1 <= args.max_searches <= 120:
            parser.error("--max-searches must be between 1 and 120")
        if not 1 <= args.llm_attempts <= 6:
            parser.error("--llm-attempts must be between 1 and 6")
        if not 1 <= args.max_consecutive_failures <= 20:
            parser.error("--max-consecutive-failures must be between 1 and 20")
        if not 1 <= args.max_detail_attempts <= 10:
            parser.error("--max-detail-attempts must be between 1 and 10")
        logging.basicConfig(
            level=logging.INFO if args.verbose else logging.WARNING,
            format="%(asctime)s %(levelname)s %(name)s %(message)s")
        load_dotenv()
        settings = Settings.from_env()
        if args.sources:
            settings = replace(settings, enabled_sources=tuple(_csv(args.sources)))
        if args.remote_germany is not None:
            settings = replace(settings, allow_remote_germany=args.remote_germany)
        if args.source_share is not None:
            if not 0.0 <= args.source_share <= 1.0:
                parser.error("--source-share must be between 0 and 1")
            settings = replace(settings, source_default_share=args.source_share)
        if args.fill_unused:
            settings = replace(settings, source_fill_leftover=True)
        if args.potential_min is not None:
            if not 0 <= args.potential_min <= 100:
                parser.error("--potential-min must be between 0 and 100")
            settings = replace(settings, potential_min_score=args.potential_min)
        repo = GoogleSheetsRepository(make_sheets_service(settings),
                                      settings.spreadsheet_id,
                                      settings.vacancies_tab,
                                      settings.profile_tab)
        # The Profile tab is canonical for cities; this list only configures the
        # board and search adapters that filter locally.
        cities = _csv(repo.get_profile().get("Locations", "").replace(";", ","))
        candidate_profile = load_candidate_profile()
        tracks = _csv(args.tracks) or (list(settings.career_tracks) or None)

        # Web-search queries come from the candidate's evidence, so the profile
        # is read here and never inside a collector.
        search_queries = []
        if "brave" in settings.enabled_sources and settings.brave_api_key:
            from .query_builder import build_queries, resolve_query_tracks
            search_queries = build_queries(
                candidate_profile, cities or ["Frankfurt am Main"],
                settings.brave_max_queries,
                tracks=resolve_query_tracks(tracks))

        registry = load_registry(settings.employers_file)
        sources = build_sources(settings, cities=tuple(cities),
                                registry=registry, queries=search_queries)
        provider = make_provider(args.provider) if args.ai else None
        cache = None if args.no_cache else DescriptionCache.load(settings.cache_file)
        report = run_daily(repository=repo, sources=sources,
                           profile=candidate_profile, provider=provider,
                           cache=cache, tracks=tracks,
                           write=args.write, max_searches=args.max_searches,
                           allow_remote_germany=settings.allow_remote_germany,
                           source_shares=settings.source_shares,
                           source_default_share=settings.source_default_share,
                           source_fill_leftover=settings.source_fill_leftover,
                           potential_min=settings.potential_min_score,
                           llm_attempts=args.llm_attempts,
                           max_consecutive_failures=args.max_consecutive_failures,
                           max_detail_attempts=args.max_detail_attempts,
                           per_search=args.per_search, max_new=args.max_new,
                           max_matches=args.max_matches, max_detail=args.max_detail)
        threshold_label = repo.minimum_match()
        print(f"Sources: {report.source}")
        print(f"Queries planned: {report.queries_planned}; requests: {report.requests}; "
              f"fetched: {report.fetched}; kept by Stage A: {report.screened}; "
              f"duplicates dropped: {report.duplicates}")
        print("Per-source funnel (fetched -> kept by Stage A; dropped by city/title):")
        for key in sorted(report.fetched_by_source):
            print(f"  {key:12} {report.fetched_by_source[key]:4} -> "
                  f"{report.screened_by_source.get(key, 0):4}   "
                  f"city={report.dropped_city.get(key, 0)} "
                  f"title={report.dropped_title.get(key, 0)}")
        print(f"New: {report.new}; previously found/unscored: {report.pending}; "
              f"by source: {report.by_source or '{}'}; by track: {report.by_track or '{}'}")
        if report.source_capped:
            print(f"Held back by the per-source cap "
                  f"(share {settings.source_default_share}): {report.source_capped}"
                  " -- raise --source-share or pass --fill-unused to use those slots")
        if report.track_evidence:
            print("Inferred track labels and the words they rest on:")
            for track, reasons in sorted(report.track_evidence.items()):
                print(f"  {track:20} {'; '.join(reasons[:4])}")
        if report.unclassified:
            print(f"Unclassified titles ({len(report.unclassified)}) -- counted "
                  "separately, never assigned a track on a guess:")
            for line in report.unclassified[:6]:
                print("  " + line)
        print(f"Descriptions ready: {report.detail_ready}; unavailable: {report.detail_missing}; "
              f"cache reuse: {report.cache_hits}; pre-screen blocked: {report.prescreen_blocked}; "
              f"flagged for verification: {report.prescreen_flagged}")
        if report.flushed or report.recovery_skipped:
            print(f"Recovered from cache without a model call: {report.flushed} "
                  f"assessment(s) that had been computed but never saved"
                  + (f"; {report.recovery_skipped} already present and skipped cleanly"
                     if report.recovery_skipped else ""))
        if report.protected_rows:
            print(f"Rows left untouched (human decision or already scored): "
                  f"{report.protected_rows}")
        if report.llm_failed:
            print(f"Assessments that failed: {report.llm_failed} "
                  f"(retries spent: {report.llm_retried}); those jobs keep their "
                  "unscored Found row and are retried on the next run")
        if report.stop_reason:
            print(f"Matching stage stopped early: {report.stop_reason}")
        if report.closed_ineligible or report.closed_unreadable:
            print(f"Closed out: {report.closed_ineligible} Ineligible, "
                  f"{report.closed_unreadable} Unreadable "
                  "(no score invented; they stop re-queueing)")
        print(f"Backlog of unscored Found rows still waiting: {report.backlog_remaining}")
        print(f"AI calls: {report.llm_attempted}; valid scores: {report.llm_scored}; "
              f"Review (>={threshold_label}%): {report.review}; "
              f"Potential ({settings.potential_min_score}-{threshold_label}%): "
              f"{report.potential}; Skip: {report.skip}")
        if report.by_eligibility:
            eligible = report.by_eligibility.get("eligible", 0)
            unknown = report.by_eligibility.get("unknown", 0)
            ineligible = report.by_eligibility.get("ineligible", 0)
            print(f"Eligibility: {eligible} eligible; {unknown} unknown "
                  f"(open question, NOT a disqualification); "
                  f"{ineligible} ineligible (stated condition conflicts; "
                  f"overrides the score)")
        for line in report.preview:
            print("  " + line)
        for source in sources:
            if hasattr(source, "report"):
                stats = source.report()
                print(f"{getattr(source, 'key', '?')}: "
                      + "; ".join(f"{name}={value}" for name, value in stats.items()))
        if report.verification_questions:
            print("Questions a human must verify before applying:")
            for question in report.verification_questions[:8]:
                print("  ? " + question)
        if report.errors:
            print(f"Warnings: {len(report.errors)}")
            for error in report.errors[:8]:
                print("  " + error)
        if args.write:
            print(f"Google Sheets: {report.appended} new jobs appended; {report.saved_scores} AI scores saved.")
        else:
            print("DRY RUN: no Google Sheets changes. --ai enables LLM; --write saves results.")
        print("Applications submitted: 0. Human approval is always required.")
        return

    if args.command in {"profile", "github-sync"}:
        candidate_profile = load_candidate_profile(args.file)
        if args.command == "profile":
            print(f"Candidate: {candidate_profile['candidate']['display_name']}")
            print(f"Curated public GitHub projects: {len(public_projects(candidate_profile))}")
            print(f"Skills in evidence profile: {len(candidate_profile['skills'])}")
            print(f"Blocking CV discrepancies: {len(blocking_review_items(candidate_profile))}")
            for item in blocking_review_items(candidate_profile):
                print(f"  - {item['id']}: review required before application generation")
            print("No applications submitted and no Google Sheets changes.")
        else:
            report = collect_curated_public_evidence(candidate_profile)
            print(f"Public GitHub repos verified: {len(report['public_repositories'])}")
            print(f"Metadata read errors: {len(report['errors'])}")
            for item in report['public_repositories']:
                print(f"  {item['full_name']} | language: {item.get('language') or 'unknown'}")
            for item in report['errors']:
                print(f"  Unavailable: {item['repository']} ({item['error']})")
            if args.write:
                save_cache(report)
                print("Saved local data/github_projects.json (not sent to Sheets).")
            else:
                print("DRY RUN: no cache written. Add --write to persist metadata locally.")
        return

    if args.command == "match":
        from dotenv import load_dotenv
        from .ai_matching import JobDescription, make_provider
        from .match_workflow import build_match_graph

        load_dotenv()
        candidate_profile = load_candidate_profile(args.profile_file)
        raw = json.loads(args.file.read_text(encoding="utf-8"))
        job = JobDescription.from_dict(raw)
        repository = None
        threshold = int(candidate_profile["search_preferences"]["minimum_ai_score"])
        if args.write:
            from .auth import make_sheets_service
            from .config import Settings
            from .sheets import GoogleSheetsRepository
            settings = Settings.from_env()
            repository = GoogleSheetsRepository(make_sheets_service(settings),
                                                settings.spreadsheet_id,
                                                settings.vacancies_tab,
                                                settings.profile_tab)
            threshold = repository.minimum_match()  # Google Sheets is canonical.
        provider = make_provider(args.provider)
        graph = build_match_graph(profile=candidate_profile,
                                  provider=provider, repository=repository)
        state = graph.invoke({"job": raw, "threshold": threshold, "write": args.write})
        a = state["assessment"]
        print(f"{job.company} — {job.role} — {job.location}")
        print(f"AI score: {a['score']}%, threshold: {threshold}%, status: {state['status']}")
        print(f"Eligibility: {a['eligibility']}; track: {a['track']}")
        print(f"Summary: {a['summary']}")
        if a["critical_gaps"]:
            print("Critical gaps: " + "; ".join(a["critical_gaps"]))
        if args.write:
            print(f"Google Sheets: {state.get('outcome', 'no change')}")
        else:
            print("DRY RUN: Google Sheets unchanged. Add --write to save assessment.")
        print("No applications submitted. A manual approval is always required.")
        return

    # Import Google OAuth and LangGraph only for commands that need them.
    from .auth import make_sheets_service
    from .config import Settings
    from .models import JobCandidate
    from .collectors.arbeitnow import ArbeitnowCollector
    from .job_filter import normalize_matching_jobs, diagnose_matching_jobs
    from .sheets import GoogleSheetsRepository
    from .workflow import build_intake_graph

    settings = Settings.from_env()
    service = make_sheets_service(settings)
    repo = GoogleSheetsRepository(service, settings.spreadsheet_id,
                                  settings.vacancies_tab, settings.profile_tab)

    if args.command == "prepare":
        from dotenv import load_dotenv

        from .cache import DescriptionCache
        from .documents.contact import (DEFAULT_CONTACT_PATH, load_contact,
                                        write_example)
        from .documents.pipeline import generate_package, selectable_rows

        if not 1 <= args.limit <= 20:
            parser.error("--limit must be between 1 and 20")
        if not 1 <= args.max_projects <= 6:
            parser.error("--max-projects must be between 1 and 6")
        cv_styles = (["modern", "ats"] if args.style == "both" else [args.style])
        load_dotenv()
        if not DEFAULT_CONTACT_PATH.is_file():
            example = write_example()
            print(f"No {DEFAULT_CONTACT_PATH} yet; wrote a template to {example}.")
            print("Fill it in and copy it to that path. Contact details stay local "
                  "and are never sent to the model.\n")

        candidate_profile = load_candidate_profile()
        contact = load_contact(
            fallback_name=str(candidate_profile["candidate"].get("display_name", "")),
            github_url=str(candidate_profile["candidate"].get("public_github_url", "")),
            photo=bool(args.photo))
        if args.photo and contact.photo_note:
            print(contact.photo_note)
        rows = repo.list_vacancies(limit=999)
        chosen = selectable_rows(rows, statuses=_csv(args.status),
                                 job_id=args.job_id or "", limit=args.limit)
        if not chosen:
            target = args.job_id or f"status in {args.status}"
            print(f"No vacancy matched {target}.")
            print("Eligible rows are Review or Potential without a confirmed "
                  "hard disqualification. Check 'job-agent dashboard'.")
            return

        provider = None
        if not args.no_ai:
            from .ai_matching import make_provider
            try:
                provider = make_provider(args.provider)
            except ValueError as exc:
                print(f"No model provider available ({exc}); using the "
                      "deterministic letter template.")

        cache = DescriptionCache.load(settings.cache_file)
        write_files = bool(args.files or args.write)
        drafted = []
        for row in chosen:
            job_id = str(row["Job ID"])
            description = cache.description_for(job_id) or ""
            job = {
                "job_id": job_id, "company": row["Company"], "role": row["Role"],
                "location": row["Location"], "status": row["Status"],
                "score": row["Match %"], "eligibility": row.get("Eligibility", ""),
                "description": description,
                "track": _track_for(row),
            }
            assessment = {
                "summary": str(row.get("Match explanation") or "")[:600],
                "eligibility": str(row.get("Eligibility") or "").strip() or "unknown",
                "unknown_requirements": _gap_items(row, "To verify:"),
                "mandatory_unmet": _gap_items(row, "Mandatory unmet:"),
                "missing_skills": _gap_items(row, "Missing:"),
            }
            try:
                package = generate_package(
                    job=job, profile=candidate_profile, provider=provider,
                    contact=contact, output_dir=args.output,
                    assessment=assessment, language=args.language or "",
                    write_files=write_files, styles=cv_styles,
                    max_projects=args.max_projects)
            except ValueError as exc:
                print(f"\nSKIPPED {job_id} - {exc}")
                continue
            drafted.append(package)
            print()
            print(package.report.render_text())

            if args.write:
                cv_reference = (f"local: {package.cv_docx}" if package.cv_docx
                                else "")
                letter_reference = (f"local: {package.letter_docx}"
                                    if package.letter_docx else "")
                if args.drive:
                    from .documents.drive import (DriveNotAuthorised,
                                                  make_drive_service, upload_files)
                    try:
                        service = make_drive_service(settings)
                        uploaded = upload_files(service, package.files)
                        links = {item.name: item.url for item in uploaded}
                        cv_reference = next(
                            (url for name, url in links.items()
                             if name.startswith(("Lebenslauf", "CV"))), cv_reference)
                        letter_reference = next(
                            (url for name, url in links.items()
                             if name.startswith(("Anschreiben", "Cover"))),
                            letter_reference)
                        print(f"  Drive: uploaded {len(uploaded)} private file(s)")
                    except DriveNotAuthorised as exc:
                        print(f"  Drive upload not available:\n{exc}")
                outcome = repo.record_documents(
                    job_id, cv_reference=cv_reference,
                    letter_reference=letter_reference,
                    overwrite=args.overwrite_links)
                print(f"  Google Sheets: {outcome}")

        print(f"\n{len(drafted)} draft(s) prepared.")
        if not write_files:
            print("DRY RUN: no files written and no Google Sheets change. "
                  "Add --files to write documents, or --write to also record "
                  "references in columns O/P.")
        elif not args.write:
            print("Files written locally. Google Sheets was not modified.")
        print("No application was submitted and no approval was changed. "
              "Every document is a draft for your review.")
        return

    if args.command == "dashboard":
        summary = repo.status_summary()
        print(f"Vacancies tracked: {summary['total']} "
              f"({summary['scored']} scored, "
              f"{summary['unscored_found_backlog']} unscored Found awaiting AI)")
        print("\nStatus:")
        for name in ("Found", "Review", "Potential", "Skip", "Ineligible",
                     "Unreadable", "Approved", "Applied"):
            if name in summary["status"]:
                print(f"  {name:12} {summary['status'][name]:4}")
        extra = {k: v for k, v in summary["status"].items()
                 if k not in {"Found", "Review", "Potential", "Skip", "Ineligible",
                              "Unreadable", "Approved", "Applied"}}
        for name, count in sorted(extra.items()):
            print(f"  {name:12} {count:4}  (other)")
        print(f"\nProcessing errors (no score recorded): {summary['errors']}")
        print(f"  Ineligible {summary['status'].get('Ineligible', 0):4}  "
              "hard eligibility conflict")
        print(f"  Unreadable {summary['status'].get('Unreadable', 0):4}  "
              "advert could not be retrieved")
        bands = summary["score_bands"]
        print(f"\nScore bands among scored rows: Review>=80: {bands['review']}; "
              f"Potential 65-79: {bands['potential']}; below 65: {bands['below']}")
        if summary["eligibility"]:
            print(f"Eligibility column: {summary['eligibility']}")
        else:
            print("Eligibility column: not present (run 'migrate-schema')")
        print(f"Approval: {summary['approval']}")
        print(f"By source: {summary['by_source']}")
        print("\nRead-only: nothing was written.")
        return

    if args.command == "reband":
        result = repo.reband_potential(potential_min=args.potential_min,
                                       write=args.write)
        rows = result["eligible_rows"]
        print(f"Review threshold {result['threshold']}%, "
              f"Potential from {result['potential_min']}%")
        print(f"Machine-written Skip rows inside the Potential band: {len(rows)}")
        for item in rows:
            print(f"  row {item['row']:4} {item['score']:3}%  {item['job_id']}")
        for note in result["protected"]:
            print(f"  PROTECTED {note}")
        if args.write:
            print(f"Updated {result['written']} row(s): column M only. "
                  "Scores, approvals, documents and notes untouched.")
        else:
            print("DRY RUN: no changes. Repeat with --write to apply.")
        return

    if args.command == "migrate-schema":
        outcome = repo.migrate_schema(write=args.write)
        print(outcome)
        if not args.write:
            print("Repeat with --write to apply. Columns A:S are never modified.")
        return

    if args.command == "check":
        repo.validate_schema()
        profile = repo.get_profile()
        print("OK: authenticated and existing Vacancies schema is valid")
        print("Eligibility column (T): "
              + ("present" if repo.has_eligibility_column else "absent (run 'migrate-schema')"))
        for key in ("Preferred roles", "Locations", "Min match score", "Search frequency", "Application mode"):
            print(f"{key}: {profile.get(key, '(not set)')}")
        print("Submission capability: NOT IMPLEMENTED")
    elif args.command == "list":
        for row in repo.list_vacancies(limit=args.limit):
            print(f"{row['Job ID']} | {row['Company']} | {row['Role']} | "
                  f"{(str(row['Match %']) + '%') if row['Match %'] != '' else 'unscored'} | {row['Status']} | {row['Approval']}")
    elif args.command == "approved":
        for row in repo.get_approved():
            print(f"{row['Job ID']} | {row['Company']} | {row['Job URL']}")
        print("No applications have been submitted.")
    elif args.command == "collect":
        if not 1 <= args.pages <= 5 or not 1 <= args.max_new <= 100:
            parser.error("--pages must be 1..5 and --max-new must be 1..100")
        repo.validate_schema()
        profile = repo.get_profile()
        fetched = ArbeitnowCollector().fetch(pages=args.pages)
        selected = normalize_matching_jobs(fetched, profile)
        print(f"Fetched: {len(fetched)} postings from Arbeitnow; city/title filtered: {len(selected)}")
        if args.diagnose or not selected:
            stats = diagnose_matching_jobs(fetched, profile)
            print(f"Filter diagnostics: valid={stats.structurally_valid}, "
                  f"invalid={stats.invalid}, city matches={stats.city_matches}, "
                  f"title matches={stats.role_matches}, both={stats.both_matches}")
            if stats.top_locations:
                print("Most common API locations: " + ", ".join(
                    f"{loc} ({count})" for loc, count in stats.top_locations))
            if stats.city_only_examples:
                print("In requested cities but not a matching title:")
                for title, loc in stats.city_only_examples:
                    print(f"  {title} — {loc}")
            if stats.role_only_examples:
                print("Matching titles outside requested cities:")
                for title, loc in stats.role_only_examples:
                    print(f"  {title} — {loc}")
            if stats.both_matches == 0:
                print("No job met both filters. Consider scanning --pages 5, "
                      "adding a targeted source, or adjusting Profile locations explicitly.")
        for job in selected[:args.max_new]:
            print(f"{job.company} | {job.role} | {job.location} | {job.job_url}")
        if not args.write:
            print("DRY RUN. No Sheet changes. Repeat with --write to append new rows.")
            return
        inserted = repo.append_discoveries(selected, max_new=args.max_new)
        print(f"New unscored jobs saved: {inserted}; Status=Found, Approval=Not reviewed")
        print("No AI score calculated. No applications submitted.")
    elif args.command == "ingest":
        raw = json.loads(args.file.read_text(encoding="utf-8"))
        candidate = JobCandidate.from_dict(raw)
        threshold = repo.minimum_match()
        status = "Review" if candidate.match_score >= threshold else "Skip"
        print(f"Candidate: {candidate.company} — {candidate.role}")
        print(f"Score: {candidate.match_score}%, threshold: {threshold}%, status: {status}")
        if not args.write:
            print("DRY RUN: no write. Repeat with --write to insert/update.")
            return
        state = build_intake_graph(repo).invoke({"candidate": raw})
        print(f"Google Sheets: {state['outcome']}")
        print("No application has been submitted. Candidate approval is mandatory.")


if __name__ == "__main__":
    main()
