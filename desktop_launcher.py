"""Run the bundled question bank in a desktop window or a local browser.

Only the seed database belongs to the installation.  The writable database and
the single-instance lock live in the user's application-data directory.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import uuid
import webbrowser

PRODUCT = "CETPractice"
TITLE = "四六级真题练习"
HEALTH_PATH = "/_launcher/health"
SHUTDOWN_PATH = "/_launcher/shutdown"
_HTTP = build_opener(ProxyHandler({}))
_SEED_STATE = "_cet_seed_state"
_EDUCATIONAL = ("question_group", "question", "option")
_PERSONAL = ("user_history", "wrong_question")
_IDENTITY = {"question_group": ("source_file",),
             "question": ("source_file", "group_id", "q_number"),
             "option": ("question_id", "label")}


def resource_directory() -> Path:
    configured = os.environ.get("CET4_RESOURCE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent)).resolve()


def default_data_directory() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base).expanduser() / PRODUCT
    return Path.home() / ".local" / "share" / PRODUCT


def application_version(resources: Path) -> str:
    version_file = resources / "VERSION"
    if version_file.is_file():
        value = version_file.read_text(encoding="utf-8-sig").strip()
        if value and len(value) < 64 and "\n" not in value:
            return value
    return "1.0.0"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class InstanceLock:
    """A kernel-owned lock: crashes release it without deleting anyone's file."""

    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"\0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.stream.close()
            self.stream = None
            return False
        return True

    def release(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None


def _seed_stat(seed: Path) -> tuple[str, int, int, int]:
    stat = seed.stat()
    return str(seed.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _seed_digest(seed: Path) -> str:
    digest = hashlib.sha256()
    with seed.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _seed_state(connection) -> tuple | None:
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_SEED_STATE,)).fetchone()
    if exists:
        return connection.execute(f'SELECT seed_sha256, seed_path, seed_size, seed_mtime_ns, seed_ctime_ns FROM "{_SEED_STATE}" WHERE id=1').fetchone()
    return None


def _record_seed(connection, digest: str, signature: tuple) -> None:
    connection.execute(f'''CREATE TABLE IF NOT EXISTS "{_SEED_STATE}" (
        id INTEGER PRIMARY KEY CHECK(id=1), seed_sha256 TEXT NOT NULL,
        seed_path TEXT NOT NULL, seed_size INTEGER NOT NULL,
        seed_mtime_ns INTEGER NOT NULL, seed_ctime_ns INTEGER NOT NULL)''')
    def authorize(action, table, _column, _database, _trigger):
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            return sqlite3.SQLITE_OK if table == _SEED_STATE else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    connection.set_authorizer(authorize)
    try:
        connection.execute(f'''INSERT INTO "{_SEED_STATE}" VALUES(1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET seed_sha256=excluded.seed_sha256,
            seed_path=excluded.seed_path, seed_size=excluded.seed_size,
            seed_mtime_ns=excluded.seed_mtime_ns, seed_ctime_ns=excluded.seed_ctime_ns''', (digest, *signature))
    finally:
        connection.set_authorizer(None)


