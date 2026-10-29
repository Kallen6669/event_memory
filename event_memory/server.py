"""仅本机使用的真实查询消费者；固定账号、请求令牌和来源检查不可由前端绕过。"""

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .cli import DEFAULT_LEDGER, export_selection
from .config import ChatGPTConfig, configured_budget
from .errors import MemoryError
from .model import Ledger
from .openai_model import ChatGPTModel
from .pipeline import Pipeline
from .prompt import PROMPT_VERSION
from .store import Store, dumps, fingerprint

ASSETS = Path(__file__).resolve().parent / "web"


def create_server(db_path: Path, owner: str = "local", port: int = 8765,
                  ledger_path: Path = DEFAULT_LEDGER) -> ThreadingHTTPServer:
    session_token = secrets.token_urlsafe(32)
    active_jobs = set()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            # 默认访问日志会带查询内容；真实材料仅存本地数据库，不写终端日志。
            pass

        def send_body(self, value, status=200, content_type="application/json; charset=utf-8"):
            body = dumps(value).encode("utf-8") if isinstance(value, (dict, list)) else value
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def check_host(self):
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in allowed:
                raise MemoryError("permission_denied", "仅允许本机地址访问。")

        def check_api(self):
            self.check_host()
            if not secrets.compare_digest(self.headers.get("X-Local-Token", ""), session_token):
                raise MemoryError("permission_denied", "缺少有效的本地会话令牌。")
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                raise MemoryError("permission_denied", "拒绝跨站请求。")

        def error(self, exc: MemoryError):
            status = 403 if exc.code == "permission_denied" else 404 if exc.code == "not_found" else 409 if "conflict" in exc.code else 400
            self.send_body({"error": exc.code, "message": str(exc)}, status)

        def schedule(self, job_id: str, refine: bool = False):
            config = ChatGPTConfig.load()
            budget = configured_budget()
            key = ("refine:" if refine else "resume:") + job_id
            with lock:
                if key in active_jobs:
                    return
                active_jobs.add(key)
                def work():
                    worker_store = Store(db_path)
                    ledger = Ledger(ledger_path, budget)
                    try:
                        pipeline = Pipeline(worker_store, ChatGPTModel(config, ledger))
                        if refine:
                            pipeline.refine_unclassified(owner, job_id)
                        else:
                            pipeline.resume(owner, job_id)
                    except MemoryError:
                        pass  # 业务失败由作业状态承载，不输出原文或密钥。
                    finally:
                        worker_store.close()
                        ledger.close()
                        with lock:
                            active_jobs.discard(key)
                threading.Thread(target=work, daemon=True).start()

        def do_GET(self):
            store = None
            try:
                self.check_host()
                url = urlsplit(self.path)
                if url.path == "/":
                    html = (ASSETS / "index.html").read_text(encoding="utf-8").replace("__SESSION_TOKEN__", session_token)
                    html = html.replace("__STORE_ID__", fingerprint([str(Path(db_path).resolve()),owner])[:24])
                    self.send_body(html.encode("utf-8"), content_type="text/html; charset=utf-8")
                    return
                static = {"/app.js": ("app.js", "text/javascript; charset=utf-8"),
                          "/operations.js": ("operations.js", "text/javascript; charset=utf-8"),
                          "/style.css": ("style.css", "text/css; charset=utf-8")}
                if url.path in static:
                    file, mime = static[url.path]
                    self.send_body((ASSETS / file).read_bytes(), content_type=mime)
                    return
                self.check_api()
                store = Store(db_path)
                store.authorize(owner)
                parts = url.path.strip("/").split("/")
                query = parse_qs(url.query)
                if url.path == "/api/events":
                    self.send_body(store.list_events(owner, query.get("q", [""])[0], query.get("topic", [None])[0], int(query.get("limit", ["100"])[0]), int(query.get("offset", ["0"])[0])))
                elif len(parts) == 3 and parts[:2] == ["api", "events"]:
                    event = store.event(owner, parts[2])
                    self.send_body({"event": event, "evidence": store.evidence(owner, event["source_id"], event["refs"])})
                elif url.path == "/api/topics":
                    self.send_body(store.topics(owner))
                elif url.path == "/api/sources":
                    self.send_body([dict(r) for r in store.db.execute("SELECT id,recording,revision,state,created_at FROM sources WHERE owner=? ORDER BY created_at DESC", (owner,))])
                elif url.path == "/api/jobs":
                    self.send_body([dict(r) for r in store.db.execute("SELECT id,source_id,status,next_batch,error,created_at FROM jobs WHERE owner=? ORDER BY created_at DESC", (owner,))])
                elif len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                    job = store.job(owner, parts[2])
                    rows = [dict(r) for r in store.db.execute("SELECT ref,reason FROM unclassified WHERE job_id=? ORDER BY ref", (job["id"],))]
                    self.send_body({"job": job, "unclassified": [
                        {**row, "evidence": store.evidence(owner, job["source_id"], [row["ref"]])[0]}
                        for row in rows]})
                elif len(parts) == 3 and parts[:2] == ["api", "selections"]:
                    self.send_body(export_selection(store, owner, parts[2]))
                elif len(parts) == 3 and parts[:2] == ["api", "operations"]:
                    self.send_body(store.operation(owner, parts[2]))
                elif url.path == "/api/budget":
                    ledger = Ledger(ledger_path, configured_budget())
                    try:
                        ledger.bind_store(store)
                        self.send_body(ledger.summary())
                    finally:
                        ledger.close()
                else:
                    raise MemoryError("not_found", "页面或接口不存在。")
            except MemoryError as exc:
                self.error(exc)
            except (ValueError, TypeError):
                self.error(MemoryError("invalid_request", "查询参数格式不正确。"))
            finally:
                if store:
                    store.close()

        def do_POST(self):
            store = None
            try:
                self.check_api()
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4_000_000 or self.headers.get_content_type() != "application/json":
                    raise MemoryError("invalid_request", "请提交不超过 4 MB 的 JSON 请求。")
                value = json.loads(self.rfile.read(size))
                if not isinstance(value, dict):
                    raise MemoryError("invalid_request", "请求必须是 JSON 对象。")
                store = Store(db_path)
                store.authorize(owner)
                parts = urlsplit(self.path).path.strip("/").split("/")
                if parts == ["api", "sources"]:
                    if not isinstance(value.get("text"), str) or not isinstance(value.get("recording"), str):
                        raise MemoryError("invalid_request", "需要转录文本和录音标识。")
                    source = store.import_source(owner, value["text"], value["recording"])
                    self.send_body({k: v for k, v in source.items() if k != "raw"}, 201)
                elif parts == ["api", "process"]:
                    config = ChatGPTConfig.load()
                    source_id = value["source_id"]
                    settings = {"batch_chars": 6000, "context_chars": 900,
                                "prompt_version": PROMPT_VERSION, "model": config.model,
                                "model_principal_hash": config.principal_hash, "billing_path": config.provider}
                    existing = store.db.execute("SELECT settings FROM jobs WHERE owner=? AND source_id=? AND status='succeeded' ORDER BY created_at LIMIT 1", (owner, source_id)).fetchone()
                    if existing:
                        settings = json.loads(existing[0])
                    job = store.create_job(owner, source_id, value.get("operation_key") or "process-" + source_id, settings)
                    if job["status"] not in {"succeeded", "cancelled"}:
                        self.schedule(job["id"])
                    self.send_body({"job_id": job["id"], "status": job["status"], "effective_model": settings["model"], "configured_model": config.model}, 202)
                elif len(parts) == 4 and parts[:2] == ["api", "events"] and parts[3] == "topics":
                    if not isinstance(value.get("operation_id"),str):
                        raise MemoryError("invalid_operation", "纠正请求必须提供稳定的operation_id用于结果核对。")
                    self.send_body(store.correct_topics(owner, parts[2], value["revision"], value["names"], value["operation_id"]))
                elif parts == ["api", "selections"]:
                    if not isinstance(value.get("operation_id"),str):
                        raise MemoryError("invalid_operation", "固定选择请求必须提供稳定的operation_id用于结果核对。")
                    self.send_body(store.select(owner, value["event_ids"], value["purpose"], value["operation_id"]), 201)
                elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
                    store.job_state(owner, parts[2], "cancelled")
                    self.send_body({"status": "cancelled"})
                elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "refine":
                    parent = store.check_job(owner, parts[2])
                    if parent["status"] != "succeeded":
                        raise MemoryError("invalid_refinement", "主作业完成后才能复核未归类。")
                    self.schedule(parent["id"], refine=True)
                    self.send_body({"status": "review_queued", "parent_job_id": parent["id"]}, 202)
                else:
                    raise MemoryError("not_found", "操作接口不存在。")
            except MemoryError as exc:
                self.error(exc)
            except (ValueError, KeyError, TypeError, AttributeError):
                self.error(MemoryError("invalid_request", "请求字段不完整或格式不正确。"))
            finally:
                if store:
                    store.close()

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(db_path: Path, owner: str, port: int, ledger_path: Path = DEFAULT_LEDGER):
    server = create_server(db_path, owner, port, ledger_path)
    print(f"本地查询已启动：http://127.0.0.1:{server.server_port}；按 Ctrl+C 停止。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("本地查询已停止。")
    finally:
        server.server_close()
