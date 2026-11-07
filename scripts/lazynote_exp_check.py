#!/usr/bin/env python3
"""Propose / apply filling empty explanation slots from lazynote 解析版 HTML.

Never writes correct_answer. Never overwrites a non-empty explanation.
Default is dry-run (--check). Apply only with --apply-from <check.json>
(文字组-approved), which re-checks current explanation empty before UPDATE,
backs up the DB first, and writes an apply report under instance/.

Usage:
  .venv/bin/python scripts/lazynote_exp_check.py
  .venv/bin/python scripts/lazynote_exp_check.py --out instance/lazynote-exp6-check-YYYYMMDD-HHMM.md
  .venv/bin/python scripts/lazynote_exp_check.py --apply-from instance/lazynote-exp6-check-YYYYMMDD-HHMM.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from lxml import html

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "instance" / "cet4_v2.db"
EXP_DIR = ROOT / "instance" / "lazynote-exp-v6"
EXP_DIRS = [
    EXP_DIR,
    ROOT / "instance" / "lazynote-exp-v5",
    ROOT / "instance" / "lazynote-exp-v4",
    ROOT / "instance" / "lazynote-exp-v3",
    ROOT / "instance" / "lazynote-exp-v2",
    ROOT / "instance" / "lazynote-exp-v1",  # reuse older section HTML (CET6 on-disk expand)
]
# Prefer exp-v6, then v5..v1, then older materials' page.html for identity if needed.
LAZY_PAGE_DIRS = [
    EXP_DIR,
    ROOT / "instance" / "lazynote-exp-v5",
    ROOT / "instance" / "lazynote-exp-v4",
    ROOT / "instance" / "lazynote-exp-v3",
    ROOT / "instance" / "lazynote-exp-v2",
    ROOT / "instance" / "lazynote-exp-v1",
    ROOT / "instance" / "lazynote-gaps-v3",
    ROOT / "instance" / "lazynote-parse-batch",
    ROOT / "instance" / "lazynote-keys-batch",
    ROOT / "instance" / "lazynote-priority",
]

# exp-gaps-priority-20261105-v6.md priorities 1–10 + 2022-12-CET4-1 (explanation-only).
# FETCH: cet4-2022-12-* → lazynote-exp-v6. ON-DISK expand: 2020-09 CET4/CET6, 2020-12 CET4/CET6,
# 2021-06/2023-06/2023-12/2025-12 CET6, 2025-06 CET4 (via v1..v5).
# Never touch answers / 2020-07 letters. Keep: 2022-06-CET6-2-3 Q24, 2021-12-CET6-3 Q40.
# identity_fail / conflict / no_source are skipped (not proposed).
EXP_BATCH = [
    ("multi", "2020-09-CET4-3.pdf", ["cet4-2020-09-3", "cet4-2020-09-1", "cet4-2020-09-2"]),
    ("multi", "2020-09-CET6-1.pdf", ["2020-09-1", "2020-09-2", "2020-09-3"]),
    ("multi", "2020-09-CET6-3.pdf", ["2020-09-3", "2020-09-1", "2020-09-2"]),
    ("multi", "2020-12-CET4-2.pdf", ["cet4-2020-12-2", "cet4-2020-12-1", "cet4-2020-12-3"]),
    ("multi", "2020-12-CET6-2.pdf", ["2020-12-2", "2020-12-1", "2020-12-3"]),
    ("multi", "2021-06-CET6-2.pdf", ["2021-06-2", "2021-06-1", "2021-06-3"]),
    ("multi", "2023-06-CET6-2.pdf", ["2023-06-2", "2023-06-1", "2023-06-3"]),
    ("multi", "2023-12-CET6-2.pdf", ["2023-12-2", "2023-12-1", "2023-12-3"]),
    ("multi", "2025-06-CET4-2.pdf", ["cet4-2025-06-2", "cet4-2025-06-1", "cet4-2025-06-3"]),
    ("multi", "2025-12-CET6-1.pdf", ["2025-12-1", "2025-12-2", "2025-12-3"]),
    ("multi", "2022-12-CET4-1.pdf", ["cet4-2022-12-1", "cet4-2022-12-2", "cet4-2022-12-3"]),
]

OBJECTIVE_TYPES = {"listening", "cloze", "matching", "reading"}
IDENTITY_MIN = 0.94
# Prefer these analysis rows in order; skip noisy UI chrome.
PREFERRED_TAGS = (
    "【答案】",
    "【判型】",
    "【拆句】",
    "【定位】",
    "【信号】",
    "【替换】",
    "【排除】",
    "【词性槽】",
    "【依据】",
    "【竞争词】",
    "【锚点】",
    "【翻译】",
)
SKIP_TAGS = {"【题目】"}  # stem already in DB
MAX_EXP_CHARS = 1800


def md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scrub_option(text: str) -> str:
    """Drop OCR junk often appended after the real option (date stamp / Part I / etc.)."""
    t = (text or "").strip()
    # cut at first newline if remainder looks like metadata
    if "\n" in t:
        head, tail = t.split("\n", 1)
        if re.search(r"\d{4}-\d{2}|Part\s*I|CET|真题", tail, re.I) or len(head) >= 12:
            t = head
    return t.strip()


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", scrub_option(text).lower())


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b), autojunk=False).ratio()


def empty_text(val) -> bool:
    return val is None or not str(val).strip()


def slug_dir(slug: str) -> Path | None:
    for base in EXP_DIRS:
        d = base / slug
        if d.exists() and (d / "sections").exists():
            return d
    for base in EXP_DIRS:
        d = base / slug
        if d.exists():
            return d
    return None


def page_path(slug: str) -> Path | None:
    for base in LAZY_PAGE_DIRS:
        p = base / slug / "page.html"
        if p.exists() and p.stat().st_size > 5000:
            return p
    return None


def load_root(slug: str):
    p = page_path(slug)
    if p is None:
        return None
    return html.fromstring(p.read_bytes())


def section_files(slug: str) -> list[Path]:
    d = slug_dir(slug)
    if d is None:
        return []
    sec = d / "sections"
    if not sec.exists():
        return []
    out = []
    for name in (
        "listening.html",
        "part3-section-a.html",
        "part3-section-b.html",
        "part3-section-c-1.html",
        "part3-section-c.html",
        "part3-section-c-2.html",
    ):
        p = sec / name
        if p.exists() and p.stat().st_size > 20000:
            out.append(p)
    return out


def db_options(conn: sqlite3.Connection, qid: int) -> dict[str, str]:
    return dict(conn.execute("SELECT label,text FROM option WHERE question_id=?", (qid,)))


def lazy_options(root, number: int) -> dict[str, str]:
    if root is None:
        return {}
    el = root.xpath(f'//*[@id="qp-{number}"]')
    if len(el) != 1:
        return {}
    return {
        e.get("data-pdh-letter"): e.text_content()
        for e in el[0].xpath(".//*[@data-pdh-letter]")
        if e.get("data-pdh-letter")
    }


def labels_aligned(lazy_opts: dict[str, str], local_opts: dict[str, str]) -> bool:
    if not lazy_opts or set(lazy_opts) != set(local_opts):
        return False
    if len(local_opts) < 2:
        return False
    return min(similarity(local_opts[l], lazy_opts[l]) for l in local_opts) >= IDENTITY_MIN


def clean_ws(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def extract_ana_text(unit) -> str | None:
    """Build explanation text from .rp-ana rows inside a #q-N unit."""
    anas = unit.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " rp-ana ")]')
    if not anas:
        # fallback: whole unit text with 【答案】
        raw = clean_ws(unit.text_content())
        if "【答案】" not in raw:
            return None
        # trim before 【答案】 if very long preamble
        idx = raw.find("【答案】")
        raw = raw[idx:]
        return raw[:MAX_EXP_CHARS]

    rows: dict[str, str] = {}
    order: list[str] = []
    for row in anas[0].xpath('.//*[contains(@class,"rp-row")]'):
        tag_el = row.xpath('.//*[contains(@class,"rp-tag")]')
        if not tag_el:
            continue
        tag = clean_ws(tag_el[0].text_content())
        if not tag.startswith("【"):
            continue
        # body = row text minus leading tag
        full = clean_ws(row.text_content())
        body = full
        if full.startswith(tag):
            body = full[len(tag) :].strip()
        if tag in SKIP_TAGS:
            continue
        if tag not in rows:
            order.append(tag)
        rows[tag] = body

    if "【答案】" not in rows and not any("答案" in t for t in rows):
        return None

    parts: list[str] = []
    used = set()
    for tag in PREFERRED_TAGS:
        if tag in rows and tag not in used:
            parts.append(f"{tag}{rows[tag]}")
            used.add(tag)
    for tag in order:
        if tag not in used:
            parts.append(f"{tag}{rows[tag]}")
            used.add(tag)
    text = clean_ws("\n".join(parts))
    if len(text) > MAX_EXP_CHARS:
        text = text[: MAX_EXP_CHARS - 1].rstrip() + "…"
    return text or None


