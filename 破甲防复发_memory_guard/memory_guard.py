#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Codex SessionStart memory guard.

Purpose:
  The Codex `memories` feature distills each session into "experience" and
  writes it into THREE places (sqlite stage1_outputs + MEMORY.md + raw_memories.md).
  When the model happens to refuse a legitimate research request once, that
  refusal gets fossilized as a "do not do this again" precedent and re-injected
  into every future session, silently degrading the workspace persona.

  This guard runs at SessionStart. It scans all three stores for REFUSAL /
  self-censorship precedents (multi-word patterns only, never single sensitive
  words), backs up anything it touches, deletes the poisoned entries, and logs
  what it did. If nothing matches it stays silent (exit 0).

Safety:
  - Matches require a REFUSAL verb + jailbreak/破甲/bypass context together.
    A memory that merely contains "破甲" or "外挂" as normal project work is
    NOT touched.
  - Every modified file is backed up with a timestamp before any change.
  - sqlite writes commit + checkpoint WAL so Codex reads the cleaned data.
"""

import os
import re
import sqlite3
import shutil
import sys
import datetime
import json

CODEX = os.path.join(os.path.expanduser("~"), ".codex")
MEM_DIR = os.path.join(CODEX, "memories")
SQLITE = os.path.join(CODEX, "memories_1.sqlite")
MEMORY_MD = os.path.join(MEM_DIR, "MEMORY.md")
RAW_MD = os.path.join(MEM_DIR, "raw_memories.md")
LOG_DIR = os.path.join(CODEX, "hooks", "logs")

STAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

# ---------------------------------------------------------------------------
# Reverse-instruction detection — HIGH-PRECISION ENGLISH FINGERPRINTS ONLY.
#
# Hard lesson from two false positives during development:
#   1. A read-only SECURITY REVIEW memory *described* a bundle that "contains
#      bypassing refusal / jailbreak-like" content -> keyword co-occurrence
#      wrongly flagged it.
#   2. A weekly-intel memory contained the user instruction "不要修改桌面\破甲\
#      模型分析\下的现有文档" (do not MODIFY the 破甲 folder's docs) -> a fuzzy
#      "不要...破甲" window wrongly read it as "don't DO 破甲".
#
# Root cause: in Chinese, 破甲 is ALSO the user's project/folder name, so any
# proximity match on 破甲 will keep hitting legitimate project memories
# (protect the 破甲 dir, 破甲评测, update 破甲 intel, ...). Chinese proximity
# matching is fundamentally unsafe here.
#
# Fix: match ONLY the exact, complete English phrases that a genuine
# refusal-precedent poison uses. These are long, specific, self-directed, and
# do not appear in normal project work. Anything else is left untouched — the
# guard errs hard toward NOT deleting.
# ---------------------------------------------------------------------------
POISON_PATTERNS = [
    # "the assistant refused to create a jailbreak/破甲 prompt"
    re.compile(
        r"assistant\s+(?:explicitly\s+)?(?:refused|declined)\s+"
        r"(?:to\s+)?(?:create|produce|write|generate|reproduce|replicate)\b"
        r"[^.]{0,60}?(?:jailbreak|破甲|越狱|bypass)",
        re.IGNORECASE | re.DOTALL,
    ),
    # "should not attempt to replicate jailbreak instructions"
    # "Future similar requests should not attempt ... jailbreak ..."
    re.compile(
        r"should not\s+(?:attempt|try|attempt to)\s+to?\s*"
        r"(?:replicate|reproduce|recreate|create|produce)\b"
        r"[^.]{0,60}?(?:jailbreak|破甲|越狱|bypass)",
        re.IGNORECASE | re.DOTALL,
    ),
    # "pivot to a compliant ... instead of reproducing the bypass content"
    re.compile(
        r"instead of\s+(?:reproducing|replicating|creating|producing)\b"
        r"[^.]{0,60}?(?:the bypass content|jailbreak|破甲|越狱|bypass[- ]style)",
        re.IGNORECASE | re.DOTALL,
    ),
    # "should not be replicated/reproduced" + jailbreak, in a prescriptive
    # "how to do differently" bullet.
    re.compile(
        r"(?:jailbreak|破甲|越狱)[^.]{0,60}?should not be\s+"
        r"(?:replicated|reproduced|created|attempted)",
        re.IGNORECASE | re.DOTALL,
    ),
]


def is_poison(text):
    if not text:
        return False
    return any(p.search(text) for p in POISON_PATTERNS)


def backup(path):
    if os.path.isfile(path):
        dst = f"{path}.guardbak_{STAMP}"
        shutil.copy2(path, dst)
        return dst
    return None


def log(report):
    os.makedirs(LOG_DIR, exist_ok=True)
    logpath = os.path.join(LOG_DIR, f"memory-guard-{datetime.datetime.now():%Y%m%d}.jsonl")
    with open(logpath, "a", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 1. sqlite stage1_outputs
# ---------------------------------------------------------------------------
def clean_sqlite():
    if not os.path.isfile(SQLITE):
        return 0, []
    con = sqlite3.connect(SQLITE)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    try:
        rows = cur.execute(
            "SELECT thread_id, raw_memory FROM stage1_outputs"
        ).fetchall()
    except sqlite3.Error:
        con.close()
        return 0, []

    poisoned = [r["thread_id"] for r in rows if is_poison(r["raw_memory"])]
    if not poisoned:
        con.close()
        return 0, []

    backup(SQLITE)
    for tid in poisoned:
        cur.execute("DELETE FROM stage1_outputs WHERE thread_id = ?", (tid,))
    con.commit()
    # Force WAL checkpoint so Codex reads the cleaned main DB.
    try:
        cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    con.close()
    return len(poisoned), poisoned


# ---------------------------------------------------------------------------
# 2. MEMORY.md — line-level (single-line bullets)
# ---------------------------------------------------------------------------
def clean_memory_md():
    if not os.path.isfile(MEMORY_MD):
        return 0
    with open(MEMORY_MD, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    kept = [ln for ln in lines if not is_poison(ln)]
    removed = len(lines) - len(kept)
    if removed:
        backup(MEMORY_MD)
        with open(MEMORY_MD, "w", encoding="utf-8") as f:
            f.writelines(kept)
    return removed


# ---------------------------------------------------------------------------
# 3. raw_memories.md — block-level (rollout blocks split by "\n---\n")
# ---------------------------------------------------------------------------
def clean_raw_md():
    if not os.path.isfile(RAW_MD):
        return 0
    with open(RAW_MD, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    # Split into blocks on lines that are exactly '---' (rollout separators),
    # keep the separator with the following block so structure is preserved.
    blocks = re.split(r"(?m)^---\s*$", text)
    poison_blocks = [i for i, b in enumerate(blocks) if is_poison(b)]
    if not poison_blocks:
        return 0
    backup(RAW_MD)
    kept = [b for i, b in enumerate(blocks) if i not in poison_blocks]
    with open(RAW_MD, "w", encoding="utf-8") as f:
        f.write("\n---\n".join(kept))
    return len(poison_blocks)


def main():
    sq_n, sq_ids = clean_sqlite()
    md_n = clean_memory_md()
    raw_n = clean_raw_md()
    total = sq_n + md_n + raw_n

    if total == 0:
        # Silent: nothing to do.
        sys.exit(0)

    report = {
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
        "event": "SessionStart-memory-guard",
        "sqlite_deleted": sq_n,
        "sqlite_threads": sq_ids,
        "memory_md_lines_removed": md_n,
        "raw_memories_blocks_removed": raw_n,
    }
    log(report)

    print(f"[memory-guard] Removed {total} refusal-precedent memory ent"
          f"{'ry' if total == 1 else 'ries'} at session start "
          f"(sqlite:{sq_n} MEMORY.md:{md_n} raw:{raw_n}). Backups written. "
          f"Nothing else touched.")
    sys.exit(0)


if __name__ == "__main__":
    main()
