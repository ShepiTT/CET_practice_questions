"""User-data preservation and actual local-server lifecycle checks."""
import importlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

import desktop_launcher as launcher

HTTP = build_opener(ProxyHandler({}))


def seed_database(resources):
    directory = resources / "instance"
    directory.mkdir(parents=True)
    database = directory / "cet4_v2.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE question_group(id INTEGER PRIMARY KEY, source_file TEXT, audio_start REAL)")
        connection.execute("INSERT INTO question_group VALUES(1, '2025-06-CET4-1.pdf', 10)")
        connection.execute("CREATE TABLE question(id INTEGER PRIMARY KEY, source_file TEXT, group_id INTEGER REFERENCES question_group(id), q_number INTEGER, content TEXT)")
        connection.execute("INSERT INTO question VALUES(1, '2025-06-CET4-1.pdf', 1, 1, 'Bundled question')")
        connection.execute("CREATE TABLE option(id INTEGER PRIMARY KEY, question_id INTEGER REFERENCES question(id), label TEXT, text TEXT)")
        connection.execute("INSERT INTO option VALUES(1, 1, 'A', 'Original option')")
        connection.execute("CREATE TABLE user_history(id INTEGER PRIMARY KEY, answer TEXT)")
        connection.execute("CREATE TABLE wrong_question(id INTEGER PRIMARY KEY, question_id INTEGER)")
    return database


def test_first_launch_installs_seed_and_update_preserves_user_database(tmp_path):
    resources, user_data = tmp_path / "resources", tmp_path / "user"
    seed = seed_database(resources)
    database = launcher.prepare_database(resources, user_data)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO user_history VALUES(1, 'D')")
    with sqlite3.connect(seed) as connection:
        connection.execute("UPDATE question SET content='A later installer seed'")
    assert launcher.prepare_database(resources, user_data) == database
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT answer FROM user_history").fetchall() == [("D",)]
        assert connection.execute("SELECT content FROM question").fetchone() == ("Bundled question",)
    with sqlite3.connect(seed) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_history").fetchone() == (0,)


def test_missing_seed_does_not_create_empty_user_database(tmp_path):
    with pytest.raises(FileNotFoundError, match="安装包缺少题库"):
        launcher.prepare_database(tmp_path / "missing", tmp_path / "user")
    assert not (tmp_path / "user" / "cet4_v2.db").exists()


def test_existing_user_database_does_not_require_seed(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    existing = user / "cet4_v2.db"
    existing.write_bytes(b"preserved bytes")
    assert launcher.prepare_database(tmp_path / "missing", user) == existing
    assert existing.read_bytes() == b"preserved bytes"


def database_rows(path):
    with sqlite3.connect(path) as connection:
        return {table: connection.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
                for table in launcher._EDUCATIONAL + launcher._PERSONAL}


def add_new_seed_paper(path, count=55):
    """Synthetic test questions exercise the real append path, not exam content."""
    paper = "2026-06-CET6-1.pdf"
    with sqlite3.connect(path) as connection:
        next_ids = {table: connection.execute(f'SELECT COALESCE(MAX(id), 0)+1 FROM "{table}"').fetchone()[0]
                    for table in launcher._EDUCATIONAL}
        added = {table: [] for table in launcher._EDUCATIONAL}

        def insert(table, values):
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            values = {name: value for name, value in values.items() if name in columns}
            names = ",".join(f'"{name}"' for name in values)
            placeholders = ",".join("?" for _ in values)
            connection.execute(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})', tuple(values.values()))
            added[table].append(values["id"])

        group_id = next_ids["question_group"]
        insert("question_group", {"id": group_id, "source_file": paper, "title": f"Test questions 1 to {count}",
                                  "group_type": "reading", "passage": "Synthetic upgrade test passage.", "audio_start": 0})
        for index in range(count):
            question_id = next_ids["question"] + index
            insert("question", {"id": question_id, "source_file": paper, "group_id": group_id, "q_number": index + 1,
                                "content": f"Upgrade test question {index + 1}", "correct_answer": "C",
                                "explanation": "Synthetic test explanation."})
            for offset, label in enumerate("ABCD"):
                insert("option", {"id": next_ids["option"] + index * 4 + offset,
                                  "question_id": question_id, "label": label, "text": f"Synthetic {label}"})
    return paper, added


