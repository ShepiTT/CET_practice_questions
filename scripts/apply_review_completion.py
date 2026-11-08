"""Apply the reviewed 2026-11-08 data plan with strict old-value checks.

Dry run: python scripts/apply_review_completion.py
Apply:   python scripts/apply_review_completion.py --apply
Use --database for a disposable copy. No learner history is changed.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    "question": {"correct_answer", "explanation", "content"},
    "question_group": {"passage"},
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "instance/cet4_v2.db")
    parser.add_argument("--plan", type=Path, default=ROOT / "reports/review-completion-plan.json")
    parser.add_argument("--report", type=Path, default=ROOT / "reports/review-completion-applied.json")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    for name, expected in plan["evidence_report_sha256"].items():
        if digest(ROOT / name) != expected:
            raise ValueError(f"Review report changed after plan generation: {name}")

    before_hash = digest(database)
    conn = sqlite3.connect(database.as_uri() + ("?mode=rw" if args.apply else "?mode=ro"), uri=True)
    conn.row_factory = sqlite3.Row
    pending, insertions = [], []
    try:
        conn.execute("BEGIN IMMEDIATE" if args.apply else "BEGIN")
        for change in plan["updates"]:
            table, column = change["table"], change["column"]
            if table not in ALLOWED or column not in ALLOWED[table]:
                raise ValueError(f"Unsupported change: {table}.{column}")
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (change["id"],)).fetchone()
            if row is None or row["source_file"] != change["source_file"]:
                raise ValueError(f"Question identity changed: {table}/{change['id']}")
            for key in ("q_number", "group_id"):
                if key in change.get("identity", {}) and row[key] != change["identity"][key]:
                    raise ValueError(f"Question identity changed: {table}/{change['id']}/{key}")
            if row[column] == change["after"]:
                continue
            if row[column] != change["before"]:
                raise ValueError(f"Stale review: {table}/{change['id']}/{column}")
            pending.append(change)

        for option in plan["option_insertions"]:
            row = conn.execute("SELECT source_file, group_id FROM question WHERE id=?", (option["question_id"],)).fetchone()
            if row is None or row["source_file"] != option["source_file"] or row["group_id"] != option["group_id"]:
                raise ValueError(f"Option question identity changed: {option['question_id']}")
            existing = conn.execute("SELECT text FROM option WHERE question_id=? AND label=?", (option["question_id"], option["label"])).fetchone()
            if existing:
                if existing[0] != option["text"]:
                    raise ValueError(f"Existing option differs: {option['question_id']}/{option['label']}")
                continue
            insertions.append(option)

        history_before = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                          for table in ("user_history", "wrong_question")}
        backup_path = None
        if args.apply and (pending or insertions):
            # Backup via a second read connection before this connection writes.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = database.with_name(database.name + f".bak-{stamp}-review-completion")
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
                with sqlite3.connect(backup_path) as target:
                    source.backup(target)
            for change in pending:
                conn.execute(f"UPDATE {change['table']} SET {change['column']}=? WHERE id=?",
                             (change["after"], change["id"]))
            conn.executemany("INSERT INTO option(question_id,label,text) VALUES(?,?,?)",
                             [(o["question_id"], o["label"], o["text"]) for o in insertions])

        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
        history_after = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                         for table in history_before}
        if integrity != "ok" or foreign_keys or history_before != history_after:
            raise ValueError("Database validation failed; transaction rolled back")
        counts = dict(conn.execute("SELECT count(*) AS questions, sum(trim(coalesce(correct_answer,''))='') AS missing_answers, sum(trim(coalesce(explanation,''))='') AS missing_explanations FROM question").fetchone())
        if args.apply:
            if counts != plan["expected_counts"]:
                raise ValueError(f"Unexpected final counts: {counts}")
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()

    result = {"applied": args.apply, "database": str(database), "backup": str(backup_path) if backup_path else None,
              "sha256_before": before_hash, "sha256_after": digest(database),
              "plan_sha256": digest(args.plan), "updated_fields": len(pending), "inserted_options": len(insertions),
              "counts": counts, "integrity_check": integrity, "foreign_key_errors": foreign_keys,
              "history_unchanged": history_before == history_after,
              "updated_question_ids": sorted({r["id"] for r in pending if r["table"] == "question"}),
              "updated_group_ids": sorted({r["id"] for r in pending if r["table"] == "question_group"})}
    if args.apply:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
