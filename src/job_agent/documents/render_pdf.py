"""PDF rendering, by whichever route this machine supports.

Two paths, and the preference is deliberate:

1. **LibreOffice conversion of the DOCX**, when `soffice` is installed. This is
   the better route: one source document, so the PDF is exactly what the
   editable file says, and no layout can drift between the two.
2. **Direct ReportLab rendering** of the same content model, otherwise.

This machine currently has no LibreOffice, so route 2 is what runs. Installing
`libreoffice-writer` switches to route 1 automatically, with no code change.
The DOCX is the editable deliverable either way; the PDF is the sending copy.

Pagination is measured, not hoped for. `render_cv_pdf` builds the document,
reads the page count back off the result and, when a junior CV has spilled onto
a second page, retries once at the `compact` density tier. If one page is still
impossible it keeps the *roomier* layout rather than shrinking the text to win
an argument with A4 — a legible two-page CV beats an unreadable one-page CV.
Whatever comes out, `LayoutReport` says how many pages there are, how full the
last one is, and what a human should look at.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (HRFlowable, Image, KeepTogether, ListFlowable,
                                ListItem, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)
from reportlab.platypus.doctemplate import LayoutError

from .contact import ContactDetails
from .content import CvContent, LetterContent, today_long
from .layout import (MAX_COMFORTABLE_FILL, MAX_PAGES, MODERN, PREFERRED_PAGES,
                     CvStyle, LayoutReport, assess_pages, pdf_page_count)

log = logging.getLogger("job_agent.documents.pdf")

CONVERTERS = ("soffice", "libreoffice")


@dataclass(frozen=True)
class RenderedPdf:
    """A written PDF and what was measured about its layout."""

    path: Path
    layout: LayoutReport


def _escape(text: str) -> str:
    """ReportLab paragraphs accept inline markup, so escape the content."""
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def converter_available() -> str:
    """Path to a local DOCX->PDF converter, or "" when none is installed."""
    for name in CONVERTERS:
        found = shutil.which(name)
        if found:
            return found
    return ""


def convert_with_libreoffice(docx_path: Path | str, out_dir: Path | str,
                             *, timeout: int = 120) -> Path | None:
    """Convert a DOCX in place. Returns None when no converter is installed.

    Runs headless and offline; LibreOffice is given its own profile directory
    so a conversion cannot disturb an interactive LibreOffice session.
    """
    binary = converter_available()
    if not binary:
        return None
    source = Path(docx_path)
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    profile = target_dir / ".soffice-profile"
    try:
        subprocess.run(
            [binary, "--headless", "--norestore",
             f"-env:UserInstallation=file://{profile.resolve()}",
             "--convert-to", "pdf", "--outdir", str(target_dir), str(source)],
            check=True, capture_output=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        log.info("LibreOffice conversion failed (%s); using the direct renderer",
                 type(exc).__name__)
        return None
    produced = target_dir / (source.stem + ".pdf")
    return produced if produced.is_file() else None


# --- styles ---------------------------------------------------------------


def _styles(style: CvStyle) -> dict[str, ParagraphStyle]:
    ink = HexColor(style.accent if style.is_ats else "#1A1A1A")
    muted = HexColor(style.muted)
    base = ParagraphStyle("body", fontName="Helvetica", fontSize=style.body_pt,
                          leading=style.body_pt * style.leading, textColor=ink,
                          alignment=TA_LEFT, spaceAfter=style.space_after)
    return {
        "body": base,
        "name": ParagraphStyle("name", parent=base, fontName="Helvetica-Bold",
                               fontSize=style.name_pt,
                               leading=style.name_pt * 1.1,
                               textColor=HexColor(style.name_colour), spaceAfter=1),
        "headline": ParagraphStyle("headline", parent=base,
                                   fontName="Helvetica-Bold",
                                   fontSize=style.headline_pt,
                                   leading=style.headline_pt * 1.2,
                                   textColor=HexColor(style.accent), spaceAfter=1),
        "subline": ParagraphStyle("subline", parent=base, fontSize=style.subline_pt,
                                  leading=style.subline_pt * 1.25,
                                  textColor=muted, spaceAfter=2),
        "meta": ParagraphStyle("meta", parent=base, fontSize=style.meta_pt,
                               leading=style.meta_pt * 1.3, textColor=muted,
                               spaceAfter=0),
        "heading": ParagraphStyle("heading", parent=base,
                                  fontName="Helvetica-Bold",
                                  fontSize=style.heading_pt,
                                  leading=style.heading_pt * 1.2,
                                  textColor=HexColor(style.heading_colour),
                                  spaceBefore=style.section_before,
                                  spaceAfter=style.section_after),
        "entry": ParagraphStyle("entry", parent=base, fontName="Helvetica-Bold",
                                spaceBefore=style.entry_before, spaceAfter=0),
        "period": ParagraphStyle("period", parent=base, fontSize=style.meta_pt,
                                 leading=style.meta_pt * 1.3, textColor=muted,
                                 alignment=TA_RIGHT, spaceAfter=0),
        "sub": ParagraphStyle("sub", parent=base, fontSize=style.meta_pt,
                              leading=style.meta_pt * 1.3, textColor=muted,
                              spaceAfter=1),
        "rowlabel": ParagraphStyle("rowlabel", parent=base,
                                   fontName="Helvetica-Bold",
                                   textColor=HexColor(style.heading_colour),
                                   spaceAfter=0),
        "rowvalue": ParagraphStyle("rowvalue", parent=base, spaceAfter=0),
        "right": ParagraphStyle("right", parent=base, alignment=TA_RIGHT,
                                textColor=muted, spaceAfter=10),
        "subject": ParagraphStyle("subject", parent=base,
                                  fontName="Helvetica-Bold",
                                  textColor=HexColor(style.accent),
                                  spaceBefore=8, spaceAfter=9),
    }


class _MeasuredDoc(SimpleDocTemplate):
    """A document template that remembers how full each page ended up.

    `afterFlowable` runs with the live frame, so the cursor position after the
    last flowable on the final page is exactly the fill level a reader sees.
    Nothing here changes the output; it only observes it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.page_cursor: dict[int, float] = {}

    def afterFlowable(self, flowable) -> None:  # noqa: N802 - ReportLab's name
        frame = getattr(self, "frame", None)
        if frame is not None:
            self.page_cursor[self.page] = float(getattr(frame, "_y", 0.0))

    def fill_of_last_page(self) -> float:
        """Fraction of the final page's text frame that carries content."""
        frame = getattr(self, "frame", None)
        if frame is None or not self.page_cursor:
            return 0.0
        top_padding = float(getattr(frame, "_topPadding", 0.0))
        bottom_padding = float(getattr(frame, "_bottomPadding", 0.0))
        usable = float(getattr(frame, "height", 0.0)) - top_padding - bottom_padding
        if usable <= 0:
            return 0.0
        top = float(getattr(frame, "_y1", 0.0)) + float(
            getattr(frame, "height", 0.0)) - top_padding
        used = top - self.page_cursor[max(self.page_cursor)]
        return max(0.0, min(1.0, used / usable))


