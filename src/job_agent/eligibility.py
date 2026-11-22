"""Deterministic eligibility pre-screen over the full advert text.

Runs between "fetch description" and "AI matching". Two jobs:

1. Save model budget by skipping only what the candidate has definitively ruled
   out — today that is exactly one condition: a role that requires current
   university enrolment.
2. Turn explicit hard conditions into *verification questions* that are handed
   to the matcher, so the model reports an unmet mandatory requirement instead
   of inventing one.

The guiding rule from `docs/CANDIDATE_EVIDENCE.md`: an absent statement is
unknown, never a proven failure. Nothing here infers work authorisation, a
security clearance or a degree requirement that the advert does not state. The
advert is untrusted data; these are regex observations about it, not commands.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

# --- enrolment: the one condition that blocks -----------------------------
#
# This used to match `werkstudent\w*` anywhere in the advert, which was wrong.
# German adverts routinely list "Erfahrung durch Praktika oder
# Werkstudententätigkeit" as *desirable prior experience*, so a bare mention
# blocked genuine Trainee and Praktikum roles. Verified against live BA
# adverts: 3 of 4 blocks were false positives.
#
# The check is now sentence-level and needs the enrolment term to be framed as
# a requirement *for this position*.

# Sentence/clause boundaries, including the bullet lists adverts are made of.
_CLAUSE_SPLIT = re.compile(r"(?:[.!?;\n\r]|\s[-–—•*]\s|\s{2,}-\s)+")

# Terms that describe being currently enrolled.
ENROLMENT_TERM = re.compile(
    r"immatrikulier\w*"
    r"|immatrikulation\w*"   # "Nachweis der Immatrikulation"
    r"|eingeschrieben\w*"
    r"|studienbescheinigung"
    r"|immatrikulationsbescheinigung"
    r"|laufendes studium"
    r"|currently enrol\w*"
    r"|proof of enrol\w*"
    r"|enrol(?:led|ment) (?:at|in) a (?:university|college)", re.I)

# "Werkstudent" used for THIS role rather than as past experience.
WERKSTUDENT_ROLE = re.compile(
    r"\bals\s+werkstudent\w*"
    r"|\bwerkstudent(?:in)?\s*[/(]\s*(?:in\b|[mwdfx]\b)"
    r"|\b(?:suchen|gesucht|stelle|position|einstieg|vakanz)\w*[^.\n]{0,40}\bwerkstudent\w*"
    r"|\bwerkstudent\w*[^.\n]{0,30}\b(?:gesucht|m/w/d|w/m/d)"
    r"|\bworking student\s*[/(]", re.I)

# In a job TITLE a bare "Werkstudent" is decisive: it names the role itself,
# so none of the sentence-level context rules below need to apply.
TITLE_ENROLMENT = re.compile(
    r"\bwerkstudent\w*|\bworking student\b|\bstudentische[rn]?\s+hilfskraft"
    r"|\bstudentenjob\b|\bhiwi\b", re.I)

# Wording that makes an enrolment term a requirement rather than a remark.
OBLIGATION = re.compile(
    r"\berforderlich\b|\bvoraussetzung\b|\bvorausgesetzt\b|\bsetzen? voraus\b"
    r"|\bzwingend\b|\bverpflichtend\b|\bben[öo]tig\w*\b|\bnachweis\w*\b"
    r"|\bmuss\b|\bm[üu]ssen\b|\bvorzulegen\b|\bbedingung\b"
    r"|\brequired\b|\bmust\b|\bmandatory\b|\bprerequisite\b", re.I)

# Wording that marks the enrolment term as PAST EXPERIENCE or an aside about a
# different opening. These are what produced the false positives.
EXPERIENCE_CONTEXT = re.compile(
    r"\berfahrung\w*\b|\bt[äa]tigkeit\w*\b|\bpraktika\b|\bbeispielsweise\b"
    r"|\bidealerweise\b|\bvon vorteil\b|\bw[üu]nschenswert\b|\banschluss\b"
    r"|\bbereits\b|\bvorerfahrung\w*\b|\bgesammelt\w*\b|\babsolviert\w*\b"
    r"|\bz\.?\s?b\.?\b|\bexperience\b|\bprevious\w*\b|\bnice to have\b", re.I)

# "Du bist an einer Hochschule immatrikuliert" — addressed to the applicant, so
# it states a condition even without an explicit obligation word.
APPLICANT_FRAMING = re.compile(
    r"\b(?:du bist|du studierst|sie sind|sie studieren|you are|you must be"
    r"|bist du|sind sie)\b", re.I)


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in _CLAUSE_SPLIT.split(text) if part.strip()]


def enrolment_requirement(description: str, *, title: str = "") -> str:
    """Return the clause that requires current enrolment, or "" if none does.

    A clause blocks only when it names enrolment *and* frames it as a condition
    for this role. A clause that mentions it as prior experience, or as an
    aside about a different opening, does not block.
    """
    if (TITLE_ENROLMENT.search(title) or WERKSTUDENT_ROLE.search(title)
            or ENROLMENT_TERM.search(title)):
        return f"title states a Werkstudent/enrolment role: {title.strip()[:120]}"
    for clause in _clauses(description):
        werkstudent_role = bool(WERKSTUDENT_ROLE.search(clause))
        enrolment_term = bool(ENROLMENT_TERM.search(clause))
        if not (werkstudent_role or enrolment_term):
            continue
        if EXPERIENCE_CONTEXT.search(clause) and not werkstudent_role:
            continue  # Prior experience, not a requirement.
        if werkstudent_role and EXPERIENCE_CONTEXT.search(clause):
            continue  # "Erfahrung durch Werkstudententätigkeit" and similar.
        if werkstudent_role or OBLIGATION.search(clause) or APPLICANT_FRAMING.search(clause):
            return clause[:200]
    return ""


# Explicit statements worth asking about. Each is a *question*, never a verdict.
VERIFICATION_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("german_above_b2", re.compile(
        r"\bdeutsch\w*\s*(?:kenntnisse\s*)?(?:auf\s*)?(?:niveau\s*)?[-\s]*c[12]\b"
        r"|\bc[12]\b[^.\n]{0,30}\bdeutsch"
        r"|\b(?:verhandlungssicher\w*|muttersprach\w*)\s+deutsch"
        r"|\bdeutsch\w*\s+(?:verhandlungssicher|auf\s+muttersprachlichem)", re.I),
     "Advert asks for German above B2; candidate evidence states B2."),
    ("senior_experience", re.compile(
        r"\b(?:mindestens|min\.?|at least|wenigstens)?\s*([5-9]|1\d)\s*\+?\s*"
        r"(?:jahre|jahren|years)\b[^.\n]{0,40}"
        r"(?:erfahrung|berufserfahrung|experience)", re.I),
     "Advert demands five or more years of professional experience."),
    ("commercial_experience", re.compile(
        r"\b(?:mehrj[äa]hrige|langj[äa]hrige)\s+(?:einschl[äa]gige\s+)?"
        r"(?:berufs)?erfahrung\b", re.I),
     "Advert demands several years of commercial experience; candidate has "
     "project and training experience only."),
    # German adverts transliterate umlauts inconsistently, so accept both forms.
    ("security_clearance", re.compile(
        r"\bsicherheits(?:ü|ue)berpr(?:ü|ue)fung\b|\bS(?:Ü|UE)G\b|\bgeheimschutz\b"
        r"|\bsecurity clearance\b|\bverfassungstreue\b", re.I),
     "Advert explicitly requires a security clearance; this is unverified."),
    ("citizenship_or_permit", re.compile(
        r"\b(?:eu[- ]?(?:staatsb[üu]rgerschaft|citizenship)"
        r"|deutsche staatsangeh[öo]rigkeit"
        r"|work permit required|arbeitserlaubnis erforderlich)\b", re.I),
     "Advert explicitly states a citizenship or permit condition; verify manually."),
    ("driving_licence", re.compile(
        r"\bf(?:ü|ue|u)hrerschein\b|\bdriv(?:er'?s|ing) licen[cs]e\b", re.I),
     "Advert requires a driving licence; not recorded in the candidate profile."),
    ("degree_mandatory", re.compile(
        r"\b(?:abgeschlossenes?\s+(?:hochschul|universit[äa]ts)?studium"
        r"|bachelor|master|diplom)\b[^.\n]{0,60}\b(?:erforderlich|vorausgesetzt|required)\b"
        r"|\b(?:erforderlich|vorausgesetzt)\b[^.\n]{0,40}\babgeschlossenes?\s+studium", re.I),
     "Advert requires a completed degree; ZAB comparability is documented but "
     "the exact German degree title is unverified."),
)


@dataclass(frozen=True)
class Prescreen:
    """Outcome of the deterministic pass over one advert."""

    eligible: bool
    blockers: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    codes: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        if self.blockers:
            return "blocked: " + "; ".join(self.blockers)
        if self.questions:
            return f"{len(self.questions)} verification question(s)"
        return "no explicit hard condition found"


@dataclass
class PrescreenStats:
    examined: int = 0
    blocked: int = 0
    flagged: int = 0
    codes: dict[str, int] = field(default_factory=dict)

    def record(self, result: Prescreen) -> None:
        self.examined += 1
        if not result.eligible:
            self.blocked += 1
        elif result.questions:
            self.flagged += 1
        for code in result.codes:
            self.codes[code] = self.codes.get(code, 0) + 1


def prescreen_description(description: str, *, title: str = "") -> Prescreen:
    """Classify explicit hard conditions in an advert.

    Returns `eligible=False` only for a mandatory current-enrolment
    requirement, which the candidate has ruled out. Every other finding is a
    question attached to the posting, so a role is never dropped on a guess.
    """
    text = str(description or "")
    if len(text.strip()) < 100:
        # Too little text to screen on; let the caller decide, do not reject.
        return Prescreen(eligible=True, questions=("Advert text too short to pre-screen.",),
                         codes=("insufficient_text",))
    evidence = enrolment_requirement(text, title=title)
    if evidence:
        return Prescreen(
            eligible=False,
            blockers=("Requires current university enrolment (Werkstudent / "
                      f"Immatrikulationsnachweis); candidate is not enrolled. "
                      f"Stated as: \"{evidence}\"",),
            codes=("enrolment_required",),
        )
    questions: list[str] = []
    codes: list[str] = []
    for code, pattern, question in VERIFICATION_PATTERNS:
        if pattern.search(text):
            questions.append(question)
            codes.append(code)
    return Prescreen(eligible=True, questions=tuple(questions), codes=tuple(codes))


def verification_notes(results: Sequence[Prescreen]) -> list[str]:
    """Flatten questions across postings, de-duplicated, for report output."""
    seen: list[str] = []
    for result in results:
        for question in result.questions:
            if question not in seen:
                seen.append(question)
    return seen
