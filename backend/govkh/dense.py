"""Dense index (proposal 9, 9.1, 10.1): one embedding model for the whole index.

Vectors are L2-normalised, so cosine similarity is a dot product. The matrix is kept on disk at
INDEX_DIR/<strategy>/dense_<model>.npy (float16) with the chunk ids beside it; this is the exact-search
index and the source for the ANN index. When the database has pgvector, the vectors are also written to
embeddings.vector and searched through its HNSW index; otherwise HNSW is served by hnswlib with the same
parameters (m = 16, ef_construction = 64). Model name and version are stored for every vector.
"""
from __future__ import annotations

import json
import time
from functools import lru_cache

import numpy as np

from . import config, db

# Query / document prompts from each model card.
PROMPTS = {
    "BAAI/bge-m3": ({}, {}),
    "intfloat/multilingual-e5-large": ({"prompt": "query: "}, {"prompt": "passage: "}),
    "Qwen/Qwen3-Embedding-0.6B": ({"prompt_name": "query"}, {}),
    "google/embeddinggemma-2": ({"prompt_name": "query"}, {"prompt_name": "document"}),
    "google/embeddinggemma-300m": ({"prompt_name": "query"}, {"prompt_name": "document"}),
}
MAX_TOKENS = 512
HNSW_M, HNSW_EF_CONSTRUCTION = 16, 64


def slug(model: str) -> str:
    return model.split("/")[-1].lower()


@lru_cache(maxsize=1)
def load_model(name: str = config.EMBED_MODEL, device: str | None = None):
    import torch
    from sentence_transformers import SentenceTransformer
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    model = SentenceTransformer(name, device=device)
    model.max_seq_length = MAX_TOKENS  # float32: half precision is slower on the GTX 1650 (measured 1.0 vs 4.2 chunks/s)
    return model


def model_version(name: str) -> str:
    try:
        from huggingface_hub import snapshot_download
        return snapshot_download(name, local_files_only=True).rstrip("/").split("/")[-1][:12]
    except Exception:
        return "unknown"


def encode(texts: list[str], name: str = config.EMBED_MODEL, query: bool = False, batch_size: int = 16, progress: bool = False) -> np.ndarray:
    model = load_model(name)
    kw = PROMPTS.get(name, ({}, {}))[0 if query else 1]
    vec = model.encode(texts, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=progress,
                       convert_to_numpy=True, **kw)
    return vec.astype(np.float32)


def paths(strategy: str, name: str = config.EMBED_MODEL):
    d = config.INDEX_DIR / strategy
    d.mkdir(parents=True, exist_ok=True)
    return d / f"dense_{slug(name)}.npy", d / f"dense_{slug(name)}_ids.npy", d / f"dense_{slug(name)}.json"


def build(strategies=("structure", "fixed"), name: str = config.EMBED_MODEL, batch_docs: int = 2000) -> dict:
    """Embed child chunks that have no vector yet; drop vectors of chunks that no longer exist."""
    report = {}
    version = model_version(name)
    for strategy in strategies:
        vec_p, ids_p, meta_p = paths(strategy, name)
        with db.connect() as con:
            rows = con.execute(
                """SELECT chunk_id, token_count, context_header || E'\n' || text AS body FROM chunks
                   WHERE chunking_strategy = %s AND NOT is_parent ORDER BY chunk_id""", (strategy,)).fetchall()
        if not rows:
            continue
        want = np.asarray([r["chunk_id"] for r in rows], dtype=np.int64)
        have_ids = np.load(ids_p) if ids_p.exists() else np.empty(0, dtype=np.int64)
        have = np.load(vec_p) if vec_p.exists() else None
        keep = np.isin(have_ids, want)
        have_ids = have_ids[keep]
        have = have[keep] if have is not None else None
        done = set(have_ids.tolist())
        todo = sorted((r for r in rows if r["chunk_id"] not in done), key=lambda r: -r["token_count"])
        t0 = time.time()
        for i in range(0, len(todo), batch_docs):
            part = todo[i:i + batch_docs]
            v = encode([r["body"] for r in part], name).astype(np.float16)
            have = v if have is None else np.concatenate([have, v])
            have_ids = np.concatenate([have_ids, np.asarray([r["chunk_id"] for r in part], dtype=np.int64)])
            order = np.argsort(have_ids)
            np.save(vec_p, have[order])  # checkpoint: a stopped run resumes here
            np.save(ids_p, have_ids[order])
            rate = (i + len(part)) / (time.time() - t0)
            print(f"embed {strategy} {i + len(part)}/{len(todo)} ({rate:.1f} chunks/s)", flush=True)
        order = np.argsort(have_ids)
        have, have_ids = have[order], have_ids[order]
        np.save(vec_p, have)
        np.save(ids_p, have_ids)
        secs = time.time() - t0
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else {"embed_seconds": 0.0, "embedded": 0}
        meta.update({"model": name, "version": version, "dim": int(have.shape[1]), "vectors": int(len(have_ids)),
                     "embed_seconds": round(meta["embed_seconds"] + secs, 1), "embedded": meta["embedded"] + len(todo),
                     "matrix_bytes_float16": vec_p.stat().st_size, "bytes_float32": int(have.size * 4)})
        meta_p.write_text(json.dumps(meta, indent=1))
        _register(have_ids, have, name, version)
        hp = vec_p.with_suffix(".hnsw")
        if todo or keep.size and not keep.all():
            hp.unlink(missing_ok=True)  # stale ANN index
        report[strategy] = meta
    return report


