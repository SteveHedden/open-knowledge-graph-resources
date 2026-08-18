CREATE TABLE IF NOT EXISTS vector_generations (
  generation_id TEXT PRIMARY KEY,
  namespace TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('seeding', 'ready', 'failed', 'retiring', 'retired')),
  vector_count INTEGER NOT NULL DEFAULT 0,
  ontology_count INTEGER NOT NULL DEFAULT 0,
  software_count INTEGER NOT NULL DEFAULT 0,
  mutation_ids TEXT NOT NULL DEFAULT '[]',
  retirement_mutation_ids TEXT NOT NULL DEFAULT '[]',
  started_at TEXT NOT NULL,
  verified_at TEXT,
  retired_at TEXT,
  failure_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_vector_generations_ready
  ON vector_generations (status, verified_at DESC);
