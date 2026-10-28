"""本地命令入口；所有真实材料及响应默认只写入被忽略的运行目录。"""

import argparse
import json
import sys
from pathlib import Path

from .config import ROOT, ChatGPTConfig, configured_budget, env_values, default_ledger_path
from .errors import MemoryError
from .model import Ledger
from .openai_model import create_model
from .pipeline import Pipeline
from .store import Store
from .transcript import build_batches, parse_transcript

DEFAULT_DB = ROOT / ".runtime" / "memory.sqlite"
DEFAULT_LEDGER = default_ledger_path()


class ChineseParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._positionals.title = "位置参数"
        self._optionals.title = "可选参数"
        for action in self._actions:
            if isinstance(action, argparse._HelpAction):
                action.help = "显示帮助并退出"

    def format_help(self):
        return super().format_help().replace("usage:", "用法：", 1)

    def format_usage(self):
        return super().format_usage().replace("usage:", "用法：", 1)

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "参数格式不正确，请使用 --help 查看中文说明。\n")


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def read_text(path: str) -> str:
    return Path(path).read_bytes().decode("utf-8-sig")


def export_selection(store: Store, owner: str, selection_id: str) -> dict:
    selected = store.selection(owner, selection_id)
    return {**selected, "mode": "local_experiment", "items": [
        {"event": store.event(owner, item["id"]),
         "evidence": store.evidence(owner, item["source_id"], item["evidence_refs"])}
        for item in selected["events"]]}