def load_explanations(slug: str) -> dict[int, dict]:
    """Map q_number -> {text, source_file, stem?, opts?} from section HTML."""
    out: dict[int, dict] = {}
    for path in section_files(slug):
        try:
            root = html.fromstring(path.read_bytes())
        except Exception:
            continue
        for unit in root.xpath('//*[starts-with(@id,"q-")]'):
            uid = unit.get("id") or ""
            m = re.fullmatch(r"q-(\d{1,2})", uid)
            if not m:
                continue
            n = int(m.group(1))
            text = extract_ana_text(unit)
            if not text:
                continue
            # options/stem from unit or sibling qp
            opts = {}
            for e in unit.xpath(".//*[@data-pdh-letter]"):
                lab = e.get("data-pdh-letter")
                if lab:
                    opts[lab] = e.text_content()
            stem_el = unit.xpath('.//*[@data-pdh-stem] | .//*[contains(@class,"lqp-stem")] | .//*[contains(@class,"exam-stem")] | .//*[contains(@class,"rp-stem-en")]')
            stem = stem_el[0].text_content() if stem_el else ""
            # paper-style qp may live elsewhere on same page
            qp = root.xpath(f'//*[@id="qp-{n}"]')
            if qp:
                for e in qp[0].xpath(".//*[@data-pdh-letter]"):
                    lab = e.get("data-pdh-letter")
                    if lab and lab not in opts:
                        opts[lab] = e.text_content()
                if not stem:
                    st = qp[0].xpath(".//*[@data-pdh-stem]")
                    if st:
                        stem = st[0].text_content()
            # keep first good explanation; prefer longer analysis
            prev = out.get(n)
            if prev is None or len(text) > len(prev["text"]):
                out[n] = {
                    "text": text,
                    "source_file": str(path.relative_to(ROOT)),
                    "slug": slug,
                    "opts": opts,
                    "stem": stem,
                }
    return out


