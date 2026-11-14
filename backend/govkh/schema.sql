-- GovKH Search physical schema v1 (proposal section 11).
-- The vector column is added by db.init(): `vector(dim)` + HNSW when pgvector is installed,
-- `real[]` otherwise (dense search then runs on the exported matrix, see dense.py).
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS sources (
    source_id       text PRIMARY KEY,
    canonical_url   text NOT NULL,
    alternate_urls  jsonb NOT NULL DEFAULT '[]',
    institution     text NOT NULL,
    doc_type        text NOT NULL,
    doc_number      text NOT NULL DEFAULT '',
    title           text NOT NULL DEFAULT '',
    language        text NOT NULL CHECK (language IN ('km', 'en', 'mixed')),
    issue_date      date,
    effective_date  date,
    status          text NOT NULL DEFAULT 'unknown'
                    CHECK (status IN ('in_force', 'amended', 'repealed', 'unknown')),
    collected_at    timestamptz NOT NULL,
    checksum        char(64) NOT NULL,
    file_type       text NOT NULL,
    reuse_status    text NOT NULL DEFAULT 'unclear',
    storage_uri     text NOT NULL,
    -- extra manifest fields
    site_id         text NOT NULL,
    institution_km  text NOT NULL DEFAULT '',
    doc_subtype     text NOT NULL DEFAULT 'other',
    is_press        boolean NOT NULL DEFAULT false,
    needs_ocr       boolean NOT NULL DEFAULT false,
    text_uri        text NOT NULL DEFAULT '',
    landing_url     text NOT NULL DEFAULT '',
    meta_source     jsonb NOT NULL DEFAULT '{}'   -- where enriched values came from
);
CREATE INDEX IF NOT EXISTS sources_institution ON sources (institution);
CREATE INDEX IF NOT EXISTS sources_doc_type ON sources (doc_type, doc_subtype);
CREATE INDEX IF NOT EXISTS sources_issue_date ON sources (issue_date);
CREATE INDEX IF NOT EXISTS sources_status ON sources (status);
CREATE INDEX IF NOT EXISTS sources_language ON sources (language);
CREATE INDEX IF NOT EXISTS sources_title_trgm ON sources USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS sources_number_trgm ON sources USING gin (doc_number gin_trgm_ops);

CREATE TABLE IF NOT EXISTS documents (
    document_id     bigserial PRIMARY KEY,
    source_id       text NOT NULL UNIQUE REFERENCES sources ON DELETE CASCADE,
    checksum        char(64) NOT NULL,          -- checksum of the file this text was parsed from
    extracted_text  text NOT NULL DEFAULT '',   -- pages separated by form feed (U+000C)
    page_count      int,
    parse_status    text NOT NULL,              -- ok | ocr | pending_ocr | empty | unsupported | error:<why>
    ocr_used        boolean NOT NULL DEFAULT false,
    ocr_confidence  real,
    text_quality    real                        -- share of well-formed Khmer clusters, 0..1
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        bigserial PRIMARY KEY,
    document_id     bigint NOT NULL REFERENCES documents ON DELETE CASCADE,
    parent_chunk_id bigint REFERENCES chunks ON DELETE CASCADE,
    chunk_index     int NOT NULL,
    text            text NOT NULL,
    context_header  text NOT NULL DEFAULT '',
    section_title   text NOT NULL DEFAULT '',
    article_number  text NOT NULL DEFAULT '',
    page_number     int,
    token_count     int NOT NULL,
    chunking_strategy text NOT NULL,            -- structure | fixed
    is_parent       boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks (document_id, chunking_strategy);
CREATE INDEX IF NOT EXISTS chunks_parent ON chunks (parent_chunk_id);

CREATE TABLE IF NOT EXISTS embeddings (
    embedding_id    bigserial PRIMARY KEY,
    chunk_id        bigint NOT NULL REFERENCES chunks ON DELETE CASCADE,
    model_name      text NOT NULL,
    model_version   text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chunk_id, model_name)
);

CREATE TABLE IF NOT EXISTS doc_relations (
    from_source_id  text NOT NULL REFERENCES sources ON DELETE CASCADE,
    to_source_id    text NOT NULL REFERENCES sources ON DELETE CASCADE,
    relation        text NOT NULL CHECK (relation IN ('amends', 'repeals', 'implements', 'references', 'translation_of')),
    evidence        text NOT NULL DEFAULT '',
    PRIMARY KEY (from_source_id, to_source_id, relation)
);
CREATE INDEX IF NOT EXISTS doc_relations_to ON doc_relations (to_source_id);

CREATE TABLE IF NOT EXISTS assets (
    asset_id        bigserial PRIMARY KEY,
    document_id     bigint NOT NULL REFERENCES documents ON DELETE CASCADE,
    asset_type      text NOT NULL,              -- table | form_image
    storage_uri     text NOT NULL DEFAULT '',
    page_number     int,
    extracted_text  text NOT NULL DEFAULT '',
    rows            jsonb
);
CREATE INDEX IF NOT EXISTS assets_doc ON assets (document_id);

CREATE TABLE IF NOT EXISTS queries (
    query_id        text PRIMARY KEY,
    query_text      text NOT NULL,
    language        text NOT NULL,
    query_type      text NOT NULL,
    split           text NOT NULL CHECK (split IN ('dev', 'test')),
    seed_source_id  text,
    notes           text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS relevance_judgments (
    query_id        text NOT NULL REFERENCES queries ON DELETE CASCADE,
    chunk_or_doc_id text NOT NULL,              -- source_id: judgments are at document level
    grade           smallint NOT NULL CHECK (grade BETWEEN 0 AND 3),
    assessor        text NOT NULL,
    labeled_by      text NOT NULL CHECK (labeled_by IN ('human', 'model')),
    notes           text NOT NULL DEFAULT '',
    PRIMARY KEY (query_id, chunk_or_doc_id, assessor)
);

CREATE TABLE IF NOT EXISTS search_runs (
    run_id          text NOT NULL,
    query_id        text NOT NULL,
    method          text NOT NULL,
    rank            int NOT NULL,
    result_id       text NOT NULL,
    score           real NOT NULL,
    latency_ms      real,
    config_version  text NOT NULL,
    PRIMARY KEY (run_id, query_id, method, rank)
);

CREATE TABLE IF NOT EXISTS feedback (
    feedback_id     bigserial PRIMARY KEY,
    query_text      text NOT NULL,
    method          text NOT NULL,
    source_id       text NOT NULL,
    relevant        boolean NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
