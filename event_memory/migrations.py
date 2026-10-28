"""事务迁移与迁移前备份；不丢弃未知调用或清除标记。"""
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

from .errors import MemoryError


def execute_schema(db, schema):
    for statement in schema.split(";"):
        if statement.strip():
            db.execute(statement)


def table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def backup_before_migration(db, path):
    if str(path) == ":memory:":
        return None
    source = Path(path).resolve()
    directory = source.parent / "migration-backups"
    directory.mkdir(exist_ok=True)
    target = directory / (source.name + ".before-v2-" + uuid.uuid4().hex[:10] + ".sqlite")
    saved = sqlite3.connect(target)
    try:
        db.backup(saved)
    finally:
        saved.close()
    return target


def migrate_business(db, path, schema):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > 2:
        raise MemoryError("unsupported_schema", "数据库版本高于当前程序支持范围，未进行降级或修改。")
    if version == 2:
        return
    old = table_exists(db, "segments") and "payload" in {r[1] for r in db.execute("PRAGMA table_info(segments)")}
    if not old:
        execute_schema(db, schema)
        db.execute("PRAGMA user_version=2")
        return
    if db.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():
        raise MemoryError("migration_busy", "旧库存在运行中作业，请先停止旧服务并核对作业状态，再迁移。")
    backup_before_migration(db, path)
    db.execute("BEGIN IMMEDIATE")
    try:
        # 并发打开时，第二个连接获得锁后重新确认，不重复迁移。
        if "payload" not in {r[1] for r in db.execute("PRAGMA table_info(segments)")}:
            db.commit()
            return
        names = ["accounts", "tombstones", "sources", "segments", "jobs", "events", "topics",
                 "associations", "corrections", "unclassified", "batch_results", "selections"]
        data = {name: [dict(r) for r in db.execute('SELECT * FROM "'+name+'"')] for name in names}
        sources = {s["id"]: s for s in data["sources"]}
        segment_map = {}
        converted = []
        for row in data["segments"]:
            value = json.loads(row["payload"])
            raw = sources[row["source_id"]]["raw"]
            if raw[value["char_start"]:value["char_end"]] != value["text"] or value["ref"] != row["ref"]:
                raise MemoryError("migration_invalid_source", "片段原文或编号与定位不一致，迁移已拒绝；请检查备份。")
            segment_map[(row["source_id"], row["ref"])] = value
            converted.append({"source_id":row["source_id"], "ref":row["ref"], **{
                k: json.dumps(value[k],ensure_ascii=False) if k=="speakers" else value[k]
                for k in ("start_ms","end_ms","speakers","line","char_start","char_end","kind")}})
        data["segments"] = converted
        jobs = {j["id"]: j for j in data["jobs"]}
        for job in data["jobs"]:
            if sources[job["source_id"]]["owner"] != job["owner"]:
                raise MemoryError("migration_invalid_job", "旧作业与来源所属账号不一致，拒绝迁移。")
            value = json.dumps([job["source_id"],json.loads(job["settings"])],ensure_ascii=False,separators=(",",":"),sort_keys=True)
            if hashlib.sha256(value.encode()).hexdigest() != job["request_hash"]:
                raise MemoryError("migration_invalid_job", "旧作业身份摘要与参数不一致，拒绝静默改写恢复身份。")
            del job["request_hash"]
        for event in data["events"]:
            job = jobs[event["job_id"]]
            if event["owner"] != job["owner"] or event["source_id"] != job["source_id"]:
                raise MemoryError("migration_invalid_event", "事件与作业归属不一致，迁移已拒绝。")
            claims = json.loads(event["claims"])
            for claim in claims:
                derived = sorted({speaker for ref in claim["refs"] for speaker in segment_map[(event["source_id"],ref)]["speakers"]})
                if claim.get("speaker_candidates") == derived:
                    claim.pop("speaker_candidates")
                if claim.get("reality_verified") is False:
                    claim.pop("reality_verified")
            event["claims"] = json.dumps(claims,ensure_ascii=False,separators=(",",":"))
            del event["owner"], event["source_id"]
        for correction in data["corrections"]:
            correction.pop("owner",None)
        for batch in data["batch_results"]:
            value=json.loads(batch.pop("payload"))
            batch["result_hash"]=hashlib.sha256(json.dumps(value,ensure_ascii=False,separators=(",",":"),sort_keys=True).encode()).hexdigest()
            batch["warnings"]=json.dumps(value.get("normalization_warnings",[]),ensure_ascii=False,separators=(",",":"))
        for name in reversed(names):
            db.execute('DROP TABLE "'+name+'"')
        execute_schema(db, schema)
        for name in names:
            for row in data[name]:
                keys=list(row)
                db.execute('INSERT INTO "'+name+'" ('+','.join('"'+k+'"' for k in keys)+') VALUES ('+','.join('?' for _ in keys)+')', [row[k] for k in keys])
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise MemoryError("migration_reference_error", "迁移后的对象关联不完整，已回滚。")
        db.execute("PRAGMA user_version=2")
        db.commit()
    except BaseException:
        db.rollback()
        raise
