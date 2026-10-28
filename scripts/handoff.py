"""交付入口：查看快照或在独立数据库中处理转录，不修改交付原件。"""
import argparse
import json
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from event_memory.config import ChatGPTConfig
from event_memory.errors import MemoryError
from event_memory.ledger import Ledger
from event_memory.openai_model import ChatGPTModel
from event_memory.pipeline import Pipeline
from event_memory.server import create_server
from event_memory.store import Store

OWNER = "gt-evaluation"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def snapshot(source, target):
    """用SQLite备份接口复制已提交事务，包括WAL中的数据。"""
    if target.exists():
        return
    if not source.is_file():
        raise MemoryError("delivery_missing", "缺少私有数据包中的数据库，请将delivery_data放到项目根目录。")
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    dest = sqlite3.connect(target)
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()


def all_events(store):
    rows, offset = [], 0
    while True:
        page = store.list_events(OWNER, limit=500, offset=offset)
        rows.extend(page)
        if len(page) < 500:
            return rows
        offset += 500


def import_credentials():
    from event_memory.chatgpt_auth import DIR, DIRECT_SCOPE, ProbeError, atomic_json, principal_hash, save_profile
    if (DIR / "credentials.dpapi").exists():
        return
    portable = ROOT / "private_credentials" / "transfer.json"
    if not portable.exists():
        return
    if sys.platform != "win32":
        raise MemoryError("windows_required", "当前凭据保存实现需要Windows，请在Windows上运行。")
    try:
        value = json.loads(portable.read_text(encoding="utf-8"))
        profile, proof = value["profile"], value["verified_inference"]
        if (DIRECT_SCOPE not in profile["scopes"] or not profile.get("refresh_token")
                or proof["status"] != "subscription_inference_verified"
                or proof["principal_hash"] != principal_hash(profile)):
            raise ValueError
        # 转移文件不进入源码仓库；首次导入后由目标Windows用户重新加密。
        save_profile(profile)
        atomic_json(DIR / "verified-inference.json", proof)
        atomic_json(DIR / "registration.json", {"client_id": profile["client_id"]})
        print("已导入交付凭据并在当前Windows用户下加密；未调用模型。", flush=True)
    except ProbeError as exc:
        raise MemoryError(exc.code, str(exc)) from None
    except (OSError, KeyError, ValueError, TypeError):
        raise MemoryError("invalid_transfer", "交付凭据文件不完整，请重新登录；未输出凭据内容。") from None


