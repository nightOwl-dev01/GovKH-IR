"""PostgreSQL access: connection, schema, and the pgvector switch."""
from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from . import config


def connect(**kw) -> psycopg.Connection:
    return psycopg.connect(config.DSN, row_factory=dict_row, **kw)


def has_pgvector(con) -> bool:
    return con.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone() is not None


def init(dim: int | None = None) -> bool:
    """Create the schema. Returns True when the vector column is a pgvector column."""
    with connect() as con:
        con.execute(Path(__file__).with_name("schema.sql").read_text())
        try:
            with con.transaction():
                con.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except psycopg.Error:
            pass  # extension not installed on this server
        vec = has_pgvector(con)
        col = con.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name='embeddings' AND column_name='vector'"
        ).fetchone()
        if not col and dim:
            con.execute(f"ALTER TABLE embeddings ADD COLUMN vector {'vector(%d)' % dim if vec else 'real[]'}")
            if vec:
                # cosine distance operator class matches the normalised embeddings (proposal 9, 11)
                con.execute(
                    "CREATE INDEX IF NOT EXISTS embeddings_hnsw ON embeddings USING hnsw (vector vector_cosine_ops) "
                    "WITH (m = 16, ef_construction = 64)"
                )
        return vec
