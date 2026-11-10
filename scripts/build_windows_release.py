"""Build two self-contained Windows x64 distributions without personal records.

Run with the isolated environment from requirements-build.txt.  The first stage
can build only the onedir runtime; after testing it, --reuse-runtime creates the
two ZIPs without rebuilding Python or copying the recordings twice.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
EDUCATIONAL_TABLES = ("question_group", "question", "option")
PERSONAL_TABLES = ("user_history", "wrong_question")
SOURCE_MODULES = ("desktop_launcher.py", "app.py", "models.py", "parser.py", "ocr.py", "shared_listening.py", "verified_corrections.py")
VERSIONS = {
    "desktop": ("CETPractice", "CETPractice.exe"),
    "web-launcher": ("CETPractice-Web", "CETPractice-Web.exe"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def json_write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def contained(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or resolved == root.resolve():
        raise ValueError(f"Path must be inside {root}: {resolved}")
    return resolved


def rows_fingerprint(connection: sqlite3.Connection, table: str) -> dict:
    # Table names are a fixed application whitelist, never user input.
    columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
    if not columns:
        raise ValueError(f"Missing database table: {table}")
    rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()
    payload = json.dumps({"columns": columns, "rows": rows}, ensure_ascii=False, separators=(",", ":"))
    return {"rows": len(rows), "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()}


def database_fingerprints(path: Path) -> dict:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        unexpected = tables - set(EDUCATIONAL_TABLES + PERSONAL_TABLES) - {"sqlite_sequence"}
        if unexpected:
            raise ValueError(f"Unreviewed database tables: {sorted(unexpected)}")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Database has broken foreign keys")
        return {table: rows_fingerprint(connection, table) for table in EDUCATIONAL_TABLES + PERSONAL_TABLES}


def prepare_database(destination: Path, public_database: Path | None = None, git_ref: str = "HEAD") -> dict:
    """Export the committed public seed, never silently copy the live workspace DB."""
    if destination.exists():
        raise ValueError(f"Refusing to overwrite database staging file: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = {"kind": "git", "ref": git_ref, "path": "instance/cet4_v2.db"}
    if public_database is None:
        commit = subprocess.check_output(["git", "rev-parse", "--verify", f"{git_ref}^{{commit}}"], cwd=ROOT, text=True).strip()
        # Binary stdout must not pass through PowerShell redirection.
        with destination.open("xb") as stream:
            subprocess.run(["git", "show", f"{commit}:instance/cet4_v2.db"], cwd=ROOT, check=True, stdout=stream)
        source["commit"] = commit
    else:
        public_database = public_database.resolve(strict=True)
        if public_database == (ROOT / "instance/cet4_v2.db").resolve():
            raise ValueError("The live practice database cannot be an explicit public database")
        with sqlite3.connect(public_database.as_uri() + "?mode=ro", uri=True) as source_connection:
            with sqlite3.connect(destination) as target_connection:
                source_connection.backup(target_connection)
        source = {"kind": "explicit-public-database", "sha256": sha256(public_database)}
    before = database_fingerprints(destination)
    with sqlite3.connect(destination) as connection:
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        # Clear the child records first; the educational rows and their IDs remain.
        connection.execute("DELETE FROM wrong_question")
        connection.execute("DELETE FROM user_history")
        has_sequence = connection.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone()
        if has_sequence:
            connection.execute("DELETE FROM sqlite_sequence WHERE name IN ('user_history','wrong_question')")
        connection.commit()
        connection.execute("VACUUM")
    after = database_fingerprints(destination)
    if any(before[table] != after[table] for table in EDUCATIONAL_TABLES):
        raise ValueError("Sanitizing changed educational database rows")
    if any(after[table]["rows"] != 0 for table in PERSONAL_TABLES):
        raise ValueError("Distribution database contains personal records")
    return {"source": source, "sha256": sha256(destination), "tables": after, "educational_rows_unchanged": True}


def educational_assets(root: Path = ROOT) -> list[Path]:
    """A narrow resource whitelist deliberately excludes OCR and ASR caches."""
    question_pdfs = sorted((root / "data").glob("*.pdf"))
    answer_pdfs = sorted((root / "data/answers").glob("*.pdf"))
    audio = sorted((root / "data/audio").glob("*.mp3"))
    if not question_pdfs or not answer_pdfs or not audio:
        raise ValueError("Question PDFs, answer PDFs, and MP3s must all be present")
    names = {path.stem for path in question_pdfs}
    if names != {path.stem for path in answer_pdfs} or names != {path.stem for path in audio}:
        raise ValueError("Question, answer, and audio filenames do not match")
    config = root / "data/shared_listening.json"
    if not config.is_file():
        raise ValueError("Missing shared-listening source configuration")
    return question_pdfs + answer_pdfs + audio + [config]


def copy_resources(stage: Path, database: dict, version: str) -> dict:
    files = educational_assets()
    for folder in ("templates", "static"):
        candidates = sorted((ROOT / folder).rglob("*"))
        files.extend(path for path in candidates if path.is_file())
    manifest = {}
    for source in files:
        if source.is_symlink():
            raise ValueError(f"Symlink resources are not allowed: {source}")
        relative = source.relative_to(ROOT)
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        manifest[relative.as_posix()] = {"size": target.stat().st_size, "sha256": sha256(target)}
    seeded = stage / "instance/cet4_v2.db"
    manifest["instance/cet4_v2.db"] = {"size": seeded.stat().st_size, "sha256": database["sha256"]}
    version_file = stage / "VERSION"
    version_file.write_text(version + "\n", encoding="ascii")
    manifest["VERSION"] = {"size": version_file.stat().st_size, "sha256": sha256(version_file)}
    return manifest


def license_filename(path: str) -> bool:
    name = Path(path).name.lower()
    return bool(re.search(r"licen[cs]e|copying|notice|copyright|authors|third.?party", name))


def collect_notices(destination: Path) -> dict:
    """Copy actual installed licenses, including bundled native DLL notices."""
    destination.mkdir(parents=True, exist_ok=True)
    python_candidates = [Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE"]
    python_license = next((path for path in python_candidates if path.is_file()), None)
    if python_license is None:
        raise ValueError("The CPython redistribution license could not be located")
    shutil.copy2(python_license, destination / "Python-LICENSE.txt")
    # The pywebview wheel contains these Microsoft SDK binaries but does not
    # carry their upstream notices. Preserve the matching official NuGet texts.
    microsoft_notices = ROOT / "docs/licenses/Microsoft.Web.WebView2-1.0.3856.49"
    microsoft_source = json.loads((microsoft_notices / "SOURCE.json").read_text(encoding="utf-8"))
    webview_distribution = metadata.distribution("pywebview")
    for relative, expected in microsoft_source["bundled_dll_sha256"].items():
        binary = Path(webview_distribution.locate_file("webview/lib/" + relative))
        if not binary.is_file() or sha256(binary) != expected:
            raise ValueError("The WebView2 SDK changed; collect notices matching its current binaries")
    shutil.copytree(microsoft_notices, destination / microsoft_notices.name)
    packages = []
    for distribution in sorted(metadata.distributions(), key=lambda item: item.metadata["Name"].lower()):
        name = distribution.metadata["Name"]
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        copied = []
        for index, package_file in enumerate(distribution.files or []):
            if not license_filename(str(package_file)):
                continue
            source = Path(distribution.locate_file(package_file))
            if not source.is_file():
                continue
            # Packages may contain same-named upstream licenses in nested folders.
            target = destination / safe_name / f"{index:05d}-{source.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(target.relative_to(destination).as_posix())
        packages.append({"name": name, "version": distribution.version,
                         "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "See package license files",
                         "license_files": copied})
    json_write(destination / "build-environment-packages.json", packages)
    return {"python": sys.version, "packages": packages}


def write_root_documents(runtime: Path, licenses: Path, version: str, packages: dict) -> None:
    shutil.copy2(ROOT / "LICENSE", runtime / "LICENSE.txt")
    shutil.copytree(licenses, runtime / "licenses")
    package_lines = "\n".join(f"- {item['name']} {item['version']}" for item in packages["packages"])
    notice = f"""CET Practice {version} — third-party notices

