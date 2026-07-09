-- Create Keycloak database (ekap db is created by POSTGRES_DB env var)
SELECT 'CREATE DATABASE keycloak'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak')\gexec

\c ekap;

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Documents
CREATE TABLE IF NOT EXISTS documents (
    document_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title          TEXT NOT NULL,
    owner          TEXT NOT NULL,
    department     TEXT,
    file_type      TEXT,
    file_path      TEXT,
    page_count     INTEGER,
    version        TEXT DEFAULT '1.0',
    classification TEXT DEFAULT 'Internal'
                       CHECK (classification IN ('Public','Internal','Confidential','Restricted')),
    tags           TEXT[] DEFAULT '{}',
    status         TEXT DEFAULT 'pending'
                       CHECK (status IN ('pending','processing','completed','failed')),
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Sections (Phase 4: hierarchical retrieval index)
CREATE TABLE IF NOT EXISTS sections (
    section_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    section_title   TEXT,
    summary         TEXT NOT NULL,
    start_chunk_idx INTEGER NOT NULL,
    end_chunk_idx   INTEGER NOT NULL,
    qdrant_point_id UUID
);
CREATE INDEX IF NOT EXISTS idx_sections_document_id ON sections(document_id);

-- Chunks (one row per text chunk; qdrant_point_id links to vector store)
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    section_id      UUID REFERENCES sections(section_id) ON DELETE SET NULL,
    chunk_index     INTEGER NOT NULL,
    content         TEXT NOT NULL,
    section         TEXT,
    chapter         TEXT,
    page_number     INTEGER,
    qdrant_point_id UUID,
    -- BM25 / full-text search: auto-computed, never set manually
    content_fts     TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED
);

-- Conversations
CREATE TABLE IF NOT EXISTS conversations (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    TEXT NOT NULL,
    title      TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Messages
CREATE TABLE IF NOT EXISTS messages (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id   UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role              TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content           TEXT NOT NULL,
    model_used        TEXT,
    latency_ms        INTEGER,
    retrieved_sources JSONB DEFAULT '[]',
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Document-level permissions
CREATE TABLE IF NOT EXISTS permissions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL,
    document_id UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    can_read    BOOLEAN DEFAULT TRUE,
    granted_by  TEXT,
    granted_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, document_id)
);

-- Audit log (append-only)
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    timestamp  TIMESTAMPTZ DEFAULT NOW(),
    user_id    TEXT,
    event_type TEXT NOT NULL,
    resource_id TEXT,
    details    JSONB DEFAULT '{}'
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_documents_department      ON documents(department);
CREATE INDEX IF NOT EXISTS idx_documents_classification  ON documents(classification);
CREATE INDEX IF NOT EXISTS idx_documents_status          ON documents(status);
CREATE INDEX IF NOT EXISTS idx_chunks_document_id        ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_fts                ON chunks USING GIN(content_fts);
CREATE INDEX IF NOT EXISTS idx_conversations_user_id     ON conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_id  ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_permissions_user_id       ON permissions(user_id);
CREATE INDEX IF NOT EXISTS idx_permissions_document_id   ON permissions(document_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp       ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_log_user_id         ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_event_type      ON audit_log(event_type);

-- Safe migration: Phase 3 — FTS column
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'chunks' AND column_name = 'content_fts'
    ) THEN
        ALTER TABLE chunks
            ADD COLUMN content_fts TSVECTOR
            GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED;
        CREATE INDEX idx_chunks_fts ON chunks USING GIN(content_fts);
    END IF;
END $$;

-- Safe migration: Phase 4 — sections table + section_id FK on chunks
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'sections') THEN
        CREATE TABLE sections (
            section_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id     UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
            section_title   TEXT,
            summary         TEXT NOT NULL,
            start_chunk_idx INTEGER NOT NULL,
            end_chunk_idx   INTEGER NOT NULL,
            qdrant_point_id UUID
        );
        CREATE INDEX idx_sections_document_id ON sections(document_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'chunks' AND column_name = 'section_id'
    ) THEN
        ALTER TABLE chunks ADD COLUMN section_id UUID;
    END IF;
END $$;

-- ── Phase 7: Document Management System ──────────────────────────────────────

-- Folder hierarchy
CREATE TABLE IF NOT EXISTS folders (
    folder_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             TEXT NOT NULL,
    parent_folder_id UUID REFERENCES folders(folder_id) ON DELETE CASCADE,
    owner            TEXT NOT NULL,
    department       TEXT DEFAULT '',
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_folder_id);

-- Document versions
CREATE TABLE IF NOT EXISTS document_versions (
    version_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id    UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    file_path      TEXT NOT NULL,
    file_type      TEXT NOT NULL,
    file_size      BIGINT,
    uploaded_by    TEXT,
    change_note    TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(document_id, version_number)
);
CREATE INDEX IF NOT EXISTS idx_document_versions_doc ON document_versions(document_id);

