"""Generate one application package and the review report that goes with it.

Order of operations, and why:

1. Select evidence deterministically. Nothing is generated yet.
2. Build the CV by assembly. It cannot contain an invented fact.
3. Ask the model for the letter body, if a provider was given.
4. Fact-check that body. On a blocking finding, discard it and fall back to
   the deterministic letter. There is always a truthful letter to ship.
5. Render DOCX, then PDF.
6. Verify both files are readable, and report everything a human must check.

Nothing here writes to Google Sheets, uploads a file or touches an approval.
Every output is a draft.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..candidate_profile import load_candidate_profile
from .contact import ContactDetails, load_contact
from .content import (CvContent, LetterContent, build_cv, build_letter,
                      deterministic_letter, letter_messages,
                      parse_letter_paragraphs, slugify,
                      untranslated_descriptions)
from .evidence import EvidenceSelection, select_evidence
from .factcheck import FactCheckResult, check_claims, check_text
from .layout import STYLE_NAMES, LayoutReport, get_style
from .render_docx import read_docx_text, render_cv, render_letter
from .render_pdf import (convert_with_libreoffice, converter_available,
                         pdf_is_readable, render_cv_pdf, render_letter_pdf,
                         verify_pdf_pages)

log = logging.getLogger("job_agent.documents")

DEFAULT_OUTPUT_DIR = Path("output/applications")


@dataclass
class ReviewReport:
    """What a human needs in order to approve or reject a draft."""

    job_id: str
    company: str
    role: str
    location: str
    language: str
    track: str
    status: str = ""
    score: Any = ""
    eligibility: str = ""
    cv_variant: str = ""
    cv_styles: tuple[str, ...] = ()
    layouts: list[str] = field(default_factory=list)
    letter_source: str = "template"
    evidence_skills: tuple[str, ...] = ()
    evidence_projects: tuple[str, ...] = ()
    requirements_addressed: tuple[str, ...] = ()
    requirements_unmet: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    excluded_evidence: tuple[str, ...] = ()
    cautions: tuple[str, ...] = ()
    verify_before_sending: list[str] = field(default_factory=list)
    factcheck_findings: list[str] = field(default_factory=list)
    documents: dict[str, str] = field(default_factory=dict)
    document_checks: list[str] = field(default_factory=list)
    generated_at: str = ""

    @property
    def approved(self) -> bool:
        """Always False. A generated package is a draft, never an approval."""
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "company": self.company, "role": self.role,
            "location": self.location, "language": self.language,
            "track": self.track, "sheet_status": self.status,
            "match_score": self.score, "eligibility": self.eligibility,
            "cv_variant": self.cv_variant, "cv_styles": list(self.cv_styles),
            "cv_layouts": list(self.layouts),
            "letter_source": self.letter_source,
            "evidence_skills": list(self.evidence_skills),
            "evidence_projects": list(self.evidence_projects),
            "requirements_addressed": list(self.requirements_addressed),
            "requirements_unmet": list(self.requirements_unmet),
            "open_questions": list(self.open_questions),
            "excluded_evidence": list(self.excluded_evidence),
            "cautions": list(self.cautions),
            "verify_before_sending": list(self.verify_before_sending),
            "factcheck_findings": list(self.factcheck_findings),
            "documents": dict(self.documents),
            "document_checks": list(self.document_checks),
            "generated_at": self.generated_at,
            "state": "DRAFT - requires human review; no application was submitted",
        }

    def render_text(self) -> str:
        lines = [
            f"Application draft for {self.company} - {self.role}",
            f"  job id        : {self.job_id}",
            f"  location      : {self.location or '(not recorded)'}",
            f"  sheet status  : {self.status or '(none)'}"
            f"   score: {self.score or '(unscored)'}"
            f"   eligibility: {self.eligibility or '(unknown)'}",
            f"  language      : {self.language}   track: {self.track}",
            f"  CV variant    : {self.cv_variant}"
            + (f"   styles: {', '.join(self.cv_styles)}" if self.cv_styles else ""),
            f"  letter written by: {self.letter_source}",
        ]
        lines.extend(f"  layout        : {item}" for item in self.layouts)
        lines.append("  evidence used:")
        for skill in self.evidence_skills:
            lines.append(f"    skill   {skill}")
        for project in self.evidence_projects:
            lines.append(f"    project {project}")
        if self.requirements_addressed:
            lines.append("  requirements addressed: "
                         + ", ".join(self.requirements_addressed))
        if self.requirements_unmet:
            lines.append("  requirements NOT met  : "
                         + ", ".join(self.requirements_unmet))
        if self.open_questions:
            lines.append("  open questions        : " + "; ".join(self.open_questions))
        if self.factcheck_findings:
            lines.append("  fact-check findings:")
            lines.extend(f"    {item}" for item in self.factcheck_findings)
        if self.cautions:
            lines.append("  project cautions:")
            lines.extend(f"    {item}" for item in self.cautions)
        if self.verify_before_sending:
            lines.append("  VERIFY BEFORE SENDING:")
            lines.extend(f"    - {item}" for item in self.verify_before_sending)
        if self.excluded_evidence:
            lines.append("  deliberately excluded:")
            lines.extend(f"    {item}" for item in self.excluded_evidence[:8])
        if self.documents:
            lines.append("  documents:")
            lines.extend(f"    {kind}: {path}"
                         for kind, path in sorted(self.documents.items()))
        lines.extend(f"  check: {item}" for item in self.document_checks)
        lines.append("  STATE: draft. Nothing was submitted and no approval changed.")
        return "\n".join(lines)


@dataclass
class ApplicationPackage:
    """Generated documents plus the review report."""

    report: ReviewReport
    evidence: EvidenceSelection
    cv: CvContent
    letter: LetterContent
    directory: Path
    cv_docx: Path | None = None
    cv_pdf: Path | None = None
    letter_docx: Path | None = None
    letter_pdf: Path | None = None
    # Further CV styles rendered in the same run, keyed by style name. The
    # primary style stays on `cv_docx`/`cv_pdf`, so what reaches Google Sheets
    # column O is unchanged by asking for a second layout.
    alternates: dict[str, tuple[Path, Path]] = field(default_factory=dict)
    layouts: dict[str, LayoutReport] = field(default_factory=dict)

    @property
    def files(self) -> list[Path]:
        paths = [self.cv_docx, self.cv_pdf, self.letter_docx, self.letter_pdf]
        for docx, pdf in self.alternates.values():
            paths.extend((docx, pdf))
        return [path for path in paths if path is not None]


def _private_repositories(profile: Mapping[str, Any]) -> list[str]:
    return [str(item.get("repository", "")) for item in profile.get("projects", []) or []
            if isinstance(item, Mapping) and item.get("visibility") != "public"]


def _excluded_files(profile: Mapping[str, Any]) -> list[str]:
    variants = profile.get("cv_variants", {}) or {}
    return [str(name) for name in variants.get("excluded_files", []) or []]


def _write_letter(evidence: EvidenceSelection, *, provider: Any, company: str,
                  role: str, location: str, description: str,
                  assessment_summary: str, profile: Mapping[str, Any],
                  report: ReviewReport) -> LetterContent:
    """Ask the model for the body, then decide whether it may be used."""
    private = _private_repositories(profile)
    excluded = _excluded_files(profile)

    if provider is None:
        return deterministic_letter(evidence, company=company, role=role,
                                    findings=("no model provider configured",))

    messages = letter_messages(evidence, company=company, role=role,
                               location=location, description=description,
                               assessment_summary=assessment_summary)
    try:
        raw = provider.complete(messages)
        paragraphs = parse_letter_paragraphs(raw)
    except Exception as exc:  # noqa: BLE001 - any provider fault falls back
        note = f"model letter unusable ({type(exc).__name__}); used the template"
        log.info("%s", note)
        report.factcheck_findings.append(f"[note] provider - {note}")
        return deterministic_letter(evidence, company=company, role=role,
                                    findings=(note,))

    body = "\n\n".join(paragraphs)
    result = check_text(body, evidence, company=company, role=role,
                        private_repositories=private, excluded_files=excluded)
    report.factcheck_findings.extend(str(item) for item in result.findings)
    if not result.ok:
        note = (f"model letter failed fact-checking "
                f"({len(result.blocking)} blocking); used the template instead")
        log.warning("%s for %s", note, evidence.job_id)
        return deterministic_letter(
            evidence, company=company, role=role,
            findings=(note, *(str(item) for item in result.blocking)))
    return build_letter(paragraphs, evidence, role=role, generated_by="model",
                        findings=tuple(str(item) for item in result.warnings))


def generate_package(*, job: Mapping[str, Any], profile: Mapping[str, Any] | None = None,
                     provider: Any = None, contact: ContactDetails | None = None,
                     output_dir: Path | str = DEFAULT_OUTPUT_DIR,
                     assessment: Mapping[str, Any] | None = None,
                     language: str = "", write_files: bool = True,
                     contact_path: Path | str | None = None,
                     styles: Sequence[str] = ("modern",),
                     max_projects: int = 3) -> ApplicationPackage:
    """Build one application draft.

    `job` carries the spreadsheet row's own fields: job_id, company, role,
    location, description, track, status, score, eligibility.

    `styles` names the CV layouts to render (see `layout.STYLE_NAMES`). The
    first is the primary one: it keeps the plain filename and is the only one
    a `--write` run records in the tracker. `max_projects` is how many pieces
    of project evidence the CV shows for this vacancy.
    """
    data = dict(job)
    styles_to_render = [name for name in dict.fromkeys(str(item) for item in styles)]
    if not styles_to_render:
        raise ValueError("at least one CV style is required")
    for name in styles_to_render:
        get_style(name)  # Fail before anything is written.
    if not 1 <= int(max_projects) <= 6:
        raise ValueError("max_projects must be between 1 and 6")
    job_id = str(data.get("job_id") or data.get("Job ID") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    company = str(data.get("company") or data.get("Company") or "").strip()
    role = str(data.get("role") or data.get("Role") or "").strip()
    if not company or not role:
        raise ValueError(f"{job_id}: company and role are required")
    location = str(data.get("location") or data.get("Location") or "").strip()
    description = str(data.get("description") or "")
    if len(description.strip()) < 100:
        raise ValueError(
            f"{job_id}: the job description is missing or too short "
            f"({len(description.strip())} characters). Run 'job-agent daily --ai' "
            "so the advert is fetched and cached, then try again.")

    candidate = dict(profile or load_candidate_profile())
    verdict = dict(assessment or {})
    details = contact or load_contact(
        contact_path or Path("data/contact.json"),
        fallback_name=str((candidate.get("candidate") or {}).get("display_name", "")),
        github_url=str((candidate.get("candidate") or {}).get("public_github_url", "")))

    evidence = select_evidence(
        candidate, job_id=job_id, role=role, company=company,
        description=description, track=str(data.get("track") or ""),
        assessment=verdict, language=language)

    report = ReviewReport(
        job_id=job_id, company=company, role=role, location=location,
        language=evidence.language, track=evidence.track,
        status=str(data.get("status") or data.get("Status") or ""),
        score=data.get("score", data.get("Match %", "")),
        eligibility=str(verdict.get("eligibility")
                        or data.get("eligibility") or data.get("Eligibility") or ""),
        evidence_skills=evidence.skill_names,
        evidence_projects=tuple(f"{item.name} ({item.url})"
                                for item in evidence.projects),
        requirements_addressed=evidence.matched_requirements,
        requirements_unmet=evidence.unmet_requirements,
        open_questions=evidence.open_questions,
        excluded_evidence=evidence.excluded,
        cautions=evidence.cautions,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    cv = build_cv(evidence, role=role, max_projects=int(max_projects))
    report.cv_variant = cv.variant
    report.cv_styles = tuple(styles_to_render)
    cv_check = check_claims(cv.all_claims(), evidence)
    report.factcheck_findings.extend(str(item) for item in cv_check.findings)
    if not cv_check.ok:
        raise ValueError(
            f"{job_id}: the assembled CV contains {len(cv_check.blocking)} line(s) "
            "that do not trace back to the profile. This is a bug in evidence "
            "selection, not something to send: " + "; ".join(
                item.excerpt for item in cv_check.blocking[:3]))

    letter = _write_letter(evidence, provider=provider, company=company, role=role,
                           location=location, description=description,
                           assessment_summary=str(verdict.get("summary") or ""),
                           profile=candidate, report=report)
    report.letter_source = letter.generated_by
    report.factcheck_findings.extend(f"[note] letter - {item}"
                                     for item in letter.findings)

    report.verify_before_sending.extend(details.verification_notes())
    if details.photo_note and any(get_style(name).allow_photo
                                  for name in styles_to_render):
        report.verify_before_sending.append(details.photo_note)
    untranslated = untranslated_descriptions(evidence)
    if untranslated:
        report.verify_before_sending.append(
            "These descriptions are in English inside a German document: "
            + ", ".join(untranslated[:4])
            + ". Add 'evidence_points_de' / 'background_de' to "
              "data/candidate_profile.json for a fully German CV, or edit the "
              "DOCX. Nothing was machine-translated.")
    if letter.generated_by == "template":
        report.verify_before_sending.append(
            "The letter came from the deterministic template; read it for tone "
            "and add a sentence about this employer before sending.")
    for question in evidence.open_questions:
        report.verify_before_sending.append(f"Open requirement: {question}")
    for unmet in evidence.unmet_requirements:
        report.verify_before_sending.append(
            f"The advert asks for '{unmet}', which the evidence does not cover.")
    report.verify_before_sending.append(
        "Confirm the employer name, the exact job title and the address block.")
    report.verify_before_sending.append(
        "Read both documents in full. They are drafts generated from your "
        "profile, not a submission.")

    base = Path(output_dir) / slugify(f"{job_id}-{company}")
    package = ApplicationPackage(report=report, evidence=evidence, cv=cv,
                                 letter=letter, directory=base)
    if not write_files:
        report.documents = {"preview": "dry run; no files written"}
        return package

    base.mkdir(parents=True, exist_ok=True)
    # German convention: document type, applicant, then employer. Each part is
    # shortened separately so neither is cut mid-word by a single budget.
    who = slugify(details.full_name or "bewerbung", limit=24)
    where = slugify(company, limit=28)
    cv_label = "Lebenslauf" if cv.language == "de" else "CV"
    letter_label = "Anschreiben" if letter.language == "de" else "Cover-Letter"
    converter = converter_available()

    def render_cv_pair(style_name: str, suffix: str) -> tuple[Path, Path, LayoutReport]:
        """Write one CV style as DOCX + PDF and measure the result."""
        style = get_style(style_name)
        docx_path = render_cv(cv, details, base / f"{cv_label}-{who}-{where}{suffix}.docx",
                             style=style)
        # Prefer converting the DOCX, so the PDF is exactly the editable file.
        converted = convert_with_libreoffice(docx_path, base) if converter else None
        if converted is not None:
            layout = verify_pdf_pages(
                converted, style=style.name, tier=style.tier, source=converter,
                photo=bool(style.allow_photo and details.has_photo))
            return docx_path, converted, layout
        rendered = render_cv_pdf(cv, details,
                                 base / f"{cv_label}-{who}-{where}{suffix}.pdf",
                                 style=style)
        return docx_path, rendered.path, rendered.layout

    primary = styles_to_render[0]
    package.cv_docx, package.cv_pdf, primary_layout = render_cv_pair(primary, "")
    package.layouts[primary] = primary_layout
    for extra in styles_to_render[1:]:
        docx_path, pdf_path, layout = render_cv_pair(extra, f"-{extra.upper()}")
        package.alternates[extra] = (docx_path, pdf_path)
        package.layouts[extra] = layout

    letter_style = get_style(primary)
    package.letter_docx = render_letter(
        letter, details, base / f"{letter_label}-{who}-{where}.docx",
        company=company, style=letter_style)
    converted_letter = (convert_with_libreoffice(package.letter_docx, base)
                        if converter else None)
    if converted_letter is not None:
        package.letter_pdf = converted_letter
    else:
        package.letter_pdf = render_letter_pdf(
            letter, details, base / f"{letter_label}-{who}-{where}.pdf",
            company=company, style=letter_style)
    report.document_checks.append(
        f"PDF produced by converting the DOCX ({converter})" if converter
        else "PDF rendered directly (no local LibreOffice found; install "
             "libreoffice-writer to convert the DOCX instead)")

    for name, layout in package.layouts.items():
        report.layouts.append(layout.describe())
        for warning in layout.warnings:
            report.verify_before_sending.append(f"CV layout ({name}): {warning}")

    for kind, path in (("cv_docx", package.cv_docx), ("cv_pdf", package.cv_pdf),
                       ("letter_docx", package.letter_docx),
                       ("letter_pdf", package.letter_pdf)):
        report.documents[kind] = str(path)
    for name, (docx_path, pdf_path) in package.alternates.items():
        report.documents[f"cv_docx_{name}"] = str(docx_path)
        report.documents[f"cv_pdf_{name}"] = str(pdf_path)
    checked_pdfs = [(f"CV ({primary})", package.cv_pdf),
                    ("letter", package.letter_pdf)]
    checked_docx = [(f"CV ({primary})", package.cv_docx),
                    ("letter", package.letter_docx)]
    for name, (docx_path, pdf_path) in package.alternates.items():
        checked_pdfs.append((f"CV ({name})", pdf_path))
        checked_docx.append((f"CV ({name})", docx_path))
    for label, path in checked_pdfs:
        ok, detail = pdf_is_readable(path)
        report.document_checks.append(
            f"{label} PDF {'readable' if ok else 'NOT READABLE'}: {detail}")
    for label, path in checked_docx:
        text = read_docx_text(path)
        report.document_checks.append(
            f"{label} DOCX readable: {len(text)} characters of text")

    (base / "review.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (base / "review.txt").write_text(report.render_text() + "\n", encoding="utf-8")
    report.documents["review_json"] = str(base / "review.json")
    report.documents["review_txt"] = str(base / "review.txt")
    return package


def selectable_rows(rows: Sequence[Mapping[str, Any]], *,
                    statuses: Sequence[str] = ("Review", "Potential"),
                    job_id: str = "", limit: int = 0) -> list[Mapping[str, Any]]:
    """Pick the vacancies a draft may be generated for.

    A `Potential` row with a confirmed hard disqualification is excluded: the
    band exists for uncertain cases, not for ones already ruled out.
    """
    wanted = {str(name) for name in statuses}
    chosen: list[Mapping[str, Any]] = []
    for row in rows:
        identifier = str(row.get("Job ID") or "")
        if job_id:
            if identifier == job_id:
                chosen.append(row)
            continue
        if str(row.get("Status") or "") not in wanted:
            continue
        if str(row.get("Eligibility") or "").strip().casefold() == "ineligible":
            continue
        chosen.append(row)
    if limit and limit > 0:
        return chosen[:limit]
    return chosen