Project source: https://github.com/ShepiTT/CET_practice_questions
Project code license: Apache License 2.0 (LICENSE.txt).

This distribution embeds CPython and open-source runtime components. Their
original license and copyright files are preserved under licenses/. The build
environment inventory below may include packaging tools not shipped at runtime.
PyInstaller's bootloader exception permits distribution of the generated app;
its original license is included in licenses/ as supplied by the build package.

The desktop window uses pywebview and the installed Microsoft Edge WebView2
Runtime. The WebView2 Runtime itself is not bundled. Microsoft WebView2 SDK
components are bundled with pywebview. Their matching official NuGet license
and third-party notices are preserved in licenses/Microsoft.Web.WebView2-1.0.3856.49/.

Question PDFs, answer PDFs, and audio were obtained from CETTong's public resource
repository: https://github.com/whoamiARC/cet-website . These educational source
materials retain their original authorship and notices; the software license
does not relicense third-party examination content.

Build environment packages:
{package_lines}
"""
    (runtime / "THIRD_PARTY_NOTICES.txt").write_text(notice, encoding="utf-8")
    instructions = f"""四、六级真题刷题系统 {version}

请先将整个 ZIP 解压到普通文件夹，再双击程序；保留 _internal 文件夹。

桌面程序版：双击 CETPractice.exe，在独立窗口中练习。
网页启动器版：双击 CETPractice-Web.exe，随后在默认浏览器中练习。
两版均自带 Python 运行环境、题库及全部配套试卷 PDF、答案 PDF 和
MP3，不需要另装 Python，也不需要注册账号。

