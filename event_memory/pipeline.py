"""模型调用在事务外，已验证的批次检查点与发布在短事务内。"""

import json
import re
from typing import Callable

from .errors import InvalidOutput, MemoryError
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, LEGACY_PROMPT_VERSION, LEGACY_SYSTEM_PROMPT
from .context import compact_payload
from .store import Store, dumps, fingerprint
from .transcript import build_batches, parse_transcript
from .validation import CONDITIONAL, validate_output


class Pipeline:
    def __init__(self, store: Store, model, on_progress: Callable[[dict], None] | None = None):
        self.store, self.model = store, model
        self.on_progress = on_progress or (lambda _: None)

    def process(self, owner: str, source_id: str, operation_key: str,
                batch_chars: int = 6000, context_chars: int = 900) -> dict:
        settings = {"batch_chars": batch_chars, "context_chars": context_chars,
                    "prompt_version": PROMPT_VERSION,
                    "model": getattr(getattr(self.model, "config", None), "model", "test-fixture")}
        config = getattr(self.model, "config", None)
        if getattr(config, "principal_hash", None):
            settings["model_principal_hash"] = config.principal_hash
            settings["billing_path"] = config.provider
        existing = self.store.db.execute("SELECT * FROM jobs WHERE owner=? AND source_id=? AND status='succeeded' ORDER BY created_at LIMIT 1", (owner, source_id)).fetchone()
        if existing:
            saved = json.loads(existing["settings"])
            if all(saved.get(key) == settings[key] for key in ("batch_chars", "context_chars")):
                # 策略升级不触发成功经历的静默重解释，明确返回原有效版本。
                return {**self.summary(owner, existing["id"]), "reused_existing_result": True,
                        "effective_prompt_version": saved.get("prompt_version"), "effective_model": saved.get("model"),
                        "configured_model": settings["model"],
                        "message": "保留既有成功版本，模型切换不会自动覆盖旧结果或重复调用。"}
        job = self.store.create_job(owner, source_id, operation_key, settings)
        return self.resume(owner, job["id"])

    def resume(self, owner: str, job_id: str) -> dict:
        job = self.store.check_job(owner, job_id)
        if job["status"] == "succeeded":
            return self.summary(owner, job_id)
        settings = json.loads(job["settings"])
        model_name = getattr(getattr(self.model, "config", None), "model", "test-fixture")
        if model_name != settings["model"] or settings["prompt_version"] not in {PROMPT_VERSION, LEGACY_PROMPT_VERSION}:
            raise MemoryError("model_conflict", "恢复必须沿用原模型及提示词版本。")
        identity = getattr(getattr(self.model, "config", None), "principal_hash", None)
        if settings.get("model_principal_hash") and settings["model_principal_hash"] != identity:
            raise MemoryError("account_changed", "作业绑定的模型账号已变化，不向新账号续发原材料。")
        source = self.store.source(owner, job["source_id"])
        transcript = parse_transcript(source["raw"])
        if settings.get("kind") == "salient_refinement":
            by_ref = {s.ref: s.payload() for s in transcript.segments}
            batches = [{"primary": [by_ref[r] for r in settings["primary_refs"]],
                        "context": [by_ref[r] for r in settings["context_refs"]]}]
        else:
            batches = build_batches(transcript.segments, settings["batch_chars"], settings["context_chars"])
        self.store.job_state(owner, job_id, "running")
        try:
            for index in range(job["next_batch"], len(batches)):
                self.store.check_job(owner, job_id)
                batch = batches[index]
                text = "\n".join(row["text"] for row in batch["primary"])
                compact = settings["prompt_version"] == PROMPT_VERSION
                candidates = self.store.candidates(owner, text, job_id, full_summary=compact)
                payload = {**batch, "candidates": candidates,
                           "source": {"recording": source["recording"], "revision": source["revision"],
                                      "transcript_version": source["transcript_version"],
                                      "mode": "local_experiment", "candidate": transcript.candidate}}
                if "review_focus" in settings:
                    payload["review_focus"] = settings["review_focus"]
                if compact:
                    payload = compact_payload(batch, candidates, payload["source"], settings.get("review_focus"))
                base_messages = [{"role": "system", "content": SYSTEM_PROMPT if compact else LEGACY_SYSTEM_PROMPT},
                            {"role": "user", "content": dumps(payload)}]
                messages = base_messages
                validated = None
                for attempt in range(4):
                    call_id = f"{job_id}:{index}:{attempt}"
                    replay = getattr(self.model, "replay", None)
                    response = replay(call_id, job_id) if replay else None
                    self.on_progress({"job_id": job_id, "batch": index+1, "total_batches": len(batches),
                                      "attempt": attempt+1, "cached": response is not None})
                    if response is None:
                        response = self.model.generate(call_id, job_id, messages)
                    self.store.check_job(owner, job_id)
                    try:
                        validated = validate_output(response["content"], batch, candidates)
                        break
                    except InvalidOutput as exc:
                        if attempt == 3:
                            raise
                        # 只对明确收到但校验失败的输出做一次有预算的新请求；未知调用不会走这里。
                        detail = ""
                        if attempt >= 1:
                            conditional_refs = [row["ref"] for row in batch["context"] + batch["primary"]
                                                if any(word in row["text"] for word in CONDITIONAL)]
                            detail = " 特别检查含明确条件的原文编号：" + ",".join(conditional_refs) + "；若事件引用了这些编号，必须有 kind=condition 且 refs 包含该编号的逐字主张。"
                        if attempt >= 2:
                            detail += " 允许的全部引用编号仅为：" + ",".join(row["ref"] for row in batch["context"] + batch["primary"]) + "。任何未列出的编号都不可出现于事件或主张依据；不能推测下一个编号，不能引用纯声学事件作为发言。"
                        messages = (base_messages if compact else messages) + [
                            {"role": "assistant", "content": response["content"]},
                            {"role": "user", "content": "上次输出未通过工程校验：" + str(exc) + "。请根据同一批原文修正并返回完整 JSON，逐字 quote 不得改写。" + detail}]
                self.store.save_batch(owner, job_id, index, validated)
            with self.store.transaction():
                self.store.check_job(owner, job_id)
                self.store.db.executemany("INSERT OR IGNORE INTO unclassified VALUES(?,?,?)", (
                    (job_id, row.ref, "纯声学候选不作为说话人发言，保留原文定位")
                    for row in transcript.segments if row.kind == "acoustic" and settings.get("kind") != "salient_refinement"))
            self.store.finish(owner, job_id)
        except MemoryError as exc:
            if exc.code in {"cleared", "cancelled", "permission_denied", "stale_source"}:
                ledger = getattr(self.model, "ledger", None)
                if ledger:
                    ledger.purge_jobs([job_id])
            try:
                self.store.job_state(owner, job_id, "unknown" if exc.code == "unknown_call" else "failed", exc.code)
            except MemoryError:
                pass  # 撤权/清除后的状态不通过无权写入恢复；提交围栏已经阻止发布。
            raise
        except Exception:
            try:
                self.store.job_state(owner, job_id, "failed", "local_processing_error")
            except MemoryError:
                pass
            raise MemoryError("local_processing_error", "本地处理异常；已保留原操作身份和已保存的合法结果，请修复后恢复。") from None
        return self.summary(owner, job_id)

    def refine_unclassified(self, owner: str, parent_job_id: str) -> dict:
        """显式、可审计地补充高信息未归类；不改写任何已成功经历。"""
        parent = self.store.check_job(owner, parent_job_id)
        if parent["status"] != "succeeded":
            raise MemoryError("invalid_refinement", "主作业完成后才能复核未归类内容。")
        source = self.store.source(owner, parent["source_id"])
        segments = parse_transcript(source["raw"]).segments
        unclassified = {r[0] for r in self.store.db.execute("SELECT ref FROM unclassified WHERE job_id=?", (parent_job_id,))}
        salient = re.compile(r"点好了|做完了|完成了|取消了|决定了|已经.{0,10}(?:提交|发送|付款|下单|续费)|都已经写清楚了")
        selected = [s.ref for s in segments if s.ref in unclassified and s.kind == "speech" and salient.search(s.text)][:16]
        if not selected:
            return {"status": "no_salient_unclassified", "model_calls": 0}
        context = set()
        for index, segment in enumerate(segments):
            if segment.ref in selected:
                context.update(s.ref for s in segments[max(0,index-20):index+9] if s.kind == "speech" and s.ref not in selected)
        original = json.loads(parent["settings"])
        settings = {**original, "kind": "salient_refinement", "parent_job_id": parent_job_id,
                    "prompt_version": PROMPT_VERSION,
                    "primary_refs": selected, "context_refs": sorted(context),
                    "review_focus": "primary 是从未归类中筛出的高信息表达，不要仅因短句就判无主张。像点好了、发完了等可保留为 claimed_completion；取消、决定同样要区分。即使无法确定具体事项，也可以记录说话人声称完成某事并标明未知，不认定现实完成。利用 context 和历史候选理解，必要时给关系候选，不修改任何已成功经历。"}
        active = getattr(self.model, "config", None)
        settings["model"] = getattr(active, "model", "test-fixture")
        if getattr(active, "principal_hash", None):
            settings["model_principal_hash"] = active.principal_hash
            settings["billing_path"] = active.provider
        operation = "refine-" + parent_job_id + "-" + fingerprint(selected)[:12]
        job = self.store.create_job(owner, parent["source_id"], operation, settings)
        return self.resume(owner, job["id"])

    def summary(self, owner: str, job_id: str) -> dict:
        job = self.store.job(owner, job_id)
        count = self.store.db.execute("SELECT COUNT(*) FROM events WHERE job_id=?", (job_id,)).fetchone()[0]
        unclassified = self.store.db.execute("SELECT COUNT(*) FROM unclassified WHERE job_id=?", (job_id,)).fetchone()[0]
        source_count = self.store.db.execute("SELECT COUNT(*) FROM event_details e JOIN jobs j ON j.id=e.job_id WHERE e.source_id=? AND e.owner=? AND j.status='succeeded'", (job["source_id"], owner)).fetchone()[0]
        return {"job_id": job_id, "source_id": job["source_id"], "status": job["status"],
                "event_count": count, "unclassified_count": unclassified,
                "completed_batches": job["next_batch"], "mode": "local_experiment",
                "source_event_count": source_count,
                "semantic_independently_reviewed": False}