def matching_identity(conn, q, paper_root, key_letter: str | None) -> tuple[bool, str]:
    """Confirm matching stem (+ optional paragraph) against lazynote paper HTML."""
    if paper_root is None:
        return False, "no_paper_html"
    el = paper_root.xpath(f'//*[@id="qp-{q["q_number"]}"]')
    if len(el) != 1:
        return False, "no_qp"
    stem = el[0].xpath(".//*[@data-pdh-stem]")
    if not stem or similarity(q["content"], stem[0].text_content()) < IDENTITY_MIN:
        return False, "stem_mismatch"
    if not key_letter:
        return True, "stem_only"
    paragraphs = paper_root.xpath(
        f'//*[starts-with(@id,"p-part3-section-b-")]//*[@data-pdh-letter="{key_letter}"]'
    )
    local = re.search(
        rf"(?:^|\s)[\[（(]?{key_letter}\s*[)）\]](.*?)(?=\s[\[（(]?[A-Z]\s*[)）\]]|$)",
        q["passage"] or "",
        re.S,
    )
    if not paragraphs or not local:
        # stem matched; paragraph optional for explanation-only
        return True, "stem_ok_para_skip"
    if similarity(local.group(1), paragraphs[0].text_content()) >= IDENTITY_MIN:
        return True, "matching_identity"
    return True, "stem_ok_para_low"  # still allow explanation if stem matches


def cloze_identity(conn, q, paper_root) -> tuple[bool, str]:
    if paper_root is None:
        return False, "no_paper_html"
    bank = paper_root.xpath('//*[@id="choices-part3-section-a"]//div[span[2]]')
    if len(bank) < 10:
        return False, "no_bank"
    local_opts = db_options(conn, q["id"])
    reference = {
        e[0].text_content().strip(" )"): e[1].text_content() for e in bank if len(e) >= 2
    }
    if len(reference) < 10 or not local_opts:
        return False, "bank_incomplete"
    # require most bank labels align
    scores = []
    for lab, word in reference.items():
        if lab in local_opts:
            scores.append(similarity(local_opts[lab], word))
    if len(scores) < 8:
        return False, "bank_label_gap"
    if min(scores) >= 0.85 and sum(1 for s in scores if s >= IDENTITY_MIN) >= max(8, len(scores) - 2):
        # also soft-check passage prefix
        passage = paper_root.xpath('//*[@id="p-part3-section-a-1"]')
        if passage:
            pref = norm(passage[0].text_content())[:120]
            if pref and pref[:60] in norm(q["passage"] or ""):
                return True, "cloze_bank_passage"
            # bank alone is strong for CET cloze shared sets
        return True, "cloze_bank"
    return False, "cloze_bank_mismatch"


def option_identity(conn, q, paper_root, exp_meta: dict | None) -> tuple[bool, str]:
    local_opts = db_options(conn, q["id"])
    lazy_opts = lazy_options(paper_root, q["q_number"]) if paper_root is not None else {}
    if labels_aligned(lazy_opts, local_opts):
        return True, "paper_labels_aligned"
    # try remap: every local option finds a unique lazy option
    if lazy_opts and local_opts and set(lazy_opts) == set(local_opts):
        # already handled; fall through
        pass
    if lazy_opts and local_opts:
        # allow remapped letter sets if each local text matches some lazy text
        matched = 0
        for lab, txt in local_opts.items():
            best = max((similarity(txt, lt) for lt in lazy_opts.values()), default=0)
            if best >= IDENTITY_MIN:
                matched += 1
        if matched == len(local_opts) and len(local_opts) >= 2:
            return True, f"paper_opts_remap:{matched}"
    # section HTML opts
    if exp_meta and exp_meta.get("opts") and local_opts:
        so = exp_meta["opts"]
        if labels_aligned(so, local_opts):
            return True, "section_labels_aligned"
        matched = 0
        for lab, txt in local_opts.items():
            best = max((similarity(txt, lt) for lt in so.values()), default=0)
            if best >= IDENTITY_MIN:
                matched += 1
        if matched == len(local_opts) and len(local_opts) >= 2:
            return True, f"section_opts_remap:{matched}"
    # stem-only fallback for listening (no stem in paper for CET listening often)
    if exp_meta and exp_meta.get("stem") and (q["content"] or "").strip():
        if similarity(q["content"], exp_meta["stem"]) >= IDENTITY_MIN:
            return True, "section_stem"
    if not lazy_opts and not (exp_meta and exp_meta.get("opts")):
        return False, "no_qp_or_opts"
    return False, "low_sim"


