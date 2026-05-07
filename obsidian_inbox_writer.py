"""
obsidian_inbox_writer.py — 把 process 過的 highlight + user reaction
寫成 markdown，上傳 Dropbox `/Obsidian/0 Inbox/`。

User 的 Obsidian vault 在 ~/Dropbox/Obsidian/，bot 直接寫 Dropbox API，
Mac 端 Dropbox 桌面 sync 後 Obsidian 自然看到。
"""

import os
import re
from datetime import datetime
from typing import Optional


DROPBOX_OBSIDIAN_INBOX_PATH = os.environ.get(
    "DROPBOX_OBSIDIAN_INBOX_PATH", "/Greens Obsidian/0 Inbox"
)


def _slugify(s: str, max_len: int = 50) -> str:
    """把書名轉成檔名安全的 slug（保留中英數字、合併空白為單一空格、截長）。"""
    # 拿掉檔案系統不允許的字元
    s = re.sub(r"[/\\:*?\"<>|\r\n\t]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len].rstrip()


def _build_markdown(
    *,
    book_filename: str,
    book_title: str,
    author: str,
    highlight_text: str,
    highlight_timestamp: str,
    user_reaction: Optional[str],
    chapter: Optional[str] = None,
    note: Optional[str] = None,
) -> str:
    """組 markdown 內容（含 frontmatter）。"""
    processed_at = datetime.now().strftime("%Y-%m-%dT%H:%M")
    # YAML frontmatter — 字串用雙引號 escape
    def yaml_str(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

    fm_lines = [
        "---",
        f"source: {yaml_str('Kobo Highlights/' + book_filename)}",
        f"book: {yaml_str(book_title)}",
        f"author: {yaml_str(author)}",
        f"highlighted_at: {yaml_str(highlight_timestamp)}",
        f"processed_at: {yaml_str(processed_at)}",
        "status: inbox",
    ]
    if chapter:
        fm_lines.append(f"chapter: {yaml_str(chapter)}")
    if note:
        fm_lines.append(f"original_note: {yaml_str(note)}")
    fm_lines.append("---")
    fm_lines.append("")  # blank after frontmatter

    body_lines = []
    # 引用原 highlight
    for line in highlight_text.splitlines():
        body_lines.append(f"> {line}")
    body_lines.append("")
    body_lines.append(f"_— {book_title} ({author}), {highlight_timestamp}_")
    body_lines.append("")
    if note:
        body_lines.append(f"**畫線時的原筆記：** {note}")
        body_lines.append("")
    body_lines.append("## 反應")
    body_lines.append("")
    if user_reaction:
        body_lines.append(user_reaction.strip())
    else:
        body_lines.append("(尚未撰寫)")
    body_lines.append("")

    return "\n".join(fm_lines + body_lines)


def write_to_inbox(
    *,
    book_filename: str,
    book_title: str,
    author: str,
    highlight_text: str,
    highlight_timestamp: str,
    user_reaction: Optional[str],
    chapter: Optional[str] = None,
    note: Optional[str] = None,
) -> str:
    """寫一篇 inbox markdown 到 Dropbox。回傳雲端路徑。"""
    from dropbox_uploader import _get_client
    from dropbox.files import WriteMode

    md = _build_markdown(
        book_filename=book_filename,
        book_title=book_title,
        author=author,
        highlight_text=highlight_text,
        highlight_timestamp=highlight_timestamp,
        user_reaction=user_reaction,
        chapter=chapter,
        note=note,
    )

    # 檔名：<slug>_<YYYYMMDD-HHMM>.md（用 highlight 時間戳，避免同 highlight 多次 process 衝突時清楚對應）
    ts_compact = re.sub(r"[^\d]", "", highlight_timestamp)[:12]  # YYYYMMDDHHMM
    slug = _slugify(book_title, max_len=40)
    filename = f"{slug}_{ts_compact}.md"
    remote_path = f"{DROPBOX_OBSIDIAN_INBOX_PATH}/{filename}"

    dbx = _get_client()
    dbx.files_upload(
        md.encode("utf-8"),
        remote_path,
        mode=WriteMode("overwrite"),
        autorename=False,
        mute=True,
    )
    return remote_path


if __name__ == "__main__":
    # Dry run：印出 markdown 內容
    md = _build_markdown(
        book_filename="On Vibe Coding.md",
        book_title="On Vibe Coding",
        author="Naval",
        highlight_text="我們將看到跨越式的進步，這股趨勢已不可阻擋。",
        highlight_timestamp="2026-05-04 10:11",
        user_reaction="這跟之前看 a16z 的訪談呼應 — 硬體、網絡效應、AI 模型三選一。",
    )
    print(md)
