"""
kobo_highlights_reader.py — 讀取 kobo-highlight 推到 Dropbox 的 markdown，
parse 成個別 highlight，跟 review_state.json 比對找出未 process 的。

Dropbox path: /Kobo Highlights/<book>.md（kobo-highlight script 寫入）
State path:   <BASE_DIR>/review_state.json（本模組維護）

格式 reference（kobo-highlight sync-highlights.sh 輸出）：

    # 書名
    Author
    Publisher (可空)


    ---

    [chapter line  ]            ← optional, 章節切換時才出現
    highlight 文字 line 1
    highlight 文字 line 2
    2026-05-04 10:11
    筆記：optional content
    (blank line)
    ...
"""

import json
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
REVIEW_STATE_PATH = BASE_DIR / "review_state.json"

DROPBOX_KOBO_HIGHLIGHTS_PATH = os.environ.get(
    "DROPBOX_KOBO_HIGHLIGHTS_PATH", "/Kobo Highlights"
)

# 偵測時間戳：YYYY-MM-DD HH:MM（kobo-highlight 不寫秒）
_TIMESTAMP_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*$")
_NOTE_PREFIX_RE = re.compile(r"^\s*筆記[:：]\s*")


@dataclass
class Highlight:
    book_filename: str          # "On Vibe Coding.md"
    book_title: str             # "On Vibe Coding"
    author: str                 # "Naval"
    text: str                   # 多行 highlight 合成單一字串
    timestamp: str              # "2026-05-04 10:11"
    chapter: Optional[str] = None
    note: Optional[str] = None


# ─────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────

def _strip_md_hardbreak(line: str) -> str:
    """移除 markdown 行尾兩空白硬換行 marker。"""
    return line.rstrip().rstrip()  # rstrip 兩次冗餘但無害


def parse_highlights_md(content: str, book_filename: str) -> list[Highlight]:
    """Parse 一本書的 .md 內容成 list[Highlight]。"""
    lines = content.splitlines()
    if not lines:
        return []

    # Header：第一行 H1 是書名，後面 1-2 行是 author/publisher，遇到 `---` 結束
    book_title = ""
    author = ""
    i = 0
    if lines and lines[0].startswith("# "):
        book_title = lines[0][2:].strip()
        i = 1
        # 接下來是 author / publisher（可能 0-2 行非空）
        meta_lines = []
        while i < len(lines):
            s = lines[i].strip()
            if s == "---":
                i += 1
                break
            if s == "":
                i += 1
                continue
            meta_lines.append(s)
            i += 1
        if meta_lines:
            author = meta_lines[0]

    # Body：以「timestamp line」當切割點。在 timestamp 之前累積的非空行 = 該 highlight 的 text。
    # 章節行特徵：在 highlight block 之前單獨出現的一行（非 timestamp、非筆記、後面接 highlight 內容）。
    # 但實作上：把累積的 buffer 在遇到 timestamp 時，視最後一行（如果跟 highlight 主體有空行隔開）為 chapter，
    # 否則整 buffer 當 text。為保守正確，**不嘗試識別 chapter** — 把所有非 timestamp/非筆記行都當 text 一部分。
    # 這樣即使 chapter 跟 text 黏在一起，highlight 仍完整保留 — 沒有資料遺失，下游若想分章再看。

    highlights: list[Highlight] = []
    buf_lines: list[str] = []
    pending_note: Optional[str] = None

    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        # 移除尾部硬換行的 markdown 標記（兩空白）
        # 注意：strip() 已經移除尾空白，line 是純內容

        if line == "":
            # 空行 = 一段 highlight block 結束（如果 buf 有內容、且有 timestamp 已 flush）
            i += 1
            continue

        m = _TIMESTAMP_RE.match(line)
        if m:
            # timestamp 行 → 把累積 buf 當 text
            ts = m.group(1)
            text = "\n".join(s for s in buf_lines if s.strip()).strip()
            buf_lines = []
            # 看下一行是不是「筆記：」
            note = None
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if _NOTE_PREFIX_RE.match(next_line):
                    note = _NOTE_PREFIX_RE.sub("", next_line).strip()
                    i += 1  # 多消耗一行
            if text:
                highlights.append(Highlight(
                    book_filename=book_filename,
                    book_title=book_title,
                    author=author,
                    text=text,
                    timestamp=ts,
                    note=note,
                ))
            i += 1
            continue

        if _NOTE_PREFIX_RE.match(line):
            # 「筆記：」自己一行但跟前一個 highlight 對不起來（理論上不會走到這，因為 timestamp 處理時會吃掉）
            # 保險起見直接 skip
            i += 1
            continue

        # 一般 text line，累積到 buffer
        buf_lines.append(line)
        i += 1

    return highlights


