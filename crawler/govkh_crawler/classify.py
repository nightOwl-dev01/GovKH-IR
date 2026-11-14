"""Rule-based metadata: language, document type, document number, dates, exclusions.

These are collection-time heuristics for provenance and corpus accounting. Every value records
where it came from, and the indexing stage may refine it (proposal section 8.6).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date

KHMER_DIGITS = str.maketrans("០១២៣៤៥៦៧៨៩", "0123456789")
ZW = re.compile("[​‌‍﻿]")


def norm(text: str) -> str:
    """NFC, no zero-width characters, Khmer digits as ASCII, one spelling of the ្ត/្ដ subscript."""
    text = unicodedata.normalize("NFC", text or "")
    return ZW.sub("", text).translate(KHMER_DIGITS).replace("្ដ", "្ត")


# -- language -----------------------------------------------------------
def detect_language(text: str, min_letters: int = 20) -> str:
    """km | en | mixed | other | '' (no letters to judge by)."""
    km = lat = other = 0
    for ch in text[:20000]:
        o = ord(ch)
        if 0x1780 <= o <= 0x17FF or 0x19E0 <= o <= 0x19FF:
            km += 1
        elif ch.isascii() and ch.isalpha():
            lat += 1
        elif 0x0E00 <= o <= 0x0EFF or 0x1000 <= o <= 0x109F or 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF:
            other += 1
    total = km + lat + other
    if total < min_letters:
        return ""
    if other / total > 0.5:
        return "other"
    share = km / (km + lat) if km + lat else 0
    return "km" if share >= 0.6 else "en" if share <= 0.1 else "mixed"


# -- document type ------------------------------------------------------
# Ordered: the first match wins. "សេចក្តីប្រកាសព័ត៌មាន" (press release) must be tested before
# "ប្រកាស" (prakas), and the specific instruments before the generic words.
_RULES: list[tuple[str, str, str]] = [
    ("press_release", "announcement", r"សេចក្តីប្រកាសព័ត៌មាន|សេចក្តីថ្លែងការណ៍|press[ -]?release|press statement|joint statement|communiqu"),
    ("notification", "announcement", r"សេចក្តីជូនដំណឹង|សេចក្តីជូនព័ត៌មាន|លិខិតជូនដំណឹង|ជូនដំណឹង|\bnotification\b|\bannouncement\b|\bnotice\b"),
    ("royal_gazette", "legal_instrument", r"រាជកិច្ច|royal gazette"),
    ("constitution", "legal_instrument", r"រដ្ឋធម្មនុញ្ញ|\bconstitution"),
    ("royal_decree", "legal_instrument", r"ព្រះរាជក្រឹត្យ|royal[ _-]decree"),
    ("law", "legal_instrument", r"ព្រះរាជក្រម|ច្បាប់ស្តីពី|ច្បាប់|\bkram\b|\blaws?\b|\bact\b"),
    ("sub_decree", "legal_instrument", r"អនុក្រឹត្យ|sub[ _-]?decree|anukret"),
    ("prakas", "legal_instrument", r"ប្រកាស|\bprakas\b|\bproclamation"),
    ("circular", "legal_instrument", r"សារាចរ|\bcircular"),
    ("decision", "legal_instrument", r"សេចក្តីសម្រេច|\bdecision"),
    ("instruction", "legal_instrument", r"សេចក្តីណែនាំ|\binstruction|\bguidelines?\b"),
    ("order", "legal_instrument", r"បទបញ្ជា|\bregulation|\border\b"),
    ("form", "form_fee", r"ពាក្យស្នើសុំ|ពាក្យសុំ|បែបបទ|ទម្រង់|គំរូ|\bforms?\b|\btemplate"),
    ("fee_schedule", "form_fee", r"តារាងតម្លៃ|តារាងថ្លៃ|កម្រៃសេវា|ថ្លៃសេវា|fee schedule|service fees?|\btariff"),
    ("procedure", "procedure", r"នីតិវិធី|សេវាសាធារណៈ|របៀប|លក្ខខណ្ឌ|\bprocedures?\b|how to|\brequirements?\b|public[ -]services?|\bfaq"),
    ("report", "report", r"របាយការណ៍|ព្រឹត្តិបត្រ|ស្ថិតិ|ថវិកា|ជំរឿន|\breports?\b|\bbulletin|\bstatistic|\bsurvey|\bbudget|\bcensus|\bstrategic plan|ផែនការយុទ្ធសាស្ត្រ"),
]
RULES = [(sub, fam, re.compile(rx, re.I)) for sub, fam, rx in _RULES]
FAMILY_OF = {sub: fam for sub, fam, _ in _RULES}


def classify_doc_type(*texts: str) -> tuple[str, str] | None:
    """(family, subtype) from the first text that matches a rule, e.g. title, then URL."""
    for text in texts:
        t = norm(text)
        if not t:
            continue
        for sub, fam, rx in RULES:
            if rx.search(t):
                return fam, sub
    return None


# -- document number ----------------------------------------------------
_NUM_KM = re.compile(r"លេខ\s*[:៖.]?\s*(\d{1,5}(?:[/\-.]\d{1,4})*(?:\s*[^\s,;:()\d]{1,24}[./][^\s,;:()]{1,24})?)")
_NUM_EN = re.compile(r"\bN(?:o|º|°)\.?\s*:?\s*(\d{1,5}(?:[/\-.][\w.]{1,12})*(?:\s+[A-Za-z]{1,10}(?:[./][A-Za-z]{1,10})+\.?)?)", re.I)


# Instrument codes written without "No.", e.g. នស/រកម/១២២៣/០២២, NS/RKM/1094/006, អនក្រ.បក/0123
_NUM_CODE = re.compile(r"(?<![\w/])((?:[^\W\d_]{1,8}\.?){1,3}/[^\W\d_.]{1,8}(?:\.[^\W\d_]{1,8})?/\d{3,4}/\d{2,4})(?![\w/])")


def extract_doc_number(text: str) -> str:
    t = norm(text)
    for rx in (_NUM_KM, _NUM_EN, _NUM_CODE):
        m = rx.search(t)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip(" .")
    return ""


# -- dates --------------------------------------------------------------
KM_MONTHS = {
    "មករា": 1, "កុម្ភៈ": 2, "កុម្ភះ": 2, "កុម្ភ": 2, "មីនា": 3, "មិនា": 3, "មេសា": 4, "ឧសភា": 5, "មិថុនា": 6,
    "កក្កដា": 7, "សីហា": 8, "កញ្ញា": 9, "តុលា": 10, "វិច្ឆិកា": 11, "ធ្នូ": 12,
}
EN_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_KM_DATE = re.compile(r"(\d{1,2})\s*(?:ខែ\s*)?(" + "|".join(sorted(KM_MONTHS, key=len, reverse=True)) + r")\s*(?:ឆ្នាំ\s*)?(\d{4})")
_EN_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_EN_MDY = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")
_NUMERIC = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b")


def _mk(y: int, m: int, d: int) -> str:
    try:
        if not 1900 <= y <= date.today().year + 1:
            return ""
        return date(y, m, d).isoformat()
    except ValueError:
        return ""


def extract_date(text: str) -> str:
    """First full date in the text as ISO, Khmer or English; '' if none."""
    t = norm(text)
    m = _KM_DATE.search(t)
    if m:
        out = _mk(int(m.group(3)), KM_MONTHS[m.group(2)], int(m.group(1)))
        if out:
            return out
    m = _ISO.search(t)
    if m:
        out = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if out:
            return out
    m = _EN_DMY.search(t)
    if m and m.group(2)[:3].lower() in EN_MONTHS:
        out = _mk(int(m.group(3)), EN_MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
        if out:
            return out
    m = _EN_MDY.search(t)
    if m and m.group(1)[:3].lower() in EN_MONTHS:
        out = _mk(int(m.group(3)), EN_MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
        if out:
            return out
    m = _NUMERIC.search(t)  # Cambodian convention is day/month/year
    if m:
        return _mk(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return ""


def iso_date(value: str | None) -> str:
    """Normalise an API date/datetime string to YYYY-MM-DD, or ''."""
    m = _ISO.search(str(value or ""))
    return _mk(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else ""


# -- exclusions ---------------------------------------------------------
PERSONAL = re.compile(
    r"បញ្ជីឈ្មោះ|បញ្ជីរាយនាម|រាយនាម|លទ្ធផលប្រឡង|លទ្ធផលនៃការប្រឡង|បេក្ខជនជាប់|ជាប់ស្ថាពរ|បេក្ខជនដែល"
    r"|list of (?:successful |selected |short-?listed )?(?:candidates|applicants|students|trainees)"
    r"|name ?list|short-?list(?:ed)? candidates|exam(?:ination)? results?",
    re.I,
)


def looks_personal(title: str) -> bool:
    """Titles of documents whose main content is a list of named individuals (section 5.4)."""
    return bool(PERSONAL.search(norm(title)))


GENERIC_ANCHOR = re.compile(
    r"^(download|ទាញយក|ទាញយកឯកសារ|ឯកសារ|pdf|view|read more|អានបន្ត|click here|here|open|file|document|"
    r"ចុចទីនេះ|មើល|ទាញយក pdf|download pdf|english|khmer|ខ្មែរ|en|kh|km|\d+)$",
    re.I,
)


def good_title(text: str | None) -> str:
    t = re.sub(r"\s+", " ", ZW.sub("", text or "")).strip(" -–|:")
    if len(t) < 4 or GENERIC_ANCHOR.match(t):
        return ""
    return t[:500]
