"""Chunking (proposal 8.5).

`structure`: parent/child chunks that follow the document's own structure.
  - legal instruments: ជំពូក / Chapter is the parent, មាត្រា / Article / ប្រការ is the child
  - other documents: heading- and paragraph-based sections are the parents, packed paragraphs the children
`fixed`: 512-token windows with 12.5% overlap, the baseline of experiment E6.

Token counts use the tokenizer of the embedding model, so the limits are the model's own.
"""
from __future__ import annotations

import bisect
import re
from concurrent.futures import ProcessPoolExecutor

from . import config, db

_tok = None


def tokenizer():
    global _tok
    if _tok is None:
        from tokenizers import Tokenizer
        from huggingface_hub import hf_hub_download
        _tok = Tokenizer.from_file(hf_hub_download(config.EMBED_MODEL, "tokenizer.json"))
        _tok.no_truncation()
        _tok.no_padding()
    return _tok


class Counter_:
    """Token count of any character span of one text, from a single tokenizer pass."""

    def __init__(self, text: str):
        enc = tokenizer().encode(text, add_special_tokens=False)
        self.starts = [o[0] for o in enc.offsets]
        self.ends = [o[1] for o in enc.offsets]
        self.n = len(self.starts)

    def count(self, a: int, b: int) -> int:
        return max(0, bisect.bisect_left(self.starts, b) - bisect.bisect_left(self.starts, a))

    def char_at(self, token_index: int) -> int:
        return self.starts[token_index] if token_index < self.n else (self.ends[-1] if self.n else 0)


_NUM = r"(?:\d+|[០-៩]+)"
_CHAPTER = re.compile(rf"^[ \t]*(?:ជំពូក(?:ទី)?[ \t]*{_NUM}|ជំពូក[ \t]*[ក-អ]\b|Chapter[ \t]+(?:\d+|[IVXLC]+)\b|CHAPTER[ \t]+(?:\d+|[IVXLC]+)\b|ផ្នែក(?:ទី)?[ \t]*{_NUM}|Section[ \t]+\d+\b).*$", re.M)
_ARTICLE = re.compile(rf"^[ \t]*(?:មាត្រា[ \t]*({_NUM})|ប្រការ[ \t]*({_NUM})|Article[ \t]+(\d+)|Prakar[ \t]+(\d+)|Clause[ \t]+(\d+))[ \t]*(?:[.៖:\-_]|ថ្មី|$|\n|[ \t])", re.M | re.I)
_SENT = re.compile(r"(?<=[។៕!?])\s+|(?<=[a-z0-9)\]]\.)\s+(?=[A-Z])|\n+")
_PARA = re.compile(r"\n\s*\n|\f")
_HEADING = re.compile(r"^(?:[A-Z0-9][^\n]{2,70}|[^\n]{3,70}[:៖])$")


def _page_of(pos: int, page_starts: list[int]) -> int | None:
    return bisect.bisect_right(page_starts, pos) if len(page_starts) > 1 else None


def _split_span(text: str, a: int, b: int, tc: Counter_, limit: int, level: int = 0) -> list[tuple[int, int]]:
    """Split [a, b) into pieces of at most `limit` tokens: by paragraph, then sentence, then token windows."""
    if tc.count(a, b) <= limit:
        return [(a, b)]
    if level >= 2:
        return _windows(a, b, tc, limit, 0)
    rx = (_PARA, _SENT)[level]
    cuts = sorted({a, *[a + m.end() for m in rx.finditer(text[a:b])], b})
    if len(cuts) <= 2:
        return _split_span(text, a, b, tc, limit, level + 1)
    pieces, start = [], a
    for prev, cut in zip(cuts, cuts[1:]):
        if tc.count(start, cut) > limit and prev > start:  # adding this unit would overflow: close the piece
            pieces.append((start, prev))
            start = prev
    pieces.append((start, b))
    out: list[tuple[int, int]] = []
    for s, e in pieces:
        out.extend(_split_span(text, s, e, tc, limit, level + 1))
    return out


def _windows(a: int, b: int, tc: Counter_, size: int, overlap: int) -> list[tuple[int, int]]:
    i0, i1 = bisect.bisect_left(tc.starts, a), bisect.bisect_left(tc.starts, b)
    out, i = [], i0
    while i < i1:
        j = min(i + size, i1)
        out.append((tc.char_at(i) if i > i0 else a, tc.char_at(j) if j < i1 else b))
        if j >= i1:
            break
        i = j - overlap
    return out