def _table_rows(connection, table: str) -> tuple[list[str], dict[int, tuple]]:
    # All callers use the fixed educational/personal table whitelist above.
    columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
    if "id" not in columns:
        raise ValueError("题库表结构不完整，无法安全追加新试卷。")
    index = columns.index("id")
    return columns, {row[index]: tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY id')}


def _append_new_papers(connection, source) -> None:
    before = {table: _table_rows(connection, table) for table in _EDUCATIONAL + _PERSONAL}
    bundled = {table: _table_rows(source, table) for table in _EDUCATIONAL}
    for table in _EDUCATIONAL:
        columns, existing = before[table]
        seed_columns, seed_rows = bundled[table]
        if columns != seed_columns or any(field not in columns for field in _IDENTITY[table]):
            raise ValueError("题库表结构与新版不一致，无法安全追加新试卷。")
        identity = [columns.index(field) for field in _IDENTITY[table]]
        for row_id in existing.keys() & seed_rows.keys():
            if tuple(existing[row_id][i] for i in identity) != tuple(seed_rows[row_id][i] for i in identity):
                raise ValueError("新版试卷编号与本机题目冲突，已取消追加；旧题和练习记录未被覆盖。")

    def paper_ids(tables):
        result = {}
        for table in ("question_group", "question"):
            columns, rows = tables[table]
            index = columns.index("source_file")
            for row_id, row in rows.items():
                result.setdefault(row[index], {}).setdefault(table, set()).add(row_id)
        return result

    installed, available = paper_ids(before), paper_ids(bundled)
    for paper in installed.keys() & available.keys():
        for table in ("question_group", "question"):
            if not available[paper].get(table, set()).issubset(installed[paper].get(table, set())):
                raise ValueError(f"本机已存在同名试卷 {paper}，但题目编号不一致，已取消追加。")
    new_papers = available.keys() - installed.keys()
    additions = {}
    for table in ("question_group", "question"):
        columns, rows = bundled[table]
        index = columns.index("source_file")
        additions[table] = {row_id: row for row_id, row in rows.items() if row[index] in new_papers}
    option_columns, options = bundled["option"]
    question_index = option_columns.index("question_id")
    additions["option"] = {row_id: row for row_id, row in options.items()
                           if row[question_index] in additions["question"]}
    # An existing paper must also retain all source option identities. A partial
    # manual import is not silently treated as an already installed new paper.
    for row_id, row in options.items():
        if row_id not in before["option"][1] and row_id not in additions["option"]:
            raise ValueError("本机已有同名试卷的选项不完整，已取消追加，请先核对该卷。")

    def authorize(action, table, _column, _database, _trigger):
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            if action == sqlite3.SQLITE_INSERT and table in _EDUCATIONAL:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    try:
        for table in _EDUCATIONAL:
            columns, existing = before[table]
            if existing.keys() & additions[table].keys():
                raise ValueError("新增试卷的编号已被本机题目占用，已取消追加。")
            names = ", ".join(f'"{name}"' for name in columns)
            placeholders = ", ".join("?" for _ in columns)
            connection.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})', additions[table].values())
        for table in _EDUCATIONAL + _PERSONAL:
            expected = {**before[table][1], **additions.get(table, {})}
            if _table_rows(connection, table) != (before[table][0], expected):
                raise ValueError("追加后检测到旧题或练习记录变化，已取消本次更新。")
    finally:
        connection.set_authorizer(None)


