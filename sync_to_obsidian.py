#!/usr/bin/env python3
"""
sync_to_obsidian.py — 一次性 bulk sync：

1. 從多個 `_data.json` 路徑把 yt2epub 訪談逐字稿轉成 markdown，
   寫進 ~/Dropbox/Obsidian/1 Sources/Transcripts/
2. 把 ~/Dropbox/Kobo Highlights/*.md 全部複製到
   ~/Dropbox/Obsidian/1 Sources/Highlights/

之後 Obsidian vault 設在 ~/Dropbox/Obsidian/，這些都自動被視為 vault 內容。

跑法：
    python3 sync_to_obsidian.py [--data-dir DIR1 DIR2 ...]
若沒給 --data-dir，預設掃 ~/yt2epub_output/ 跟 /tmp/server_data/
"""

import argparse
import glob
import json
import re
import shutil
import sys
from pathlib import Path

VAULT_ROOT = Path.home() / "Dropbox" / "Greens Obsidian"
TRANSCRIPTS_DIR = VAULT_ROOT / "1 Sources" / "Transcripts"
HIGHLIGHTS_DIR = VAULT_ROOT / "1 Sources" / "Highlights"
KOBO_HIGHLIGHTS_SOURCE = Path.home() / "Dropbox" / "Kobo Highlights"