def parser() -> argparse.ArgumentParser:
    p = ChineseParser(description="离线转录文档的事件、主题与原文查询")
    p.add_argument("--db", default=str(DEFAULT_DB), help="本地事实数据库路径")
    p.add_argument("--owner", default="local", help="可信本地账号命名空间，不等同于远程身份认证")
    commands = p.add_subparsers(dest="command", required=True, title="操作")
    for name, help_text in (("inspect", "只解析文档并查看批次，不调用模型"),
                            ("import", "导入本地候选文档，不调用模型"),
                            ("process", "导入并调用官方模型处理文档")):
        child = commands.add_parser(name, help=help_text)
        child.add_argument("file", help="UTF-8 转录文档路径")
        child.add_argument("--recording-id", help="逻辑录音标识，默认文件名；不同录音请使用不同标识")
        child.add_argument("--revision", help="来源修订，默认原文内容摘要")
        child.add_argument("--batch-chars", type=int, default=6000, help="单批阅读容量，非事件边界")
        child.add_argument("--context-chars", type=int, default=900, help="前置上下文容量")
        child.add_argument("--operation", help="操作幂等标识，默认由来源身份确定")
    child = commands.add_parser("resume", help="按原身份和参数恢复作业；未知调用不自动重发")
    child.add_argument("job_id")
    child = commands.add_parser("refine", help="追加复核高信息未归类，不改写既有成功经历")
    child.add_argument("job_id")
    child = commands.add_parser("events", help="查询已完成的有效经历")
    child.add_argument("--query", default="")
    child.add_argument("--topic-id")
    child.add_argument("--limit", type=int, default=100)
    child.add_argument("--offset", type=int, default=0)
    commands.add_parser("topics", help="查看主题及有效经历数量")
    commands.add_parser("sources", help="查看已导入来源及处理状态")
    commands.add_parser("jobs", help="查看作业状态及恢复身份")
    child = commands.add_parser("detail", help="查看经历、主张及原文依据")
    child.add_argument("event_id")
    child = commands.add_parser("correct", help="按预期关联修订纠正主题")
    child.add_argument("event_id")
    child.add_argument("--revision", type=int, required=True)
    child.add_argument("--topics", nargs="*", default=[])
    child = commands.add_parser("select", help="固定事件版本、关联修订及证据集合")
    child.add_argument("event_ids", nargs="+")
    child.add_argument("--purpose", required=True)
    child = commands.add_parser("export", help="导出先前选定的固定内容")
    child.add_argument("selection_id")
    child.add_argument("--out", required=True)
    child = commands.add_parser("cancel", help="取消未完成作业，阻止迟到发布")
    child.add_argument("job_id")
    commands.add_parser("revoke", help="撤销当前本地账号访问并使在途代次失效")
    commands.add_parser("grant", help="重新授权本地账号，不恢复已清除内容")
    child = commands.add_parser("clear", help="清除录音内容、派生记录及模型原响应，保留不可复活标记和费用")
    child.add_argument("recording_id")
    commands.add_parser("migrate", help="备份并升级业务库和调用账本；请先停止旧服务")
    commands.add_parser("budget", help="查看持久化预算、保守费用估计与未知预留")
    child = commands.add_parser("reconcile", help="导入原调用核对材料，不发起模型请求")
    child.add_argument("call_id")
    child.add_argument("--evidence", required=True, help="含 response 或 not_billed 与 note 的本地 JSON")
    commands.add_parser("model-status", help="查看当前模型及本应用授权状态，不显示凭据")
    child = commands.add_parser("chatgpt-login", help="为本应用重新授权 ChatGPT 套餐")
    child.add_argument("--timeout", type=int, default=600)
    child = commands.add_parser("serve", help="启动仅监听本机的查询页面")
    child.add_argument("--port", type=int, default=8765)
    child = commands.add_parser("evaluate", help="生成工程评价和待人工复核的真实材料证据包")
    child.add_argument("--source-id", required=True)
    child.add_argument("--out", default=str(ROOT / ".runtime" / "evaluation.json"))
    child = commands.add_parser("evaluate-cases", help="汇总固定语义样例的既有结果，或显式执行真实评测")
    mode = child.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="明确执行真实调用；未指定模式时只显示样例文件")
    mode.add_argument("--report-only", action="store_true", help="仅汇总既有结果，不调用模型")
    child.add_argument("--report-model", help="只读报告要统计的模型；默认当前配置模型")
    child.add_argument("--report-prompt-version", help="只读报告的策略版本；旧记录可指定 offline-evidence-2")
    child.add_argument("--cases", default=str(ROOT / "tests" / "fixtures" / "semantic_cases.json"))
    child.add_argument("--out", default=str(ROOT / ".runtime" / "semantic-cases.json"))
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    store = None
    ledger = None
    try:
        if args.command == "inspect":
            transcript = parse_transcript(read_text(args.file))
            batches = build_batches(transcript.segments, args.batch_chars, args.context_chars)
            emit({"source_hash": transcript.content_hash, "candidate": transcript.candidate,
                  "segments": len(transcript.segments), "speech_segments": sum(s.kind == "speech" for s in transcript.segments),
                  "acoustic_segments": sum(s.kind == "acoustic" for s in transcript.segments),
                  "batches": len(batches), "model_calls": 0, "mode": "local_experiment"})
            return 0
        if args.command == "chatgpt-login":
            from .chatgpt_auth import login, ProbeError
            try:
                emit(login(args.timeout))
            except ProbeError as exc:
                raise MemoryError(exc.code, str(exc)) from None
            return 0
        if args.command == "model-status":
            config = ChatGPTConfig.load()
            emit({"provider":config.provider,"model":config.model,"authentication":"本应用独立 OAuth / Windows DPAPI",
                  "billing_path":"chatgpt_plan","api_key_required":False,"deepseek_active":False})
            return 0
        if args.command == "serve":
            from .server import serve
            serve(Path(args.db), args.owner, args.port)
            return 0
        if args.command == "evaluate-cases":
            if args.report_only:
                import sqlite3
                from .evaluation import report_cases
                store = Store(":memory:")
                source_db = ROOT / ".runtime" / "semantic-evaluation.sqlite"
                if source_db.exists():
                    source = sqlite3.connect(source_db.resolve().as_uri()+"?mode=ro", uri=True)
                    try:
                        source.backup(store.db)
                        store.ensure_schema()
                    finally:
                        source.close()
                model_name = args.report_model or env_values().get("OPENAI_MODEL", "gpt-6.1-sol")
                result = report_cases(store, model_name, Path(args.cases), prompt_version=args.report_prompt_version)
                result["mode"] = "report_only"
                result["model_calls_started"] = 0
                target = Path(args.out)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                emit({"report": str(target.resolve()), "model": model_name, "overall_status": result["overall_status"],
                      "case_counts": result["case_counts"], "model_calls_started": 0})
                return 0
            if not args.live:
                emit({"cases": args.cases, "model_calls": 0, "message": "如需真实调用，请显式加 --live。"})
                return 0
            if args.report_model or args.report_prompt_version:
                raise MemoryError("invalid_mode", "报告的模型/策略选择只适用于只读报告；真实调用使用当前已授权模型及策略。")
            from .evaluation import run_cases
            store = Store(ROOT / ".runtime" / "semantic-evaluation.sqlite")
            ledger = Ledger(DEFAULT_LEDGER, configured_budget())
            result = run_cases(store, create_model(ledger), Path(args.cases))
            target = Path(args.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            emit({"report": str(target.resolve()), "passed_defined_checks": result["passed_defined_checks"],
                  "cases": len(result["results"]), "budget": ledger.summary()})
            return 0 if result["passed_defined_checks"] else 1
        store = Store(args.db)
        if args.command in {"process", "resume", "refine", "budget", "reconcile", "clear", "migrate"}:
            ledger = Ledger(DEFAULT_LEDGER, configured_budget())
        if args.command in {"import", "process"}:
            source = store.import_source(args.owner, read_text(args.file), args.recording_id or Path(args.file).name, args.revision)
            if args.command == "import":
                emit({k: v for k, v in source.items() if k != "raw"})
            else:
                model = create_model(ledger)
                def progress(value):
                    action = "复用已保存响应" if value.get("cached") else "调用模型提取"
                    print(f"第 {value['batch']}/{value['total_batches']} 批，第 {value['attempt']} 次：{action}。", file=sys.stderr, flush=True)
                result = Pipeline(store, model, progress).process(args.owner, source["id"], args.operation or "process-" + source["id"], args.batch_chars, args.context_chars)
                emit({**result, "budget": ledger.summary()})
        elif args.command == "resume":
            emit(Pipeline(store, create_model(ledger)).resume(args.owner, args.job_id))
        elif args.command == "refine":
            emit(Pipeline(store, create_model(ledger)).refine_unclassified(args.owner, args.job_id))
        elif args.command == "events":
            emit(store.list_events(args.owner, args.query, args.topic_id, args.limit, args.offset))
        elif args.command == "topics":
            emit(store.topics(args.owner))
        elif args.command == "sources":
            store.authorize(args.owner)
            emit([dict(r) for r in store.db.execute("SELECT id,recording,revision,transcript_version,state,metadata,created_at FROM sources WHERE owner=? ORDER BY created_at", (args.owner,))])
        elif args.command == "jobs":
            store.authorize(args.owner)
            emit([dict(r) for r in store.db.execute("SELECT id,source_id,status,next_batch,error,created_at FROM jobs WHERE owner=? ORDER BY created_at", (args.owner,))])
        elif args.command == "detail":
            event = store.event(args.owner, args.event_id)
            emit({"event": event, "evidence": store.evidence(args.owner, event["source_id"], event["refs"])})
        elif args.command == "correct":
            emit(store.correct_topics(args.owner, args.event_id, args.revision, args.topics))
        elif args.command == "select":
            emit(store.select(args.owner, args.event_ids, args.purpose))
        elif args.command == "export":
            value = export_selection(store, args.owner, args.selection_id)
            target = Path(args.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            emit({"selection_id": args.selection_id, "saved": str(target.resolve()), "event_count": len(value["items"])})
        elif args.command == "cancel":
            store.job_state(args.owner, args.job_id, "cancelled")
            emit({"status": "cancelled"})
        elif args.command in {"revoke", "grant"}:
            store.set_access(args.owner, args.command == "grant")
            emit({"access": args.command})
        elif args.command == "clear":
            job_ids = [r[0] for r in store.db.execute("SELECT j.id FROM jobs j JOIN sources s ON s.id=j.source_id WHERE j.owner=? AND s.recording=?", (args.owner, args.recording_id))]
            store.clear_recording(args.owner, args.recording_id)
            ledger.purge_jobs(job_ids)
            emit({"status": "cleared", "recording": args.recording_id})
        elif args.command == "migrate":
            emit({"database": str(Path(args.db).resolve()), "ledger": str(DEFAULT_LEDGER.resolve()), "schema_version": 2, "status": "migrated", "model_calls_started": 0})
        elif args.command == "budget":
            emit(ledger.summary())
        elif args.command == "reconcile":
            ledger.reconcile(args.call_id, json.loads(read_text(args.evidence)))
            emit({"status": "reconciled", "call_id": args.call_id})
        elif args.command == "evaluate":
            from .evaluation import evaluate
            result = evaluate(store, args.owner, args.source_id)
            target = Path(args.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            emit({"report": str(target.resolve()), "engineering": result["engineering"],
                  "independent_semantic_review": result["independent_semantic_review"]})
        return 0
    except MemoryError as exc:
        emit({"error": exc.code, "message": str(exc)})
        return 2
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        emit({"error": "local_io_error", "message": "本地文件无法读取、编码不正确或内容不是有效 JSON。"})
        return 2
    finally:
        if ledger:
            ledger.close()
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
