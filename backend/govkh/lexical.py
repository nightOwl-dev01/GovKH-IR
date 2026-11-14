"""Lexical indexes (proposal 9, 10.1): BM25 in three Khmer variants, TF-IDF + cosine, and Boolean retrieval.

All of them are inverted indexes over the child chunks of one chunking strategy. Each chunk is indexed as
its contextual header followed by its text. Layout: INDEX_DIR/<strategy>/{meta.json, chunk_ids.npy,
bm25_<mode>/, tfidf.npz, tfidf_vocab.json, tfidf_idf.npy}.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache

import numpy as np
import scipy.sparse as sp

from . import config, db
from .text import tokenize

MODES = ("nosplit", "word", "trigram")
BM25_K1, BM25_B = 1.2, 0.75


def _tok_batch(args):
    """Tokenise a batch in all modes. Returns, per mode, a local vocabulary and flat id arrays (compact to pickle)."""
    texts, modes = args
    out = {}
    for mode in modes:
        vocab: dict[str, int] = {}
        flat, lens = [], []
        for t in texts:
            toks = tokenize(t, mode)
            lens.append(len(toks))
            flat.extend(vocab.setdefault(w, len(vocab)) for w in toks)
        out[mode] = (list(vocab), np.asarray(flat, dtype=np.int32), np.asarray(lens, dtype=np.int32))
    return out


def chunk_rows(strategy: str):
    with db.connect() as con:
        return con.execute(
            """SELECT chunk_id, context_header || E'\n' || text AS body FROM chunks
               WHERE chunking_strategy = %s AND NOT is_parent ORDER BY chunk_id""", (strategy,)).fetchall()


def build(strategies=("structure", "fixed"), workers: int = 12) -> dict:
    import bm25s
    report = {}
    for strategy in strategies:
        rows = chunk_rows(strategy)
        if not rows:
            continue
        modes = MODES if strategy == "structure" else ("word",)
        out_dir = config.INDEX_DIR / strategy
        out_dir.mkdir(parents=True, exist_ok=True)
        ids = np.asarray([r["chunk_id"] for r in rows], dtype=np.int64)
        texts = [r["body"] for r in rows]
        batches = [(texts[i:i + 200], modes) for i in range(0, len(texts), 200)]
        vocab = {m: {} for m in modes}
        flat = {m: [] for m in modes}
        lens = {m: [] for m in modes}
        t0 = time.time()
        with ProcessPoolExecutor(workers) as pool:
            for i, res in enumerate(pool.map(_tok_batch, batches), 1):
                for m in modes:
                    local, f, l = res[m]
                    g = vocab[m]
                    remap = np.asarray([g.setdefault(w, len(g)) for w in local], dtype=np.int32)
                    flat[m].append(remap[f] if len(f) else f)
                    lens[m].append(l)
                if i % 50 == 0:
                    print(f"tokenise {strategy} {i * 200}/{len(texts)}", flush=True)
        tok_s = time.time() - t0
        meta = {"strategy": strategy, "chunks": len(ids), "tokenise_seconds": round(tok_s, 1), "modes": {}}
        np.save(out_dir / "chunk_ids.npy", ids)
        for m in modes:
            f = np.concatenate(flat[m])
            l = np.concatenate(lens[m])
            offs = np.concatenate([[0], np.cumsum(l)])
            t0 = time.time()
            corpus = bm25s.tokenization.Tokenized(ids=[f[offs[i]:offs[i + 1]].tolist() for i in range(len(l))], vocab=vocab[m])
            bm = bm25s.BM25(k1=BM25_K1, b=BM25_B, method="lucene")
            bm.index(corpus, show_progress=False)
            bm.save(out_dir / f"bm25_{m}")
            size = sum(p.stat().st_size for p in (out_dir / f"bm25_{m}").rglob("*") if p.is_file())
            meta["modes"][m] = {"vocab": len(vocab[m]), "tokens": int(len(f)), "avg_len": round(float(l.mean()), 1),
                                "index_seconds": round(time.time() - t0, 1), "index_bytes": size}
            if m == "word":  # the TF-IDF and Boolean index share the word-segmented postings
                rows_idx = np.repeat(np.arange(len(l), dtype=np.int32), l)
                counts = sp.csr_matrix((np.ones(len(f), dtype=np.float32), (rows_idx, f)), shape=(len(l), len(vocab[m])))
                counts.sum_duplicates()
                df = np.asarray((counts > 0).sum(axis=0)).ravel()
                idf = (np.log((1 + len(l)) / (1 + df)) + 1).astype(np.float32)
                x = counts.copy()
                x.data = 1 + np.log(x.data)  # sublinear tf
                x = x.multiply(idf).tocsr()
                norms = np.sqrt(np.asarray(x.multiply(x).sum(axis=1)).ravel())
                norms[norms == 0] = 1
                x = sp.diags(1 / norms).dot(x).tocsc().astype(np.float32)
                sp.save_npz(out_dir / "tfidf.npz", x)
                np.save(out_dir / "tfidf_idf.npy", idf)
                (out_dir / "tfidf_vocab.json").write_text(json.dumps(vocab[m], ensure_ascii=False), encoding="utf-8")
                meta["tfidf_bytes"] = (out_dir / "tfidf.npz").stat().st_size
        (out_dir / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        report[strategy] = meta
    return report


def _topk(scores: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    k = min(k, int((scores > 0).sum()))
    if k <= 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float32)
    idx = np.argpartition(-scores, k - 1)[:k]
    idx = idx[np.argsort(-scores[idx], kind="stable")]
    return idx, scores[idx]


class LexicalIndex:
    def __init__(self, strategy: str = "structure"):
        self.dir = config.INDEX_DIR / strategy
        self.chunk_ids = np.load(self.dir / "chunk_ids.npy")
        self.meta = json.loads((self.dir / "meta.json").read_text())
        self._bm25: dict = {}
        self._tfidf = None

    def _bm(self, mode: str):
        if mode not in self._bm25:
            import bm25s
            self._bm25[mode] = bm25s.BM25.load(self.dir / f"bm25_{mode}", load_corpus=False)
        return self._bm25[mode]

    def bm25(self, query: str, mode: str = "word", k: int = 100, mask: np.ndarray | None = None):
        bm = self._bm(mode)
        toks = [t for t in dict.fromkeys(tokenize(query, mode)) if t in bm.vocab_dict]
        if not toks:
            return _topk(np.zeros(1, dtype=np.float32), 0)
        scores = bm.get_scores(toks)
        if mask is not None:
            scores = scores * mask
        return _topk(scores, k)

    def _tf(self):
        if self._tfidf is None:
            self._tfidf = (sp.load_npz(self.dir / "tfidf.npz"), np.load(self.dir / "tfidf_idf.npy"),
                           json.loads((self.dir / "tfidf_vocab.json").read_text(encoding="utf-8")))
        return self._tfidf

    def tfidf(self, query: str, k: int = 100, mask: np.ndarray | None = None, restrict: np.ndarray | None = None):
        x, idf, vocab = self._tf()
        counts: dict[int, int] = {}
        for t in tokenize(query, "word"):
            if t in vocab:
                counts[vocab[t]] = counts.get(vocab[t], 0) + 1
        if not counts:
            return _topk(np.zeros(1, dtype=np.float32), 0)
        cols = np.fromiter(counts, dtype=np.int64)
        w = (1 + np.log(np.fromiter(counts.values(), dtype=np.float32))) * idf[cols]
        w /= np.linalg.norm(w)
        scores = np.asarray(x[:, cols].dot(w)).ravel()  # cosine: rows are L2-normalised
        if mask is not None:
            scores = scores * mask
        if restrict is not None:
            keep = np.zeros_like(scores)
            keep[restrict] = scores[restrict] + 1e-6
            scores = keep
        return _topk(scores, k)

    # -- Boolean retrieval: AND, OR, NOT, parentheses; adjacent terms are ANDed ---------------
    def _postings(self, term: str) -> np.ndarray:
        x, _, vocab = self._tf()
        toks = tokenize(term, "word")
        res = None
        for t in toks:  # a term that segments into several words must contain all of them
            col = vocab.get(t)
            p = x.indices[x.indptr[col]:x.indptr[col + 1]] if col is not None else np.empty(0, dtype=np.int32)
            res = p if res is None else np.intersect1d(res, p, assume_unique=True)
        return res if res is not None else np.empty(0, dtype=np.int32)

    def boolean_match(self, query: str) -> np.ndarray:
        tokens = re.findall(r"\(|\)|\"[^\"]+\"|[^\s()]+", query)
        pos = 0
        universe = np.arange(len(self.chunk_ids), dtype=np.int32)

        def peek():
            return tokens[pos] if pos < len(tokens) else None

        def parse_or():
            nonlocal pos
            left = parse_and()
            while peek() == "OR":
                pos += 1
                left = np.union1d(left, parse_and())
            return left

        def parse_and():
            nonlocal pos
            left = parse_not()
            while peek() is not None and peek() not in ("OR", ")"):
                if peek() == "AND":
                    pos += 1
                left = np.intersect1d(left, parse_not(), assume_unique=True)
            return left

        def parse_not():
            nonlocal pos
            if peek() == "NOT":
                pos += 1
                return np.setdiff1d(universe, parse_not(), assume_unique=True)
            return parse_atom()

        def parse_atom():
            nonlocal pos
            t = peek()
            if t is None:
                return np.empty(0, dtype=np.int32)
            pos += 1
            if t == "(":
                inner = parse_or()
                if peek() == ")":
                    pos += 1
                return inner
            return self._postings(t.strip('"'))

        return parse_or()

    def boolean(self, query: str, k: int = 100, mask: np.ndarray | None = None):
        """The match set is unranked; for display and evaluation it is ordered by TF-IDF cosine on the query terms."""
        hits = self.boolean_match(query)
        if mask is not None:
            hits = hits[mask[hits] > 0]
        if not len(hits):
            return _topk(np.zeros(1, dtype=np.float32), 0)
        plain = " ".join(t for t in re.findall(r"[^\s()\"]+", query) if t not in ("AND", "OR", "NOT"))
        return self.tfidf(plain, k, restrict=hits)


@lru_cache(maxsize=4)
def get(strategy: str = "structure") -> LexicalIndex:
    return LexicalIndex(strategy)