def _safe_filename(name: str) -> str:
    """檔名安全化（保留中英、合併空白、拿掉非法字元）。"""
    name = re.sub(r'[/\\:*?"<>|\r\n\t]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120].rstrip()


def _yaml_str(s) -> str:
    if s is None:
        return '""'
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _find_matching_highlight(title: str) -> str:
    """用 normalized name 找 1 Sources/Highlights/ 裡對應的檔。
    回 highlight 檔名（不含 .md），找不到回空字串。"""
    if not HIGHLIGHTS_DIR.exists():
        return ""

    def _norm(name: str) -> str:
        n = name.removesuffix(".md").replace("_", "")
        n = re.sub(r"\s+", " ", n).strip().lower()
        return n

    norm_title = _norm(title)
    for f in HIGHLIGHTS_DIR.glob("*.md"):
        if _norm(f.name) == norm_title:
            return f.stem
    return ""


def render_transcript(data: dict) -> str:
    """把一份 _data.json 轉成 markdown。"""
    meta = data.get("meta", {})
    segments = data.get("segments", [])
    chapters = data.get("chapters", [])
    speaker_names = meta.get("speaker_names", {}) or {}
    participants = meta.get("participants", []) or []

    title = meta.get("title", "untitled")
    podcast = meta.get("podcast_name", "") or ""
    date = meta.get("date", "")
    url = meta.get("url", "")

    # Frontmatter
    fm = ["---"]
    fm.append(f"type: transcript")
    fm.append(f"title: {_yaml_str(title)}")
    if podcast:
        fm.append(f"podcast: {_yaml_str(podcast)}")
    if date:
        fm.append(f"date: {_yaml_str(date)}")
    if url:
        fm.append(f"url: {_yaml_str(url)}")
    if participants:
        fm.append("participants:")
        for p in participants:
            name = p.get("name", "")
            role = p.get("role", "")
            label = f"{name}" if not role else f"{name} — {role}"
            fm.append(f"  - {_yaml_str(label)}")
    fm.append("tags: [transcript, yt2epub]")
    fm.append("---")
    fm.append("")

    body = [f"# {title}"]
    if podcast:
        body.append(f"*{podcast}*  ·  {date}".strip())
    if url:
        body.append(f"\n[原始影片]({url})")
    # 連到對應的 Highlights（如果有畫過重點的話）
    hl_stem = _find_matching_highlight(title)
    if hl_stem:
        body.append(
            f"> 📌 我畫的重點: [[1 Sources/Highlights/{hl_stem}|{hl_stem}]]"
        )
    body.append("")

    if participants:
        body.append("## 對談人")
        for p in participants:
            name = p.get("name", "")
            role = p.get("role", "")
            line = f"- **{name}**" + (f" — {role}" if role else "")
            body.append(line)
        body.append("")

    # 章節 + segments
    if not chapters:
        # 沒分章節就一鍋
        body.append("## 內容\n")
        for seg in segments:
            body.extend(_render_segment(seg, speaker_names))
    else:
        for ch_idx, ch in enumerate(chapters, 1):
            title_en = ch.get("title_en", "")
            title_zh = ch.get("title_zh", "")
            start = ch.get("start_index", 0)
            end = ch.get("end_index", len(segments) - 1)
            head = f"## Ch.{ch_idx} — {title_en}"
            if title_zh:
                head += f"  ·  {title_zh}"
            body.append(head)
            body.append("")
            for seg in segments[start:end + 1]:
                body.extend(_render_segment(seg, speaker_names))
            body.append("")

    return "\n".join(fm + body)


def _render_segment(seg: dict, speaker_names: dict) -> list[str]:
    out = []
    speaker_id = seg.get("speaker", "")
    speaker_label = speaker_names.get(speaker_id, f"Speaker {speaker_id}") if speaker_id else ""
    ts = seg.get("timestamp", "")
    zh = seg.get("zh", "").strip()
    en = seg.get("en", "").strip()

    header_parts = []
    if speaker_label:
        header_parts.append(f"**{speaker_label}**")
    if ts:
        header_parts.append(f"`{ts}`")
    if header_parts:
        out.append("  ·  ".join(header_parts))
        out.append("")

    if zh:
        out.append(zh)
        out.append("")
    if en:
        # 英文用 blockquote 顯示，跟 zh 形成視覺層級
        out.append(f"> {en}")
        out.append("")

    return out


def sync_transcripts(data_dirs: list[Path]) -> int:
    """掃所有 data_dirs 內的 *_data.json，產出 transcript md 到 Dropbox vault。
    回傳寫入的檔案數。"""
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    seen_titles = {}  # 去重，後出現的覆蓋前面
    files = []
    for d in data_dirs:
        for f in sorted(d.glob("*_data.json")):
            files.append(f)

    written = 0
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ⚠️  跳過 {f.name}: {e}")
            continue
        title = data.get("meta", {}).get("title") or f.stem.removesuffix("_data")
        safe = _safe_filename(title)
        out_path = TRANSCRIPTS_DIR / f"{safe}.md"
        # 去重：若同 safe 已寫過，後者覆蓋（log 一下）
        if safe in seen_titles:
            print(f"  ↻ 覆蓋（同名）: {safe}")
        seen_titles[safe] = f
        md = render_transcript(data)
        out_path.write_text(md, encoding="utf-8")
        print(f"  ✓ {safe}  ({len(md)//1024} KB)")
        written += 1
    return written


def sync_highlights() -> int:
    """把 ~/Dropbox/Kobo Highlights/*.md 全部複製到 vault。"""
    HIGHLIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    if not KOBO_HIGHLIGHTS_SOURCE.exists():
        print(f"  ⚠️  找不到 {KOBO_HIGHLIGHTS_SOURCE}（Mac 上 Dropbox 桌面 client 是否啟用？）")
        return 0

    files = sorted(KOBO_HIGHLIGHTS_SOURCE.glob("*.md"))
    copied = 0
    for f in files:
        if f.name == "README.md":
            continue
        dst = HIGHLIGHTS_DIR / f.name
        shutil.copy2(f, dst)
        copied += 1
    return copied


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir", action="append",
        help="掃描 _data.json 的資料夾（可重複）。預設：~/yt2epub_output/ + /tmp/server_data/",
    )
    parser.add_argument("--skip-transcripts", action="store_true")
    parser.add_argument("--skip-highlights", action="store_true")
    args = parser.parse_args()

    if args.data_dir:
        dirs = [Path(d) for d in args.data_dir]
    else:
        dirs = [
            Path.home() / "yt2epub_output",
            Path("/tmp/server_data"),
        ]
    dirs = [d for d in dirs if d.exists()]
    print(f"Vault: {VAULT_ROOT}")
    print()

    if not args.skip_transcripts:
        print(f"📝 Transcripts → {TRANSCRIPTS_DIR}")
        print(f"   掃描：{[str(d) for d in dirs]}")
        n = sync_transcripts(dirs)
        print(f"  ✅ {n} 篇 transcript 寫入完成\n")

    if not args.skip_highlights:
        print(f"📌 Highlights → {HIGHLIGHTS_DIR}")
        n = sync_highlights()
        print(f"  ✅ {n} 個 highlight 檔複製完成\n")


if __name__ == "__main__":
    main()