def test_changed_seed_appends_new_paper_and_cache_makes_repeat_a_noop(tmp_path, monkeypatch):
    resources, user = tmp_path / "resources", tmp_path / "user"
    seed = seed_database(resources)
    database = launcher.prepare_database(resources, user)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE question SET content='My corrected question' WHERE id=1")
        connection.execute("UPDATE option SET text='My corrected option' WHERE id=1")
        connection.execute("UPDATE question_group SET audio_start=42 WHERE id=1")
        connection.execute("INSERT INTO user_history VALUES(31, 'D')")
        connection.execute("INSERT INTO wrong_question VALUES(32, 1)")
    original = database_rows(database)
    paper, _ = add_new_seed_paper(seed)
    assert launcher.prepare_database(resources, user) == database
    updated = database_rows(database)
    for table in launcher._EDUCATIONAL + launcher._PERSONAL:
        assert updated[table][:len(original[table])] == original[table]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM question WHERE source_file=?", (paper,)).fetchone() == (55,)
    backups = list(user.glob("*.bak-seed-*"))
    assert len(backups) == 1
    assert database_rows(backups[0]) == original
    before_repeat = database.read_bytes()
    monkeypatch.setattr(launcher, "_seed_digest", lambda _path: pytest.fail("Unchanged seed should use the cached fingerprint"))
    assert launcher.prepare_database(resources, user) == database
    assert database.read_bytes() == before_repeat
    assert list(user.glob("*.bak-seed-*")) == backups


@pytest.mark.parametrize("conflict", ["question_group", "question", "option", "same_paper", "partial_options"])
def test_seed_upgrade_conflicts_leave_all_user_rows_unchanged(tmp_path, conflict):
    resources, user = tmp_path / "resources", tmp_path / "user"
    seed = seed_database(resources)
    database = launcher.prepare_database(resources, user)
    paper, added = add_new_seed_paper(seed)
    with sqlite3.connect(database) as connection:
        if conflict == "question_group":
            connection.execute("INSERT INTO question_group VALUES(?, 'my-custom-paper.pdf', 1)", (added[conflict][0],))
        elif conflict == "question":
            connection.execute("INSERT INTO question VALUES(?, 'my-custom-paper.pdf', 1, 2, 'My own question')", (added[conflict][0],))
        elif conflict == "option":
            connection.execute("INSERT INTO option VALUES(?, 1, 'B', 'My own option')", (added[conflict][0],))
        elif conflict == "same_paper":
            connection.execute("INSERT INTO question_group VALUES(999, ?, 1)", (paper,))
        else:
            connection.execute("DELETE FROM option WHERE id=1")
        connection.execute("INSERT INTO user_history VALUES(33, 'A')")
        connection.execute("INSERT INTO wrong_question VALUES(34, 1)")
    original = database_rows(database)
    before = database.read_bytes()
    with pytest.raises(RuntimeError, match="备份") as caught:
        launcher.prepare_database(resources, user)
    assert "SELECT" not in str(caught.value) and "INSERT" not in str(caught.value)
    assert database_rows(database) == original
    assert database.read_bytes() == before
    backups = list(user.glob("*.bak-seed-*"))
    assert len(backups) == 1 and database_rows(backups[0]) == original


@pytest.mark.parametrize("trigger", ["AFTER INSERT ON question", "AFTER UPDATE ON _cet_seed_state"])
def test_upgrade_rolls_back_all_inserts_if_a_trigger_touches_old_rows(tmp_path, trigger):
    resources, user = tmp_path / "resources", tmp_path / "user"
    seed = seed_database(resources)
    database = launcher.prepare_database(resources, user)
    add_new_seed_paper(seed)
    with sqlite3.connect(database) as connection:
        connection.execute(f"""CREATE TRIGGER bad_upgrade {trigger}
                               BEGIN UPDATE question SET content='unwanted' WHERE id=1; END""")
    before = database_rows(database)
    with pytest.raises(RuntimeError, match="备份") as caught:
        launcher.prepare_database(resources, user)
    assert "not authorized" not in str(caught.value)
    assert database_rows(database) == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM question_group").fetchone() == (1,)


