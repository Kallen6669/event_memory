"""本地单一事实库；账号、来源、作业和提交检查使用同连接短事务。"""

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import MemoryError
from .transcript import parse_transcript
from .migrations import migrate_business


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def fingerprint(value) -> str:
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def merge_unique(previous: list, additions: list) -> list:
    """保留先后顺序，避免跨批重放把同一条元信息反复追加。"""
    result, seen = [], set()
    for value in previous + additions:
        key = dumps(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(owner TEXT PRIMARY KEY, epoch INTEGER NOT NULL, active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS tombstones(owner TEXT, recording TEXT, PRIMARY KEY(owner,recording));
CREATE TABLE IF NOT EXISTS sources(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES accounts(owner), recording TEXT NOT NULL, revision TEXT NOT NULL,
 transcript_version TEXT NOT NULL, content_hash TEXT NOT NULL, raw TEXT NOT NULL, metadata TEXT NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(owner,recording,revision,transcript_version));
CREATE INDEX IF NOT EXISTS source_owner ON sources(owner,state,recording);
CREATE TABLE IF NOT EXISTS segments(
 source_id TEXT REFERENCES sources(id), ref TEXT, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
 speakers TEXT NOT NULL, line INTEGER NOT NULL, char_start INTEGER NOT NULL, char_end INTEGER NOT NULL,
 kind TEXT NOT NULL, PRIMARY KEY(source_id,ref));
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, operation_key TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES sources(id),
 epoch INTEGER NOT NULL, status TEXT NOT NULL, next_batch INTEGER NOT NULL,
 settings TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(owner,operation_key));
CREATE TABLE IF NOT EXISTS events(
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
 version INTEGER NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL, refs TEXT NOT NULL,
 claims TEXT NOT NULL, relations TEXT NOT NULL, quality TEXT NOT NULL,
 association_revision INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS event_job ON events(job_id);
CREATE INDEX IF NOT EXISTS job_source_owner ON jobs(owner,source_id,status);
CREATE VIEW IF NOT EXISTS event_details AS SELECT e.*,j.owner,j.source_id FROM events e JOIN jobs j ON j.id=e.job_id;
CREATE TABLE IF NOT EXISTS topics(id TEXT PRIMARY KEY,owner TEXT NOT NULL,name TEXT NOT NULL,
 normalized TEXT NOT NULL, UNIQUE(owner,normalized));
CREATE TABLE IF NOT EXISTS associations(event_id TEXT REFERENCES events(id),topic_id TEXT REFERENCES topics(id),origin TEXT NOT NULL,
 PRIMARY KEY(event_id,topic_id));
CREATE TABLE IF NOT EXISTS corrections(id TEXT PRIMARY KEY,event_id TEXT REFERENCES events(id),revision INTEGER,
 before_topics TEXT,after_topics TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS unclassified(job_id TEXT REFERENCES jobs(id),ref TEXT,reason TEXT,
 PRIMARY KEY(job_id,ref));
CREATE TABLE IF NOT EXISTS batch_results(job_id TEXT REFERENCES jobs(id),batch_index INTEGER,result_hash TEXT NOT NULL,warnings TEXT NOT NULL,
 PRIMARY KEY(job_id,batch_index));
CREATE TABLE IF NOT EXISTS selections(id TEXT PRIMARY KEY,owner TEXT,epoch INTEGER,payload TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS batch_inputs(job_id TEXT NOT NULL REFERENCES jobs(id),batch_index INTEGER NOT NULL,
 context TEXT,PRIMARY KEY(job_id,batch_index));
CREATE TABLE IF NOT EXISTS input_dependencies(job_id TEXT NOT NULL REFERENCES jobs(id),batch_index INTEGER NOT NULL,
 source_id TEXT NOT NULL,event_id TEXT NOT NULL,parent_job_id TEXT NOT NULL,event_version INTEGER NOT NULL,
 association_revision INTEGER NOT NULL,source_hash TEXT NOT NULL,epoch INTEGER NOT NULL,
 PRIMARY KEY(job_id,batch_index,source_id,event_id));
CREATE INDEX IF NOT EXISTS dependency_parent ON input_dependencies(parent_job_id,job_id);
CREATE TABLE IF NOT EXISTS content_purges(job_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS operation_receipts(owner TEXT NOT NULL,operation_id TEXT NOT NULL,
 kind TEXT NOT NULL,request_hash TEXT NOT NULL,epoch INTEGER NOT NULL,event_ids TEXT NOT NULL,
 result TEXT,created_at TEXT NOT NULL,PRIMARY KEY(owner,operation_id));
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        try:
            self.ensure_schema()
        except BaseException:
            self.db.close()
            raise

    def ensure_schema(self):
        migrate_business(self.db, self.path, SCHEMA)
        self.db.execute("PRAGMA foreign_keys=ON")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def account(self, owner: str) -> dict:
        if not owner or len(owner) > 120:
            raise MemoryError("invalid_owner", "本地账号标识不能为空或超过 120 字符。")
        self.db.execute("INSERT OR IGNORE INTO accounts VALUES(?,0,1)", (owner,))
        return dict(self.db.execute("SELECT * FROM accounts WHERE owner=?", (owner,)).fetchone())

    def authorize(self, owner: str, epoch: int | None = None):
        row = self.account(owner)
        if not row["active"] or epoch is not None and epoch != row["epoch"]:
            raise MemoryError("permission_denied", "当前账号已撤权或操作代次过时。")
        return row

    def set_access(self, owner: str, active: bool):
        with self.transaction():
            self.account(owner)
            self.db.execute("UPDATE accounts SET epoch=epoch+1,active=? WHERE owner=?", (int(active), owner))
            if not active:
                ids = [r[0] for r in self.db.execute("SELECT id FROM jobs WHERE owner=?", (owner,))]
                self._invalidate_jobs(ids, clear=True)
                self.db.execute("UPDATE operation_receipts SET result=NULL WHERE owner=?", (owner,))
                self.db.execute("DELETE FROM topics WHERE owner=? AND id NOT IN (SELECT topic_id FROM associations)", (owner,))

    def import_source(self, owner: str, raw: str, recording: str,
                      revision: str | None = None, transcript_version: str = "1",
                      metadata: dict | None = None) -> dict:
        parsed = parse_transcript(raw)
        revision = revision or parsed.content_hash[:16]
        if not recording or not revision or not transcript_version:
            raise MemoryError("invalid_source", "录音、来源修订和转写版本均不能为空。")
        source_id = fingerprint([owner, recording, revision, transcript_version])[:32]
        meta = {**(metadata or {}), "candidate": parsed.candidate,
                "mode": "local_experiment", "formal_publication_verified": False}
        with self.transaction():
            self.authorize(owner)
            if self.db.execute("SELECT 1 FROM tombstones WHERE owner=? AND recording=?", (owner, recording)).fetchone():
                raise MemoryError("cleared", "该录音已清除，不允许通过新修订重新摄入。")
            existing = self.db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if existing:
                if existing["content_hash"] != parsed.content_hash:
                    raise MemoryError("source_conflict", "相同来源版本对应不同原文，请明确新的来源修订。")
                if existing["state"] != "current":
                    raise MemoryError("stale_source", "旧修订不可重新变成当前来源。")
                return dict(existing)
            old_jobs = [r[0] for r in self.db.execute("SELECT j.id FROM jobs j JOIN sources s ON s.id=j.source_id WHERE s.owner=? AND s.recording=? AND s.state='current'", (owner, recording))]
            if old_jobs:
                old_jobs.extend(r[0] for r in self.db.execute("SELECT id FROM jobs WHERE owner=? AND id NOT IN (SELECT job_id FROM input_dependencies WHERE batch_index=-1)", (owner,)))
            self._invalidate_jobs(self._dependent_jobs(old_jobs))
            self.db.execute("UPDATE sources SET state='stale' WHERE owner=? AND recording=? AND state='current'", (owner, recording))
            self.db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?)", (
                source_id, owner, recording, revision, transcript_version, parsed.content_hash,
                raw, dumps(meta), "current", now()))
            self.db.executemany("INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?)", (
                (source_id, s.ref, s.start_ms, s.end_ms, dumps(s.speakers), s.line, s.char_start, s.char_end, s.kind) for s in parsed.segments))
        return self.source(owner, source_id)

    def source(self, owner: str, source_id: str, require_current: bool = True) -> dict:
        self.authorize(owner)
        row = self.db.execute("SELECT * FROM sources WHERE id=? AND owner=?", (source_id, owner)).fetchone()
        if not row:
            raise MemoryError("not_found", "来源不存在或不属于当前账号。")
        if row["state"] == "cleared":
            raise MemoryError("cleared", "来源已清除。")
        if require_current and row["state"] != "current":
            raise MemoryError("stale_source", "来源已被新修订替代。")
        return dict(row)

    def evidence(self, owner: str, source_id: str, refs: list[str] | None = None) -> list[dict]:
        source = self.source(owner, source_id)
        if refs is None:
            rows = self.db.execute("SELECT * FROM segments WHERE source_id=? ORDER BY ref", (source_id,)).fetchall()
        else:
            rows = []
            for start in range(0, len(refs), 500):
                group = refs[start:start+500]
                rows.extend(self.db.execute("SELECT * FROM segments WHERE source_id=? AND ref IN ("+",".join("?" for _ in group)+")", [source_id,*group]))
        values = {}
        for row in rows:
            value = {k: row[k] for k in ("ref","start_ms","end_ms","line","char_start","char_end","kind")}
            value["speakers"] = json.loads(row["speakers"])
            value["text"] = source["raw"][row["char_start"]:row["char_end"]]
            values[row["ref"]] = value
        if refs is None:
            return list(values.values())
        if any(ref not in values for ref in refs):
            raise MemoryError("invalid_reference", "原文引用不存在于指定来源版本。")
        return [values[ref] for ref in refs]

    @staticmethod
    def _job_dict(row):
        result = dict(row)
        result["request_hash"] = fingerprint([result["source_id"],json.loads(result["settings"])])
        return result

    def create_job(self, owner: str, source_id: str, operation_key: str, settings: dict) -> dict:
        if not isinstance(source_id, str) or not isinstance(operation_key, str) or not operation_key or len(operation_key) > 200:
            raise MemoryError("invalid_operation", "来源及操作标识须为有效文本，操作标识最多 200 字符。")
        request_hash = fingerprint([source_id, settings])
        with self.transaction():
            account = self.authorize(owner)
            self.source(owner, source_id)
            row = self.db.execute("SELECT * FROM jobs WHERE owner=? AND operation_key=?", (owner, operation_key)).fetchone()
            if row:
                if self._job_dict(row)["request_hash"] != request_hash:
                    raise MemoryError("idempotency_conflict", "同一操作标识不可用于不同输入或模型参数。")
                return self._job_dict(row)
            is_refinement = settings.get("kind") == "salient_refinement"
            if is_refinement:
                parent = self.job(owner, settings["parent_job_id"])
                if parent["status"] != "succeeded" or parent["source_id"] != source_id:
                    raise MemoryError("invalid_refinement", "只能追加复核同来源已完成作业中的明确未归类片段。")
                unclassified = {r[0] for r in self.db.execute("SELECT ref FROM unclassified WHERE job_id=?", (parent["id"],))}
                if not set(settings["primary_refs"]).issubset(unclassified):
                    raise MemoryError("invalid_refinement", "追加复核范围已变化或包含已解释片段。")
            # 已成功经历不通用重解释；补充作业仅解释此前明确未归类的证据。
            row = None if is_refinement else self.db.execute("SELECT * FROM jobs WHERE owner=? AND source_id=? AND status='succeeded' ORDER BY created_at LIMIT 1", (owner, source_id)).fetchone()
            if row:
                return self._job_dict(row)
            pending = None if is_refinement else self.db.execute("SELECT * FROM jobs WHERE owner=? AND source_id=? AND status IN ('queued','running','unknown','failed') ORDER BY created_at LIMIT 1", (owner, source_id)).fetchone()
            if pending:
                if self._job_dict(pending)["request_hash"] != request_hash:
                    raise MemoryError("operation_conflict", "该来源已有未完成操作；不能更换参数绕过原调用核对。")
                return self._job_dict(pending)
            job_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                job_id, owner, operation_key, source_id, account["epoch"],
                "queued", 0, dumps(settings), None, now(), now()))
            source = self.source(owner, source_id)
            self.db.execute("INSERT INTO input_dependencies VALUES(?,?,?,?,?,?,?,?,?)",
                            (job_id, -1, source_id, "", job_id, 0, 0, source["content_hash"], account["epoch"]))
        return self.job(owner, job_id)

    def job(self, owner: str, job_id: str) -> dict:
        self.authorize(owner)
        row = self.db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
        if not row:
            raise MemoryError("not_found", "作业不存在或不属于当前账号。")
        return self._job_dict(row)

    def check_job(self, owner: str, job_id: str) -> dict:
        job = self.job(owner, job_id)
        self.authorize(owner, job["epoch"])
        self.source(owner, job["source_id"])
        if job["status"] == "cancelled":
            raise MemoryError("cancelled", "作业已取消，禁止迟到发布。")
        if job["status"] == "invalidated":
            raise MemoryError("dependency_changed", "历史输入依赖已变化，原解释已失效，不能原地重新发布。")
        self._check_dependencies(job)
        return job

    def _check_dependencies(self, job):
        for dependency in self.db.execute("SELECT * FROM input_dependencies WHERE job_id=?", (job["id"],)):
            source = self.db.execute("SELECT * FROM sources WHERE id=? AND owner=?", (dependency["source_id"], job["owner"])).fetchone()
            self.authorize(job["owner"], dependency["epoch"])
            if not source or source["state"] != "current" or source["content_hash"] != dependency["source_hash"]:
                raise MemoryError("dependency_changed", "曾提供给模型的来源已清除或过时，禁止发布和消费。")
            if dependency["event_id"] and dependency["parent_job_id"] != job["id"]:
                event = self.db.execute("SELECT e.version,e.association_revision,j.status FROM events e JOIN jobs j ON j.id=e.job_id WHERE e.id=?", (dependency["event_id"],)).fetchone()
                if not event or (event["version"], event["association_revision"], event["status"]) != (dependency["event_version"], dependency["association_revision"], "succeeded"):
                    raise MemoryError("dependency_changed", "曾提供给模型的经历或主题关联版本已失效。")

    def _dependent_jobs(self, roots):
        affected, frontier = set(roots), set(roots)
        while frontier:
            following = set()
            for parent in frontier:
                following.update(r[0] for r in self.db.execute("SELECT DISTINCT job_id FROM input_dependencies WHERE parent_job_id=?", (parent,)))
            frontier = following - affected
            affected.update(frontier)
        return affected

    def _invalidate_jobs(self, job_ids, clear=False):
        for job_id in set(job_ids):
            self.db.execute("UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=? AND (status<>'cancelled' OR ?)", (
                "cancelled" if clear else "invalidated", "dependency_cleared" if clear else "dependency_changed", now(), job_id, int(clear)))
            if not clear:
                continue
            self.db.execute("INSERT OR IGNORE INTO content_purges VALUES(?)", (job_id,))
            event_ids = {r[0] for r in self.db.execute("SELECT id FROM events WHERE job_id=?", (job_id,))}
            for receipt in self.db.execute("SELECT owner,operation_id,event_ids FROM operation_receipts WHERE result IS NOT NULL").fetchall():
                if event_ids.intersection(json.loads(receipt["event_ids"])):
                    self.db.execute("UPDATE operation_receipts SET result=NULL WHERE owner=? AND operation_id=?", (receipt["owner"], receipt["operation_id"]))
            for row in self.db.execute("SELECT id,payload FROM selections").fetchall():
                if any(e["id"] in event_ids for e in json.loads(row["payload"])["events"]):
                    self.db.execute("DELETE FROM selections WHERE id=?", (row["id"],))
            for event_id in event_ids:
                self.db.execute("DELETE FROM associations WHERE event_id=?", (event_id,))
                self.db.execute("DELETE FROM corrections WHERE event_id=?", (event_id,))
            self.db.execute("DELETE FROM events WHERE job_id=?", (job_id,))
            self.db.execute("DELETE FROM unclassified WHERE job_id=?", (job_id,))
            self.db.execute("DELETE FROM batch_results WHERE job_id=?", (job_id,))
            self.db.execute("UPDATE batch_inputs SET context=NULL WHERE job_id=?", (job_id,))

    def flush_purges(self, ledger):
        ids = [r[0] for r in self.db.execute("SELECT job_id FROM content_purges")]
        if ids:
            ledger.purge_jobs(ids)

    def job_state(self, owner: str, job_id: str, state: str, error: str | None = None):
        with self.transaction():
            job = self.job(owner, job_id)
            if job["status"] == "cancelled":
                return
            if job["status"] == "succeeded" and state != "succeeded":
                raise MemoryError("job_finished", "已成功作业不能重新进入执行状态。")
            self.db.execute("UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?", (state, error, now(), job_id))

    def topics(self, owner: str, job_id: str | None = None) -> list[dict]:
        self.authorize(owner)
        availability = "(j.status='succeeded' OR e.job_id=?)" if job_id else "j.status='succeeded'"
        params = (owner, job_id) if job_id else (owner,)
        return [dict(row) for row in self.db.execute(
            "SELECT t.id,t.name,COUNT(e.id) AS event_count FROM topics t JOIN associations a ON a.topic_id=t.id JOIN event_details e ON e.id=a.event_id JOIN jobs j ON j.id=e.job_id JOIN sources s ON s.id=e.source_id WHERE t.owner=? AND s.state='current' AND " + availability + " GROUP BY t.id ORDER BY t.name", params)]

    def _topic(self, owner: str, name: str) -> str:
        name = name.strip()
        if not name or len(name) > 120:
            raise MemoryError("invalid_topic", "主题名称须为 1—120 字符。")
        normalized = " ".join(name.casefold().split())
        row = self.db.execute("SELECT id FROM topics WHERE owner=? AND normalized=?", (owner, normalized)).fetchone()
        if row:
            return row[0]
        topic_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO topics VALUES(?,?,?,?)", (topic_id, owner, name, normalized))
        return topic_id

    def save_batch(self, owner: str, job_id: str, batch_index: int, output: dict):
        with self.transaction():
            job = self.check_job(owner, job_id)
            if job["next_batch"] != batch_index:
                raise MemoryError("checkpoint_conflict", "批次已保存或检查点发生变化。")
            for index, item in enumerate(output["events"]):
                relations = []
                for relation in item.get("relations", []):
                    target = self.db.execute("SELECT version FROM event_details WHERE id=? AND owner=?",
                                             (relation["target_id"], owner)).fetchone()
                    relations.append({"target_id": relation["target_id"], "kind": relation["kind"],
                                      "target_version": target["version"] if target else None})
                existing_id = item.get("continuation_of")
                if existing_id:
                    old = self.db.execute("SELECT * FROM event_details WHERE id=? AND owner=? AND job_id=?", (existing_id, owner, job_id)).fetchone()
                    if not old:
                        raise MemoryError("invalid_continuation", "仅允许首次发布前延续同一作业的候选经历。")
                    event_id = existing_id
                    refs = list(dict.fromkeys(json.loads(old["refs"]) + item["evidence_refs"]))
                    claims = merge_unique(json.loads(old["claims"]), self._compact_claims(item["claims"]))
                    relations = merge_unique(json.loads(old["relations"]), relations)
                    # 追加有据说明而非覆盖前批，避免丢掉早先条件。
                    quality = json.loads(old["quality"])
                    quality["uncertainties"] = merge_unique(quality.get("uncertainties", []), item.get("uncertainties", []))
                    quality["normalization_warnings"] = merge_unique(quality.get("normalization_warnings", []), output.get("normalization_warnings", []))
                    self.db.execute("UPDATE events SET summary=?,refs=?,claims=?,quality=?,relations=? WHERE id=?", (
                        old["summary"] + "\n后续讨论：" + item["summary"], dumps(refs), dumps(claims), dumps(quality), dumps(relations), event_id))
                else:
                    event_id = fingerprint([job_id, batch_index, index])[:32]
                    self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                        event_id, job_id, 1, item["title"], item["summary"],
                        dumps(item["evidence_refs"]), dumps(self._compact_claims(item["claims"])), dumps(merge_unique([], relations)),
                        dumps({"mode": "local_experiment", "semantic_verified": False,
                               "uncertainties": item.get("uncertainties", []),
                               "normalization_warnings": output.get("normalization_warnings", [])}), 1, now()))
                for suggestion in item["topics"]:
                    topic_id = suggestion.get("existing_id")
                    if topic_id:
                        if not self.db.execute("SELECT 1 FROM topics WHERE id=? AND owner=?", (topic_id, owner)).fetchone():
                            raise MemoryError("invalid_topic", "主题不属于当前账号。")
                    else:
                        topic_id = self._topic(owner, suggestion["name"])
                    self.db.execute("INSERT OR IGNORE INTO associations VALUES(?,?,'model')", (event_id, topic_id))
            self.db.executemany("INSERT OR REPLACE INTO unclassified VALUES(?,?,?)", (
                (job_id, row["ref"], row["reason"]) for row in output["unclassified"]))
            self.db.execute("INSERT INTO batch_results VALUES(?,?,?,?)", (job_id, batch_index, fingerprint(output), dumps(output.get("normalization_warnings", []))))
            self.db.execute("UPDATE jobs SET next_batch=next_batch+1,updated_at=? WHERE id=?", (now(), job_id))

    def frozen_input(self, owner: str, job_id: str, batch_index: int, value: dict | None = None):
        with self.transaction():
            self.check_job(owner, job_id)
            row = self.db.execute("SELECT context FROM batch_inputs WHERE job_id=? AND batch_index=?", (job_id, batch_index)).fetchone()
            if row:
                if row[0] is None:
                    raise MemoryError("cleared", "原请求上下文已清除，不允许重新构造。")
                frozen = json.loads(row[0])
                if not frozen.get("dependencies_tracked"):
                    raise MemoryError("dependency_untracked", "历史批次缺少实际输入依赖记录，不能冒充已核验的恢复；请先审查原请求。")
                return frozen
            if value is not None:
                self._capture_dependencies(owner, job_id, batch_index, value["candidates"])
                value = {**value, "dependencies_tracked": True}
                self.db.execute("INSERT INTO batch_inputs VALUES(?,?,?)", (job_id, batch_index, dumps(value)))
            return value

    def _capture_dependencies(self, owner, job_id, batch_index, candidates):
        events = {e["id"]: e for e in candidates["events"]}
        for topic in candidates["topics"]:
            row = self.db.execute("SELECT name FROM topics WHERE id=? AND owner=?", (topic["id"], owner)).fetchone()
            if not row or row[0] != topic["name"]:
                raise MemoryError("dependency_changed", "送入模型的主题候选已变化。")
            if not topic.get("input_events"):
                raise MemoryError("dependency_untracked", "主题候选缺少首次读取时的来源版本，不能补造历史依赖。")
            for event in topic["input_events"]:
                events.setdefault(event["id"], event)
        for candidate in events.values():
            event = self.db.execute("SELECT * FROM event_details WHERE id=? AND owner=?", (candidate["id"], owner)).fetchone()
            if not event:
                raise MemoryError("dependency_changed", "实际历史候选已不可用。")
            parent = self.check_job(owner, event["job_id"])
            if parent["id"] != job_id and parent["status"] != "succeeded":
                raise MemoryError("dependency_changed", "历史候选尚未发布或已经失效。")
            for key in ("version", "association_revision", "source_id"):
                if key in candidate and candidate[key] != event[key]:
                    raise MemoryError("dependency_changed", "历史候选版本在构建输入时发生变化。")
            source = self.source(owner, event["source_id"])
            self.db.execute("INSERT OR IGNORE INTO input_dependencies VALUES(?,?,?,?,?,?,?,?,?)", (
                job_id, batch_index, source["id"], event["id"], event["job_id"], event["version"],
                event["association_revision"], source["content_hash"], self.authorize(owner)["epoch"]))

    def finish(self, owner: str, job_id: str):
        with self.transaction():
            job = self.check_job(owner, job_id)
            referenced = set()
            for row in self.db.execute("SELECT refs FROM event_details WHERE job_id=?", (job_id,)):
                referenced.update(json.loads(row[0]))
            referenced.update(row[0] for row in self.db.execute("SELECT ref FROM unclassified WHERE job_id=?", (job_id,)))
            all_refs = {row[0] for row in self.db.execute("SELECT ref FROM segments WHERE source_id=?", (job["source_id"],))}
            settings = json.loads(job["settings"])
            required = set(settings["primary_refs"]) if settings.get("kind") == "salient_refinement" else all_refs
            if not required.issubset(referenced) or not referenced.issubset(all_refs):
                raise MemoryError("coverage_gap", "来源仍有未归属片段，不能发布为成功结果。")
            if settings.get("kind") == "salient_refinement":
                # 原批次模型响应和 batch_results 保留审计；当前未归类归属由补充结果承接。
                self.db.executemany("DELETE FROM unclassified WHERE job_id=? AND ref=?", (
                    (settings["parent_job_id"], ref) for ref in required))
            self.db.execute("UPDATE jobs SET status='succeeded',error=NULL,updated_at=? WHERE id=?", (now(), job_id))

    def list_events(self, owner: str, query: str = "", topic_id: str | None = None,
                    limit: int = 100, offset: int = 0) -> list[dict]:
        self.authorize(owner)
        if not 1 <= limit <= 500 or offset < 0:
            raise MemoryError("invalid_page", "每页须为 1—500 条，偏移量不能为负。")
        sql = "SELECT e.* FROM event_details e JOIN sources s ON s.id=e.source_id JOIN jobs j ON j.id=e.job_id WHERE e.owner=? AND s.state='current' AND j.status='succeeded'"
        params = [owner]
        if query:
            sql += " AND (e.title LIKE ? ESCAPE '\\' OR e.summary LIKE ? ESCAPE '\\')"
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params += [f"%{escaped}%"] * 2
        if topic_id:
            sql += " AND EXISTS(SELECT 1 FROM associations a WHERE a.event_id=e.id AND a.topic_id=?)"
            params.append(topic_id)
        sql += " ORDER BY e.created_at,e.id LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [self._event_dict(row) for row in self.db.execute(sql, params)]

    @staticmethod
    def _compact_claims(claims):
        return [{k:v for k,v in claim.items() if k != "speaker_candidates" and not (k == "reality_verified" and v is False)} for claim in claims]

    def _event_dict(self, row) -> dict:
        result = dict(row)
        job = self.db.execute("SELECT settings FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
        generation = json.loads(job[0]) if job else {}
        result["model"] = generation.get("model", "unknown")
        result["prompt_version"] = generation.get("prompt_version", "unknown")
        for key in ("refs", "claims", "relations", "quality"):
            result[key] = json.loads(result[key])
        claim_refs = sorted({ref for claim in result["claims"] for ref in claim["refs"]})
        evidence = {r["ref"]:r for r in self.evidence(row["owner"], row["source_id"], claim_refs)}
        for claim in result["claims"]:
            claim.setdefault("speaker_candidates", sorted({sp for ref in claim["refs"] for sp in evidence[ref]["speakers"]}))
            claim.setdefault("reality_verified", False)
        result["relations"] = [self._relation_view(row["owner"], relation) for relation in result["relations"]]
        result["topics"] = [dict(t) for t in self.db.execute("SELECT t.id,t.name,a.origin FROM topics t JOIN associations a ON a.topic_id=t.id WHERE a.event_id=? ORDER BY t.name", (row["id"],))]
        return result

    def _relation_view(self, owner: str, relation: dict) -> dict:
        """有效性由当前事实查询得出，不修改旧解释，也不把有效目标等同语义属实。"""
        target = self.db.execute(
            "SELECT e.version,e.title,s.state,j.status FROM event_details e JOIN sources s ON s.id=e.source_id "
            "JOIN jobs j ON j.id=e.job_id WHERE e.id=? AND e.owner=?",
            (relation["target_id"], owner)).fetchone()
        status, reason = "valid", None
        if not target or target["state"] == "cleared":
            status, reason = "unavailable", "target_unavailable"
        elif target["state"] != "current":
            status, reason = "stale", "target_source_revised"
        elif target["status"] != "succeeded":
            status, reason = "unavailable", "target_unavailable"
        elif relation.get("target_version") is not None and relation["target_version"] != target["version"]:
            status, reason = "stale", "target_version_changed"
        return {**relation, "status": status, "invalid_reason": reason, "semantic_status": "candidate",
                "target_title": target["title"] if status == "valid" else None}

    def event(self, owner: str, event_id: str) -> dict:
        self.authorize(owner)
        row = self.db.execute("SELECT e.* FROM event_details e JOIN jobs j ON j.id=e.job_id WHERE e.id=? AND e.owner=? AND j.status='succeeded'", (event_id, owner)).fetchone()
        if not row:
            raise MemoryError("not_found", "经历不存在、尚未完成或不属于当前账号。")
        self.source(owner, row["source_id"])
        self._check_dependencies(self.job(owner, row["job_id"]))
        return self._event_dict(row)

    def candidates(self, owner: str, query: str, job_id: str, limit: int = 12, full_summary: bool = False) -> dict:
        with self.transaction():
            return self._candidates(owner, query, job_id, limit, full_summary)

    def _candidates(self, owner, query, job_id, limit, full_summary):
        self.authorize(owner)
        rows = self.db.execute("SELECT e.id,e.title,e.summary,e.job_id,e.source_id,e.version,e.association_revision FROM event_details e JOIN sources s ON s.id=e.source_id JOIN jobs j ON j.id=e.job_id WHERE e.owner=? AND s.state='current' AND (j.status='succeeded' OR e.job_id=?)", (owner, job_id))
        grams = {query[i:i+2] for i in range(len(query)-1)}
        ranked = []
        for row in rows:
            text = row["title"] + row["summary"]
            hits = sum(gram in text for gram in grams)
            ranked.append((hits, row["id"], dict(row)))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        chosen = [row for _, _, row in ranked[:limit]]
        related_topic_scores = {}
        for hits, event_id, _ in ranked[:limit]:
            for relation in self.db.execute("SELECT topic_id FROM associations WHERE event_id=?", (event_id,)):
                related_topic_scores[relation["topic_id"]] = max(related_topic_scores.get(relation["topic_id"], 0), hits)
        for row in chosen:
            row["can_continue"] = row["job_id"] == job_id
            if not full_summary:
                row["summary"] = row["summary"][-1000:]
            evidence_refs = json.loads(self.db.execute("SELECT refs FROM event_details WHERE id=?", (row["id"],)).fetchone()[0])
            evidence = self.evidence(owner, row["source_id"], evidence_refs)
            row["start_ms"] = min((s["start_ms"] for s in evidence), default=None)
            row["end_ms"] = max((s["end_ms"] for s in evidence), default=None)
            row["speaker_candidates"] = sorted({speaker for s in evidence for speaker in s["speakers"]})
        topics = self.topics(owner, job_id)
        def topic_rank(topic):
            name_score = sum(gram in topic["name"] for gram in grams)
            related_score = related_topic_scores.get(topic["id"], 0)
            return (-(name_score + related_score), -name_score, -related_score, topic["name"], topic["id"])
        topics.sort(key=topic_rank)
        topics = topics[:80]
        for topic in topics:
            topic["input_events"] = [dict(r) for r in self.db.execute(
                "SELECT e.id,e.source_id,e.job_id,e.version,e.association_revision FROM event_details e JOIN associations a ON a.event_id=e.id JOIN jobs j ON j.id=e.job_id JOIN sources s ON s.id=e.source_id WHERE a.topic_id=? AND e.owner=? AND s.state='current' AND (j.status='succeeded' OR e.job_id=?)",
                (topic["id"], owner, job_id))]
        return {"events": chosen, "topics": topics[:80],
                "scope": {"owner_filtered": True, "event_limit": limit,
                          "topic_limit": 80, "topic_total": len(topics), "topic_method": "name_and_recalled_events",
                          "method": "local_character_bigram", "relation_is_suggestion": True}}

    @staticmethod
    def operation_id(value=None):
        value = uuid.uuid4().hex if value is None else value
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
            raise MemoryError("invalid_operation", "操作编号须为1—128位字母、数字或_.:-。")
        return value

    def operation(self, owner: str, operation_id: str):
        account = self.authorize(owner)
        operation_id = self.operation_id(operation_id)
        row = self.db.execute("SELECT * FROM operation_receipts WHERE owner=? AND operation_id=?", (owner, operation_id)).fetchone()
        if not row:
            raise MemoryError("not_found", "未找到该账号的操作回执；不代表未知请求一定未执行，请保留原编号核对。")
        receipt = {k: row[k] for k in ("operation_id", "kind", "request_hash", "created_at")}
        if row["result"] is None or row["epoch"] != account["epoch"]:
            return {**receipt, "status":"cleared", "current_state":"unavailable", "result":None}
        result = json.loads(row["result"])
        current_state = "valid"
        try:
            if row["kind"] == "correct_topics":
                event = self.event(owner, result["id"])
                if event["association_revision"] != result["association_revision"]:
                    current_state = "changed"
            else:
                self.selection(owner, result["id"])
        except MemoryError:
            current_state = "unavailable"
        return {**receipt, "status":"applied", "current_state":current_state,
                "result":result if current_state != "unavailable" else None}

    def _operation_replay(self, owner, operation_id, kind, request):
        row = self.db.execute("SELECT kind,request_hash FROM operation_receipts WHERE owner=? AND operation_id=?", (owner, operation_id)).fetchone()
        if row is None:
            return None
        if row["kind"] != kind or row["request_hash"] != fingerprint(request):
            raise MemoryError("operation_conflict", "操作编号已对应其他参数或类型，不允许用新请求覆盖原回执。")
        receipt = self.operation(owner, operation_id)
        if receipt["result"] is None:
            raise MemoryError("operation_unavailable", "原操作已执行，但结果已清除或失效；请查询原回执，不会重新执行。")
        return self._operation_result(receipt["result"], receipt)

    @staticmethod
    def _operation_result(result, receipt):
        return {**result, "operation_id":receipt["operation_id"],
                "receipt":{k:receipt[k] for k in ("status", "current_state", "kind", "request_hash")}}

    def _record_operation(self, owner, operation_id, kind, request, event_ids, result):
        self.db.execute("INSERT INTO operation_receipts VALUES(?,?,?,?,?,?,?,?)", (
            owner, operation_id, kind, fingerprint(request), self.authorize(owner)["epoch"],
            dumps(event_ids), dumps(result), now()))
        return self._operation_result(result, self.operation(owner, operation_id))

    def correct_topics(self, owner: str, event_id: str, expected_revision: int, names: list[str], operation_id: str | None = None) -> dict:
        explicit_operation = operation_id is not None
        if not isinstance(names, list) or len(names) > 20 or not all(isinstance(name, str) for name in names):
            raise MemoryError("invalid_topic", "一次最多设置 20 个主题名称。")
        if type(expected_revision) is not int or expected_revision < 1:
            raise MemoryError("invalid_revision", "关联修订须为正整数。")
        operation_id = self.operation_id(operation_id)
        request = {"event_id":event_id,"revision":expected_revision,"names":names}
        with self.transaction():
            self.authorize(owner)
            replay = self._operation_replay(owner, operation_id, "correct_topics", request)
            if replay is not None:
                return replay
            event = self.event(owner, event_id)
            if event["association_revision"] != expected_revision:
                raise MemoryError("revision_conflict", "主题关联已变化，请重新读取后再纠正。")
            before = event["topics"]
            self.db.execute("DELETE FROM associations WHERE event_id=?", (event_id,))
            for name in names:
                self.db.execute("INSERT OR IGNORE INTO associations VALUES(?,?,'human')", (event_id, self._topic(owner, name)))
            self.db.execute("UPDATE events SET association_revision=association_revision+1 WHERE id=?", (event_id,))
            self.db.execute("INSERT INTO corrections VALUES(?,?,?,?,?,?)", (
                uuid.uuid4().hex, event_id, expected_revision+1, dumps(before), dumps(names), now()))
            dependents = [r[0] for r in self.db.execute("SELECT DISTINCT job_id FROM input_dependencies WHERE event_id=? AND job_id<>parent_job_id", (event_id,))]
            self._invalidate_jobs(self._dependent_jobs(dependents))
            event = self.event(owner, event_id)
            result = self._record_operation(owner, operation_id, "correct_topics", request, [event_id], event)
            return result if explicit_operation else event

    def select(self, owner: str, event_ids: list[str], purpose: str, operation_id: str | None = None) -> dict:
        explicit_operation = operation_id is not None
        if not isinstance(event_ids, list) or not event_ids or len(event_ids) > 100 or not all(isinstance(v, str) for v in event_ids) or not isinstance(purpose, str) or not purpose.strip():
            raise MemoryError("invalid_selection", "请选择 1—100 个事件并说明用途。")
        operation_id = self.operation_id(operation_id)
        request = {"event_ids":event_ids,"purpose":purpose}
        with self.transaction():
            account = self.authorize(owner)
            replay = self._operation_replay(owner, operation_id, "select", request)
            if replay is not None:
                return replay
            events = [self.event(owner, event_id) for event_id in dict.fromkeys(event_ids)]
            selection = {"id": uuid.uuid4().hex, "purpose": purpose, "events": [
                {"id": e["id"], "version": e["version"], "source_id": e["source_id"],
                 "association_revision": e["association_revision"], "evidence_refs": e["refs"]}
                for e in events]}
            self.db.execute("INSERT INTO selections VALUES(?,?,?,?,?)", (selection["id"], owner, account["epoch"], dumps(selection), now()))
            # 无编号的内部调用仍返回兼容结构；显式编号的调用可查询和重放回执。
            result = self._record_operation(owner, operation_id, "select", request, [e["id"] for e in events], selection)
            return result if explicit_operation else selection

    def selection(self, owner: str, selection_id: str) -> dict:
        row = self.db.execute("SELECT * FROM selections WHERE id=? AND owner=?", (selection_id, owner)).fetchone()
        if not row:
            raise MemoryError("not_found", "选定范围不存在或不属于当前账号。")
        self.authorize(owner, row["epoch"])
        value = json.loads(row["payload"])
        for selected in value["events"]:
            current = self.event(owner, selected["id"])
            if (current["version"], current["association_revision"]) != (selected["version"], selected["association_revision"]):
                raise MemoryError("selection_conflict", "所选版本或关联修订已变化，请重新选择。")
        return value

    def clear_recording(self, owner: str, recording: str, ledger=None):
        with self.transaction():
            self.authorize(owner)
            self.db.execute("INSERT OR IGNORE INTO tombstones VALUES(?,?)", (owner, recording))
            ids = [row[0] for row in self.db.execute("SELECT id FROM sources WHERE owner=? AND recording=?", (owner, recording))]
            roots = [r[0] for r in self.db.execute("SELECT id FROM jobs WHERE owner=? AND source_id IN (SELECT id FROM sources WHERE owner=? AND recording=?)", (owner, owner, recording))]
            # 旧作业没有实际输入依赖记录，不能根据最终关系边证明独立，保守撤下并清除其调用副本。
            if ids:
                roots.extend(r[0] for r in self.db.execute("SELECT id FROM jobs WHERE owner=? AND id NOT IN (SELECT job_id FROM input_dependencies WHERE batch_index=-1)", (owner,)))
            self._invalidate_jobs(self._dependent_jobs(roots), clear=True)
            for source_id in ids:
                self.db.execute("UPDATE jobs SET status='cancelled',error='cleared' WHERE source_id=?", (source_id,))
                self.db.execute("DELETE FROM segments WHERE source_id=?", (source_id,))
                self.db.execute("UPDATE sources SET state='cleared',raw='',metadata='{}' WHERE id=?", (source_id,))
            affected_sources = set(ids)
            for selection in self.db.execute("SELECT id,payload FROM selections WHERE owner=?", (owner,)).fetchall():
                # 混合选择整体失效，不能静默删掉其中一项；完全不相关的选择继续有效。
                members = json.loads(selection["payload"])["events"]
                if any(member["source_id"] in affected_sources for member in members):
                    self.db.execute("DELETE FROM selections WHERE id=? AND owner=?", (selection["id"], owner))
            self.db.execute("DELETE FROM topics WHERE owner=? AND id NOT IN (SELECT topic_id FROM associations)", (owner,))
        if ledger is not None:
            self.flush_purges(ledger)
