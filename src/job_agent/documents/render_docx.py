"""DOCX rendering. The editable deliverable.

Two layouts of the same content, chosen by `CvStyle` (see `layout.py`):

- **`ats`** is ATS-friendly by omission, which mostly means doing less: no
  tables, text boxes, headers/footers, columns or images; a single common font;
  near-black text; real paragraph styles, so a parser sees headings as
  headings. Applicant tracking systems parse decoration unreliably or drop it.
- **`modern`** is for a human reader at the employer. It adds navy section
  headings with a hairline rule, a right tab stop for dates, and — when one
  exists locally — a Bewerbungsfoto in a borderless one-row header table. That
  table is the one structural concession, and it exists only in this variant;
  `ats` still renders the header as plain paragraphs.

Both layouts stay fully editable in Word, LibreOffice and Google Docs, and
neither can change a fact: they consume the same `CvContent`.

The photo is placed by height alone, so python-docx preserves its aspect ratio.
Nothing is cropped, retouched or generated.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor

from .contact import ContactDetails
from .content import CvContent, LetterContent, today_long
from .layout import MODERN, CvStyle

BODY_FONT = "Calibri"


def _rgb(value: str) -> RGBColor:
    return RGBColor.from_string(str(value).lstrip("#").upper())


def _configure(document: Document, style: CvStyle) -> None:
    """One font, style-driven margins, nothing an ATS could trip over."""
    normal = document.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(style.body_pt)
    normal.font.color.rgb = _rgb(style.accent if style.is_ats else "#1A1A1A")
    normal.paragraph_format.space_after = Pt(style.space_after)
    normal.paragraph_format.line_spacing = style.leading
    for section in document.sections:
        section.top_margin = Mm(style.margin_top)
        section.bottom_margin = Mm(style.margin_bottom)
        section.left_margin = Mm(style.margin_left)
        section.right_margin = Mm(style.margin_right)


def _paragraph(container, text: str, *, size=None, bold=False, colour=None,
               space_before=0.0, space_after=None, style_name=None,
               align=WD_ALIGN_PARAGRAPH.LEFT, italic=False):
    paragraph = (container.add_paragraph(style=style_name) if style_name
                 else container.add_paragraph())
    paragraph.alignment = align
    paragraph.paragraph_format.space_before = Pt(space_before)
    if space_after is not None:
        paragraph.paragraph_format.space_after = Pt(space_after)
    if text:
        run = paragraph.add_run(text)
        run.bold = bold
        run.italic = italic
        if size is not None:
            run.font.size = Pt(size)
        if colour is not None:
            run.font.color.rgb = _rgb(colour)
    return paragraph


def _bottom_rule(paragraph, colour: str, *, size: int = 6) -> None:
    """A hairline under a paragraph. Decoration only; carries no text."""
    properties = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "2")
    bottom.set(qn("w:color"), str(colour).lstrip("#").upper())
    borders.append(bottom)
    properties.append(borders)


def _clear_table_borders(table) -> None:
    """Make a layout table invisible. It must not read as a data table."""
    properties = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "none")
        element.set(qn("w:sz"), "0")
        borders.append(element)
    properties.append(borders)


def _heading(document: Document, text: str, style: CvStyle) -> None:
    """A real heading style, so a parser recognises the section."""
    paragraph = document.add_heading(text, level=2)
    paragraph.paragraph_format.space_before = Pt(style.section_before)
    paragraph.paragraph_format.space_after = Pt(style.section_after)
    for run in paragraph.runs:
        run.font.name = BODY_FONT
        run.font.size = Pt(style.heading_pt)
        run.font.color.rgb = _rgb(style.heading_colour)
        run.bold = True
    if style.section_rules:
        _bottom_rule(paragraph, style.rule_colour)


def _bullet(document: Document, text: str, style: CvStyle) -> None:
    paragraph = document.add_paragraph(text, style="List Bullet")
    paragraph.paragraph_format.space_after = Pt(style.bullet_after)
    for run in paragraph.runs:
        run.font.size = Pt(style.body_pt)


def _row(document: Document, label: str, value: str, style: CvStyle) -> None:
    """A compact "Category: skills" line. Two runs, no table, parses cleanly."""
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(style.bullet_after)
    head = paragraph.add_run(f"{label}: ")
    head.bold = True
    head.font.size = Pt(style.body_pt)
    if not style.is_ats:
        head.font.color.rgb = _rgb(style.heading_colour)
    tail = paragraph.add_run(value)
    tail.font.size = Pt(style.body_pt)


def _entry_heading(document: Document, title: str, period: str,
                   style: CvStyle, *, width_mm: float) -> None:
    """Entry title on the left, period right-aligned on the same line.

    A right tab stop rather than a table: it keeps the pair on one line in both
    variants and still reads as plain text to a parser.
    """
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(style.entry_before)
    paragraph.paragraph_format.space_after = Pt(0)
    if period:
        paragraph.paragraph_format.tab_stops.add_tab_stop(
            Mm(width_mm), WD_TAB_ALIGNMENT.RIGHT)
    run = paragraph.add_run(title)
    run.bold = True
    run.font.size = Pt(style.body_pt)
    if period:
        tail = paragraph.add_run(f"\t{period}")
        tail.font.size = Pt(style.meta_pt)
        tail.font.color.rgb = _rgb(style.muted)


def _contact_lines(contact: ContactDetails, cv: CvContent | None = None) -> str:
    lines = list(contact.header_lines())
    if cv is not None and cv.availability_line:
        lines.append(cv.availability_line)
    return "  ·  ".join(lines)


def _header(document: Document, cv: CvContent, contact: ContactDetails,
            style: CvStyle, *, width_mm: float) -> None:
    """Name and headline top left, optional Bewerbungsfoto top right."""
    use_photo = bool(style.allow_photo and style.allow_tables and contact.has_photo)

    def identity(container) -> None:
        if contact.full_name:
            _paragraph(container, contact.full_name, size=style.name_pt, bold=True,
                       colour=style.name_colour, space_after=1)
        if cv.headline:
            _paragraph(container, cv.headline, size=style.headline_pt, bold=True,
                       colour=style.accent, space_after=1)
        if cv.subline:
            _paragraph(container, cv.subline, size=style.subline_pt,
                       colour=style.muted, space_after=3)
        details = _contact_lines(contact, cv)
        if details:
            _paragraph(container, details, size=style.meta_pt,
                       colour=style.muted, space_after=0)

    if not use_photo:
        identity(document)
        last = document.paragraphs[-1]
        last.paragraph_format.space_after = Pt(6)
        if style.header_rule:
            _bottom_rule(last, style.rule_colour)
        return

    table = document.add_table(rows=1, cols=2)
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    table.autofit = False
    _clear_table_borders(table)
    photo_width = style.photo_height_mm * 0.80
    left, right = table.rows[0].cells
    left.width = Mm(width_mm - photo_width - 4)
    right.width = Mm(photo_width + 4)
    # add_table gives each cell an empty paragraph; reuse it instead of
    # leaving a blank line above the name.
    left._element.remove(left.paragraphs[0]._p)
    identity(left)
    picture = right.paragraphs[0]
    picture.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    picture.paragraph_format.space_after = Pt(0)
    picture.add_run().add_picture(contact.photo_path, height=Mm(style.photo_height_mm))

    spacer = _paragraph(document, "", space_after=4)
    if style.header_rule:
        _bottom_rule(spacer, style.rule_colour)


def render_cv(cv: CvContent, contact: ContactDetails, path: Path | str, *,
              style: CvStyle = MODERN) -> Path:
    """Write the tailored CV in the requested style. Returns the file written."""
    document = Document()
    _configure(document, style)
    width_mm = 210.0 - style.margin_left - style.margin_right
    _header(document, cv, contact, style, width_mm=width_mm)

    labels = {"de": "Profil", "en": "Profile"}
    _heading(document, labels.get(cv.language, "Profil"), style)
    _paragraph(document, cv.summary, space_after=style.space_after)

    for section in cv.sections:
        if section.empty:
            continue
        _heading(document, section.heading, style)
        for label, value in section.rows:
            _row(document, label, value, style)
        for bullet in section.bullets:
            _bullet(document, bullet, style)
        for entry in section.entries:
            _entry_heading(document, entry.title, entry.period, style,
                           width_mm=width_mm)
            meta = " | ".join(part for part in (entry.subtitle, entry.link) if part)
            if meta:
                _paragraph(document, meta, size=style.meta_pt, colour=style.muted,
                           space_after=1)
            for bullet in entry.bullets:
                _bullet(document, bullet, style)

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(target))
    return target


def render_letter(letter: LetterContent, contact: ContactDetails,
                  path: Path | str, *, company: str = "",
                  company_address: Sequence[str] = (),
                  city_line: str = "", style: CvStyle = MODERN) -> Path:
    """Write the cover letter in German business-letter order.

    A German Anschreiben carries no photo, whatever the CV style, so the
    letterhead here only follows the style's typography and accent colour.
    """
    document = Document()
    _configure(document, style)
    if contact.full_name:
        _paragraph(document, contact.full_name, size=style.name_pt - 4, bold=True,
                   colour=style.name_colour, space_after=1)
    details = _contact_lines(contact)
    if details:
        header = _paragraph(document, details, size=style.meta_pt,
                            colour=style.muted, space_after=6)
        if style.header_rule:
            _bottom_rule(header, style.rule_colour)

    if company:
        _paragraph(document, company, space_before=10, space_after=0)
    for line in company_address:
        _paragraph(document, line, space_after=0)

    place = city_line or contact.city
    date_line = today_long(letter.language)
    _paragraph(document, f"{place}, {date_line}" if place else date_line,
               colour=style.muted, space_before=14, space_after=12,
               align=WD_ALIGN_PARAGRAPH.RIGHT)

    _paragraph(document, letter.subject, bold=True, size=style.body_pt + 0.5,
               colour=style.accent, space_after=10)
    _paragraph(document, letter.salutation, space_after=8)
    for text in letter.paragraphs:
        _paragraph(document, text, space_after=8)
    if letter.closing:
        _paragraph(document, letter.closing, space_after=12)
    _paragraph(document, letter.signoff, space_after=24)
    if contact.full_name:
        _paragraph(document, contact.full_name)

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(target))
    return target


def read_docx_text(path: Path | str) -> str:
    """Read a DOCX back as plain text. Used to verify the output is readable.

    Table cells are included, because the `modern` header puts the name and the
    headline in a borderless layout table.
    """
    document = Document(str(path))
    lines = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                lines.extend(paragraph.text for paragraph in cell.paragraphs)
    return "\n".join(lines)