def _register(ids: np.ndarray, vecs: np.ndarray, name: str, version: str) -> None:
    """Record model name/version per vector; write the vector itself when pgvector is available."""
    vec = db.init(dim=int(vecs.shape[1]))
    with db.connect() as con:
        existing = {r["chunk_id"] for r in con.execute("SELECT chunk_id FROM embeddings WHERE model_name = %s", (name,)).fetchall()}
        new = [i for i, c in enumerate(ids.tolist()) if c not in existing]
        with con.cursor() as cur:
            if vec:
                cur.executemany(
                    "INSERT INTO embeddings (chunk_id, model_name, model_version, vector) VALUES (%s,%s,%s,%s::vector) ON CONFLICT DO NOTHING",
                    [(int(ids[i]), name, version, "[" + ",".join(f"{x:.5f}" for x in vecs[i].astype(np.float32)) + "]") for i in new])
            else:
                cur.executemany(
                    "INSERT INTO embeddings (chunk_id, model_name, model_version) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    [(int(ids[i]), name, version) for i in new])


class DenseIndex:
    def __init__(self, strategy: str = "structure", name: str = config.EMBED_MODEL):
        vec_p, ids_p, meta_p = paths(strategy, name)
        self.name = name
        self.chunk_ids = np.load(ids_p)
        self.matrix = np.load(vec_p).astype(np.float32)
        self.meta = json.loads(meta_p.read_text())
        self.hnsw_path = vec_p.with_suffix(".hnsw")
        self._hnsw = None

    def exact(self, qvec: np.ndarray, k: int = 100, mask: np.ndarray | None = None):
        scores = self.matrix @ qvec
        if mask is not None:
            scores = np.where(mask > 0, scores, -2.0)
        k = min(k, len(scores))
        idx = np.argpartition(-scores, k - 1)[:k]
        idx = idx[np.argsort(-scores[idx], kind="stable")]
        idx = idx[scores[idx] > -2.0]
        return idx, scores[idx]

    def hnsw_index(self):
        """hnswlib HNSW over the same vectors (cosine via inner product on normalised vectors)."""
        if self._hnsw is None:
            import hnswlib
            ix = hnswlib.Index(space="ip", dim=self.matrix.shape[1])
            if self.hnsw_path.exists():
                ix.load_index(str(self.hnsw_path), max_elements=len(self.chunk_ids))
            else:
                t0 = time.time()
                ix.init_index(max_elements=len(self.chunk_ids), ef_construction=HNSW_EF_CONSTRUCTION, M=HNSW_M)
                ix.add_items(self.matrix, np.arange(len(self.chunk_ids)))
                ix.save_index(str(self.hnsw_path))
                self.meta["hnsw_build_seconds"] = round(time.time() - t0, 1)
                self.meta["hnsw_bytes"] = self.hnsw_path.stat().st_size
            self._hnsw = ix
        return self._hnsw

    def hnsw(self, qvec: np.ndarray, k: int = 100, ef_search: int = 100):
        ix = self.hnsw_index()
        ix.set_ef(max(ef_search, k))
        labels, dist = ix.knn_query(qvec[None, :], k=min(k, len(self.chunk_ids)))
        return labels[0].astype(np.int64), 1.0 - dist[0]

    def search(self, query: str, k: int = 100, mask: np.ndarray | None = None, ann: bool = False, ef_search: int = 100):
        q = encode([query], self.name, query=True)[0]
        if ann and mask is None:
            return self.hnsw(q, k, ef_search)
        return self.exact(q, k, mask)


def pgvector_search(qvec: np.ndarray, k: int = 100, ef_search: int = 100, name: str = config.EMBED_MODEL):
    """HNSW search inside PostgreSQL. Only available when the pgvector extension is installed."""
    with db.connect() as con:
        con.execute(f"SET hnsw.ef_search = {int(ef_search)}")
        rows = con.execute(
            "SELECT chunk_id, 1 - (vector <=> %s::vector) AS score FROM embeddings WHERE model_name = %s ORDER BY vector <=> %s::vector LIMIT %s",
            (v := "[" + ",".join(f"{x:.5f}" for x in qvec) + "]", name, v, k)).fetchall()
    return [r["chunk_id"] for r in rows], [r["score"] for r in rows]


@lru_cache(maxsize=4)
def get(strategy: str = "structure", name: str = config.EMBED_MODEL) -> DenseIndex:
    return DenseIndex(strategy, name)