-- Changes made by someone other than a document's owner (classification,
-- folder move, delete, legal hold, new version) are staged here instead of
-- applying immediately, and only take effect once the owner (or an
-- administrator) approves them. A change made by the owner themselves never
-- goes through this table — see can_apply_immediately() in dms.py.
CREATE TABLE IF NOT EXISTS pending_changes (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id      UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    change_type      TEXT NOT NULL CHECK (change_type IN
                          ('classification', 'folder_move', 'delete', 'legal_hold_set', 'legal_hold_release', 'new_version')),
    payload          JSONB NOT NULL DEFAULT '{}',
    requested_by     TEXT NOT NULL,
    requested_by_id  TEXT NOT NULL,
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by       TEXT,
    decided_at       TIMESTAMPTZ,
    decision_comment TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_changes_document ON pending_changes(document_id);
CREATE INDEX IF NOT EXISTS idx_pending_changes_pending  ON pending_changes(status) WHERE status = 'pending';

-- Workflow audit trail
CREATE TABLE IF NOT EXISTS workflow_transitions (
    transition_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    from_state    TEXT,
    to_state      TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    username      TEXT,
    comment       TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_workflow_transitions_doc ON workflow_transitions(document_id);

-- In-app notifications. user_id = NULL means "broadcast to anyone who can
-- manage documents" (e.g. new item in the approval queue); a specific user_id
-- targets that user directly (e.g. "your document was approved").
CREATE TABLE IF NOT EXISTS notifications (
    notification_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT,
    event_type      TEXT NOT NULL,
    document_id     UUID REFERENCES documents(document_id) ON DELETE CASCADE,
    message         TEXT NOT NULL,
    read_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notifications_user   ON notifications(user_id);
CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(user_id) WHERE read_at IS NULL;

-- Safe migration: add DMS columns to documents
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='documents' AND column_name='folder_id') THEN
        ALTER TABLE documents ADD COLUMN folder_id UUID REFERENCES folders(folder_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='documents' AND column_name='lifecycle_state') THEN
        ALTER TABLE documents ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'draft';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='documents' AND column_name='deleted_at') THEN
        ALTER TABLE documents ADD COLUMN deleted_at TIMESTAMPTZ;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='documents' AND column_name='current_version') THEN
        ALTER TABLE documents ADD COLUMN current_version INTEGER NOT NULL DEFAULT 1;
    END IF;
END $$;

-- Promote existing completed documents so they stay retrievable
UPDATE documents
SET lifecycle_state = 'published'
WHERE status = 'completed' AND lifecycle_state = 'draft';

CREATE INDEX IF NOT EXISTS idx_documents_lifecycle ON documents(lifecycle_state) WHERE deleted_at IS NULL;

-- Safe migration: Phase 8 — processing progress tracking
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='documents' AND column_name='processing_stage') THEN
        ALTER TABLE documents ADD COLUMN processing_stage TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='documents' AND column_name='processing_started_at') THEN
        ALTER TABLE documents ADD COLUMN processing_started_at TIMESTAMPTZ;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_documents_deleted   ON documents(deleted_at)       WHERE deleted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_documents_folder    ON documents(folder_id)        WHERE folder_id  IS NOT NULL;

-- Safe migration: upload duplicate detection (content hash of the current file)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='documents' AND column_name='content_hash') THEN
        ALTER TABLE documents ADD COLUMN content_hash TEXT;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON documents(content_hash) WHERE deleted_at IS NULL;

-- Safe migration: retention policy + legal hold
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='documents' AND column_name='retention_until') THEN
        ALTER TABLE documents ADD COLUMN retention_until TIMESTAMPTZ;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='documents' AND column_name='legal_hold') THEN
        ALTER TABLE documents ADD COLUMN legal_hold BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='documents' AND column_name='legal_hold_reason') THEN
        ALTER TABLE documents ADD COLUMN legal_hold_reason TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='documents' AND column_name='legal_hold_set_by') THEN
        ALTER TABLE documents ADD COLUMN legal_hold_set_by TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='documents' AND column_name='legal_hold_set_at') THEN
        ALTER TABLE documents ADD COLUMN legal_hold_set_at TIMESTAMPTZ;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_documents_retention ON documents(retention_until)
    WHERE retention_until IS NOT NULL AND legal_hold = FALSE AND deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS retention_policies (
    classification TEXT PRIMARY KEY,
    retention_days INTEGER NOT NULL,
    action         TEXT NOT NULL DEFAULT 'archive' CHECK (action IN ('archive', 'delete')),
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);
INSERT INTO retention_policies (classification, retention_days, action) VALUES
    ('Public',       1095, 'archive'),  -- 3 years
    ('Internal',     1825, 'archive'),  -- 5 years
    ('Confidential', 2555, 'archive'),  -- 7 years
    ('Restricted',   3650, 'archive')   -- 10 years
ON CONFLICT (classification) DO NOTHING;