def _document(path: Path, style: CvStyle, title: str, author: str) -> _MeasuredDoc:
    return _MeasuredDoc(
        str(path), pagesize=A4,
        topMargin=style.margin_top * mm, bottomMargin=style.margin_bottom * mm,
        leftMargin=style.margin_left * mm, rightMargin=style.margin_right * mm,
        title=title, author=author, subject=title,
        creator="job-application-agent")


def _bullets(items: Sequence[str], style: CvStyle,
             styles: dict[str, ParagraphStyle]) -> ListFlowable:
    return ListFlowable(
        [ListItem(Paragraph(_escape(item), styles["body"]), leftIndent=10)
         for item in items],
        bulletType="bullet", start="–", bulletFontName="Helvetica",
        bulletFontSize=style.body_pt - 1, leftIndent=11,
        spaceAfter=style.bullet_after)


def _photo_flowable(contact: ContactDetails, style: CvStyle) -> Image | None:
    """The Bewerbungsfoto, scaled by height with its aspect ratio preserved.

    Never cropped and never altered: the only operation is a uniform scale.
    """
    if not (style.allow_photo and contact.has_photo):
        return None
    height = style.photo_height_mm * mm
    try:
        from reportlab.lib.utils import ImageReader

        reader = ImageReader(contact.photo_path)
        pixel_width, pixel_height = reader.getSize()
        if pixel_height <= 0:
            return None
        width = height * (pixel_width / pixel_height)
    except Exception as exc:  # noqa: BLE001 - an unusable image is simply omitted
        log.info("photo not embedded (%s)", type(exc).__name__)
        return None
    return Image(contact.photo_path, width=width, height=height)


