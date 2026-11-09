"""Privacy and resource-boundary checks for the actual release packager."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/build_windows_release.py"
SPEC = importlib.util.spec_from_file_location("windows_release_build", MODULE_PATH)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def seed(path, private=True):
    with sqlite3.connect(path) as connection:
        for table in builder.EDUCATIONAL_TABLES:
            connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT)')
            connection.execute(f'INSERT INTO "{table}" VALUES (1, ?)', ("public learning material",))
        for table in builder.PERSONAL_TABLES:
            connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT)')
            if private:
                connection.execute(f'INSERT INTO "{table}" VALUES (1, ?)', ("PRIVATE_RECORD_DO_NOT_SHIP",))


def test_distribution_seed_drops_private_rows_and_free_pages(tmp_path):
    source = tmp_path / "explicit-public.db"
    target = tmp_path / "stage/instance/cet4_v2.db"
    seed(source)
    source_before = source.read_bytes()
    expected = builder.database_fingerprints(source)
    report = builder.prepare_database(target, source)
    assert source.read_bytes() == source_before
    actual = builder.database_fingerprints(target)
    assert all(actual[table] == expected[table] for table in builder.EDUCATIONAL_TABLES)
    assert all(actual[table]["rows"] == 0 for table in builder.PERSONAL_TABLES)
    assert b"PRIVATE_RECORD_DO_NOT_SHIP" not in target.read_bytes()
    assert report["educational_rows_unchanged"] is True


def test_packager_refuses_unknown_database_tables(tmp_path):
    source = tmp_path / "unknown.db"
    seed(source)
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE private_notes (id INTEGER PRIMARY KEY, body TEXT)")
    with pytest.raises(ValueError, match="Unreviewed database tables"):
        builder.prepare_database(tmp_path / "stage.db", source)


def test_packager_refuses_live_database_as_explicit_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    live = tmp_path / "instance/cet4_v2.db"
    live.parent.mkdir()
    seed(live)
    with pytest.raises(ValueError, match="live practice database"):
        builder.prepare_database(tmp_path / "stage.db", live)


@pytest.mark.parametrize("paper_count", [1, 94])
def test_educational_assets_are_exact_narrow_whitelist(tmp_path, paper_count):
    for folder, extension in (("data", ".pdf"), ("data/answers", ".pdf"), ("data/audio", ".mp3")):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
        for index in range(paper_count):
            (tmp_path / folder / f"paper-{index}{extension}").touch()
    (tmp_path / "data/shared_listening.json").write_text("{}")
    (tmp_path / "data/ocr").mkdir()
    (tmp_path / "data/ocr/private-cache.txt").write_text("not for distribution")
    files = builder.educational_assets(tmp_path)
    assert len(files) == paper_count * 3 + 1
    assert all("ocr" not in path.parts for path in files)
    (tmp_path / "data/audio/paper-0.mp3").rename(tmp_path / "data/audio/wrong-name.mp3")
    with pytest.raises(ValueError, match="filenames do not match"):
        builder.educational_assets(tmp_path)


def fake_runtime(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "_internal/data").mkdir(parents=True)
    (runtime / "_internal/instance").mkdir()
    (runtime / "licenses").mkdir()
    (runtime / "licenses/Python-LICENSE.txt").write_text("Python license")
    exe = runtime / "CETPractice.exe"
    exe.write_bytes(b"MZ_FAKE_EXE_FOR_ARCHIVE_TEST")
    data = runtime / "_internal/data/shared_listening.json"
    data.write_text("{}")
    database = runtime / "_internal/instance/cet4_v2.db"
    seed(database, private=False)
    resources = {}
    for file in (data, database):
        resources[file.relative_to(runtime / "_internal").as_posix()] = {
            "size": file.stat().st_size, "sha256": builder.sha256(file)}
    manifest = {"version": "1.0.0", "source": {"kind": "working-tree"}, "source_files": {},
                "runtime_executable": {"sha256": builder.sha256(exe)}, "resources": resources,
                "database": {"tables": builder.database_fingerprints(database)}}
    builder.json_write(runtime / "RELEASE-MANIFEST.json", manifest)
    return runtime


def test_archive_modes_contain_separate_names_and_same_seed(tmp_path):
    runtime = fake_runtime(tmp_path)
    output = tmp_path / "downloads"
    report = builder.create_archives(runtime, output, compression_level=1)
    assert len(report["archives"]) == 2
    for mode, (folder, executable) in builder.VERSIONS.items():
        archive = output / f"cet-practice-1.0.0-windows-{mode}-x64.zip"
        with zipfile.ZipFile(archive) as stream:
            assert stream.read(f"{folder}/{executable}") == (runtime / "CETPractice.exe").read_bytes()
            assert f"{folder}/_internal/instance/cet4_v2.db" in stream.namelist()
        assert report["archives"][archive.name]["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert len((output / "SHA256SUMS.txt").read_text().splitlines()) == 2


def test_each_extracted_variant_manifest_matches_executable_paths(tmp_path):
    runtime = fake_runtime(tmp_path)
    original_manifest = json.loads((runtime / "RELEASE-MANIFEST.json").read_text())
    original_manifest["source_files"] = {"desktop_launcher.py": "recorded-source-hash"}
    original_manifest["runtime_files"] = {
        path.relative_to(runtime).as_posix(): {"size": path.stat().st_size, "sha256": builder.sha256(path)}
        for path in runtime.rglob("*") if path.is_file() and path.name != "RELEASE-MANIFEST.json"}
    builder.json_write(runtime / "RELEASE-MANIFEST.json", original_manifest)
    base_manifest_bytes = (runtime / "RELEASE-MANIFEST.json").read_bytes()
    output = tmp_path / "downloads"
    builder.create_archives(runtime, output, compression_level=1)
    for mode, (folder, executable) in builder.VERSIONS.items():
        archive = output / f"cet-practice-1.0.0-windows-{mode}-x64.zip"
        with zipfile.ZipFile(archive) as stream:
            stream.extractall(tmp_path / "extracted")
        extracted = tmp_path / "extracted" / folder
        manifest = builder.validate_runtime(extracted)
        assert manifest["runtime_executable"]["filename"] == executable
        assert executable in manifest["runtime_files"]
        assert manifest["variant"] == mode
        assert manifest["default_mode"] == ("desktop" if mode == "desktop" else "browser")
        assert manifest["source_files"] == original_manifest["source_files"]
        if mode == "web-launcher":
            assert "CETPractice.exe" not in manifest["runtime_files"]
            assert not (extracted / "CETPractice.exe").exists()
    # Creating variants must never rewrite the already-tested base runtime.
    assert (runtime / "RELEASE-MANIFEST.json").read_bytes() == base_manifest_bytes


def test_runtime_validation_rejects_extra_data_and_mutation(tmp_path):
    runtime = fake_runtime(tmp_path)
    builder.validate_runtime(runtime)
    extra = runtime / "_internal/data/private-cache.txt"
    extra.write_text("must never ship")
    with pytest.raises(ValueError, match="unreviewed files"):
        builder.validate_runtime(runtime)
    extra.unlink()
    (runtime / "_internal/data/shared_listening.json").write_text("modified")
    with pytest.raises(ValueError, match="differs from source staging"):
        builder.validate_runtime(runtime)


def test_existing_release_is_checked_before_any_archive_is_written(tmp_path):
    runtime = fake_runtime(tmp_path)
    output = tmp_path / "downloads"
    output.mkdir()
    existing = output / "cet-practice-1.0.0-windows-web-launcher-x64.zip"
    existing.write_bytes(b"existing release")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        builder.create_archives(runtime, output)
    assert list(output.iterdir()) == [existing]