def _pack(spans: list[tuple[int, int]], tc: Counter_, target: int) -> list[tuple[int, int]]:
    """Merge neighbouring small spans up to the target size."""
    out: list[tuple[int, int]] = []
    for s, e in spans:
        if out and tc.count(out[-1][0], e) <= target:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def chunk_document(doc: dict) -> list[dict]:
    """All chunk rows of one document, both strategies. Parents carry a local `key`; children refer to it."""
    text = doc["extracted_text"]
    if len(text.strip()) < 40:
        return []
    tc = Counter_(text)
    page_starts = [0] + [m.end() for m in re.finditer("\f", text)]
    head = " | ".join(x for x in (
        doc["institution"],
        " ".join(x for x in (doc["doc_subtype"].replace("_", " ") if doc["doc_subtype"] != "other" else doc["doc_type"].replace("_", " "), doc["doc_number"]) if x),
        str(doc["issue_date"] or ""), doc["title"][:160]) if x)
    rows: list[dict] = []

    def add(strategy, a, b, parent=None, key=None, section="", article="", is_parent=False):
        body = text[a:b].replace("\f", "\n").strip()
        if not body:
            return
        rows.append({
            "strategy": strategy, "text": body, "parent": parent, "key": key, "is_parent": is_parent,
            "section": section[:200], "article": article, "page": _page_of(a, page_starts),
            "tokens": tc.count(a, b),
            "header": head + (f" | {section[:80]}" if section else "") + (f" | {article}" if article else ""),
        })

    # -- structure-aware -----------------------------------------------------------------
    arts = [(m.start(), next(g for g in m.groups() if g)) for m in _ARTICLE.finditer(text)]
    legal = doc["doc_type"] == "legal_instrument" and len(arts) >= 3
    if tc.n <= config.CHILD_MAX_TOKENS:
        add("structure", 0, len(text), key=0, is_parent=True)
        add("structure", 0, len(text), parent=0)
    elif legal:
        chapters = [(m.start(), m.group(0).strip()) for m in _CHAPTER.finditer(text)]
        bounds = sorted({0, *[c[0] for c in chapters], len(text)})
        titles = dict(chapters)
        key = 0
        for ca, cb in zip(bounds, bounds[1:]):
            section = titles.get(ca, "")
            # a chapter that is too long for the language model's context is cut into parent-sized parts
            for pa, pb in _pack_articles(ca, cb, arts, tc):
                add("structure", pa, pb, key=key, section=section, is_parent=True)
                inner = sorted({pa, *[a for a, _ in arts if pa <= a < pb], pb})
                label = {a: n for a, n in arts}
                for aa, ab in zip(inner, inner[1:]):
                    art = label.get(aa, "")
                    art = f"មាត្រា {art}" if art and doc["language"] != "en" else (f"Article {art}" if art else "")
                    for s, e in _split_span(text, aa, ab, tc, config.CHILD_MAX_TOKENS):
                        add("structure", s, e, parent=key, section=section, article=art)
                key += 1
    else:
        paras = sorted({0, *[m.end() for m in _PARA.finditer(text)], len(text)})
        units: list[tuple[int, int]] = []
        for a, b in zip(paras, paras[1:]):
            units.extend(_split_span(text, a, b, tc, config.CHILD_MAX_TOKENS))
        key = 0
        for pa, pb in _pack(units, tc, config.PARENT_MAX_TOKENS):
            first = text[pa:pb].lstrip().split("\n", 1)[0].strip()
            section = first if _HEADING.match(first) else ""
            add("structure", pa, pb, key=key, section=section, is_parent=True)
            for s, e in _pack([u for u in units if pa <= u[0] < pb], tc, config.CHILD_TARGET_TOKENS):
                add("structure", s, e, parent=key, section=section)
            key += 1

    # -- fixed-size baseline ----------------------------------------------------------------
    for s, e in _windows(0, len(text), tc, config.FIXED_TOKENS, config.FIXED_OVERLAP):
        add("fixed", s, e)
    return rows


def _pack_articles(ca: int, cb: int, arts, tc: Counter_) -> list[tuple[int, int]]:
    """Parent spans inside one chapter: whole chapter if it fits, otherwise groups of articles."""
    if tc.count(ca, cb) <= config.PARENT_MAX_TOKENS:
        return [(ca, cb)]
    cuts = sorted({ca, *[a for a, _ in arts if ca < a < cb], cb})
    spans = []
    for a, b in zip(cuts, cuts[1:]):
        spans.extend(_windows(a, b, tc, config.PARENT_MAX_TOKENS, 0) if tc.count(a, b) > config.PARENT_MAX_TOKENS else [(a, b)])
    return _pack(spans, tc, config.PARENT_MAX_TOKENS)


def _work(doc: dict):
    try:
        return doc["document_id"], chunk_document(doc), ""
    except Exception as e:
        return doc["document_id"], [], f"{type(e).__name__}: {e}"


def run(workers: int = 12, rebuild: bool = False) -> dict:
    """Chunk every parsed document that has no chunks yet."""
    with db.connect() as con:
        if rebuild:
            con.execute("TRUNCATE chunks CASCADE")
        ids = [r["document_id"] for r in con.execute(
            """SELECT d.document_id FROM documents d
               WHERE d.parse_status IN ('ok', 'ocr') AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.document_id)
               ORDER BY 1""").fetchall()]
    tokenizer()  # download before forking
    n = errors = 0
    with ProcessPoolExecutor(workers) as pool, db.connect() as con, db.connect() as rd:
        def docs():
            for i in range(0, len(ids), 200):
                yield from rd.execute(
                    """SELECT d.document_id, d.extracted_text, s.institution, s.doc_type, s.doc_subtype, s.doc_number,
                              s.issue_date, s.title, s.language
                       FROM documents d JOIN sources s USING (source_id) WHERE d.document_id = ANY(%s)""", (ids[i:i + 200],)).fetchall()
        for i, (doc_id, rows, err) in enumerate(pool.map(_work, docs(), chunksize=4), 1):
            if err:
                errors += 1
                print("chunk error", doc_id, err, flush=True)
            keys: dict[int, int] = {}
            idx = {"structure": 0, "fixed": 0}
            for r in rows:
                cid = con.execute(
                    """INSERT INTO chunks (document_id, parent_chunk_id, chunk_index, text, context_header, section_title,
                           article_number, page_number, token_count, chunking_strategy, is_parent)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING chunk_id""",
                    (doc_id, keys.get(r["parent"]) if r["parent"] is not None else None, idx[r["strategy"]], r["text"], r["header"],
                     r["section"], r["article"], r["page"], r["tokens"], r["strategy"], r["is_parent"]),
                ).fetchone()["chunk_id"]
                idx[r["strategy"]] += 1
                if r["is_parent"]:
                    keys[r["key"]] = cid
                n += 1
            if i % 500 == 0:
                con.commit()
                print(f"chunk {i}/{len(ids)}", flush=True)
    return {"documents": len(ids), "chunks": n, "errors": errors}
