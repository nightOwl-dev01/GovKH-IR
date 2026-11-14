"""CKAN catalogues (Open Development Cambodia law library): rich metadata, original files."""
from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import quote, urlencode

from ..classify import FAMILY_OF, classify_doc_type, iso_date
from ..urlnorm import DOC_EXT, canonicalize, extension
from .base import Adapter, Candidate, log

SUBTYPE = {
    "law": "law", "act": "law", "amendments": "law", "constitution": "constitution",
    "royal_decree": "royal_decree", "sub-decree": "sub_decree", "decree": "sub_decree",
    "proclamation": "prakas", "decision": "decision", "circular": "circular",
    "instruction": "instruction", "guideline": "instruction", "order": "order", "resolution": "decision",
    "notification": "notification", "letter": "notification", "declaration": "press_release",
    "joint_statement": "press_release",
}
REPORT_LIKE = {"policy", "strategic_plan", "action_plan", "strategy"}
STATUS = {
    "signed_and_in_effect": "in_force", "adopted___validated___in_effect": "in_force",
    "repealed": "repealed", "cancelled": "repealed", "expired": "repealed",
    "draft": "draft", "under_negotiation": "draft", "under_negotitation": "draft",
    "negotiations_launched": "draft", "signed_but_not_yet_in_effect_validated": "not_yet_in_force",
}
SMALL = {"of", "and", "the", "for", "in", "on", "to"}


def agency_name(slug: str) -> str:
    words = slug.replace("-", "_").split("_")
    return " ".join(w if w in SMALL and i else w.capitalize() for i, w in enumerate(words) if w)


def _tr(value: Any, lang: str) -> str:
    if isinstance(value, dict):
        return (value.get(lang) or value.get("en") or value.get("km") or "").strip()
    return str(value or "").strip()


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in (value or []) if str(v).strip()]


class CkanAdapter(Adapter):
    """cfg: api (…/api/3/action), fq (Solr filter), dataset_url (template with {name}), rows."""

    type = "ckan"

    def _search(self, start: int, rows: int, since: str | None) -> tuple[dict | None, str]:
        fq = self.cfg["fq"]
        if since:
            fq = f"({fq}) AND metadata_modified:[{since[:19]}Z TO *]"
        q = urlencode({"fq": fq, "rows": rows, "start": start, "sort": "metadata_modified asc"}, quote_via=quote)
        data, _, _, error = self.get_json(f"{self.cfg['api'].rstrip('/')}/package_search?{q}")
        if error or not isinstance(data, dict) or not data.get("success"):
            return None, error or "ckan_error"
        return data["result"], ""

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        key = "ckan:" + self.cfg["fq"]
        cur = self.db.get_cursor(self.src.id, key, {"since": None, "start": 0, "max_mod": None})
        since, start, max_mod = cur["since"], cur["start"], cur["max_mod"]
        rows, emitted = int(self.cfg.get("rows", 100)), 0
        while not self.client.stop.is_set():
            result, error = self._search(start, rows, since)
            if result is None:
                log.warning("[%s] CKAN search at offset %s failed: %s", self.src.id, start, error)
                return
            packages = result.get("results") or []
            for pkg in packages:
                mod = pkg.get("metadata_modified") or ""
                if mod and (not max_mod or mod > max_mod):
                    max_mod = mod
                for cand in self._candidates(pkg):
                    yield cand
                    emitted += 1
            start += len(packages)
            self.db.set_cursor(self.src.id, key, {"since": since, "start": start, "max_mod": max_mod})
            if limit and emitted >= limit:
                return
            if not packages or start >= int(result.get("count") or 0):
                break
        else:
            return
        self.db.set_cursor(self.src.id, key, {"since": max_mod or since, "start": 0, "max_mod": max_mod})

    def _candidates(self, pkg: dict) -> Iterator[Candidate]:
        raw_type = (pkg.get("odm_document_type") or "").strip().lower()
        if raw_type in SUBTYPE:
            sub = SUBTYPE[raw_type]
            fam = FAMILY_OF.get(sub, "legal_instrument")
        elif raw_type in REPORT_LIKE:
            fam, sub = "report", raw_type
        else:
            guess = classify_doc_type(_tr(pkg.get("title_translated"), "en"), _tr(pkg.get("title_translated"), "km"))
            fam, sub = guess or (self.src.default_doc_type, raw_type or "other")
        status = STATUS.get((pkg.get("odm_laws_status") or "").strip().lower(), "unknown")
        number = _tr(pkg.get("odm_document_number"), "en")
        if status == "draft" and (iso_date(pkg.get("odm_promulgation_date")) or number):
            # Early catalogue imports carry "draft" as a default even for promulgated, numbered
            # instruments. A promulgation date or number means it was issued; the status is unknown.
            status = "unknown"
        agencies = [agency_name(a) for a in _list(pkg.get("odm_laws_issuing_agency_parties"))]
        license_id = (pkg.get("license_id") or "").lower()
        landing = self.cfg.get("dataset_url", "").format(name=pkg.get("name", ""), id=pkg.get("id", ""))
        relations = {
            "implements": _list(pkg.get("odm_laws_parent_document")),
            "amends": _list(pkg.get("odm_laws_previous_legal_document")),
            "references": _list(pkg.get("odm_laws_other_references")),
        }
        for res in pkg.get("resources") or []:
            url = canonicalize(res.get("url") or "")
            if not url or not self.src.allows(url) or not self.wanted(url):
                continue
            fmt = (res.get("format") or "").strip().lower()
            if fmt not in DOC_EXT and extension(url) not in DOC_EXT:
                continue
            langs = _list(res.get("odm_language")) or _list(pkg.get("odm_language"))
            lang = langs[0] if len(langs) == 1 else ""
            lang = lang if lang in ("km", "en") else ("other" if lang else "")
            tl = lang if lang in ("km", "en") else "en"
            yield Candidate(url, "file", {
                "title": _tr(pkg.get("title_translated"), tl) or pkg.get("title") or "",
                "title_en": _tr(pkg.get("title_translated"), "en"),
                "title_km": (pkg.get("title_translated") or {}).get("km", "").strip(),
                "doc_number": _tr(pkg.get("odm_document_number"), tl),
                "doc_type": fam,
                "doc_subtype": sub,
                "issue_date": iso_date(pkg.get("odm_promulgation_date")),
                "issue_date_source": "catalog",
                "effective_date": iso_date(pkg.get("odm_effective_date")),
                "status": status,
                "institution": "; ".join(agencies),
                "language_hint": lang,
                "group_key": f"ckan:{pkg.get('id')}",
                "catalog_name": pkg.get("name"),
                "landing_url": landing,
                "modified": (pkg.get("metadata_modified") or "")[:19],
                "reuse_status": "open" if license_id.startswith(("cc", "odc", "pddl")) else "",
                "license": pkg.get("license_id") or "",
                "relations": {k: v for k, v in relations.items() if v},
                "original_source": pkg.get("odm_laws_source") or pkg.get("odm_laws_official_publication_reference") or "",
            })

    def estimate(self) -> tuple[int | None, str]:
        result, error = self._search(0, 0, None)
        if result is None:
            return None, f"CKAN search failed: {error}"
        return int(result.get("count") or 0), "CKAN package_search count (one record may hold a Khmer and an English file)"