桌面版需要系统 Microsoft Edge WebView2 Runtime；Windows 10/11 通常已具备。
如缺少运行环境，可使用网页启动器版，或从微软官网下载 WebView2 Runtime：
https://developer.microsoft.com/microsoft-edge/webview2/

练习记录由程序保存到当前 Windows 用户的数据目录，两种启动模式共用记录。
更新前关闭旧程序，再将新版本解压到新文件夹。启动时会备份并追加新增试卷，
保留已有题目修订和个人练习记录。关闭网页不会立即停止后台服务，
请使用启动器提供的退出入口停止服务。

项目与使用说明：https://github.com/ShepiTT/CET_practice_questions
"""
    (runtime / "使用说明.txt").write_text(instructions, encoding="utf-8-sig")


def version_info(version: str) -> str:
    parts = tuple(int(part) for part in version.split(".")) + (0,)
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={parts!r}, prodvers={parts!r}, mask=0x3f, flags=0,
    OS=0x40004, fileType=0x1, subtype=0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'ShepiTT'),
    StringStruct('FileDescription', 'CET Practice - English examination practice'),
    StringStruct('FileVersion', '{version}.0'),
    StringStruct('InternalName', 'CETPractice'),
    StringStruct('OriginalFilename', 'CETPractice.exe'),
    StringStruct('ProductName', 'CET Practice'),
    StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
"""