-- Safe migration: tamper-evident audit log (hash chain)
-- Each row's hash covers its own content plus the previous row's hash, so
-- editing or deleting any row (or reordering them) breaks the chain from that
-- point on — verifiable via a SQL recompute, see /api/audit-log/verify.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='audit_log' AND column_name='prev_hash') THEN
        ALTER TABLE audit_log ADD COLUMN prev_hash TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='audit_log' AND column_name='row_hash') THEN
        ALTER TABLE audit_log ADD COLUMN row_hash TEXT;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION audit_log_hash_chain() RETURNS TRIGGER AS $$
DECLARE
    prev TEXT;
BEGIN
    SELECT row_hash INTO prev FROM audit_log ORDER BY id DESC LIMIT 1;
    NEW.prev_hash := COALESCE(prev, '');
    NEW.row_hash := encode(
        digest(
            COALESCE(NEW.prev_hash, '') || '|' ||
            COALESCE(NEW.user_id, '')   || '|' ||
            NEW.event_type              || '|' ||
            COALESCE(NEW.resource_id, '') || '|' ||
            COALESCE(NEW.details::text, '{}') || '|' ||
            NEW.timestamp::text,
            'sha256'
        ),
        'hex'
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_log_hash_chain ON audit_log;
CREATE TRIGGER trg_audit_log_hash_chain
    BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_hash_chain();

-- Backfill the chain for rows written before this migration existed (safe to
-- rerun — deterministic given unchanged data, so it reproduces the same hashes).
DO $$
DECLARE
    r RECORD;
    prev TEXT := '';
BEGIN
    IF EXISTS (SELECT 1 FROM audit_log WHERE row_hash IS NULL) THEN
        FOR r IN SELECT * FROM audit_log ORDER BY id LOOP
            UPDATE audit_log SET
                prev_hash = prev,
                row_hash = encode(
                    digest(
                        COALESCE(prev, '') || '|' ||
                        COALESCE(r.user_id, '') || '|' ||
                        r.event_type || '|' ||
                        COALESCE(r.resource_id, '') || '|' ||
                        COALESCE(r.details::text, '{}') || '|' ||
                        r.timestamp::text,
                        'sha256'
                    ),
                    'hex'
                )
            WHERE id = r.id;
            SELECT row_hash INTO prev FROM audit_log WHERE id = r.id;
        END LOOP;
    END IF;
END $$;

-- Admin-submitted output of scripts/gpu-setup-check.sh (run manually on the
-- real Docker host, since containers can't see host GPU/OS — see admin-ui's
-- "Hardware & GPU Setup" page).
CREATE TABLE IF NOT EXISTS gpu_diagnostics (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submitted_by  TEXT NOT NULL,
    submitted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    output        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gpu_diagnostics_submitted_at ON gpu_diagnostics(submitted_at DESC);

-- Admin-added "Pull New Model" chips for models outside the built-in curated
-- list (see admin-ui's POPULAR_MODELS) — download sizes are resolved from the
-- Ollama registry once, at add time, and cached here rather than re-fetched
-- on every Settings page load.
CREATE TABLE IF NOT EXISTS custom_models (
    name        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    note        TEXT,
    quant_sizes JSONB NOT NULL DEFAULT '{}',
    added_by    TEXT NOT NULL,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Which chat backend is live and, when it's an external provider, which
-- classifications may be sent to it as retrieval context. Singleton row
-- (id is always 1) — unlike the in-memory "Active Model" override, this is
-- a compliance-relevant setting so it's persisted and survives restarts;
-- defaults to 'local' / Public-only so a fresh deployment never sends
-- anything externally until an admin explicitly opts in.
CREATE TABLE IF NOT EXISTS external_llm_settings (
    id                      INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    backend                 TEXT NOT NULL DEFAULT 'local'
                                CHECK (backend IN ('local','openai','anthropic','deepseek','google','grok')),
    model                   TEXT,
    allowed_classifications TEXT[] NOT NULL DEFAULT ARRAY['Public'],
    updated_by              TEXT,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Encrypted API keys for external LLM providers. Encrypted at the application
-- layer (Fernet, keyed by SECRETS_ENCRYPTION_KEY — never stored in this
-- table) before insert, so a DB dump/backup alone never exposes a usable key.
CREATE TABLE IF NOT EXISTS external_llm_credentials (
    provider    TEXT PRIMARY KEY CHECK (provider IN ('openai','anthropic','deepseek','google','grok')),
    api_key_enc TEXT NOT NULL,
    added_by    TEXT NOT NULL,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Company/organization name shown in the admin sidebar and the Employee
-- Portal topbar — singleton row, admin-configurable, defaults to "EKAP" so a
-- fresh deployment still shows something before an admin sets it.
CREATE TABLE IF NOT EXISTS app_branding (
    id           INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    company_name TEXT NOT NULL DEFAULT 'EKAP',
    updated_by   TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
