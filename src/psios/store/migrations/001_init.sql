-- PSI OS schema v1 —— 对应 docs/data-model.md v1.2
-- 由 store.runner 包裹在单事务中执行;成功后 PRAGMA user_version = 1。
-- 约定: 所有时间戳为 ISO8601 TEXT;JSON 列存 json.dumps 文本。

-- 运行记录(被多处外键引用,先建)
CREATE TABLE model_run (
  id              TEXT PRIMARY KEY,
  stages          JSON,
  config_snapshot JSON NOT NULL,
  code_version    TEXT,
  status          TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
  stats           JSON,
  started_at      TEXT NOT NULL,
  finished_at     TEXT
);

-- ── Conversation Schema ────────────────────────────────────────────────

CREATE TABLE source (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  adapter     TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  file_hash   TEXT,
  meta        JSON
);

CREATE TABLE conversation (
  id            TEXT PRIMARY KEY,
  source_id     TEXT NOT NULL REFERENCES source(id),
  external_id   TEXT NOT NULL,
  title         TEXT,
  started_at    TEXT,
  ended_at      TEXT,
  participants  JSON,
  content_hash  TEXT NOT NULL,
  meta          JSON,
  created_at    TEXT NOT NULL,
  UNIQUE (source_id, external_id)
);

CREATE TABLE message (
  id              TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversation(id),
  seq             INTEGER NOT NULL,
  role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'other')),
  content         TEXT NOT NULL,
  ts              TEXT,
  content_hash    TEXT NOT NULL,
  meta            JSON,
  created_at      TEXT NOT NULL,
  UNIQUE (conversation_id, seq)
);

CREATE TABLE chunk (
  id              TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversation(id),
  message_id      TEXT NOT NULL REFERENCES message(id),
  seq             INTEGER NOT NULL,
  text            TEXT NOT NULL,
  token_count     INTEGER,
  embedding       BLOB,
  embedding_model TEXT,
  created_at      TEXT NOT NULL,
  UNIQUE (message_id, seq)
);

-- ── Topic ──────────────────────────────────────────────────────────────

CREATE TABLE topic (
  id               TEXT PRIMARY KEY,
  label            TEXT NOT NULL,
  label_source     TEXT NOT NULL CHECK (label_source IN ('llm', 'keyword', 'human')),
  summary          TEXT,
  keywords         JSON,
  parent_id        TEXT REFERENCES topic(id),
  confidence       REAL,
  inference_method TEXT NOT NULL,
  model_version    TEXT,
  status           TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'merged', 'retired')),
  merged_into_id   TEXT REFERENCES topic(id),
  first_seen       TEXT,
  last_seen        TEXT,
  run_id           TEXT REFERENCES model_run(id),
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE topic_chunk (
  topic_id TEXT NOT NULL REFERENCES topic(id),
  chunk_id TEXT NOT NULL REFERENCES chunk(id),
  weight   REAL DEFAULT 1.0,
  method   TEXT NOT NULL,
  run_id   TEXT REFERENCES model_run(id),
  PRIMARY KEY (topic_id, chunk_id)
);

-- ── Interest ───────────────────────────────────────────────────────────

CREATE TABLE interest (
  topic_id         TEXT PRIMARY KEY REFERENCES topic(id),
  score            REAL,
  scores           JSON,
  raw_stats        JSON,
  interest_type    TEXT CHECK (interest_type IN
                     ('intrinsic', 'professional', 'instrumental', 'temporary', 'unknown')),
  lifecycle        TEXT CHECK (lifecycle IN
                     ('emerging', 'stable', 'declining', 'dormant', 'reactivated', 'temporary')),
  confidence       REAL,
  inference_method TEXT NOT NULL,
  model_version    TEXT,
  first_seen       TEXT,
  last_seen        TEXT,
  computed_at      TEXT NOT NULL,
  run_id           TEXT REFERENCES model_run(id),
  updated_at       TEXT NOT NULL
);

CREATE TABLE meta_interest (
  id                         TEXT PRIMARY KEY,
  label                      TEXT NOT NULL,
  supporting_topic_ids       JSON NOT NULL,
  evidence_count             INTEGER NOT NULL,
  alternative_interpretation TEXT NOT NULL,
  confidence                 REAL,
  inference_method           TEXT NOT NULL,
  model_version              TEXT,
  status                     TEXT NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active', 'rejected', 'superseded')),
  run_id                     TEXT REFERENCES model_run(id),
  created_at                 TEXT NOT NULL,
  updated_at                 TEXT NOT NULL
);

-- ── Goal / Project / Question ──────────────────────────────────────────

