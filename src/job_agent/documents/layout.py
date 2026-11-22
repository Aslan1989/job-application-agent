"""CV layout styles, density tiers and page verification.

Three things live here so the DOCX and the PDF cannot drift apart:

1. **`CvStyle`** — the two variants the user chooses between.

   - `modern`: a polished Bewerbungs-Lebenslauf. Dark navy accents, a rule
     under each section heading, an optional Bewerbungsfoto at the top right.
     For applying directly to a human.
   - `ats`: one column, near-black text, no photo, no rules, no tables. For an
     applicant tracking system, which parses decoration unreliably or drops it.

   Both describe the *same* content model. Choosing a style never changes a
   fact, only its presentation.

2. **Density tiers** — `regular` and `compact`. A tier scales spacing, not
   legibility: body text stays between `MIN_BODY_PT` and 11 pt in both. The
   renderer tries `regular` first and only drops to `compact` when that is
   what turns two pages into one.

3. **`LayoutReport`** — what the review report says about pagination: how many
   pages were produced, how full the last one is, and a warning when a page is
   nearly empty, when content was clipped, or when a junior CV spilled past
   two pages.

Page counts are read back from the finished PDF, so the number in the report
is the number a recruiter will see, whichever renderer produced it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

# Never shrink body text below this to win a page. A cramped CV that nobody
# reads is worse than a well-balanced second page.
MIN_BODY_PT = 10.0

# A last page holding less than this fraction of the text frame reads as an
# accident rather than a layout. The current output's second page was ~12%.
MIN_LAST_PAGE_FILL = 0.18

# Junior and trainee applications are expected on one page, and two is the
# absolute ceiling.
PREFERRED_PAGES = 1
MAX_PAGES = 2

NAVY = "#1F3A5F"
NAVY_DEEP = "#14283F"
INK = "#1A1A1A"
MUTED = "#555555"
RULE = "#B9C6D6"


@dataclass(frozen=True)
class CvStyle:
    """How a CV looks. Never what it says."""

    name: str
    # Typography, in points.
    body_pt: float = 10.5
    name_pt: float = 23.0
    headline_pt: float = 12.0
    subline_pt: float = 10.0
    heading_pt: float = 11.0
    meta_pt: float = 9.0
    leading: float = 1.22
    # Vertical rhythm, in points.
    space_after: float = 3.0
    section_before: float = 11.0
    section_after: float = 4.0
    entry_before: float = 5.0
    bullet_after: float = 1.5
    # Page geometry, in millimetres.
    margin_top: float = 16.0
    margin_bottom: float = 15.0
    margin_left: float = 19.0
    margin_right: float = 19.0
    # Decoration.
    accent: str = NAVY
    name_colour: str = NAVY_DEEP
    heading_colour: str = NAVY
    muted: str = MUTED
    rule_colour: str = RULE
    section_rules: bool = True
    header_rule: bool = True
    allow_photo: bool = True
    allow_tables: bool = True
    photo_height_mm: float = 42.0
    tier: str = "regular"

    @property
    def is_ats(self) -> bool:
        return self.name == "ats"

    def compacted(self) -> "CvStyle":
        """The same style, one density tier tighter.

        Spacing shrinks and the headline comes down a little; body text loses
        half a point and stops at `MIN_BODY_PT`. Nothing else changes, so a
        compacted CV is the same document, not a smaller-print one.
        """
        return replace(
            self,
            tier="compact",
            body_pt=max(MIN_BODY_PT, self.body_pt - 0.5),
            name_pt=self.name_pt - 2.0,
            headline_pt=self.headline_pt - 0.5,
            heading_pt=self.heading_pt - 0.5,
            leading=max(1.12, self.leading - 0.06),
            space_after=max(1.5, self.space_after - 1.0),
            section_before=max(6.0, self.section_before - 3.5),
            section_after=max(2.0, self.section_after - 1.0),
            entry_before=max(2.5, self.entry_before - 2.0),
            bullet_after=max(0.5, self.bullet_after - 0.5),
            margin_top=max(12.0, self.margin_top - 3.0),
            margin_bottom=max(12.0, self.margin_bottom - 3.0),
            photo_height_mm=max(34.0, self.photo_height_mm - 5.0),
        )


MODERN = CvStyle(name="modern")

ATS = CvStyle(
    name="ats",
    body_pt=11.0,
    name_pt=19.0,
    headline_pt=11.5,
    heading_pt=11.5,
    leading=1.18,
    margin_top=18.0,
    margin_bottom=18.0,
    margin_left=22.0,
    margin_right=22.0,
    accent=INK,
    name_colour=INK,
    heading_colour=INK,
    rule_colour=RULE,
    section_rules=False,
    header_rule=False,
    allow_photo=False,
    allow_tables=False,
)

STYLES: dict[str, CvStyle] = {"modern": MODERN, "ats": ATS}
STYLE_NAMES = ("modern", "ats")


def get_style(name: str) -> CvStyle:
    """Look a style up by name. Unknown names are a caller error, not a default."""
    key = str(name or "modern").strip().lower()
    if key not in STYLES:
        raise ValueError(f"Unknown CV style '{name}'. Choose one of: "
                         + ", ".join(STYLE_NAMES))
    return STYLES[key]


@dataclass
class LayoutReport:
    """What happened to the page layout, for the human reading the report."""

    style: str = ""
    tier: str = "regular"
    pages: int = 0
    last_page_fill: float = 0.0
    attempts: tuple[str, ...] = ()
    photo: bool = False
    warnings: tuple[str, ...] = ()
    source: str = "reportlab"

    @property
    def ok(self) -> bool:
        return not self.warnings

    def describe(self) -> str:
        fill = f"{self.last_page_fill * 100:.0f}%" if self.last_page_fill else "unknown"
        pages = self.pages or "unknown"
        return (f"CV layout '{self.style}' ({self.tier} spacing, {self.source}): "
                f"{pages} page(s), last page {fill} full"
                + (", photo included" if self.photo else "")
                + (f" [tried {', '.join(self.attempts)}]" if self.attempts else "")
                + (f" | {'; '.join(self.warnings)}" if self.warnings else ""))


def assess_pages(pages: int, last_page_fill: float, *,
                 preferred: int = PREFERRED_PAGES,
                 maximum: int = MAX_PAGES) -> tuple[str, ...]:
    """Turn a measured layout into the warnings a human should act on."""
    warnings: list[str] = []
    if pages <= 0:
        return ("page count could not be verified; open the PDF and check it",)
    if pages > maximum:
        warnings.append(
            f"{pages} pages: a junior CV should not exceed {maximum}. Reduce the "
            "number of projects or skills in the profile")
    if pages > preferred and 0.0 < last_page_fill < MIN_LAST_PAGE_FILL:
        warnings.append(
            f"page {pages} is only {last_page_fill * 100:.0f}% full, which reads "
            "as an accidental overflow rather than a second page")
    return tuple(warnings)


_PAGES_COUNT = re.compile(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", re.S)
_COUNT_PAGES = re.compile(rb"/Count\s+(\d+)[^>]*?/Type\s*/Pages\b", re.S)
_PAGE_OBJECT = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")


def pdf_page_count(path: Path | str) -> int:
    """Count pages in a finished PDF. Returns 0 when it cannot be determined.

    Deliberately dependency-free. The page tree root carries `/Count`, and both
    ReportLab and LibreOffice write it as a plain object, so a regex over the
    bytes is enough; counting `/Type /Page` objects is the fallback. A PDF that
    hides its page tree inside an object stream reports 0, which the caller
    surfaces as "could not be verified" rather than guessing.
    """
    target = Path(path)
    if not target.is_file():
        return 0
    try:
        data = target.read_bytes()
    except OSError:
        return 0
    counts = [int(match.group(1)) for match in _PAGES_COUNT.finditer(data)]
    counts += [int(match.group(1)) for match in _COUNT_PAGES.finditer(data)]
    if counts:
        return max(counts)
    objects = len(_PAGE_OBJECT.findall(data))
    return objects