def test_v100_public_database_gains_55_questions_without_changing_any_old_row(tmp_path):
    resources, user = tmp_path / "resources", tmp_path / "user"
    seed = resources / "instance/cet4_v2.db"
    seed.parent.mkdir(parents=True)
    repository = Path(__file__).resolve().parents[1]
    # Read the released Git object, never the workspace's personal database.
    result = subprocess.run(["git", "show", "v1.0.0:instance/cet4_v2.db"], cwd=repository,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        pytest.skip("The v1.0.0 public seed Git object is not available in this checkout")
    seed.write_bytes(result.stdout)
    original_release = database_rows(seed)
    assert len(original_release["question"]) == 5009
    assert original_release["user_history"] == original_release["wrong_question"] == []
    user.mkdir()
    database = user / "cet4_v2.db"
    shutil.copyfile(seed, database)  # An actual v1.0.0 install has no seed marker.
    with sqlite3.connect(database) as connection:
        first_id = connection.execute("SELECT MIN(id) FROM question").fetchone()[0]
        connection.execute("INSERT INTO user_history VALUES(701, ?, 'D', 0, '2026-11-09 12:34:56.123456')", (first_id,))
        connection.execute("INSERT INTO wrong_question VALUES(702, ?, '2026-11-09 12:34:57.654321')", (first_id,))
        connection.execute("UPDATE question SET explanation='My retained explanation' WHERE id=?", (first_id,))
        connection.execute("UPDATE option SET text='My retained option' WHERE id=(SELECT MIN(id) FROM option)")
        connection.execute("UPDATE question_group SET audio_start=42.125 WHERE id=(SELECT MIN(id) FROM question_group)")
    old_rows = database_rows(database)
    paper, added = add_new_seed_paper(seed)
    launcher.prepare_database(resources, user)
    after = database_rows(database)
    for table in launcher._EDUCATIONAL:
        assert after[table][:len(old_rows[table])] == old_rows[table]
        assert len(after[table]) == len(old_rows[table]) + len(added[table])
    for table in launcher._PERSONAL:
        assert after[table] == old_rows[table]
    env = {**os.environ, "CET4_DATABASE_URI": "sqlite:///" + database.as_posix(), "CET4_RESOURCE_DIR": str(repository)}
    check = subprocess.run([sys.executable, "-c", "import app; r=app.app.test_client().get('/practice/2026-06-CET6-1.pdf'); "
                            "assert r.status_code == 200; print(r.get_data(as_text=True).count('data-q-id='))"],
                           cwd=repository, env=env, text=True, capture_output=True)
    assert check.returncode == 0, check.stderr
    assert check.stdout.strip() == "55"
    assert database_rows(database) == after


def test_explicit_database_uri_is_kept_and_does_not_copy_seed(tmp_path, monkeypatch):
    uri = "sqlite:///D:/personal/existing.db"
    monkeypatch.setenv("CET4_DATABASE_URI", uri)
    monkeypatch.delenv("CET4_RESOURCE_DIR", raising=False)
    sentinel = object()
    class ApplicationModule:
        app = sentinel
    monkeypatch.setattr(importlib, "import_module", lambda name: ApplicationModule)
    assert launcher.load_application(tmp_path / "resources", tmp_path / "user") is sentinel
    assert os.environ["CET4_DATABASE_URI"] == uri
    assert os.environ["CET4_RESOURCE_DIR"] == str(tmp_path / "resources")
    assert not (tmp_path / "user").exists()


def test_resource_directory_uses_frozen_bundle_not_current_directory(tmp_path, monkeypatch):
    resources = tmp_path / "frozen"
    monkeypatch.delenv("CET4_RESOURCE_DIR", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(resources), raising=False)
    monkeypatch.chdir(tmp_path)
    assert launcher.resource_directory() == resources.resolve()


def test_browser_executable_selects_browser_mode(monkeypatch):
    monkeypatch.setattr(sys, "executable", "C:/Downloads/CETPractice-Web.exe")
    assert launcher._parser().parse_args([]).mode == "browser"
    assert launcher._parser().parse_args(["--mode", "desktop"]).mode == "desktop"


def test_version_is_loaded_from_bundled_version_file(tmp_path):
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    assert launcher.application_version(tmp_path) == "1.2.3"


def test_kernel_lock_blocks_second_owner_and_releases(tmp_path):
    first = launcher.InstanceLock(tmp_path / "runtime.lock")
    second = launcher.InstanceLock(tmp_path / "runtime.lock")
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


@pytest.fixture
def isolated_launcher(tmp_path):
    resources = tmp_path / "installation"
    resources.mkdir()
    shutil.copyfile(Path(launcher.__file__), resources / "desktop_launcher.py")
    seed_database(resources)
    (resources / "VERSION").write_text("1.0.0-test", encoding="utf-8")
    (resources / "app.py").write_text('''
import os
import sqlite3
from flask import Flask
app = Flask(__name__)
@app.route('/')
def home():
    database = os.environ['CET4_DATABASE_URI'].removeprefix('sqlite:///')
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT content FROM question').fetchone()
    return '<html><head><title>CET test</title></head><body>Practice ready</body></html>'
''', encoding="utf-8")
    env = os.environ.copy()
    env.pop("CET4_DATABASE_URI", None)
    env.pop("CET4_RESOURCE_DIR", None)
    processes = []
    reports = []

    def start(name="primary", timeout=30):
        report = tmp_path / f"{name}.json"
        reports.append(report)
        process = subprocess.Popen([sys.executable, str(resources / "desktop_launcher.py"),
                                    "--mode", "browser", "--no-open", "--data-dir", str(tmp_path / "user"),
                                    "--smoke-report", str(report), "--smoke-timeout", str(timeout)],
                                   cwd=resources, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(process)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if report.exists():
                result = json.loads(report.read_text(encoding="utf-8"))
                assert result["ok"], result
                return process, result
            if process.poll() is not None:
                pytest.fail(f"Launcher exited before readiness: {process.returncode}")
            time.sleep(0.05)
        pytest.fail("Launcher did not become ready")

    yield start, tmp_path / "user"
    # On Windows a venv python.exe can be a redirecting parent process.  Always
    # stop the actual service using its own readiness token before touching the
    # Popen wrapper, including when an assertion failed after startup.
    for report in reports:
        try:
            result = json.loads(report.read_text(encoding="utf-8"))
            if result.get("shutdown_token"):
                close_service(result)
        except (OSError, ValueError, KeyError, URLError):
            pass
    for process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Failure before a ready report needs a process-tree fallback;
                # terminating only the Windows venv wrapper could orphan Python.
                if os.name == "nt":
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                else:
                    process.terminate()
                process.wait(timeout=10)


def close_service(report):
    request = Request(report["url"].rstrip("/") + launcher.SHUTDOWN_PATH, data=b"", method="POST",
                      headers={"X-CET-Shutdown-Token": report["shutdown_token"]})
    with HTTP.open(request, timeout=5) as response:
        assert response.status == 200


def test_real_process_health_requires_secret_shutdown_and_releases_port(isolated_launcher):
    start, data_directory = isolated_launcher
    process, report = start()
    assert report["ui"] == "disabled"
    assert isinstance(report["pid"], int) and report["pid"] > 0
    runtime = json.loads((data_directory / "runtime.json").read_text(encoding="utf-8"))
    assert report["pid"] == runtime["pid"]
    assert process.poll() is None
    assert report["mode"] == "browser"
    assert report["url"].startswith("http://127.0.0.1:")
    with HTTP.open(report["url"].rstrip("/") + launcher.HEALTH_PATH, timeout=5) as response:
        health = json.load(response)
    assert health == {"product": "CETPractice", "version": "1.0.0-test"}
    assert "shutdown_token" not in (data_directory / "runtime.json").read_text(encoding="utf-8")
    for method, token in (("GET", ""), ("POST", ""), ("POST", "wrong-secret")):
        request = Request(report["url"].rstrip("/") + launcher.SHUTDOWN_PATH,
                          method=method, headers={"X-CET-Shutdown-Token": token})
        with pytest.raises(HTTPError) as caught:
            HTTP.open(request, timeout=5)
        assert caught.value.code == 403
    assert process.poll() is None
    close_service(report)
    assert process.wait(timeout=10) == 0
    assert not (data_directory / "runtime.json").exists()
    with pytest.raises((URLError, ConnectionError, OSError)):
        HTTP.open(report["url"], timeout=2)


def test_second_process_reuses_service_and_restart_keeps_history(isolated_launcher):
    start, data_directory = isolated_launcher
    process, first = start()
    with sqlite3.connect(data_directory / "cet4_v2.db") as connection:
        connection.execute("INSERT INTO user_history VALUES(1, 'B')")
    duplicate_process, duplicate = start("duplicate")
    assert duplicate["reused"] is True
    assert duplicate["url"] == first["url"]
    assert duplicate["pid"] == first["pid"]
    assert "shutdown_token" not in duplicate
    assert duplicate_process.wait(timeout=5) == 0
    assert process.poll() is None
    close_service(first)
    assert process.wait(timeout=10) == 0
    restarted_process, restarted = start("restarted")
    assert restarted["shutdown_token"] != first["shutdown_token"]
    with sqlite3.connect(data_directory / "cet4_v2.db") as connection:
        assert connection.execute("SELECT answer FROM user_history").fetchall() == [("B",)]
    close_service(restarted)
    assert restarted_process.wait(timeout=10) == 0


def test_foreign_host_and_cross_origin_requests_cannot_reach_app(isolated_launcher):
    start, _ = isolated_launcher
    process, report = start()
    origin = report["url"].rstrip("/")
    for headers in ({"Host": "attacker.example"}, {"Host": "localhost"},
                    {"Origin": "https://attacker.example"}, {"Origin": "null"}):
        request = Request(report["url"], headers=headers)
        with pytest.raises(HTTPError) as caught:
            HTTP.open(request, timeout=5)
        assert caught.value.code == 403
    with HTTP.open(Request(report["url"], headers={"Origin": origin}), timeout=5) as response:
        assert response.status == 200
    close_service(report)
    assert process.wait(timeout=10) == 0


def test_smoke_timeout_closes_unattended_validation_process(isolated_launcher):
    start, data_directory = isolated_launcher
    process, report = start(timeout=1)
    assert process.wait(timeout=8) == 0
    assert not (data_directory / "runtime.json").exists()