def identity_ok(conn, q, paper_root, exp_meta: dict | None, ans_letter: str | None) -> tuple[bool, str]:
    gt = q["group_type"]
    if gt == "matching":
        # Prefer letter from 【答案】X段
        letter = ans_letter
        if letter is None and exp_meta:
            m = re.search(r"【答案】\s*([A-O])", exp_meta["text"])
            if m:
                letter = m.group(1)
        return matching_identity(conn, q, paper_root, letter)
    if gt == "cloze":
        return cloze_identity(conn, q, paper_root)
    if gt in {"listening", "reading"}:
        return option_identity(conn, q, paper_root, exp_meta)
    return False, "unsupported_type"


def fetch_questions(conn, source: str):
    return conn.execute(
        "SELECT q.id, q.source_file, q.q_number, q.correct_answer, q.explanation, "
        "g.group_type, g.passage, q.content "
        "FROM question q JOIN question_group g ON g.id=q.group_id "
        "WHERE q.source_file=? ORDER BY q.q_number",
        (source,),
    ).fetchall()


def run_paper(conn, source: str, slugs: list[str]):
    qs = fetch_questions(conn, source)
    empty_exp_before = sum(1 for q in qs if empty_text(q["explanation"]))
    empty_ans_before = sum(1 for q in qs if empty_text(q["correct_answer"]))
    nonempty_exp = sum(1 for q in qs if not empty_text(q["explanation"]))

    # Preload per-slug materials
    materials = []
    for slug in slugs:
        materials.append(
            {
                "slug": slug,
                "paper_root": load_root(slug),
                "exps": load_explanations(slug),
                "page": str(page_path(slug)) if page_path(slug) else None,
                "sections": [str(p.relative_to(ROOT)) for p in section_files(slug)],
            }
        )

    results = []
    proposed = 0
    identity_fail = 0
    skipped_nonempty = 0
    no_source = 0

    def find_candidates(q, mat):
        """Same-number first; cross-number option match for listening/reading (merged papers)."""
        out = []
        same = mat["exps"].get(q["q_number"])
        if same:
            out.append((q["q_number"], same, "same_number"))
        local_opts = db_options(conn, q["id"])
        if q["group_type"] in {"listening", "reading"} and local_opts:
            for n, exp in mat["exps"].items():
                if n == q["q_number"]:
                    continue
                so = exp.get("opts") or {}
                if (not so) and mat["paper_root"] is not None:
                    so = lazy_options(mat["paper_root"], n)
                if not so:
                    continue
                if labels_aligned(so, local_opts):
                    out.append((n, {**exp, "opts": so}, f"cross_number_labels:{n}"))
                    continue
                matched = sum(
                    1
                    for lab, txt in local_opts.items()
                    if max((similarity(txt, lt) for lt in so.values()), default=0)
                    >= IDENTITY_MIN
                )
                if matched == len(local_opts) and len(local_opts) >= 2:
                    out.append((n, {**exp, "opts": so}, f"cross_number_remap:{n}"))
        return out

    for q in qs:
        if q["group_type"] not in OBJECTIVE_TYPES:
            continue
        has_exp = not empty_text(q["explanation"])
        best_fail = None
        found = False
        for mat in materials:
            candidates = find_candidates(q, mat)
            chosen = None
            chosen_why = None
            chosen_n = None
            for n, exp, how in candidates:
                if how == "same_number":
                    ok, why = identity_ok(conn, q, mat["paper_root"], exp, None)
                    if ok:
                        chosen, chosen_why, chosen_n = exp, why, n
                        break
                    best_fail = (mat["slug"], why, exp)
                else:
                    # option-text already matched a different lazynote number
                    chosen, chosen_why, chosen_n = exp, how, n
                    break
            if chosen is None:
                continue
            exp = chosen
            if chosen_n != q["q_number"]:
                exp = {
                    **exp,
                    "source_file": f"{exp['source_file']}#lazy-q{chosen_n}",
                }
            found = True
            if has_exp:
                skipped_nonempty += 1
                results.append(
                    {
                        "id": q["id"],
                        "source": source,
                        "number": q["q_number"],
                        "group_type": q["group_type"],
                        "status": "skip_nonempty_exp",
                        "slug": mat["slug"],
                        "detail": chosen_why,
                        "exp_source": exp["source_file"],
                        "proposed_len": len(exp["text"]),
                        "existing_len": len((q["explanation"] or "").strip()),
                    }
                )
            else:
                proposed += 1
                results.append(
                    {
                        "id": q["id"],
                        "source": source,
                        "number": q["q_number"],
                        "group_type": q["group_type"],
                        "status": "propose_exp",
                        "slug": mat["slug"],
                        "detail": chosen_why,
                        "exp_source": exp["source_file"],
                        "proposed": exp["text"],
                        "proposed_len": len(exp["text"]),
                        "existing_answer": (q["correct_answer"] or "").strip() or None,
                        "lazy_q_number": chosen_n,
                    }
                )
            break
        if found:
            continue
        if has_exp:
            continue
        if best_fail is not None:
            identity_fail += 1
            results.append(
                {
                    "id": q["id"],
                    "source": source,
                    "number": q["q_number"],
                    "group_type": q["group_type"],
                    "status": "identity_fail",
                    "slug": best_fail[0],
                    "detail": best_fail[1],
                    "exp_source": best_fail[2]["source_file"],
                    "proposed_len": len(best_fail[2]["text"]),
                }
            )
        else:
            no_source += 1
            results.append(
                {
                    "id": q["id"],
                    "source": source,
                    "number": q["q_number"],
                    "group_type": q["group_type"],
                    "status": "no_source_exp",
                    "detail": "no_section_ana",
                }
            )

    return {
        "source": source,
        "slugs": slugs,
        "empty_exp_before": empty_exp_before,
        "empty_ans_before": empty_ans_before,
        "nonempty_exp": nonempty_exp,
        "total_qs": len(qs),
        "proposed_exp": proposed,
        "identity_fail": identity_fail,
        "skipped_nonempty": skipped_nonempty,
        "no_source_exp": no_source,
        "materials": [
            {"slug": m["slug"], "page": m["page"], "sections": m["sections"], "exp_n": len(m["exps"])}
            for m in materials
        ],
        "results": results,
    }


