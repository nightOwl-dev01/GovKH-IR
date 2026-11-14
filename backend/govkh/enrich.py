"""Metadata enrichment (proposal 8.6): rules first, a language model for the remainder, then relations and status.

Every enriched value records its origin in sources.meta_source so it can be audited.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date

from psycopg.types.json import Jsonb

from . import config, db
from .text import normalize
from govkh_crawler.classify import _NUM_CODE, _NUM_EN, _NUM_KM, classify_doc_type, extract_date, extract_doc_number

LEGAL_SUBTYPES = {"law", "royal_decree", "sub_decree", "prakas", "decision", "circular", "instruction", "order",
                  "notification", "constitution", "international_treaty"}
HEAD = 2500

# GDT's catalogue puts number, date and validity in the link text:
#   "<row> <number> <day month year> <title> KH <status>"
_GDT = re.compile(r"^\d+\s+(?P<num>.+?)\s+(?P<date>\d{1,2}\s+\S+\s+\d{4})\s+(?P<title>.+?)\s+(?:KH|EN)\s+(?P<status>\S.*)$")
_GENERIC_TITLE = re.compile(r"^(?:[\w\-]+\.(?:pdf|docx?|xlsx?)|\(?download here\)?\.?|GDCE Website|ទាញយក.*|សូមចុចទីនេះ.*|\d+)$", re.I)

_PREAMBLE = re.compile(r"បានឃើញ|បានទ្រង់យល់|យោង|Having seen|Seen the|Referring|Pursuant", re.I)

_REL_KEYS = [
    ("repeals", re.compile(r"និរាករណ៍|លុបចោល|ទុកជាមោឃៈ|repeal|abrogat|annul", re.I)),
    ("amends", re.compile(r"វិសោធនកម្ម|កែប្រែ|កែសម្រួល|amend", re.I)),
    ("implements", re.compile(r"អនុវត្ត|implement", re.I)),
]


def num_key(num: str) -> str:
    """Comparable form of a document number; '' when it is too generic to identify one instrument."""
    k = re.sub(r"[\s.​]", "", normalize(num)).lower().strip("/-")
    return k if len(k) >= 6 and re.search(r"\d", k) and re.search(r"[^\d/\-]", k) else ""


def _status_from_gdt(s: str) -> str:
    if s.startswith("សុពលភាព"):
        return "amended" if "និរាករណ៍" in s or "វិសោធន" in s else "in_force"
    if "និរាករណ៍" in s:
        return "repealed"
    return "unknown"


def _valid(d: str) -> date | None:
    try:
        v = date.fromisoformat(d)
    except (ValueError, TypeError):
        return None
    return v if date(1953, 1, 1) <= v <= date.today() else None


def rules(con) -> Counter:
    stats: Counter = Counter()
    rows = con.execute(
        """SELECT s.source_id, s.site_id, s.title, s.doc_type, s.doc_subtype, s.doc_number, s.issue_date, s.status,
                  s.file_type, s.meta_source, left(d.extracted_text, %s) AS head
           FROM sources s LEFT JOIN documents d USING (source_id)""", (HEAD,)
    ).fetchall()
    for r in rows:
        upd, src = {}, dict(r["meta_source"])
        title, head = r["title"], (r["head"] or "").replace("\f", "\n")
        if r["site_id"] == "gdt" and (m := _GDT.match(title)):
            upd["title"] = m["title"].strip()
            st = _status_from_gdt(m["status"])
            if st != "unknown":
                upd["status"], src["status"] = st, "catalog"
            if not r["doc_number"]:
                upd["doc_number"], src["doc_number"] = normalize(m["num"]), "catalog"
            title = upd["title"]
        legal = r["doc_type"] == "legal_instrument" or r["doc_subtype"] in LEGAL_SUBTYPES
        # the instrument's own number and date come before the preamble, which cites other instruments
        own = _PREAMBLE.split(head[:1500], maxsplit=1)[0][:600]
        if not r["doc_number"] and "doc_number" not in upd:
            n = extract_doc_number(title) or (extract_doc_number(own) if legal and r["file_type"] != "html" else "")
            if n and num_key(n):
                upd["doc_number"], src["doc_number"] = n, "title_rule" if extract_doc_number(title) else "text_rule"
        if not r["issue_date"]:
            d = _valid(extract_date(title)) or (_valid(extract_date(own)) if legal and r["file_type"] != "html" else None)
            if d:
                upd["issue_date"], src["issue_date"] = d, "title_rule" if _valid(extract_date(title)) else "text_rule"
        if r["doc_subtype"] == "other" and r["file_type"] != "html":
            c = classify_doc_type(title) or classify_doc_type(head[:200])
            if c:
                fam, sub = c
                upd["doc_type"], upd["doc_subtype"], src["doc_type"] = fam, sub, "text_rule"
        if upd:
            sets = ", ".join(f"{k} = %s" for k in upd)
            con.execute(f"UPDATE sources SET {sets}, meta_source = %s WHERE source_id = %s", (*upd.values(), Jsonb(src), r["source_id"]))
            for k in upd:
                stats[f"rule_{k}"] += 1
    return stats


# -- language model for what the rules missed -------------------------------------------
_PROMPT = """You extract metadata from the first page of a Cambodian government document.
Return JSON with keys: title (the document's own title in its original language, max 200 chars, "" if unclear),
doc_number (official number such as "123 អនក្រ.បក" or "NS/RKM/0196/18", "" if none),
issue_date (YYYY-MM-DD in the Gregorian calendar, "" if not stated),
doc_subtype (one of: law, royal_decree, sub_decree, prakas, decision, circular, instruction, order, notification,
constitution, international_treaty, report, form, procedure, fee_schedule, press_release, other).
Use only what the text states. Do not guess."""


def llm(con, limit: int | None = None) -> Counter:
    """Fill title / number / date / subtype for file documents the rules could not describe."""
    from openai import OpenAI
    from concurrent.futures import ThreadPoolExecutor
    client = OpenAI()
    dup_titles = {r["title"] for r in con.execute(
        "SELECT title FROM sources GROUP BY site_id, title HAVING count(*) >= 4").fetchall()}
    rows = con.execute(
        """SELECT s.source_id, s.title, s.doc_number, s.issue_date, s.doc_subtype, s.meta_source, left(d.extracted_text, 1800) AS head
           FROM sources s JOIN documents d USING (source_id)
           WHERE s.file_type <> 'html' AND d.parse_status IN ('ok', 'ocr') AND NOT (s.meta_source ? 'llm')
           ORDER BY s.source_id"""
    ).fetchall()
    todo = [r for r in rows if r["title"] in dup_titles or _GENERIC_TITLE.match(r["title"].strip())
            or (r["doc_subtype"] in LEGAL_SUBTYPES | {"other"} and (not r["doc_number"] or not r["issue_date"]))][:limit]

    def ask(r):
        try:
            res = client.chat.completions.create(
                model=config.LLM_MODEL, temperature=0, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": _PROMPT},
                          {"role": "user", "content": f"Link text: {r['title']}\n\nFirst page:\n{r['head']}"}])
            return r, json.loads(res.choices[0].message.content)
        except Exception as e:
            return r, {"error": str(e)[:100]}

    stats: Counter = Counter()
    with ThreadPoolExecutor(8) as pool:
        for i, (r, out) in enumerate(pool.map(ask, todo), 1):
            src = dict(r["meta_source"])
            src["llm"] = config.LLM_MODEL
            upd = {}
            if "error" in out:
                stats["llm_error"] += 1
                continue
            t = str(out.get("title") or "").strip()
            if t and (r["title"] in dup_titles or _GENERIC_TITLE.match(r["title"].strip())):
                upd["title"], src["title"] = t[:300], "llm"
            n = str(out.get("doc_number") or "").strip()
            # accept a number only if it literally occurs on the page (guards against invention)
            if n and not r["doc_number"] and re.sub(r"\s", "", normalize(n)) in re.sub(r"\s", "", normalize(r["head"])):
                upd["doc_number"], src["doc_number"] = n[:80], "llm"
            d = _valid(str(out.get("issue_date") or ""))
            if d and not r["issue_date"]:
                upd["issue_date"], src["issue_date"] = d, "llm"
            sub = out.get("doc_subtype")
            if r["doc_subtype"] == "other" and sub in LEGAL_SUBTYPES | {"report", "form", "procedure", "fee_schedule"}:
                fam = ("legal_instrument" if sub in LEGAL_SUBTYPES - {"notification"} else "announcement" if sub == "notification"
                       else "report" if sub == "report" else "procedure" if sub == "procedure" else "form_fee")
                upd["doc_subtype"], upd["doc_type"], src["doc_type"] = sub, fam, "llm"
            sets = ", ".join(f"{k} = %s" for k in upd)
            con.execute(f"UPDATE sources SET {sets + ', ' if sets else ''}meta_source = %s WHERE source_id = %s",
                        (*upd.values(), Jsonb(src), r["source_id"]))
            for k in upd:
                stats[f"llm_{k}"] += 1
            if i % 200 == 0:
                con.commit()
                print(f"enrich llm {i}/{len(todo)}", flush=True)
    stats["llm_asked"] = len(todo)
    return stats


# -- relations and status -----------------------------------------------------------------
def relations(con) -> Counter:
    """amends / repeals / implements / references from phrases that cite another instrument's number."""
    stats: Counter = Counter()
    index: dict[str, list[str]] = defaultdict(list)
    for r in con.execute("SELECT source_id, doc_number FROM sources WHERE doc_number <> ''").fetchall():
        if k := num_key(r["doc_number"]):
            index[k].append(r["source_id"])
    con.execute("DELETE FROM doc_relations WHERE evidence LIKE 'phrase:%'")
    rows = con.execute(
        """SELECT s.source_id, s.doc_number, s.title, left(d.extracted_text, 60000) AS body
           FROM sources s JOIN documents d USING (source_id)
           WHERE (s.doc_type = 'legal_instrument' OR s.doc_subtype IN ('notification', 'instruction', 'circular'))
             AND d.parse_status IN ('ok', 'ocr')"""
    ).fetchall()
    for r in rows:
        own = num_key(r["doc_number"])
        text = normalize(r["title"] + "\n" + r["body"])
        found: dict[tuple[str, str], str] = {}
        for rx in (_NUM_KM, _NUM_EN, _NUM_CODE):
            for m in rx.finditer(text):
                k = num_key(m.group(1))
                if not k or k == own or k not in index or len(index[k]) > 4:
                    continue
                ctx = text[max(0, m.start() - 90):m.start()].replace("\n", " ")
                rel = next((name for name, key in _REL_KEYS if key.search(ctx)), "references")
                for target in index[k]:
                    if target != r["source_id"]:
                        prev = found.get((target, rel))
                        if prev is None:
                            found[(target, rel)] = f"phrase:{ctx[-60:]} {m.group(1)}"[:200]
        # keep the strongest relation per target
        order = {"repeals": 0, "amends": 1, "implements": 2, "references": 3}
        best: dict[str, tuple[str, str]] = {}
        for (target, rel), ev in found.items():
            if target not in best or order[rel] < order[best[target][0]]:
                best[target] = (rel, ev)
        for target, (rel, ev) in best.items():
            con.execute("INSERT INTO doc_relations VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (r["source_id"], target, rel, ev))
            stats[f"rel_{rel}"] += 1
    return stats


def status(con) -> Counter:
    """repealed / amended when a later instrument in the corpus says so; the catalogue's own status is kept."""
    n = con.execute(
        """UPDATE sources t SET status = x.st, meta_source = meta_source || jsonb_build_object('status', 'relation')
           FROM (SELECT r.to_source_id, CASE WHEN bool_or(r.relation = 'repeals') THEN 'repealed' ELSE 'amended' END AS st
                 FROM doc_relations r JOIN sources f ON f.source_id = r.from_source_id JOIN sources t2 ON t2.source_id = r.to_source_id
                 WHERE r.relation IN ('repeals', 'amends')
                   AND (f.issue_date IS NULL OR t2.issue_date IS NULL OR f.issue_date >= t2.issue_date)
                 GROUP BY r.to_source_id) x
           WHERE t.source_id = x.to_source_id AND coalesce(t.meta_source->>'status', '') <> 'catalog'"""
    ).rowcount
    return Counter({"status_from_relations": n})


def run(llm: bool = False, limit: int | None = None) -> dict:
    out: Counter = Counter()
    with db.connect() as con:
        out += rules(con)
        con.commit()
        if llm:
            out += globals()["llm"](con, limit)
            con.commit()
        out += relations(con)
        out += status(con)
        out.update({f"status_{r['status']}": r["n"] for r in con.execute("SELECT status, count(*) n FROM sources GROUP BY 1").fetchall()})
    return dict(out)