def build_runtime(work: Path, version: str, public_database: Path | None, git_ref: str) -> Path:
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise ValueError("Release builds require 64-bit Python on Windows")
    for module in SOURCE_MODULES:
        if not (ROOT / module).is_file():
            raise ValueError(f"Missing release entrypoint or source: {module}")
    source_hashes = {name: sha256(ROOT / name) for name in SOURCE_MODULES}
    stage = work / "resources"
    stage.mkdir(parents=True)
    database = prepare_database(stage / "instance/cet4_v2.db", public_database, git_ref)
    assets = copy_resources(stage, database, version)
    licenses = work / "third-party-licenses"
    packages = collect_notices(licenses)
    version_file = work / "windows-version.txt"
    version_file.write_text(version_info(version), encoding="utf-8")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
               "--name", "CETPractice", "--contents-directory", "_internal", "--noupx",
               "--distpath", str(work / "runtime"), "--workpath", str(work / "pyinstaller-work"),
               "--specpath", str(work / "spec"), "--version-file", str(version_file),
               "--paths", str(ROOT), "--collect-all", "webview", "--collect-all", "pythonnet",
               "--collect-all", "clr_loader", "--hidden-import", "webview.platforms.edgechromium",
               "--hidden-import", "webview.platforms.winforms", "--hidden-import", "app",
               "--add-data", f"{stage / 'VERSION'}{os.pathsep}."]
    for folder in ("data", "templates", "static", "instance"):
        command.extend(["--add-data", f"{stage / folder}{os.pathsep}{folder}"])
    for module in ("rapidocr_onnxruntime", "onnxruntime", "faster_whisper", "ctranslate2", "torch",
                   "pytest", "matplotlib", "pandas", "scipy", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi"):
        command.extend(["--exclude-module", module])
    command.append(str(ROOT / "desktop_launcher.py"))
    print("Building frozen runtime; resource staging contains no live practice records.", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)
    runtime = work / "runtime/CETPractice"
    if not (runtime / "CETPractice.exe").is_file():
        raise ValueError("PyInstaller did not produce CETPractice.exe")
    write_root_documents(runtime, licenses, version, packages)
    if source_hashes != {name: sha256(ROOT / name) for name in SOURCE_MODULES}:
        raise ValueError("Runtime source changed during the build; rebuild from stable source files")
    runtime_files = {path.relative_to(runtime).as_posix(): {"size": path.stat().st_size, "sha256": sha256(path)}
                     for path in sorted(runtime.rglob("*")) if path.is_file()}
    manifest = {"schema": 1, "version": version, "created_utc": datetime.now(timezone.utc).isoformat(),
                "source": {"kind": "working-tree", "base_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()},
                "source_files": source_hashes, "database": database, "resources": assets,
                "runtime_files": runtime_files,
                "variant": "desktop", "default_mode": "desktop",
                "runtime_executable": {"filename": "CETPractice.exe", "size": (runtime / "CETPractice.exe").stat().st_size,
                                       "sha256": sha256(runtime / "CETPractice.exe")},
                "asset_counts": {
                    "question_pdfs": sum(name.startswith("data/") and name.count("/") == 1 and name.endswith(".pdf") for name in assets),
                    "answer_pdfs": sum(name.startswith("data/answers/") and name.endswith(".pdf") for name in assets),
                    "audio_mp3s": sum(name.startswith("data/audio/") and name.endswith(".mp3") for name in assets)},
                "python_version": sys.version, "personal_records_included": False,
                "external_runtime": "Microsoft Edge WebView2 (desktop window only)"}
    json_write(runtime / "RELEASE-MANIFEST.json", manifest)
    validate_runtime(runtime)
    return runtime


def validate_runtime(runtime: Path) -> dict:
    manifest = json.loads((runtime / "RELEASE-MANIFEST.json").read_text(encoding="utf-8"))
    expected_runtime = manifest.get("runtime_files")
    if expected_runtime is not None:
        actual_names = {path.relative_to(runtime).as_posix() for path in runtime.rglob("*")
                        if path.is_file() and path != runtime / "RELEASE-MANIFEST.json"}
        if actual_names != set(expected_runtime):
            raise ValueError("Runtime contains missing or unreviewed files outside the resource manifest")
        for relative, expected in expected_runtime.items():
            path = contained(runtime / relative, runtime)
            if path.stat().st_size != expected["size"] or sha256(path) != expected["sha256"]:
                raise ValueError(f"Runtime component differs from the build manifest: {relative}")
    exe = contained(runtime / manifest["runtime_executable"].get("filename", "CETPractice.exe"), runtime)
    if sha256(exe) != manifest["runtime_executable"]["sha256"]:
        raise ValueError("Frozen executable differs from the build manifest")
    for relative, expected in manifest["resources"].items():
        source = contained(runtime / "_internal" / relative, runtime / "_internal")
        if not source.is_file() or source.stat().st_size != expected["size"] or sha256(source) != expected["sha256"]:
            raise ValueError(f"Frozen resource differs from source staging: {relative}")
    actual_data = {path.relative_to(runtime / "_internal").as_posix()
                   for path in (runtime / "_internal/data").rglob("*") if path.is_file()}
    expected_data = {name for name in manifest["resources"] if name.startswith("data/")}
    if actual_data != expected_data:
        raise ValueError("Frozen data directory contains missing or unreviewed files")
    fingerprints = database_fingerprints(runtime / "_internal/instance/cet4_v2.db")
    if fingerprints != manifest["database"]["tables"]:
        raise ValueError("Frozen database differs from the sanitized seed")
    if any(fingerprints[table]["rows"] for table in PERSONAL_TABLES):
        raise ValueError("Frozen seed contains personal records")
    if not (runtime / "licenses/Python-LICENSE.txt").is_file():
        raise ValueError("Missing CPython license")
    return manifest


def create_archives(runtime: Path, output: Path, compression_level: int = 6) -> dict:
    manifest = validate_runtime(runtime)
    version = manifest["version"]
    output.mkdir(parents=True, exist_ok=True)
    targets = [output / f"cet-practice-{version}-windows-{mode}-x64.zip" for mode in VERSIONS]
    targets.extend([output / "SHA256SUMS.txt", output / f"build-validation-{version}.json"])
    for target in targets:
        if target.exists():
            raise ValueError(f"Refusing to overwrite an existing release artifact: {target}")
    results = {}
    files = sorted(path for path in runtime.rglob("*") if path.is_file())
    source_executable = manifest["runtime_executable"].get("filename", "CETPractice.exe")
    for mode, (folder_name, executable_name) in VERSIONS.items():
        archive = output / f"cet-practice-{version}-windows-{mode}-x64.zip"
        if archive.exists():
            raise ValueError(f"Refusing to overwrite a release archive: {archive}")
        # Each download describes its actual executable name. Keep the original
        # source hashes and immutable base-runtime manifest as provenance.
        variant_manifest = json.loads(json.dumps(manifest))
        variant_manifest["variant"] = mode
        variant_manifest["default_mode"] = "desktop" if mode == "desktop" else "browser"
        variant_manifest["base_runtime_manifest_sha256"] = sha256(runtime / "RELEASE-MANIFEST.json")
        variant_manifest["runtime_executable"]["filename"] = executable_name
        runtime_files = variant_manifest.get("runtime_files")
        if runtime_files is not None and source_executable != executable_name:
            runtime_files[executable_name] = runtime_files.pop(source_executable)
        print(f"Creating {archive.name} ...", flush=True)
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=compression_level, allowZip64=True) as stream:
            for source in files:
                relative = source.relative_to(runtime)
                if relative == Path("RELEASE-MANIFEST.json"):
                    stream.writestr((Path(folder_name) / relative).as_posix(),
                                    json.dumps(variant_manifest, ensure_ascii=False, indent=2) + "\n")
                    continue
                if relative == Path(source_executable):
                    relative = Path(executable_name)
                stream.write(source, (Path(folder_name) / relative).as_posix())
        with zipfile.ZipFile(archive) as stream:
            corrupt = stream.testzip()
            if corrupt:
                raise ValueError(f"ZIP verification failed: {corrupt}")
            entries = set(stream.namelist())
            if f"{folder_name}/{executable_name}" not in entries:
                raise ValueError("ZIP does not contain its expected launcher")
            saved_manifest = json.loads(stream.read(f"{folder_name}/RELEASE-MANIFEST.json"))
            if saved_manifest != variant_manifest:
                raise ValueError("ZIP contains an incorrect variant manifest")
            if runtime_files is not None:
                expected_entries = {f"{folder_name}/{name}" for name in runtime_files}
                expected_entries.add(f"{folder_name}/RELEASE-MANIFEST.json")
                if entries != expected_entries:
                    raise ValueError("ZIP file paths differ from its variant manifest")
        results[archive.name] = {"size": archive.stat().st_size, "sha256": sha256(archive)}
    sums = output / "SHA256SUMS.txt"
    if sums.exists():
        raise ValueError(f"Refusing to overwrite checksums: {sums}")
    sums.write_text("".join(f"{value['sha256']}  {name}\n" for name, value in sorted(results.items())), encoding="ascii")
    report = {"version": version, "archives": results, "database": manifest["database"],
              "personal_records_included": False, "source": manifest["source"], "source_files": manifest["source_files"]}
    json_write(output / f"build-validation-{version}.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=(ROOT / "VERSION").read_text(encoding="utf-8-sig").strip())
    parser.add_argument("--public-database", type=Path, help="Explicit already-public database instead of the committed Git seed")
    parser.add_argument("--git-ref", default="HEAD", help="Commit containing the public instance/cet4_v2.db seed")
    parser.add_argument("--work-dir", type=Path, help="New staging directory inside .cache (must not already exist)")
    parser.add_argument("--output", type=Path, default=ROOT / ".cache/windows-release-downloads")
    parser.add_argument("--skip-archives", action="store_true", help="Build and validate only; exercise the EXE before ZIP creation")
    parser.add_argument("--reuse-runtime", type=Path, help="Existing validated onedir runtime; skip PyInstaller")
    parser.add_argument("--compression-level", type=int, choices=range(10), default=6)
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        parser.error("--version must be a numeric major.minor.patch version")
    if args.reuse_runtime:
        runtime = args.reuse_runtime.resolve(strict=True)
        manifest = validate_runtime(runtime)
        if manifest["version"] != args.version:
            parser.error("Reused runtime version does not match --version")
    else:
        cache = ROOT / ".cache"
        cache.mkdir(exist_ok=True)
        if args.work_dir:
            work = contained(args.work_dir, cache)
            work.mkdir(parents=True, exist_ok=False)
        else:
            work = Path(tempfile.mkdtemp(prefix=f"windows-release-{args.version}-", dir=cache))
        runtime = build_runtime(work, args.version, args.public_database, args.git_ref)
    print(f"Validated runtime: {runtime}", flush=True)
    if not args.skip_archives:
        print(json.dumps(create_archives(runtime, args.output.resolve(), args.compression_level), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