CREATE TABLE goal (
  id               TEXT PRIMARY KEY,
  statement        TEXT NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN ('explicit', 'inferred')),
  epistemic_status TEXT NOT NULL CHECK (epistemic_status IN ('USER_STATED', 'INFERRED')),
  status           TEXT NOT NULL DEFAULT 'proposed'
                   CHECK (status IN ('proposed', 'active', 'achieved', 'abandoned', 'stale')),
  confidence       REAL,
  inference_method TEXT NOT NULL,
  model_version    TEXT,
  topic_ids        JSON,
  project_ids      JSON,
  first_seen       TEXT,
  last_seen        TEXT,
  run_id           TEXT REFERENCES model_run(id),
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE project (
  id               TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  description      TEXT,
  status           TEXT NOT NULL DEFAULT 'candidate'
                   CHECK (status IN ('candidate', 'active', 'paused', 'completed', 'abandoned')),
  confidence       REAL,
  inference_method TEXT NOT NULL,
  model_version    TEXT,
  topic_ids        JSON,
  goal_ids         JSON,
  first_seen       TEXT,
  last_seen        TEXT,
  run_id           TEXT REFERENCES model_run(id),
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE question (
  id              TEXT PRIMARY KEY,
  normalized_text TEXT NOT NULL,
  display_text    TEXT NOT NULL,
  count           INTEGER NOT NULL DEFAULT 1,
  distinct_weeks  INTEGER NOT NULL DEFAULT 1,
  status          TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'recurring', 'resolved')),
  confidence      REAL,
  first_seen      TEXT,
  last_seen       TEXT,
  topic_ids       JSON,
  run_id          TEXT REFERENCES model_run(id),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE question_occurrence (
  id          TEXT PRIMARY KEY,
  question_id TEXT NOT NULL REFERENCES question(id),
  message_id  TEXT NOT NULL REFERENCES message(id),
  quote       TEXT NOT NULL,
  ts          TEXT,
  created_at  TEXT NOT NULL
);

-- ── Evidence / History / Snapshot / Human Action ───────────────────────

CREATE TABLE evidence (
  id           TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL CHECK (subject_type IN
               ('topic', 'interest', 'goal', 'project', 'question', 'meta_interest')),
  subject_id   TEXT NOT NULL,
  message_id   TEXT REFERENCES message(id),
  chunk_id     TEXT REFERENCES chunk(id),
  quote        TEXT NOT NULL,
  span         JSON,
  signal_type  TEXT,
  weight       REAL DEFAULT 1.0,
  run_id       TEXT REFERENCES model_run(id),
  created_at   TEXT NOT NULL
);

CREATE TABLE entity_revision (
  id             TEXT PRIMARY KEY,
  entity_type    TEXT NOT NULL,
  entity_id      TEXT NOT NULL,
  previous_state JSON,
  new_state      JSON NOT NULL,
  reason         TEXT NOT NULL,
  new_evidence   JSON,
  run_id         TEXT REFERENCES model_run(id),
  created_at     TEXT NOT NULL
);

CREATE TABLE personal_model_snapshot (
  run_id       TEXT PRIMARY KEY REFERENCES model_run(id),
  generated_at TEXT NOT NULL,
  payload      JSON NOT NULL
);

CREATE TABLE human_action (
  id          TEXT PRIMARY KEY,
  action_type TEXT NOT NULL CHECK (action_type IN
              ('merge_topics', 'reject_meta_interest', 'edit_label',
               'mute_interest', 'unmute_interest', 'confirm_entity')),
  target_type TEXT NOT NULL,
  target_id   TEXT NOT NULL,
  payload     JSON,
  note        TEXT,
  created_at  TEXT NOT NULL
);

-- ── 索引(UNIQUE 约束已覆盖的主查询路径不重复建)────────────────────────

CREATE INDEX idx_chunk_conversation ON chunk(conversation_id);
CREATE INDEX idx_topic_chunk_chunk  ON topic_chunk(chunk_id);
CREATE INDEX idx_evidence_subject   ON evidence(subject_type, subject_id);
CREATE INDEX idx_evidence_message   ON evidence(message_id);
CREATE INDEX idx_question_question  ON question_occurrence(question_id);
CREATE INDEX idx_revision_entity    ON entity_revision(entity_type, entity_id, created_at);

-- ── 视图 ───────────────────────────────────────────────────────────────

CREATE VIEW v_topic_weekly AS        -- 兴趣时间线的基础(时间取自所属 message)
  SELECT t.id AS topic_id, strftime('%Y-%W', m.ts) AS week,
         COUNT(*) AS chunk_count, SUM(ch.token_count) AS tokens
  FROM topic_chunk tc
  JOIN chunk ch ON ch.id = tc.chunk_id
  JOIN topic t ON t.id = tc.topic_id
  JOIN message m ON m.id = ch.message_id
  WHERE m.ts IS NOT NULL
  GROUP BY t.id, week;

CREATE VIEW v_interest_current AS    -- 过滤 merged/retired topic 与 muted 后的当前兴趣
  SELECT i.*, t.label, t.status AS topic_status
  FROM interest i JOIN topic t ON t.id = i.topic_id
  WHERE t.status = 'active'
    AND NOT EXISTS (
      SELECT 1 FROM human_action h
      WHERE h.action_type = 'mute_interest'
        AND h.target_id = i.topic_id
        AND NOT EXISTS (
          SELECT 1 FROM human_action u
          WHERE u.action_type = 'unmute_interest'
            AND u.target_id = i.topic_id
            AND u.created_at > h.created_at));
