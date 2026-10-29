
CREATE TABLE IF NOT EXISTS accounts(owner TEXT PRIMARY KEY, epoch INTEGER NOT NULL, active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tombstones(owner TEXT, recording TEXT, PRIMARY KEY(owner,recording));
CREATE TABLE IF NOT EXISTS sources(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, recording TEXT NOT NULL, revision TEXT NOT NULL,
 transcript_version TEXT NOT NULL, content_hash TEXT NOT NULL, raw TEXT NOT NULL, metadata TEXT NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(owner,recording,revision,transcript_version));
CREATE INDEX IF NOT EXISTS source_owner ON sources(owner,state,recording);
CREATE TABLE IF NOT EXISTS segments(
 source_id TEXT, ref TEXT, payload TEXT NOT NULL, PRIMARY KEY(source_id,ref));
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, operation_key TEXT NOT NULL, source_id TEXT NOT NULL,
 request_hash TEXT NOT NULL, epoch INTEGER NOT NULL, status TEXT NOT NULL, next_batch INTEGER NOT NULL,
 settings TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(owner,operation_key));
CREATE TABLE IF NOT EXISTS events(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, source_id TEXT NOT NULL, job_id TEXT NOT NULL,
 version INTEGER NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL, refs TEXT NOT NULL,
 claims TEXT NOT NULL, relations TEXT NOT NULL, quality TEXT NOT NULL,
 association_revision INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS event_owner ON events(owner,source_id,job_id);
CREATE TABLE IF NOT EXISTS topics(id TEXT PRIMARY KEY,owner TEXT NOT NULL,name TEXT NOT NULL,
 normalized TEXT NOT NULL, UNIQUE(owner,normalized));
CREATE TABLE IF NOT EXISTS associations(event_id TEXT,topic_id TEXT,origin TEXT NOT NULL,
 PRIMARY KEY(event_id,topic_id));
CREATE TABLE IF NOT EXISTS corrections(id TEXT PRIMARY KEY,owner TEXT,event_id TEXT,revision INTEGER,
 before_topics TEXT,after_topics TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS unclassified(job_id TEXT,ref TEXT,reason TEXT,
 PRIMARY KEY(job_id,ref));
CREATE TABLE IF NOT EXISTS batch_results(job_id TEXT,batch_index INTEGER,payload TEXT,
 PRIMARY KEY(job_id,batch_index));
CREATE TABLE IF NOT EXISTS selections(id TEXT PRIMARY KEY,owner TEXT,epoch INTEGER,payload TEXT,created_at TEXT);