def _contact_text(contact: ContactDetails, cv: CvContent | None = None) -> str:
    lines = list(contact.header_lines())
    if cv is not None and cv.availability_line:
        lines.append(cv.availability_line)
    return "  ·  ".join(lines)


def _header(cv: CvContent, contact: ContactDetails, style: CvStyle,
            styles: dict[str, ParagraphStyle], *, width: float) -> list:
    identity: list = []
    if contact.full_name:
        identity.append(Paragraph(_escape(contact.full_name), styles["name"]))
    if cv.headline:
        identity.append(Paragraph(_escape(cv.headline), styles["headline"]))
    if cv.subline:
        identity.append(Paragraph(_escape(cv.subline), styles["subline"]))
    details = _contact_text(contact, cv)
    if details:
        identity.append(Paragraph(_escape(details), styles["meta"]))

    photo = _photo_flowable(contact, style)
    flow: list = []
    if photo is None:
        flow.extend(identity)
    else:
        photo_width = photo.drawWidth + 4 * mm
        table = Table([[identity, photo]],
                      colWidths=[width - photo_width, photo_width])
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (0, 0), "TOP"),
            ("VALIGN", (1, 0), (1, 0), "TOP"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        flow.append(table)
    if style.header_rule:
        flow.append(Spacer(1, 3 * mm))
        flow.append(HRFlowable(width="100%", thickness=0.8,
                               color=HexColor(style.rule_colour),
                               spaceBefore=0, spaceAfter=2))
    return flow


def _section_heading(text: str, style: CvStyle,
                     styles: dict[str, ParagraphStyle]) -> list:
    flow: list = [Paragraph(_escape(text), styles["heading"])]
    if style.section_rules:
        flow.append(HRFlowable(width="100%", thickness=0.6,
                               color=HexColor(style.rule_colour),
                               spaceBefore=-2, spaceAfter=4))
    return flow


def _rows(pairs: Sequence[tuple[str, str]], style: CvStyle,
          styles: dict[str, ParagraphStyle], *, width: float) -> list:
    """Compact category rows. A table in `modern`, plain lines in `ats`."""
    if style.is_ats or not style.allow_tables:
        return [Paragraph(f"<b>{_escape(label)}:</b> {_escape(value)}",
                          styles["body"]) for label, value in pairs]
    # Size the label column to the longest label actually present, rather than
    # a fixed guess: "Systemprogrammierung & Netzwerke" wrapped onto three
    # lines in a 46mm column and threw the whole block out of alignment.
    from reportlab.pdfbase.pdfmetrics import stringWidth

    longest = max((stringWidth(label, "Helvetica-Bold", style.body_pt)
                   for label, _ in pairs), default=0.0)
    label_width = min(max(longest + 6, 30 * mm), width * 0.40)
    table = Table(
        [[Paragraph(_escape(label), styles["rowlabel"]),
          Paragraph(_escape(value), styles["rowvalue"])] for label, value in pairs],
        colWidths=[label_width, width - label_width])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), style.bullet_after),
    ]))
    return [table]


