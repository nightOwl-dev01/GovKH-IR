"""Khmer/English normalisation and the three tokenisers compared in RQ1 (proposal 8.3, 8.4).

The same functions are applied to documents and to queries.
"""
from __future__ import annotations

import re
import sys
import unicodedata

from . import config

sys.path.insert(0, str(config.PROJECT / "crawler"))
from govkh_crawler.classify import KHMER_DIGITS, detect_language  # noqa: E402,F401  (reused from collection)

ZWSP = "​"
_ZW_OTHER = re.compile("[‌‍﻿]")
_WS = re.compile(r"[ \t ]+")


def normalize(text: str, keep_zwsp: bool = False) -> str:
    """Search-text normalisation: NFC, Khmer digits to ASCII, one spelling of the TA/DA subscript.

    U+200B is a word-boundary hint in Khmer text. With keep_zwsp it survives as a space so the
    word segmenter can use it; otherwise it is removed.
    """
    text = unicodedata.normalize("NFC", text or "")
    text = _ZW_OTHER.sub("", text).replace(ZWSP, " " if keep_zwsp else "")
    return text.translate(KHMER_DIGITS).replace("្ដ", "្ត")


def display_clean(text: str) -> str:
    """Light clean-up of text kept for display: NFC, no zero-width characters, tidy spaces."""
    text = unicodedata.normalize("NFC", text or "")
    text = _ZW_OTHER.sub("", text).replace(ZWSP, "")
    text = _WS.sub(" ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# -- Khmer text quality --------------------------------------------------
_KM = re.compile(r"[ក-៓ៗៜ៝]")
# A dependent vowel or sign that does not follow a Khmer letter, or a COENG not followed by a consonant.
_BAD = re.compile(r"(?<![ក-៓៝])[ា-៑៓៝]|្(?![ក-អ])|្[ក-អ]្[ក-អ]្")
# Legacy (pre-Unicode) fonts extracted from PDFs give runs like "ក្ស" for every "ក"; real text rarely repeats them this much.
_LEGACY = re.compile(r"ក្ស|ែ្|្ស្ថ|បប|ពពេ|ព្ក")


def khmer_quality(text: str) -> float | None:
    """Share of the Khmer text that looks well formed (1.0 = clean). None when there is too little Khmer."""
    sample = text[:20000]
    n = len(_KM.findall(sample))
    if n < 200:
        return None
    bad = len(_BAD.findall(sample)) * 3 + len(_LEGACY.findall(sample)) * 2
    return max(0.0, 1.0 - bad / n)


# -- tokenisers ------------------------------------------------------------
_KM_RUN = r"[ក-៓ៗៜ៝]+"
# document numbers and dates stay whole: 123/អនក្រ.បក, នស/រកម/0196/18, 2024-05-01, 12.5
_REF = r"[0-9A-Za-zក-៓]+(?:[/.\-][0-9A-Za-zក-៓]+)+"
_TOKEN = re.compile(rf"({_REF})|({_KM_RUN})|([A-Za-z]+(?:'[a-z]+)?)|(\d+)")
_HAS_DIGIT = re.compile(r"\d")

EN_STOP = frozenset("a an the of to in on at for and or by is are was were be been it its this that with as from".split())
KM_STOP = frozenset("និង នៃ ដែល ជា បាន នៅ ក្នុង ដោយ ពី ទៅ នេះ នោះ ដើម្បី ឬ ក៏ ត្រូវ មាន គឺ ការ".split())

_stemmer = None
_segment = None


def _stem(word: str) -> str:
    global _stemmer
    if _stemmer is None:
        import Stemmer
        _stemmer = Stemmer.Stemmer("english")
    return _stemmer.stemWord(word)


def _km_words(run: str) -> list[str]:
    global _segment
    if _segment is None:
        import logging
        logging.getLogger("khmer-nltk").setLevel(logging.ERROR)
        from khmernltk import word_tokenize
        _segment = word_tokenize
    return [w for w in _segment(run, return_tokens=True) if w.strip()]


def _trigrams(run: str) -> list[str]:
    return [run] if len(run) <= 3 else [run[i:i + 3] for i in range(len(run) - 2)]


def tokenize(text: str, mode: str) -> list[str]:
    """mode: 'nosplit' (whitespace only), 'word' (khmer-nltk), 'trigram' (Khmer character 3-grams).

    English is lowercased and lightly stemmed in every mode; numbers and references are kept whole.
    A reference such as 123/អនក្រ.បក is indexed whole and also by its parts.
    """
    text = normalize(text, keep_zwsp=(mode == "word")).lower()
    out: list[str] = []
    for ref, km, en, num in _TOKEN.findall(text):
        if ref:
            if _HAS_DIGIT.search(ref):
                out.append(ref)
                for part in re.split(r"[/.\-]", ref):
                    if part:
                        out.append(part)
            else:  # e.g. "អ.ស.យ" or "e.g" without digits: treat the pieces as ordinary text
                for part in re.split(r"[/.\-]", ref):
                    out.extend(tokenize(part, mode))
        elif km:
            if mode == "nosplit":
                out.append(km)
            elif mode == "word":
                out.extend(w for w in _km_words(km) if w not in KM_STOP)
            else:
                out.extend(_trigrams(km))
        elif en:
            if en not in EN_STOP:
                out.append(_stem(en))
        elif num:
            out.append(num)
    return out


def query_language(q: str) -> str:
    return detect_language(q, min_letters=1) or "en"