def open_page(db, ledger, port, check=False):
    if check:
        store = Store(db)
        try:
            rows = all_events(store)
            print(json.dumps({"status": "可查看", "events": len(rows),
                              "topics": len(store.topics(OWNER)), "model_calls_started": 0}, ensure_ascii=False))
        finally:
            store.close()
        return
    server = create_server(db, OWNER, port, ledger_path=ledger)
    print(f"页面：http://127.0.0.1:{server.server_port}/；按Ctrl+C停止。", flush=True)
    print(f"当前业务库：{db}\n当前调用账本：{ledger}", flush=True)
    threading.Timer(0.5, lambda: webbrowser.open(f"http://127.0.0.1:{server.server_port}/")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("本地页面已停止。")
    finally:
        server.server_close()


def view(args):
    work = ROOT / ".runtime" / "handoff-view"
    snapshot(ROOT / "delivery_data/results/memory.sqlite", work / "memory.sqlite")
    snapshot(ROOT / "delivery_data/results/model_calls.sqlite", work / "model_calls.sqlite")
    print("查看最新结果工作副本；不会自动生成或请求模型。", flush=True)
    open_page(work / "memory.sqlite", work / "model_calls.sqlite", args.port, args.check)


def run(args):
    if args.check:
        raise MemoryError("invalid_mode", "--check仅用于查看模式；不会以检查名义运行真实模型。")
    source = Path(args.input).resolve() if args.input else ROOT / "delivery_data/input/TRANSCRIPT.md"
    if not source.is_file():
        raise MemoryError("input_missing", "缺少ASR文件；请附上delivery_data，或用--input指定UTF-8转录文件。")
    import_credentials()
    config = ChatGPTConfig.load()
    runs = ROOT / ".runtime" / "handoff-runs"
    pointer = runs / "current.json"
    saved = json.loads(pointer.read_text(encoding="utf-8")) if pointer.exists() else {}
    pending = saved and saved.get("status") != "succeeded"
    if pending:
        work = ROOT / saved["directory"]
        if source != Path(saved["input"]):
            raise MemoryError("unfinished_run", "尚有未完成作业，请先用原输入恢复；不会绕过未知请求另起作业。")
        print("恢复上次未完成作业，保留原请求身份；未知请求不会自动重发。", flush=True)
    else:
        work = runs / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        work.mkdir(parents=True)
        saved = {"directory": str(work.relative_to(ROOT)), "input": str(source), "status": "preparing"}
        save(pointer, saved)
    store = Store(work / "memory.sqlite")
    ledger = Ledger(work / "model_calls.sqlite", None)
    try:
        def progress(value):
            action = "复用已保存响应" if value.get("cached") else "调用模型"
            print(f"第{value['batch']}/{value['total_batches']}批，第{value['attempt']}次，{action}。", flush=True)
        pipeline = Pipeline(store, ChatGPTModel(config, ledger), progress)
        print("本次处理会消耗已授权账号的套餐用量；完整原文上次实测约32分钟。", flush=True)
        if saved.get("job_id"):
            result = pipeline.resume(OWNER, saved["job_id"])
        else:
            frozen = work / "input.md"
            if not frozen.exists():
                frozen.write_bytes(source.read_bytes())
            imported = store.import_source(OWNER, frozen.read_text(encoding="utf-8-sig"), "TRANSCRIPT-handoff")
            result = pipeline.process(OWNER, imported["id"], "handoff-full-run", 6000, 900)
        saved.update(status=result["status"], job_id=result["job_id"])
        save(work / "result.json", result)
        save(work / "model-statistics.json", ledger.summary())
        save(work / "events.json", all_events(store))
        from event_memory.evaluation import evaluate
        save(work / "engineering.json", evaluate(store, OWNER, result["source_id"], ledger_path=work / "model_calls.sqlite"))
        print("处理完成；新结果保存在：" + str(work), flush=True)
        print("工程检查不是语义GT评分；请对照delivery_data/reports/GT逐项对比.csv人工复核。", flush=True)
    except BaseException:
        row = store.db.execute("SELECT id,status FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        if row:
            saved.update(job_id=row["id"], status=row["status"])
        raise
    finally:
        save(pointer, saved)
        ledger.close()
        store.close()
    open_page(work / "memory.sqlite", work / "model_calls.sqlite", args.port)


def main():
    p = argparse.ArgumentParser(description="交付包：查看结果或从头处理ASR文档")
    p.add_argument("mode", choices=["view", "run"], help="view查看已有结果；run真实处理")
    p.add_argument("--port", type=int, default=8765, help="本机页面端口，默认8765")
    p.add_argument("--input", help="重新处理的UTF-8转录文件，默认交付原文")
    p.add_argument("--check", action="store_true", help="只检查已有演示数据，不启动网页或调用模型")
    args = p.parse_args()
    try:
        (view if args.mode == "view" else run)(args)
        return 0
    except MemoryError as exc:
        print(f"操作停止：{exc}", file=sys.stderr)
        if exc.code in {"login_required", "login_expired", "model_not_verified", "secure_storage_error"}:
            print("需要重新授权时执行：\n.venv\\Scripts\\python -X utf8 -m event_memory.chatgpt_auth login\n"
                  ".venv\\Scripts\\python -X utf8 -m event_memory.chatgpt_auth probe\n"
                  "登录地址见.runtime/openai-probe/authorization-url.txt。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已停止，检查点和请求记录保留；再次运行同一入口恢复。")
        return 130
    except Exception:
        # 不打印异常正文、请求头或凭据；数据库中保留业务失败状态。
        print("启动失败：请检查文件、Python依赖和端口；未输出凭据。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