# ─────────────────────────────────────────────
# State management
# ─────────────────────────────────────────────

def load_state() -> dict:
    """讀 review_state.json，回傳 dict（不存在則回傳空 schema）。"""
    if not REVIEW_STATE_PATH.exists():
        return {"books": {}}
    try:
        return json.loads(REVIEW_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"books": {}}


def save_state(state: dict) -> None:
    REVIEW_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def mark_processed(book_filename: str, highlight_timestamp: str) -> None:
    """更新某本書的 last_processed_highlight_timestamp。"""
    state = load_state()
    books = state.setdefault("books", {})
    entry = books.setdefault(book_filename, {})
    prev = entry.get("last_processed_highlight_timestamp", "")
    # 取較大者（避免亂序往前倒退）
    if highlight_timestamp > prev:
        entry["last_processed_highlight_timestamp"] = highlight_timestamp
    save_state(state)


# ─────────────────────────────────────────────
# Dropbox 讀取
# ─────────────────────────────────────────────

def list_pending_highlights() -> list[Highlight]:
    """從 Dropbox 抓 /Kobo Highlights/ 所有 .md，parse、跟 state 比對，
    回傳所有「未 process 的 highlight」list（依 (book, ts) 排序）。"""
    import dropbox  # noqa: F401  延遲載入
    from dropbox_uploader import _get_client

    dbx = _get_client()
    state = load_state()
    book_state = state.get("books", {})

    pending: list[Highlight] = []

    # list folder
    try:
        result = dbx.files_list_folder(DROPBOX_KOBO_HIGHLIGHTS_PATH)
    except Exception as e:
        print(f"⚠️  Dropbox list_folder 失敗 ({DROPBOX_KOBO_HIGHLIGHTS_PATH}): {e}")
        return []

    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)

    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        if entry.name == "README.md":
            continue
        # 下載
        try:
            _, resp = dbx.files_download(entry.path_lower or entry.path_display)
            content = resp.content.decode("utf-8", errors="replace")
        except Exception as e:
            print(f"⚠️  下載失敗 {entry.name}: {e}")
            continue

        highlights = parse_highlights_md(content, entry.name)
        last_ts = book_state.get(entry.name, {}).get(
            "last_processed_highlight_timestamp", ""
        )
        for h in highlights:
            if h.timestamp > last_ts:
                pending.append(h)

    # 排序：先依書名、再依 timestamp（讓同一本書 highlight 順序連續）
    pending.sort(key=lambda h: (h.book_filename, h.timestamp))
    return pending


def count_pending() -> int:
    """快速回傳 pending 數量（給 daily digest 用）。"""
    return len(list_pending_highlights())


# ─────────────────────────────────────────────
# Manual test entry
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE_DIR / ".env", override=True)
    except ImportError:
        pass

    if len(sys.argv) > 1 and sys.argv[1] == "--parse-local":
        # 用法：python3 kobo_highlights_reader.py --parse-local <path>
        path = sys.argv[2]
        content = Path(path).read_text(encoding="utf-8")
        for h in parse_highlights_md(content, Path(path).name):
            print(f"[{h.timestamp}] {h.text[:60]}...")
            if h.note:
                print(f"     筆記：{h.note}")
        sys.exit(0)

    pending = list_pending_highlights()
    print(f"Pending: {len(pending)} highlights across {len(set(h.book_filename for h in pending))} books")
    for h in pending[:5]:
        print(f"  - [{h.book_filename[:40]}] {h.timestamp}: {h.text[:50]}...")
