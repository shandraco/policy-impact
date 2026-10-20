-- Policy impact: base schema.
-- Run by the one-shot `migrate` service as the owner role. The API connects as pi_app, which does not own
-- these tables, so row-level security always applies to it.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Each request sets these with SET LOCAL (app/db.py):
--   app.entity_ids  comma-separated entity ids the person may see (their entities and everything below)
--   app.role        admin | authority_analyst | entity_manager | viewer | system
CREATE OR REPLACE FUNCTION app_entities() RETURNS text[] LANGUAGE sql STABLE AS $$
  SELECT string_to_array(coalesce(nullif(current_setting('app.entity_ids', true), ''), ''), ',')
$$;
CREATE OR REPLACE FUNCTION app_role() RETURNS text LANGUAGE sql STABLE AS $$
  SELECT coalesce(nullif(current_setting('app.role', true), ''), 'none')
$$;
CREATE OR REPLACE FUNCTION app_sees(entity text, restricted boolean DEFAULT false) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT app_role() = 'system'
      OR (entity = ANY (app_entities())
          AND (NOT restricted OR app_role() IN ('admin', 'authority_analyst')))
$$;

CREATE TABLE entities (
  id         text PRIMARY KEY,               -- folder name under the document store, e.g. "lincoln-elementary"
  name       text NOT NULL,
  kind       text NOT NULL DEFAULT 'entity', -- free label: department, district, school, branch, firm ...
  parent_id  text REFERENCES entities(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE documents (
  id          bigserial PRIMARY KEY,
  entity_id   text NOT NULL REFERENCES entities(id),
  path        text NOT NULL,                 -- relative to the document store
  sha256      text NOT NULL,
  policy_id   text,
  status      text NOT NULL DEFAULT 'ok',    -- ok | unreadable | no_id
  note        text,
  ingested_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (path, sha256)
);

CREATE TABLE policies (
  id         text PRIMARY KEY,               -- e.g. DOE-512
  entity_id  text NOT NULL REFERENCES entities(id),
  title      text NOT NULL,
  restricted boolean NOT NULL DEFAULT false,
  current_version int NOT NULL DEFAULT 1
);

CREATE TABLE policy_versions (
  policy_id  text NOT NULL REFERENCES policies(id),
  version    int  NOT NULL,
  entity_id  text NOT NULL REFERENCES entities(id),
  restricted boolean NOT NULL DEFAULT false,
  sha256     text NOT NULL,
  body       text NOT NULL,
  effective  date,
  source     text NOT NULL DEFAULT 'store',  -- store | draft
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (policy_id, version)
);

CREATE TABLE clauses (
  id         bigserial PRIMARY KEY,
  policy_id  text NOT NULL REFERENCES policies(id),
  version    int  NOT NULL,
  entity_id  text NOT NULL REFERENCES entities(id),
  restricted boolean NOT NULL DEFAULT false,
  number     text NOT NULL,
  heading    text NOT NULL DEFAULT '',
  body       text NOT NULL,
  text_hash  text NOT NULL,                  -- sha256 of normalised text: identical clauses share one judgement
  is_current boolean NOT NULL DEFAULT true,
  embedding  vector(768)
);
CREATE INDEX clauses_current ON clauses (entity_id) WHERE is_current;
CREATE INDEX clauses_policy ON clauses (policy_id, version);
CREATE INDEX clauses_embedding ON clauses USING hnsw (embedding vector_cosine_ops);

CREATE TABLE clause_refs (
  clause_id  bigint NOT NULL REFERENCES clauses(id) ON DELETE CASCADE,
  entity_id  text NOT NULL,
  to_policy  text NOT NULL,
  to_number  text                            -- null = the whole policy
);
CREATE INDEX clause_refs_to ON clause_refs (to_policy);

CREATE TABLE runs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  policy_id     text NOT NULL REFERENCES policies(id),
  entity_id     text NOT NULL REFERENCES entities(id),  -- the authority that owns the policy
  scope_root    text NOT NULL REFERENCES entities(id),  -- check entities below this one
  old_version   int,
  new_version   int,
  requested_by  text NOT NULL,
  chat_id       text,
  status        text NOT NULL DEFAULT 'queued',
    -- queued -> extracting -> awaiting_confirmation -> confirmed -> running -> done | failed
  progress      text NOT NULL DEFAULT 'Waiting to start',
  requirements  jsonb,                       -- extracted, then as confirmed by an analyst
  confirmed_by  text,
  confirmed_at  timestamptz,
  result        jsonb,                       -- counts and table, built by code
  error         text,
  config_hash   text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);

CREATE TABLE findings (
  id             bigserial PRIMARY KEY,
  run_id         uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  entity_id      text NOT NULL REFERENCES entities(id),
  requirement    int  NOT NULL,              -- index into runs.requirements
  result         text NOT NULL,              -- conflict | gap | stale_reference | compliant | unsure
  clause_id      bigint REFERENCES clauses(id),
  why            text NOT NULL DEFAULT '',
  evidence       text NOT NULL DEFAULT '',
  stage          text NOT NULL,              -- rule | judge | escalate | cache | override | reviewer
  model          text,
  cache_key      text,
  review_status  text,                       -- null | pending | done
  reviewed_by    text,
  reviewed_at    timestamptz,
  previous       jsonb                       -- what the reviewer changed, so undo is exact
);
CREATE INDEX findings_run ON findings (run_id);
CREATE INDEX findings_review ON findings (review_status) WHERE review_status = 'pending';

-- Long memory. Keyed by hash of (requirement text, clause text, model, prompt version):
-- an identical question about identical text never goes to a GPU twice.
CREATE TABLE judgement_cache (
  key        text PRIMARY KEY,
  verdict    jsonb NOT NULL,
  model      text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- A person's decision. Same key shape as the cache, and it wins over the cache from then on.
CREATE TABLE overrides (
  key        text PRIMARY KEY,
  result     text NOT NULL,
  note       text NOT NULL DEFAULT '',
  decided_by text NOT NULL,
  decided_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE aliases (
  alias       text PRIMARY KEY,              -- lower case, e.g. "the phone rule"
  policy_id   text NOT NULL REFERENCES policies(id),
  approved_by text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE alias_misses (
  query     text PRIMARY KEY,
  count     int NOT NULL DEFAULT 1,
  last_at   timestamptz NOT NULL DEFAULT now()
);

-- Short memory: one small state block per chat, written only by tool code.
CREATE TABLE chat_state (
  chat_id    text PRIMARY KEY,
  user_id    text NOT NULL,
  state      jsonb NOT NULL DEFAULT '{}',
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Turn budget: tool and model calls per chat turn.
CREATE TABLE turns (
  chat_id     text NOT NULL,
  turn_id     text NOT NULL,
  tool_calls  int NOT NULL DEFAULT 0,
  model_calls int NOT NULL DEFAULT 0,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (chat_id, turn_id)
);

CREATE TABLE audit_log (
  id          bigserial PRIMARY KEY,
  at          timestamptz NOT NULL DEFAULT now(),
  user_id     text NOT NULL,
  role        text NOT NULL,
  action      text NOT NULL,
  detail      jsonb NOT NULL DEFAULT '{}',
  config_hash text
);

CREATE TABLE jobs (
  id           bigserial PRIMARY KEY,
  kind         text NOT NULL,
  payload      jsonb NOT NULL,
  status       text NOT NULL DEFAULT 'pending', -- pending | running | done | dead
  attempts     int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 3,
  run_at       timestamptz NOT NULL DEFAULT now(),
  locked_by    text,
  locked_until timestamptz,
  last_error   text,
  dedupe_key   text
);
CREATE INDEX jobs_due ON jobs (run_at) WHERE status IN ('pending', 'running');
CREATE UNIQUE INDEX jobs_dedupe ON jobs (dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('pending', 'running');

-- Row-level security. Tables without an entity (cache, overrides, turns, jobs, audit) hold no policy text
-- a person could read through a query, or are only touched by system code.
ALTER TABLE entities        ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents       ENABLE ROW LEVEL SECURITY;
ALTER TABLE policies        ENABLE ROW LEVEL SECURITY;
ALTER TABLE policy_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE clauses         ENABLE ROW LEVEL SECURITY;
ALTER TABLE clause_refs     ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs            ENABLE ROW LEVEL SECURITY;
ALTER TABLE findings        ENABLE ROW LEVEL SECURITY;
ALTER TABLE aliases         ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_state      ENABLE ROW LEVEL SECURITY;

CREATE POLICY see ON entities        USING (app_sees(id))                   WITH CHECK (app_role() IN ('system', 'admin'));
CREATE POLICY see ON documents       USING (app_sees(entity_id))            WITH CHECK (app_role() = 'system');
CREATE POLICY see ON policies        USING (app_sees(entity_id, restricted)) WITH CHECK (app_role() = 'system');
CREATE POLICY see ON policy_versions USING (app_sees(entity_id, restricted)) WITH CHECK (app_role() = 'system');
CREATE POLICY see ON clauses         USING (app_sees(entity_id, restricted)) WITH CHECK (app_role() = 'system');
CREATE POLICY see ON clause_refs     USING (app_sees(entity_id))            WITH CHECK (app_role() = 'system');
-- A run is visible to people who can see the authority; its findings to people who can see that entity.
CREATE POLICY see ON runs            USING (app_sees(entity_id))            WITH CHECK (app_sees(entity_id));
CREATE POLICY see ON findings        USING (app_sees(entity_id))            WITH CHECK (app_sees(entity_id));
CREATE POLICY see ON aliases         USING (app_role() <> 'none')           WITH CHECK (app_role() IN ('system', 'admin'));
CREATE POLICY see ON chat_state      USING (app_role() = 'system' OR user_id = current_setting('app.user_id', true))
                                     WITH CHECK (app_role() = 'system' OR user_id = current_setting('app.user_id', true));

-- pi_app is created by db/init/01-app-role.sh with its own password.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pi_app') THEN
    GRANT USAGE ON SCHEMA public TO pi_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO pi_app;
    GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO pi_app;
    REVOKE UPDATE, DELETE ON audit_log FROM pi_app;  -- the audit log is append-only for the app
  END IF;
END $$;