def _entry(entry, style: CvStyle, styles: dict[str, ParagraphStyle], *,
           width: float) -> list:
    """One project, job or qualification, kept together on a single page."""
    flow: list = []
    # A project has no dates but does have a repository: the right-hand slot
    # carries whichever exists, so the entry costs one line, not two.
    trailing = entry.period or (entry.link if not entry.subtitle else "")
    if trailing and not style.is_ats and style.allow_tables:
        table = Table([[Paragraph(_escape(entry.title), styles["entry"]),
                        Paragraph(_escape(trailing), styles["period"])]],
                      colWidths=[width * 0.58, width * 0.42])
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), style.entry_before),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        flow.append(table)
        meta = " | ".join(part for part in (entry.subtitle, entry.link)
                          if part and part != trailing)
    else:
        # No table in the `ats` variant, so the trailing reference is appended
        # inline. Both the DOCX and the PDF then spend one line on it, which
        # keeps the two files' line counts -- and their pagination -- aligned.
        title_text = _escape(entry.title)
        if trailing:
            title_text += (f' <font size="{style.meta_pt}" '
                           f'color="{style.muted}">\u2013 {_escape(trailing)}</font>')
        flow.append(Paragraph(title_text, styles["entry"]))
        meta = " | ".join(part for part in
                          (entry.subtitle, entry.period, entry.link)
                          if part and part != trailing)
    if meta:
        flow.append(Paragraph(_escape(meta), styles["sub"]))
    if entry.bullets:
        flow.append(_bullets(entry.bullets, style, styles))
    return [KeepTogether(flow)]


def _cv_flowables(cv: CvContent, contact: ContactDetails, style: CvStyle,
                  *, width: float) -> list:
    styles = _styles(style)
    flow = _header(cv, contact, style, styles, width=width)
    flow.extend(_section_heading("Profil" if cv.language == "de" else "Profile",
                                 style, styles))
    flow.append(Paragraph(_escape(cv.summary), styles["body"]))
    for section in cv.sections:
        if section.empty:
            continue
        flow.extend(_section_heading(section.heading, style, styles))
        if section.rows:
            flow.extend(_rows(section.rows, style, styles, width=width))
        if section.bullets:
            flow.append(_bullets(section.bullets, style, styles))
        for entry in section.entries:
            flow.extend(_entry(entry, style, styles, width=width))
    return flow


def _build_once(cv: CvContent, contact: ContactDetails, style: CvStyle,
                target: Path) -> tuple[int, float, str]:
    """Render one attempt. Returns (pages, last-page fill, clipping detail)."""
    document = _document(target, style, cv.headline or "CV", contact.full_name)
    flow = _cv_flowables(cv, contact, style, width=document.width)
    try:
        document.build(flow)
    except LayoutError as exc:
        # A flowable taller than the frame. The file may be incomplete, so the
        # caller must know rather than ship it silently.
        return 0, 0.0, f"content did not fit the page frame ({exc})"
    pages = pdf_page_count(target) or int(getattr(document, "page", 0) or 0)
    return pages, document.fill_of_last_page(), ""


