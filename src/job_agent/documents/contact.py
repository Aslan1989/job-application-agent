"""Local-only contact details and the Bewerbungsfoto.

`data/candidate_profile.json` records `contact_details_stored: false`, so the
profile deliberately holds no address, phone number or email. They live here
instead, in a separate git-ignored file, for one reason: this module is never
imported by the prompt builder, so a contact detail cannot reach the model
even by accident.

The application photo belongs to the same class of data and therefore lives
here too, as `ContactDetails.photo_path`. That placement is the privacy
guarantee: `content.py` builds every model prompt and does not import this
module, so neither a phone number nor a photo path can be interpolated into a
prompt. The image bytes are read only inside the renderers.

The photo is never generated, cropped or retouched. It is scaled to fit a box
with its aspect ratio preserved, and nothing else is done to it.

Without the files, documents render with the name alone and the review report
lists what is missing. Nothing is ever guessed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONTACT_PATH = Path("data/contact.json")
EXAMPLE_PATH = Path("data/contact.example.json")

# The Bewerbungsfoto. JPEG first because that is what the user supplies; the
# other suffixes are accepted so a PNG export is not silently ignored.
DEFAULT_PHOTO_PATH = Path("data/profile_photo.jpg")
PHOTO_CANDIDATES: tuple[Path, ...] = (
    DEFAULT_PHOTO_PATH,
    Path("data/profile_photo.jpeg"),
    Path("data/profile_photo.png"),
)
# A German Bewerbungsfoto is portrait. A landscape image is still used, but it
# is reported, because it will sit in the header with whitespace beside it
# rather than being cropped.
_MIN_PHOTO_PIXELS = 120

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


@dataclass(frozen=True)
class ContactDetails:
    """What a letterhead needs. Every field is optional and never invented."""

    full_name: str = ""
    email: str = ""
    phone: str = ""
    street: str = ""
    postal_code: str = ""
    city: str = ""
    country: str = ""
    github_url: str = ""
    linkedin_url: str = ""
    missing: tuple[str, ...] = field(default_factory=tuple)
    photo_path: str = ""
    photo_note: str = ""

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def has_photo(self) -> bool:
        return bool(self.photo_path) and Path(self.photo_path).is_file()

    @property
    def address_line(self) -> str:
        parts = [self.street, " ".join(p for p in (self.postal_code, self.city) if p)]
        return ", ".join(part for part in parts if part.strip())

    def header_lines(self) -> list[str]:
        """Contact block for the top of a document, omitting empty fields."""
        lines = [line for line in (self.address_line, self.phone, self.email,
                                   self.github_url, self.linkedin_url) if line]
        return lines

    def verification_notes(self) -> list[str]:
        """Problems with the contact data itself.

        The photo note is deliberately not here: a complete contact record
        with no photo is complete. The pipeline surfaces `photo_note` as its
        own line so an optional photo never reads as missing contact data.
        """
        if not self.missing:
            return []
        return [f"Contact details missing ({', '.join(self.missing)}): add them to "
                f"{DEFAULT_CONTACT_PATH} before sending. They are never sent to the model."]


def inspect_photo(path: Path | str) -> tuple[str, str]:
    """Validate an image file for use as a Bewerbungsfoto.

    Returns `(usable_path, note)`. The note is empty when the image is a clean
    portrait. The file is only read and measured here; it is never rewritten.
    """
    target = Path(path)
    if not target.is_file():
        return "", ""
    try:
        from PIL import Image  # Pillow ships with reportlab.
    except ImportError:  # pragma: no cover - Pillow is a reportlab dependency
        return str(target), (f"Photo {target} was not checked (Pillow missing); "
                             "open the PDF and confirm it looks right.")
    try:
        with Image.open(target) as image:
            image.verify()
        with Image.open(target) as image:
            width, height = image.size
            fmt = (image.format or "").upper()
    except Exception as exc:  # noqa: BLE001 - any unreadable image is reported
        return "", (f"Photo {target} could not be read as an image "
                    f"({type(exc).__name__}); the CV was rendered without it.")
    if width < _MIN_PHOTO_PIXELS or height < _MIN_PHOTO_PIXELS:
        return "", (f"Photo {target} is only {width}x{height} pixels, too small to "
                    "print cleanly; the CV was rendered without it.")
    if fmt not in {"JPEG", "PNG"}:
        return "", (f"Photo {target} is a {fmt or 'unknown'} file; supply a JPEG or "
                    "PNG. The CV was rendered without it.")
    if height < width:
        return str(target), (
            f"Photo {target} is landscape ({width}x{height}). It is placed "
            "uncropped, so there will be space beside it. A portrait photo of "
            "about 3:4 fills the header box properly.")
    return str(target), ""


def find_photo(path: Path | str | None = None, *, enabled: bool = True) -> tuple[str, str]:
    """Locate the local Bewerbungsfoto. Never creates or edits an image."""
    if not enabled:
        return "", ""
    if path is not None:
        target = Path(path)
        if not target.is_file():
            return "", (f"No photo at {target}; the CV was rendered without one.")
        return inspect_photo(target)
    for candidate in PHOTO_CANDIDATES:
        if candidate.is_file():
            return inspect_photo(candidate)
    return "", (f"No Bewerbungsfoto found. Save a portrait photo as "
                f"{DEFAULT_PHOTO_PATH} for the 'modern' CV; it stays local, is "
                "git-ignored and is never sent to a model.")


def write_example(path: Path = EXAMPLE_PATH) -> Path:
    """Create a template the user fills in. Contains no real data."""
    template = {
        "_note": ("Local only. Listed in .gitignore and never included in a model "
                  "prompt. Copy to data/contact.json and fill in your own details."),
        "full_name": "", "email": "", "phone": "",
        "street": "", "postal_code": "", "city": "", "country": "Deutschland",
        "github_url": "", "linkedin_url": "",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def load_contact(path: Path | str = DEFAULT_CONTACT_PATH, *,
                 fallback_name: str = "", github_url: str = "",
                 photo: bool = True,
                 photo_path: Path | str | None = None) -> ContactDetails:
    """Read contact details, reporting what is absent instead of inventing it."""
    raw: dict[str, Any] = {}
    location = Path(path)
    if location.is_file():
        try:
            loaded = json.loads(location.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except json.JSONDecodeError as exc:
            raise ValueError(f"{location} is not valid JSON: {exc}") from exc

    def value(name: str) -> str:
        found = raw.get(name)
        return str(found).strip() if isinstance(found, (str, int)) else ""

    email = value("email")
    if email and not _EMAIL.match(email):
        raise ValueError(f"{location}: 'email' is not a valid address")

    details = {
        "full_name": value("full_name") or str(fallback_name).strip(),
        "email": email,
        "phone": value("phone"),
        "street": value("street"),
        "postal_code": value("postal_code"),
        "city": value("city"),
        "country": value("country"),
        "github_url": value("github_url") or str(github_url).strip(),
        "linkedin_url": value("linkedin_url"),
    }
    # A German application is normally expected to carry these.
    required = ("full_name", "email", "phone", "city")
    missing = tuple(name for name in required if not details[name])
    found_photo, note = find_photo(photo_path or raw.get("photo_path") or None,
                                   enabled=photo)
    return ContactDetails(**details, missing=missing,
                          photo_path=found_photo, photo_note=note)