def _upgrade_database(seed: Path, destination: Path) -> None:
    signature = _seed_stat(seed)
    backup = None
    try:
        with closing(sqlite3.connect(destination, timeout=30, isolation_level=None)) as connection:
            state = _seed_state(connection)
            if state and tuple(state[1:]) == signature:
                return
            digest = _seed_digest(seed)
            if _seed_stat(seed) != signature:
                raise ValueError("安装包题库正在变化，请重新解压完整程序后启动。")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                # File metadata changes after moving an unchanged installation
                # only refresh the cache; they do not trigger another upgrade.
                if state and state[0] == digest:
                    _record_seed(connection, digest, signature)
                    connection.commit()
                    return
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup = destination.with_name(destination.name + ".bak-seed-" + stamp)
                # A separate read connection backs up the committed snapshot;
                # backup() on the connection owning BEGIN IMMEDIATE can hang.
                with closing(sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True)) as original:
                    with closing(sqlite3.connect(backup)) as saved:
                        original.backup(saved)
                        if saved.execute("PRAGMA quick_check").fetchone() != ("ok",):
                            raise ValueError("个人题库备份校验失败，已取消更新。")
                with closing(sqlite3.connect(seed.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                    if source.execute("PRAGMA integrity_check").fetchone() != ("ok",) or source.execute("PRAGMA foreign_key_check").fetchall():
                        raise ValueError("安装包题库校验失败，已取消更新。")
                    _append_new_papers(connection, source)
                if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",) or connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("更新后题库校验失败，已取消更新。")
                if _seed_stat(seed) != signature:
                    raise ValueError("安装包题库正在变化，已取消更新。")
                _record_seed(connection, digest, signature)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    except Exception as error:
        # Do not put raw SQL or row contents in the user-facing error dialog.
        logging.exception("Bundled seed upgrade failed")
        reason = str(error) if isinstance(error, ValueError) else "题库更新未完成，请查看启动日志。"
        recovery = f"\n更新前备份：{backup}" if backup and backup.is_file() else "\n原题库和练习记录已保留。"
        raise RuntimeError(reason + recovery) from error


def prepare_database(resources: Path, data_directory: Path) -> Path:
    """Install once, then append newly bundled papers without replacing old rows."""
    destination = data_directory / "cet4_v2.db"
    seed = resources / "instance" / "cet4_v2.db"
    if destination.exists():
        if not destination.is_file():
            raise RuntimeError(f"练习数据库路径不是文件：{destination}")
        if seed.is_file():
            _upgrade_database(seed, destination)
        return destination
    if not seed.is_file():
        raise FileNotFoundError(f"安装包缺少题库：{seed}")
    data_directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="cet4-seed-", suffix=".db", dir=data_directory)
    os.close(fd)
    try:
        signature = _seed_stat(seed)
        digest = _seed_digest(seed)
        if _seed_stat(seed) != signature:
            raise RuntimeError("安装包题库正在变化，请重新启动。")
        with closing(sqlite3.connect(seed.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
                result = target.execute("PRAGMA quick_check").fetchone()
                if result != ("ok",):
                    raise RuntimeError("安装包题库校验失败。")
                if _seed_stat(seed) != signature:
                    raise RuntimeError("安装包题库正在变化，请重新启动。")
                _record_seed(target, digest, signature)
                target.commit()
        # The caller owns InstanceLock.  This extra guard also protects direct use.
        if destination.exists():
            return destination
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def load_application(resources: Path, data_directory: Path):
    os.environ["CET4_RESOURCE_DIR"] = str(resources)
    # Explicit caller overrides remain supported, as in the source application.
    if not os.environ.get("CET4_DATABASE_URI"):
        database = prepare_database(resources, data_directory)
        os.environ["CET4_DATABASE_URI"] = "sqlite:///" + database.as_posix()
    return importlib.import_module("app").app


class LocalService:
    def __init__(self, application, version: str, port: int = 0):
        from werkzeug.serving import make_server

        self.version = version
        self.instance_id = str(uuid.uuid4())
        self.shutdown_token = secrets.token_urlsafe(32)
        self.stopped = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = threading.Event()
        self._server = make_server("127.0.0.1", port, self._wrap(application), threaded=True)
        self.port = self._server.server_port
        self.url = f"http://127.0.0.1:{self.port}/"
        self._thread = threading.Thread(target=self._server.serve_forever, name="cet-http", daemon=True)

    def _wrap(self, application):
        def respond(start_response, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            start_response(status, [("Content-Type", "application/json; charset=utf-8"),
                                    ("Content-Length", str(len(body))),
                                    ("Cache-Control", "no-store")])
            return [body]

        def handler(environ, start_response):
            # The packaged service is reachable only under its own loopback
            # origin, including writes sent by unrelated websites.
            expected_origin = self.url.rstrip("/")
            if environ.get("HTTP_HOST") != f"127.0.0.1:{self.port}":
                return respond(start_response, "403 Forbidden", {"error": "local access only"})
            origin = environ.get("HTTP_ORIGIN")
            if origin is not None and origin != expected_origin:
                return respond(start_response, "403 Forbidden", {"error": "origin"})
            path = environ.get("PATH_INFO", "")
            if path == HEALTH_PATH:
                if environ.get("REQUEST_METHOD") != "GET":
                    return respond(start_response, "405 Method Not Allowed", {"error": "method"})
                return respond(start_response, "200 OK", {"product": PRODUCT, "version": self.version})
            if path == SHUTDOWN_PATH:
                token = environ.get("HTTP_X_CET_SHUTDOWN_TOKEN", "")
                if environ.get("REQUEST_METHOD") != "POST" or not secrets.compare_digest(token, self.shutdown_token):
                    return respond(start_response, "403 Forbidden", {"error": "forbidden"})
                # shutdown cannot run on the serving thread itself.
                threading.Thread(target=self.stop, name="cet-shutdown", daemon=True).start()
                return respond(start_response, "200 OK", {"stopping": True})
            return application(environ, start_response)
        return handler

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if not self._shutdown_lock.acquire(blocking=False):
            self._shutdown_done.wait(timeout=10)
            return
        try:
            self.stopped.set()
            if self._thread.is_alive():
                self._server.shutdown()
            self._server.server_close()
            if threading.current_thread() is not self._thread and self._thread.ident:
                self._thread.join(timeout=5)
        finally:
            self._shutdown_done.set()


def _get_json(url: str) -> dict[str, Any]:
    with _HTTP.open(url, timeout=3) as response:
        return json.load(response)


def running_instance(data_directory: Path, timeout: float = 5) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = json.loads((data_directory / "runtime.json").read_text(encoding="utf-8"))
            port = state["port"]
            if not isinstance(port, int) or not 0 < port < 65536:
                raise ValueError("bad port")
            # Never open a URL supplied by an untrusted state file.
            url = f"http://127.0.0.1:{port}/"
            health = _get_json(url.rstrip("/") + HEALTH_PATH)
            if health.get("product") == PRODUCT:
                return {**state, "url": url, "version": health.get("version")}
        except (OSError, ValueError, KeyError, URLError):
            pass
        time.sleep(0.1)
    return None


def _smoke_base(service: LocalService, mode: str, data_directory: Path, resources: Path) -> dict[str, Any]:
    health = _get_json(service.url.rstrip("/") + HEALTH_PATH)
    with _HTTP.open(service.url, timeout=30) as response:
        status = response.status
        html = response.read().decode("utf-8", errors="replace")
    return {"ok": health.get("product") == PRODUCT and status == 200 and "<html" in html.lower(),
            "url": service.url, "pid": os.getpid(), "mode": mode,
            "data_path": str(data_directory / "cet4_v2.db"),
            "resource_dir": str(resources), "version": service.version,
            "frozen": bool(getattr(sys, "frozen", False)),
            "homepage_status": status, "shutdown_token": service.shutdown_token}


def _serve_without_window(service: LocalService, report: Path | None, base: dict[str, Any]) -> None:
    if report:
        _write_json(report, {**base, "ui": "disabled"})
    service.stopped.wait()


def _run_browser(service: LocalService, report: Path | None, base: dict[str, Any]) -> None:
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(TITLE + " · 网页启动器")
    root.geometry("500x215")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="练习服务已启动", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
    ttk.Label(frame, text="在浏览器中做题，关闭此窗口后停止本机服务。").pack(anchor="w", pady=(10, 6))
    ttk.Label(frame, text=service.url).pack(anchor="w")
    buttons = ttk.Frame(frame)
    buttons.pack(anchor="e", pady=(18, 0))
    ttk.Button(buttons, text="打开练习网页", command=lambda: webbrowser.open(service.url)).pack(side="left", padx=(0, 10))
    ttk.Button(buttons, text="关闭服务", command=service.stop).pack(side="left")

    def check_closed():
        if service.stopped.is_set():
            root.destroy()
        else:
            root.after(100, check_closed)

    def ready():
        if report:
            _write_json(report, {**base, "ui": "browser-control", "control_window_mapped": bool(root.winfo_ismapped()),
                                 "control_window_title": root.title()})
        webbrowser.open(service.url)

    root.protocol("WM_DELETE_WINDOW", service.stop)
    root.after(100, check_closed)
    root.after(150, ready)
    try:
        root.mainloop()
    finally:
        service.stop()


def _run_desktop(service: LocalService, report: Path | None, base: dict[str, Any]) -> None:
    import webview  # Browser distribution does not need pywebview.

    window = webview.create_window(TITLE, service.url, width=1200, height=850,
                                   min_size=(820, 620), text_select=True)

    def loaded():
        if report:
            try:
                dom = window.evaluate_js("JSON.stringify({title: document.title, ready_state: document.readyState, "
                                         "body_present: Boolean(document.body), "
                                         "question_links: document.querySelectorAll('a[href*=\"/practice/\"]').length})")
                if isinstance(dom, str):
                    dom = json.loads(dom)
                _write_json(report, {**base, "ok": bool(base["ok"] and dom.get("body_present")),
                                     "ui": "desktop-window", "desktop_dom": dom})
            except Exception as error:
                logging.exception("Desktop readiness check failed")
                _write_json(report, {**base, "ok": False, "ui": "desktop-window", "error": str(error)})
                service.stop()

    def watch_shutdown():
        service.stopped.wait()
        try:
            window.destroy()
        except Exception:
            logging.exception("Could not close desktop window")

    window.events.loaded += loaded
    try:
        webview.start(watch_shutdown, gui="edgechromium", debug=False)
    finally:
        service.stop()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=TITLE)
    default_mode = "browser" if "-web" in Path(sys.executable).stem.lower() else "desktop"
    parser.add_argument("--mode", choices=("desktop", "browser"), default=default_mode)
    parser.add_argument("--no-open", action="store_true", help="只运行本机服务，不打开窗口")
    parser.add_argument("--data-dir", type=Path, default=default_data_directory())
    parser.add_argument("--port", type=int, default=0, help="0 自动选择空闲本机端口")
    parser.add_argument("--smoke-report", type=Path, help="写出自动验证报告（包含仅本次有效的关闭令牌）")
    parser.add_argument("--smoke-timeout", type=float, default=180, help="验证运行的最长秒数")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port 必须是 0 到 65535")
    if args.smoke_report and args.smoke_timeout <= 0:
        parser.error("--smoke-timeout 必须大于 0")
    data_directory = args.data_dir.expanduser().resolve()
    resources = resource_directory()
    data_directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=data_directory / "launcher.log", level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    lock = InstanceLock(data_directory / "runtime.lock")
    if not lock.acquire():
        existing = running_instance(data_directory)
        if existing:
            if args.smoke_report:
                _write_json(args.smoke_report, {"ok": True, "reused": True, "url": existing["url"],
                                               "pid": existing.get("pid"), "mode": args.mode,
                                               "data_path": str(data_directory / "cet4_v2.db")})
            if not args.no_open:
                webbrowser.open(existing["url"])
            return 0
        return 2
    service = None
    timer = None
    try:
        application = load_application(resources, data_directory)
        service = LocalService(application, application_version(resources), args.port)
        service.start()
        _write_json(data_directory / "runtime.json", {"pid": os.getpid(), "port": service.port,
                                                     "mode": args.mode, "instance_id": service.instance_id})
        if args.smoke_report:
            timer = threading.Timer(args.smoke_timeout, service.stop)
            timer.daemon = True
            timer.start()
        base = _smoke_base(service, args.mode, data_directory, resources) if args.smoke_report else {}
        if args.no_open:
            _serve_without_window(service, args.smoke_report, base)
        elif args.mode == "browser":
            _run_browser(service, args.smoke_report, base)
        else:
            _run_desktop(service, args.smoke_report, base)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        logging.exception("Launcher failed")
        if args.smoke_report:
            _write_json(args.smoke_report, {"ok": False, "error": str(error), "mode": args.mode,
                                           "pid": os.getpid(), "data_path": str(data_directory / "cet4_v2.db")})
        if not args.no_open and not args.smoke_report:
            try:
                from tkinter.messagebox import showerror
                showerror(TITLE, f"启动失败：{error}\n\n详细记录：{data_directory / 'launcher.log'}")
            except Exception:
                pass
        return 1
    finally:
        if timer:
            timer.cancel()
        if service:
            service.stop()
            state_path = data_directory / "runtime.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if state.get("instance_id") == service.instance_id:
                    state_path.unlink()
            except (OSError, ValueError):
                pass
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
