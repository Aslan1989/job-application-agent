"""Validate generated prose against the evidence allowlist.

The model writes the cover-letter body. This module decides whether that text
may be used. It is the last line of defence before a document carries the
candidate's name, so it fails closed: anything it cannot verify is a finding,
and a blocking finding means the text is discarded in favour of a
deterministic template.

What it looks for, in order of seriousness:

- **Fabricated employment.** The bank roles the user rejected, or any phrasing
  that turns a personal project into a job.
- **Invented experience duration.** "3 Jahre Erfahrung" is a claim the profile
  cannot support in software, whatever the number.
- **Invented credentials.** A certification or degree the profile does not list.
- **Confidentiality leaks.** A private repository name, or a CV filename the
  user excluded.
- **Unevidenced technology.** A technology named in the letter that is not in
  the evidence selection for this vacancy.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .evidence import EvidenceSelection

# Employers the user confirmed are incorrect. Hard-coded as a backstop: even if
# the profile were edited, these must never appear in an application.
FORBIDDEN_EMPLOYERS = ("tinkoff", "sberbank")

# Phrases that assert commercial software employment the candidate does not have.
EMPLOYMENT_CLAIMS = re.compile(
    r"\b(?:als|as a)\s+(?:software\w*|backend|frontend|fullstack|web)"
    r"[- ]?(?:entwickler\w*|developer|engineer)\s+"
    r"(?:bei|at|f[üu]r|for)\b"
    r"|\b(?:angestellt|employed|besch[äa]ftigt)\s+(?:als|as)\s+"
    r"(?:software\w*|entwickler\w*|developer|engineer)\b"
    r"|\bmeine\s+(?:bisherige\s+)?t[äa]tigkeit\s+als\s+(?:software\w*|entwickler\w*)"
    r"|\bin\s+meiner\s+(?:rolle|position)\s+als\s+(?:software\w*|entwickler\w*)",
    re.I)

# Any claimed duration of professional/development experience.
DURATION_CLAIMS = re.compile(
    r"\b(?:\d+|ein|eine|zwei|drei|vier|f[üu]nf|sechs|sieben|acht|neun|zehn|"
    r"one|two|three|four|five|six|seven|eight|nine|ten)\s*"
    r"(?:\+\s*)?(?:jahr\w*|years?|yrs?)\b[^.]{0,40}"
    r"(?:erfahrung|berufserfahrung|experience|entwicklung|development|"
    r"programmier\w*|programming)"
    r"|(?:erfahrung|berufserfahrung|experience)\b[^.]{0,20}"
    r"\b(?:\d+|zwei|drei|vier|f[üu]nf|two|three|four|five)\s*(?:jahr\w*|years?)\b",
    re.I)

# Credential words. Allowed only when the profile actually lists the item.
CREDENTIAL_CLAIMS = re.compile(
    r"\b(zertifizier\w*|zertifikat\w*|certified|certification|certificate|"
    r"ausbildung als|staatlich gepr[üu]ft\w*|bachelor of|master of|"
    r"aws certified|oracle certified|scrum master|itil|cisco|ccna|comptia)\b",
    re.I)

# Hollow phrasing. Advisory only: it lowers quality, not truthfulness.
# Written as patterns because German adverts and generated text mix real
# umlauts with their "oe"/"ae"/"ue" transliterations, and a plain substring
# list silently missed the transliterated half.
# The umlaut itself must be in the alternation. `re.I` does not fold "ä" to
# "a", and these patterns are matched against the raw body (not the folded
# copy), so without it every pattern spelled with `_A`/`_O`/`_U` matched only
# the transliterated half -- "selbstverstaendlich" was caught and
# "selbstverständlich" was not.
_U = "(?:ue|\u00fc|u)"
_O = "(?:oe|\u00f6|o)"
_A = "(?:ae|\u00e4|a)"
GENERIC_PATTERNS: tuple[re.Pattern[str], ...] = tuple(re.compile(pattern, re.I) for pattern in (
    r"als ki[- ]modell", r"\bas an ai\b",
    rf"begeistert von der m{_O}glichkeit",
    rf"{_U}berzeugt, dass ich (?:die|der) perfekt",
    r"\b(?:der|die|ein|eine)? ?perfekt\w* kandidat\w*",
    r"\bideale[rn]? kandidat\w*", r"i am the perfect candidate",
    r"passion for excellence", r"dynamisch\w* und innovativ\w* umfeld",
    rf"leidenschaft f{_U}r technologie",
    rf"selbstverst{_A}ndlich bringe ich",
    r"hiermit bewerbe ich mich auf die",
    r"i was thrilled to see", r"leverage my expertise",
    r"proven track record", r"umfangreich\w* berufserfahrung",
    rf"langj{_A}hrig\w* erfahrung",
))

# Defensive self-deprecation. Truthful, but it argues against the applicant in
# their own document: a reader does not need to be told what is missing, and
# the CV already shows where the practical experience comes from. Warned on,
# never blocked, because the statement itself is not false.
DEFENSIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.I) for pattern in (
        rf"keine kommerzielle t{_A}tigkeit",
        rf"keine (?:einschl{_A}gige |relevante )?berufserfahrung als",
        r"no commercial (?:employment|experience) as a",
        r"als entwickler (?:war|bin) ich (?:bisher )?nicht",
        r"(?:ich )?habe (?:bisher )?(?:noch )?nicht als \w*entwickler",
        r"i have not held a position as a",
        r"leider (?:fehlt|habe ich)",
        rf"mir fehlt(?: die| der| das)? \w*erfahrung",
    ))

# An explicit technology vocabulary. Guessing from capitalisation was both too
# narrow and too noisy: it flagged "TensorFlow" but missed "Kubernetes", and
# tripped over ordinary capitalised German nouns. A closed list of things an
# advert might name is precise, and anything outside it is simply not checked
# here -- the structured CV path checks claims separately.
KNOWN_TECHNOLOGIES: frozenset[str] = frozenset({
    "php", "symfony", "laravel", "typo3", "wordpress", "composer", "pdo",
    "phpunit", "php-fpm", "java", "spring", "springboot", "jakarta", "jpa",
    "hibernate", "maven", "gradle", "kotlin", "scala", "junit", "mockmvc",
    "python", "django", "flask", "fastapi", "pandas", "numpy", "pytorch",
    "tensorflow", "keras", "scikit-learn", "sklearn", "langchain", "llamaindex",
    "ollama", "openai", "huggingface", "transformers", "rag", "embeddings",
    "c", "c++", "cpp", "rust", "go", "golang", "csharp", "dotnet", "swift",
    "javascript", "typescript", "react", "angular", "vue", "node", "nodejs",
    "nextjs", "svelte", "jquery", "bootstrap", "tailwind",
    "mysql", "mariadb", "postgresql", "postgres", "sqlite", "oracle",
    "mongodb", "redis", "elasticsearch", "cassandra", "snowflake", "dbt",
    "docker", "kubernetes", "k8s", "openshift", "helm", "terraform",
    "ansible", "puppet", "chef", "vagrant", "jenkins", "gitlab", "github",
    "nginx", "apache", "tomcat", "haproxy", "linux", "ubuntu", "debian",
    "centos", "redhat", "windows", "macos", "bash", "zsh", "powershell",
    "git", "svn", "jira", "confluence", "aws", "azure", "gcp", "heroku",
    "kafka", "rabbitmq", "graphql", "grpc", "soap", "oauth", "jwt", "saml",
    "sap", "abap", "hana", "fiori", "s4hana", "salesforce", "servicenow",
    "spark", "hadoop", "airflow", "tableau", "powerbi", "looker",
    "prometheus", "grafana", "datadog", "splunk", "selenium", "cypress",
    "pytest", "jest", "scrum", "kanban", "safe", "prince2",
})

# Technologies and proper nouns that need no evidence: they are the employer's
# words, the language of the advert, or plainly generic.
_NEUTRAL_TOKENS = frozenset({
    "ich", "sie", "ihr", "ihre", "ihrem", "ihren", "wir", "mein", "meine",
    "meinen", "meiner", "der", "die", "das", "und", "oder", "mit", "bei",
    "für", "fuer", "als", "auf", "von", "zu", "im", "in", "an", "am", "ein",
    "eine", "einem", "einen", "dem", "den", "des", "sehr", "geehrte",
    "geehrter", "damen", "herren", "freundlichen", "grüßen", "gruessen",
    "mit", "dear", "sir", "madam", "sincerely", "yours", "regards", "the",
    "and", "with", "for", "your", "this", "that", "team", "position", "role",
    "stelle", "aufgaben", "projekt", "projekte", "projekten", "praktikum",
    "trainee", "junior", "github", "gmbh", "ag", "kg", "se", "co", "ohg",
    "deutsch", "english", "englisch", "russisch", "b2", "c1", "a1", "a2",
    "it", "eu", "api", "apis", "rest", "web", "backend", "frontend", "cloud",
    "ci", "cd", "oop", "mvc", "crud", "json", "xml", "html", "css", "http",
    "https", "tcp", "ip", "sql", "unix", "os", "pc", "qa", "ui", "ux", "hr",
    "zab", "wirtschaftsinformatik", "wirtschaftsinformatiker", "informatik",
    "hochschule", "universität", "universitaet", "bachelor", "diplom",
    "lebenslauf", "anschreiben", "bewerbung", "referenz", "referenzen",
    "montag", "januar", "februar", "märz", "april", "mai", "juni", "juli",
    "august", "september", "oktober", "november", "dezember",
})

SEVERITY_BLOCKING = "blocking"
SEVERITY_WARNING = "warning"
SEVERITY_NOTE = "note"


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(ch for ch in text if not unicodedata.combining(ch))


@dataclass(frozen=True)
class Finding:
    """One problem with generated text."""

    code: str
    severity: str
    detail: str
    excerpt: str = ""

    def __str__(self) -> str:
        tail = f": \"{self.excerpt}\"" if self.excerpt else ""
        return f"[{self.severity}] {self.code} - {self.detail}{tail}"


@dataclass
class FactCheckResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [item for item in self.findings if item.severity == SEVERITY_BLOCKING]

    @property
    def ok(self) -> bool:
        """True when the text may be used as generated."""
        return not self.blocking

    @property
    def warnings(self) -> list[Finding]:
        return [item for item in self.findings if item.severity == SEVERITY_WARNING]

    def add(self, code: str, severity: str, detail: str, excerpt: str = "") -> None:
        self.findings.append(Finding(code, severity, detail, excerpt[:160]))

    def summary(self) -> str:
        if not self.findings:
            return "no findings"
        return (f"{len(self.blocking)} blocking, {len(self.warnings)} warning, "
                f"{len(self.findings) - len(self.blocking) - len(self.warnings)} note")


def _excerpt(text: str, match: re.Match[str]) -> str:
    start = max(0, match.start() - 50)
    return " ".join(text[start:match.end() + 50].split())


def allowed_credentials(evidence: EvidenceSelection) -> frozenset[str]:
    """Credential words the profile genuinely supports."""
    allowed: set[str] = set()
    for item in evidence.education:
        for word in re.findall(r"[A-Za-zÄÖÜäöüß]{4,}", item.title):
            allowed.add(_fold(word))
    return frozenset(allowed)


def check_text(text: str, evidence: EvidenceSelection, *,
               company: str = "", role: str = "",
               private_repositories: Sequence[str] = (),
               excluded_files: Sequence[str] = (),
               strict_technology: bool = True) -> FactCheckResult:
    """Check one generated passage against the evidence it may draw on."""
    result = FactCheckResult()
    body = str(text or "")
    folded = _fold(body)

    for employer in FORBIDDEN_EMPLOYERS:
        if employer in folded:
            result.add("fabricated_employer", SEVERITY_BLOCKING,
                       f"names '{employer}', which the user confirmed is incorrect")

    match = EMPLOYMENT_CLAIMS.search(body)
    if match:
        result.add("claimed_software_employment", SEVERITY_BLOCKING,
                   "implies employment as a software developer, which the "
                   "profile does not support", _excerpt(body, match))

    match = DURATION_CLAIMS.search(body)
    if match:
        result.add("claimed_experience_duration", SEVERITY_BLOCKING,
                   "states a duration of professional experience, which the "
                   "profile cannot support", _excerpt(body, match))

    permitted = allowed_credentials(evidence)
    for match in CREDENTIAL_CLAIMS.finditer(body):
        word = _fold(match.group(0))
        if not any(word.startswith(item[:6]) or item.startswith(word[:6])
                   for item in permitted):
            result.add("invented_credential", SEVERITY_BLOCKING,
                       f"claims a credential ('{match.group(0)}') the profile "
                       "does not list", _excerpt(body, match))

    for repository in private_repositories:
        name = _fold(str(repository).split("/")[-1])
        if name and name in folded:
            result.add("private_repository_disclosed", SEVERITY_BLOCKING,
                       f"mentions the private repository '{repository}'")

    for filename in excluded_files:
        stem = _fold(str(filename).rsplit(".", 1)[0])
        if stem and stem in folded:
            result.add("excluded_cv_referenced", SEVERITY_BLOCKING,
                       f"references the excluded file '{filename}'")

    if company and _fold(company) not in folded:
        result.add("employer_missing", SEVERITY_WARNING,
                   f"does not name the employer '{company}'")
    if role:
        role_words = [word for word in re.findall(r"[A-Za-zÄÖÜäöüß]{4,}", role)
                      if _fold(word) not in _NEUTRAL_TOKENS]
        if role_words and not any(_fold(word) in folded for word in role_words):
            result.add("role_missing", SEVERITY_WARNING,
                       f"does not name the role '{role}'")

    for pattern in GENERIC_PATTERNS:
        hit = pattern.search(body)
        if hit:
            result.add("generic_phrasing", SEVERITY_WARNING,
                       "contains hollow or exaggerated phrasing",
                       _excerpt(body, hit))

    for pattern in DEFENSIVE_PATTERNS:
        hit = pattern.search(body)
        if hit:
            result.add("defensive_phrasing", SEVERITY_WARNING,
                       "argues against the applicant instead of stating where "
                       "the experience comes from", _excerpt(body, hit))
            break

    if strict_technology:
        allowed = set(evidence.allowed_terms) | set(_NEUTRAL_TOKENS)
        allowed |= {_fold(word) for word in re.findall(r"[A-Za-z0-9+.#-]{2,}",
                                                       f"{company} {role}")}
        for requirement in evidence.matched_requirements + evidence.unmet_requirements:
            allowed |= {_fold(word) for word in re.findall(r"[A-Za-z0-9+.#-]{2,}",
                                                            requirement)}
        unknown: list[str] = []
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+.#_-]{1,}", body):
            folded_token = _fold(token).strip(".-_")
            if folded_token not in KNOWN_TECHNOLOGIES:
                continue
            if folded_token in allowed or folded_token in unknown:
                continue
            # The evidence may spell it inside a compound ("Java 17 / Spring
            # Boot"), so check containment against the allowlist too.
            if any(folded_token in term for term in allowed):
                continue
            unknown.append(folded_token)
        for token in unknown[:10]:
            result.add("unevidenced_technology", SEVERITY_WARNING,
                       f"mentions '{token}', which is not in the evidence for "
                       "this vacancy")

    return result


# The separator the CV uses inside a compact skills row.
SKILL_ROW_SEPARATOR = "\u00b7"


def skill_atoms(evidence: EvidenceSelection) -> frozenset[str]:
    """Every technology a profile skill bundle names, as its own token.

    The profile records bundles ("Java 17 / Spring Boot / JPA / Security") and
    the CV prints a category row of individual technologies, so the checker
    needs the pieces. These are matched by **exact equality only** and are
    deliberately kept out of `allowed_points`: a one-character atom like "c"
    inside a containment test would match nearly any sentence and quietly
    disable the whole check.
    """
    atoms: set[str] = set()
    for skill in evidence.skills:
        # Both spellings: the profile's English record and its `name_de`, which
        # is what a German CV prints. Neither widens what may be claimed -- the
        # two name the same evidenced technologies.
        for name in (skill.name, getattr(skill, "name_de", "")):
            for fragment in str(name or "").split("/"):
                folded = _fold(fragment).strip()
                if folded:
                    atoms.add(folded)
    return frozenset(atoms)


def check_claims(claims: Iterable[str], evidence: EvidenceSelection) -> FactCheckResult:
    """Check structured CV bullet points, which must come from evidence verbatim."""
    result = FactCheckResult()
    atoms = skill_atoms(evidence)
    allowed_points = {_fold(point) for project in evidence.projects
                      for point in project.points}
    # The German renderings in the profile (`evidence_points_de`,
    # `background_de`) are profile entries exactly as the English ones are. A
    # German CV is assembled from them, so they belong in the allowlist --
    # without this, every line of a fully German CV reads as unverified.
    allowed_points |= {_fold(point) for project in evidence.projects
                       for point in project.points_de}
    allowed_points |= {_fold(skill.name) for skill in evidence.skills}
    allowed_points |= {_fold(item.summary) for item in evidence.employment}
    allowed_points |= {_fold(item.summary_de) for item in evidence.employment
                       if item.summary_de}
    # A CV also lists languages, availability and education. These come from
    # the profile just as projects do and must not read as unverified claims.
    allowed_points |= {_fold(item) for item in evidence.languages}
    allowed_points |= {_fold(evidence.availability)}
    for item in evidence.education:
        allowed_points |= {_fold(item.title), _fold(item.institution),
                           _fold(item.dates)}
        # A CV-displayable education note, such as the ZAB recognition. The
        # internal `note` is deliberately NOT allowed: it is an instruction to
        # the generator, not text that may appear in a document.
        allowed_points |= {_fold(getattr(item, "title_de", "")),
                           _fold(getattr(item, "cv_note_de", "")),
                           _fold(getattr(item, "cv_note_en", ""))}
    allowed_points.discard("")
    for claim in claims:
        folded = _fold(claim)
        if not folded:
            continue
        if any(folded in allowed or allowed in folded for allowed in allowed_points):
            continue
        # A skills row: every technology in it must be one the profile names,
        # matched exactly. One unknown item fails the whole row.
        items = [_fold(part).strip() for part in
                 str(claim).split(SKILL_ROW_SEPARATOR)]
        items = [item for item in items if item]
        if items and all(item in atoms for item in items):
            continue
        result.add("unverified_cv_claim", SEVERITY_BLOCKING,
                   "CV line does not correspond to any profile entry", claim)
    return result