def render_cv_pdf(cv: CvContent, contact: ContactDetails, path: Path | str, *,
                  style: CvStyle = MODERN, fit_pages: bool = True) -> RenderedPdf:
    """Render the CV and verify its pagination.

    Attempts, in order: the style as given, then its `compact` tier if that
    spilled past one page. If neither fits on one page the roomier layout is
    restored, because requirement one is a *legible* page.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    attempts: list[str] = []
    ladder = style.tiers() if fit_pages else (style,)

    # Walk the ladder from roomiest to tightest and stop at the first tier that
    # fits one page with a little slack at the bottom. A tier that fits but
    # fills the page to the last millimetre is remembered as a fallback, not
    # accepted outright: the DOCX repaginates in Word and would spill.
    pages = fill = 0
    clipped = ""
    used = style
    fallback: tuple[int, float, CvStyle] | None = None
    for candidate in ladder:
        pages, fill, clipped = _build_once(cv, contact, candidate, target)
        used = candidate
        attempts.append(f"{candidate.tier}:{pages}p@{fill * 100:.0f}%")
        if clipped:
            break
        if 0 < pages <= PREFERRED_PAGES:
            if fill <= MAX_COMFORTABLE_FILL:
                break
            if fallback is None:
                fallback = (pages, fill, candidate)
    else:
        if fallback is not None:
            # Every tier that fitted was packed to the edge; take the roomiest
            # of them and re-render it, since the file now holds the last try.
            _, _, used = fallback
            pages, fill, clipped = _build_once(cv, contact, used, target)
            attempts.append(f"{used.tier}:kept")
        else:
            # No tier won a single page, so tightening bought nothing. Restore
            # the roomiest: a balanced two pages beats cramped text.
            pages, fill, clipped = _build_once(cv, contact, style, target)
            used = style
            attempts.append(f"{style.tier}:restored")

    warnings = list(assess_pages(pages, fill))
    if clipped:
        warnings.insert(0, clipped)
    layout = LayoutReport(style=used.name, tier=used.tier, pages=pages,
                          last_page_fill=fill, attempts=tuple(attempts),
                          photo=bool(used.allow_photo and contact.has_photo),
                          warnings=tuple(warnings), source="reportlab")
    return RenderedPdf(path=target, layout=layout)


def render_letter_pdf(letter: LetterContent, contact: ContactDetails,
                      path: Path | str, *, company: str = "",
                      company_address: Sequence[str] = (),
                      city_line: str = "", style: CvStyle = MODERN) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    styles = _styles(style)
    document = _document(target, style, letter.subject, contact.full_name)
    flow: list = []
    if contact.full_name:
        flow.append(Paragraph(_escape(contact.full_name),
                              ParagraphStyle("lname", parent=styles["name"],
                                             fontSize=style.name_pt - 4,
                                             leading=(style.name_pt - 4) * 1.1)))
    details = _contact_text(contact)
    if details:
        flow.append(Paragraph(_escape(details), styles["meta"]))
    if style.header_rule:
        flow.append(Spacer(1, 3 * mm))
        flow.append(HRFlowable(width="100%", thickness=0.8,
                               color=HexColor(style.rule_colour), spaceAfter=2))
    if company:
        flow.append(Spacer(1, 6 * mm))
        flow.append(Paragraph(_escape(company), styles["body"]))
    for line in company_address:
        flow.append(Paragraph(_escape(line), styles["body"]))
    place = city_line or contact.city
    date_line = today_long(letter.language)
    flow.append(Paragraph(_escape(f"{place}, {date_line}" if place else date_line),
                          styles["right"]))
    flow.append(Paragraph(_escape(letter.subject), styles["subject"]))
    flow.append(Paragraph(_escape(letter.salutation), styles["body"]))
    flow.append(Spacer(1, 2 * mm))
    for text in letter.paragraphs:
        flow.append(Paragraph(_escape(text), styles["body"]))
        flow.append(Spacer(1, 2 * mm))
    if letter.closing:
        flow.append(Paragraph(_escape(letter.closing), styles["body"]))
    flow.append(Spacer(1, 4 * mm))
    flow.append(Paragraph(_escape(letter.signoff), styles["body"]))
    flow.append(Spacer(1, 10 * mm))
    if contact.full_name:
        flow.append(Paragraph(_escape(contact.full_name), styles["body"]))
    document.build(flow)
    return target


def pdf_is_readable(path: Path | str, *, min_bytes: int = 800) -> tuple[bool, str]:
    """Shallow structural check: a real PDF header, trailer and plausible size.

    Deliberately not a parse. It catches a truncated or empty file, which is
    the failure that actually happens, without adding a PDF-reading dependency.
    """
    target = Path(path)
    if not target.is_file():
        return False, "file does not exist"
    data = target.read_bytes()
    if len(data) < min_bytes:
        return False, f"only {len(data)} bytes; likely truncated"
    if not data.startswith(b"%PDF-"):
        return False, "missing %PDF header"
    if b"%%EOF" not in data[-2048:]:
        return False, "missing %%EOF trailer"
    if b"/Page" not in data:
        return False, "no page object found"
    pages = pdf_page_count(target)
    suffix = f", {pages} page(s)" if pages else ""
    return True, f"{len(data)} bytes, header and trailer present{suffix}"


def verify_pdf_pages(path: Path | str, *, style: str = "", tier: str = "",
                     source: str = "libreoffice", photo: bool = False,
                     maximum: int = MAX_PAGES) -> LayoutReport:
    """Check pagination of a PDF this module did not lay out itself.

    Used for the LibreOffice route, where the page breaks are Writer's. The
    fill of the last page cannot be measured from outside, so only the page
    count is judged.
    """
    pages = pdf_page_count(path)
    warnings: list[str] = []
    if pages <= 0:
        warnings.append("page count could not be verified; open the PDF and check it")
    elif pages > maximum:
        warnings.append(f"{pages} pages: a junior CV should not exceed {maximum}")
    return LayoutReport(style=style, tier=tier, pages=pages, last_page_fill=0.0,
                        photo=photo, warnings=tuple(warnings), source=source)