def write_report(papers: list[dict], out_md: Path, out_json: Path, db_meta: dict):
    global_empty_before = db_meta["empty_exp_before"]
    proposed_total = sum(p["proposed_exp"] for p in papers)
    would_be = global_empty_before - proposed_total

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai"),
        "mode": "dry-run --check only (explanation fills; answers untouched)",
        "db": db_meta,
        "global": {
            "empty_exp_before": global_empty_before,
            "proposed_exp": proposed_total,
            "would_be_empty_exp": would_be,
            "empty_ans_unchanged": db_meta["empty_ans"],
        },
        "papers": papers,
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = []
    lines.append("# Lazynote 解析版 explanation dry-run (--check only)")
    lines.append("")
    lines.append(f"- Generated: {payload['generated_at']}")
    lines.append("- **ZERO DB writes.** Answers untouched. Existing explanations never overwritten.")
    lines.append(f"- Materials: `{EXP_DIR.relative_to(ROOT)}/` (section HTML 解析版 preferred)")
    lines.append(f"- Script: `scripts/lazynote_exp_check.py`")
    lines.append(f"- DB: `{DB.relative_to(ROOT)}` md5=`{db_meta['md5']}` mtime=`{db_meta['mtime']}`")
    lines.append("")
    lines.append("## Global")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|---|---:|")
    lines.append(f"| empty_exp before | **{global_empty_before}** |")
    lines.append(f"| proposed_exp (this batch) | **{proposed_total}** |")
    lines.append(f"| would-be empty_exp if applied | **{would_be}** |")
    lines.append(f"| empty_ans (untouched) | **{db_meta['empty_ans']}** |")
    lines.append(f"| identity_fail | {sum(p['identity_fail'] for p in papers)} |")
    lines.append(f"| skipped nonempty exp | {sum(p['skipped_nonempty'] for p in papers)} |")
    lines.append(f"| no_source_exp | {sum(p['no_source_exp'] for p in papers)} |")
    lines.append("")
    lines.append("## Per-paper")
    lines.append("")
    lines.append("| Paper | empty_exp before | proposed_exp | identity_fail | skipped nonempty | no_source | empty_ans |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for p in papers:
        lines.append(
            f"| `{p['source']}` | {p['empty_exp_before']} | **{p['proposed_exp']}** | "
            f"{p['identity_fail']} | {p['skipped_nonempty']} | {p['no_source_exp']} | {p['empty_ans_before']} |"
        )
    lines.append("")

    # samples
    samples = []
    for p in papers:
        for r in p["results"]:
            if r["status"] == "propose_exp" and r.get("proposed"):
                samples.append(r)
            if len(samples) >= 2:
                break
        if len(samples) >= 2:
            break
    lines.append("## Sample proposed explanations (quality check)")
    lines.append("")
    for i, s in enumerate(samples, 1):
        lines.append(f"### Sample {i}: `{s['source']}` Q{s['number']} ({s['group_type']}) ← `{s['slug']}`")
        lines.append(f"- identity: `{s['detail']}`")
        lines.append(f"- source file: `{s['exp_source']}`")
        lines.append(f"- existing answer (unchanged): `{s.get('existing_answer')}`")
        lines.append("")
        snippet = s["proposed"]
        if len(snippet) > 500:
            snippet = snippet[:500] + "…"
        lines.append("```")
        lines.append(snippet)
        lines.append("```")
        lines.append("")

    lines.append("## Materials summary")
    lines.append("")
    for p in papers:
        lines.append(f"### `{p['source']}`")
        for m in p["materials"]:
            lines.append(
                f"- slug `{m['slug']}`: exp_items={m['exp_n']}, sections={len(m['sections'])}, page={'yes' if m['page'] else 'no'}"
            )
        # propose q list
        prop = [r["number"] for r in p["results"] if r["status"] == "propose_exp"]
        fail = [r["number"] for r in p["results"] if r["status"] == "identity_fail"]
        nos = [r["number"] for r in p["results"] if r["status"] == "no_source_exp"]
        lines.append(f"- proposed Qs ({len(prop)}): {prop}")
        lines.append(f"- identity_fail Qs ({len(fail)}): {fail}")
        lines.append(f"- no_source Qs ({len(nos)}): {nos}")
        lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append("- Set-3 listening section URLs often 404; listening explanations reused from set-2/1 **only** when option-text identity holds.")
    lines.append("- Merged `*-2-3` papers use set-1 section HTML with identity guards (site publishes shared 解析 under set-1).")
    lines.append("- Cloze identity uses answer-bank alignment on paper HTML; matching uses stem (+ paragraph when available).")
    lines.append("- Proposed text is compacted from lazynote `.rp-ana` rows (【答案】/【定位】/…); capped at ~1800 chars.")
    lines.append("- Apply only via `--apply-from <this.json>` after 文字组 review; guarded empty-explanation UPDATE only.")
    lines.append("")

    out_md.write_text("\n".join(lines), encoding="utf-8")
    return proposed_total, would_be


def apply_from_check(check_path: Path) -> dict:
    """Apply exactly propose_exp items from a prior dry-run JSON.

    Hard guards:
    - only status == "propose_exp"
    - UPDATE explanation WHERE explanation empty only
    - never touch correct_answer
    - identity_fail / skip_nonempty_exp / no_source_exp not written
    - require rowcount == 1 per item (else rollback)
    """
    data = json.loads(check_path.read_text(encoding="utf-8"))
    proposed = []
    identity_fail_count = 0
    skipped_nonempty = 0
    no_source = 0
    for paper in data.get("papers", []):
        for r in paper.get("results", []):
            st = r.get("status")
            if st == "propose_exp":
                proposed.append(r)
            elif st == "identity_fail":
                identity_fail_count += 1
            elif st == "skip_nonempty_exp":
                skipped_nonempty += 1
            elif st == "no_source_exp":
                no_source += 1

    if not proposed:
        raise SystemExit(f"No propose_exp items in {check_path}")

    seen = {}
    for item in proposed:
        key = (item["source"], item["number"])
        if key in seen:
            raise SystemExit(f"Duplicate propose_exp for {key}")
        seen[key] = item
        if not (item.get("proposed") or "").strip():
            raise SystemExit(f"Empty proposed explanation for {key}")
        if item.get("id") is None:
            raise SystemExit(f"Missing id for {key}")

    before_mtime = datetime.fromtimestamp(DB.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    before_md5 = md5(DB)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB.with_name(DB.name + f".bak-{stamp}-lazynote-exp")

    with sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        empty_ans_before = conn.execute(
            'SELECT COUNT(*) FROM question WHERE correct_answer IS NULL OR trim(correct_answer)=""'
        ).fetchone()[0]
        empty_exp_before = conn.execute(
            'SELECT COUNT(*) FROM question WHERE explanation IS NULL OR trim(explanation)=""'
        ).fetchone()[0]
        # Capture sample nonempty explanations for post-verify (must stay unchanged).
        nonempty_samples = [
            dict(row)
            for row in conn.execute(
                "SELECT id, source_file, q_number, explanation FROM question "
                'WHERE explanation IS NOT NULL AND trim(explanation)!="" '
                "ORDER BY id LIMIT 20"
            )
        ]
        identity_fail_ids = [
            (r["id"], r["source"], r["number"])
            for paper in data.get("papers", [])
            for r in paper.get("results", [])
            if r.get("status") == "identity_fail"
        ]

    with sqlite3.connect(DB) as src, sqlite3.connect(backup) as dest:
        src.backup(dest)

    filled = []
    skipped = []
    with sqlite3.connect(DB) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        for item in proposed:
            # Re-check emptiness + answer fingerprint before write
            row = conn.execute(
                "SELECT explanation, correct_answer FROM question "
                "WHERE id=? AND source_file=? AND q_number=?",
                (item["id"], item["source"], item["number"]),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise RuntimeError(
                    f"Apply aborted: row missing id={item['id']} "
                    f"{item['source']} Q{item['number']}"
                )
            if not empty_text(row["explanation"]):
                skipped.append(
                    {
                        "id": item["id"],
                        "source": item["source"],
                        "number": item["number"],
                        "reason": "explanation_now_nonempty",
                    }
                )
                continue
            # Never modify correct_answer: refuse if fingerprint drifted unexpectedly
            # (existing_answer in check may be None for empty-ans rows — both OK).
            cur = conn.execute(
                "UPDATE question SET explanation=? "
                "WHERE id=? AND source_file=? AND q_number=? "
                'AND (explanation IS NULL OR trim(explanation)="")',
                (item["proposed"], item["id"], item["source"], item["number"]),
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise RuntimeError(
                    f"Apply aborted: expected 1 row for id={item['id']} "
                    f"{item['source']} Q{item['number']}; got {cur.rowcount}. "
                    "Transaction rolled back."
                )
            filled.append(item)
        conn.commit()

    after_mtime = datetime.fromtimestamp(DB.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    after_md5 = md5(DB)
    with sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        empty_ans_after = conn.execute(
            'SELECT COUNT(*) FROM question WHERE correct_answer IS NULL OR trim(correct_answer)=""'
        ).fetchone()[0]
        empty_exp_after = conn.execute(
            'SELECT COUNT(*) FROM question WHERE explanation IS NULL OR trim(explanation)=""'
        ).fetchone()[0]
        bad = []
        for item in filled:
            row = conn.execute(
                "SELECT explanation, correct_answer FROM question "
                "WHERE id=? AND source_file=? AND q_number=?",
                (item["id"], item["source"], item["number"]),
            ).fetchone()
            if not row or (row["explanation"] or "") != item["proposed"]:
                bad.append(("exp_mismatch", item))
                continue
            # correct_answer must be unchanged vs check fingerprint
            existing = (item.get("existing_answer") or "").strip() or None
            now_ans = (row["correct_answer"] or "").strip() or None
            if existing != now_ans:
                bad.append(("answer_changed", item, existing, now_ans))
        if bad:
            raise RuntimeError(f"Post-verify failed for {len(bad)} items: {bad[:3]}")

        # Existing nonempty explanations unchanged
        for sample in nonempty_samples:
            row = conn.execute(
                "SELECT explanation FROM question WHERE id=?",
                (sample["id"],),
            ).fetchone()
            if not row or row["explanation"] != sample["explanation"]:
                raise RuntimeError(
                    f"Nonempty explanation mutated for id={sample['id']} "
                    f"{sample['source_file']} Q{sample['q_number']}"
                )

        # identity_fail still empty
        still_nonempty = []
        for qid, src, num in identity_fail_ids:
            row = conn.execute(
                "SELECT explanation FROM question WHERE id=? AND source_file=? AND q_number=?",
                (qid, src, num),
            ).fetchone()
            if row is None or not empty_text(row["explanation"]):
                still_nonempty.append((qid, src, num))
        if still_nonempty:
            raise RuntimeError(
                f"identity_fail rows no longer empty: {still_nonempty[:5]}"
            )

    by_source = Counter(i["source"] for i in filled)
    by_slug = Counter(i.get("slug") or "?" for i in filled)
    report = {
        "applied": True,
        "check_source": str(check_path),
        "proposed_count": len(proposed),
        "filled_count": len(filled),
        "skipped_count": len(skipped),
        "skipped": skipped,
        "backup": str(backup),
        "db_before": {"mtime": before_mtime, "md5": before_md5},
        "db_after": {"mtime": after_mtime, "md5": after_md5},
        "empty_ans_before": empty_ans_before,
        "empty_ans_after": empty_ans_after,
        "empty_exp_before": empty_exp_before,
        "empty_exp_after": empty_exp_after,
        "by_source": dict(by_source),
        "by_slug": dict(by_slug),
        "identity_fail_not_written": identity_fail_count,
        "skip_nonempty_not_written": skipped_nonempty,
        "no_source_not_written": no_source,
        "filled": [
            {
                "id": i["id"],
                "source": i["source"],
                "number": i["number"],
                "group_type": i["group_type"],
                "slug": i.get("slug"),
                "exp_source": i.get("exp_source"),
                "proposed_len": i.get("proposed_len") or len(i["proposed"]),
                "existing_answer": i.get("existing_answer"),
                "status": "propose_exp",
                "detail": i.get("detail"),
            }
            for i in filled
        ],
    }
    return report


def write_apply_report(report: dict, out: Path) -> None:
    lines = []
    lines.append("# Lazynote 解析版 explanation APPLY report")
    lines.append("")
    lines.append(f"- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} Asia/Shanghai")
    lines.append(f"- Source check: `{report['check_source']}`")
    lines.append(f"- Backup: `{report['backup']}`")
    lines.append("- Mode: **--apply-from** of exact `status=propose_exp` items only")
    lines.append(
        "- Guards: empty `explanation` only (re-checked); `correct_answer` never written; "
        "identity_fail / skip_nonempty_exp **not** written"
    )
    lines.append("")
    lines.append("## Counts")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|---|---:|")
    lines.append(f"| Proposed in check | {report['proposed_count']} |")
    lines.append(f"| **Filled this run** | **{report['filled_count']}** |")
    lines.append(f"| Skipped | {report['skipped_count']} |")
    lines.append(f"| identity_fail not written | {report['identity_fail_not_written']} |")
    lines.append(f"| skip_nonempty not written | {report['skip_nonempty_not_written']} |")
    lines.append(f"| no_source not written | {report['no_source_not_written']} |")
    lines.append(
        f"| empty_exp before → after | {report['empty_exp_before']} → {report['empty_exp_after']} |"
    )
    lines.append(
        f"| empty_ans before → after | {report['empty_ans_before']} → {report['empty_ans_after']} |"
    )
    lines.append("")
    lines.append("## DB integrity")
    lines.append("")
    lines.append(
        f"- mtime before: `{report['db_before']['mtime']}` Asia/Shanghai; "
        f"md5=`{report['db_before']['md5']}`"
    )
    lines.append(
        f"- mtime after: `{report['db_after']['mtime']}` Asia/Shanghai; "
        f"md5=`{report['db_after']['md5']}`"
    )
    lines.append(
        f"- mtime changed: **{report['db_before']['mtime'] != report['db_after']['mtime']}**; "
        f"md5 changed: **{report['db_before']['md5'] != report['db_after']['md5']}**"
    )
    lines.append("")
    lines.append("## Filled by paper")
    lines.append("")
    lines.append("| Paper | filled |")
    lines.append("|---|---:|")
    for src, n in sorted(report["by_source"].items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"| `{src}` | {n} |")
    lines.append("")
    lines.append("## Filled by slug")
    lines.append("")
    lines.append("| slug | count |")
    lines.append("|---|---:|")
    for m, n in sorted(report["by_slug"].items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"| `{m}` | {n} |")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        f"- Δ empty_exp = {report['empty_exp_before'] - report['empty_exp_after']} "
        f"(expected {report['filled_count']})"
    )
    lines.append(
        f"- empty_ans unchanged expected ({report['empty_ans_before']} → "
        f"{report['empty_ans_after']})."
    )
    lines.append(
        f"- identity_fail kept empty: {report['identity_fail_not_written']}."
    )
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")




def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=str, default="", help="Output markdown path")
    ap.add_argument(
        "--check",
        action="store_true",
        default=True,
        help="Dry-run only (default)",
    )
    ap.add_argument(
        "--apply-from",
        help="Path to a prior lazynote-exp-check-*.json; apply exactly its propose_exp items",
    )
    args = ap.parse_args()
    if not DB.exists():
        raise SystemExit(f"DB missing: {DB}")

    if args.apply_from:
        check_path = Path(args.apply_from)
        if not check_path.is_absolute():
            check_path = ROOT / check_path
        if not check_path.exists():
            raise SystemExit(f"Check JSON missing: {check_path}")
        report = apply_from_check(check_path)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        out = Path(args.out) if args.out else ROOT / "instance" / f"lazynote-exp-apply-{stamp}.md"
        if not out.is_absolute():
            out = ROOT / out
        write_apply_report(report, out)
        sidecar = out.with_suffix(".json")
        sidecar.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"APPLY written: {out}")
        print(f"JSON sidecar: {sidecar}")
        print(
            f"filled={report['filled_count']}/{report['proposed_count']} "
            f"empty_exp {report['empty_exp_before']}->{report['empty_exp_after']} "
            f"empty_ans {report['empty_ans_before']}->{report['empty_ans_after']} "
            f"mtime {report['db_before']['mtime']}->{report['db_after']['mtime']}"
        )
        return

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out_md = Path(args.out) if args.out else ROOT / "instance" / f"lazynote-exp6-check-{stamp}.md"
    if not out_md.is_absolute():
        out_md = ROOT / out_md
    out_json = out_md.with_suffix(".json")

    db_mtime = datetime.fromtimestamp(DB.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai")
    db_md5 = md5(DB)

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    empty_exp = conn.execute(
        'SELECT COUNT(*) FROM question WHERE explanation IS NULL OR trim(explanation)=""'
    ).fetchone()[0]
    empty_ans = conn.execute(
        'SELECT COUNT(*) FROM question WHERE correct_answer IS NULL OR trim(correct_answer)=""'
    ).fetchone()[0]

    papers = []
    for kind, source, slugs in EXP_BATCH:
        assert kind == "multi"
        print(f"check {source} slugs={slugs}")
        meta = run_paper(conn, source, slugs)
        papers.append(meta)
        print(
            f"  empty_exp={meta['empty_exp_before']} proposed={meta['proposed_exp']} "
            f"id_fail={meta['identity_fail']} skip={meta['skipped_nonempty']} nosrc={meta['no_source_exp']}"
        )

    db_meta = {
        "path": str(DB.relative_to(ROOT)),
        "md5": db_md5,
        "mtime": db_mtime,
        "empty_exp_before": empty_exp,
        "empty_ans": empty_ans,
    }
    proposed_total, would_be = write_report(papers, out_md, out_json, db_meta)

    # verify DB untouched
    md5_after = md5(DB)
    mtime_after = datetime.fromtimestamp(DB.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai")
    assert md5_after == db_md5, "DB md5 changed!"
    print(f"Wrote {out_md}")
    print(f"Wrote {out_json}")
    print(f"proposed_exp={proposed_total} would-be empty_exp={would_be}")
    print(f"DB md5 unchanged={md5_after} mtime={mtime_after}")


if __name__ == "__main__":
    main()
