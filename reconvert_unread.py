#!/usr/bin/env python3
"""
reconvert_unread.py — 重轉 Kobo 上「還沒畫重點」的舊 epub。

邏輯：
1. 列 Dropbox /應用程式/Rakuten Kobo/ 的所有 epub
2. 對每個檔，用 normalized stem 找 Pharos summaries/ 對應 video URL
3. 用同樣 normalize 規則查 vault 1 Sources/Highlights/<stem>.md
4. 有 epub、沒 highlights → 候選重轉
5. 列清單 + 確認後依序跑 yt2epub.py（會覆蓋同名 Dropbox 檔，套用新 CSS）

用法：
    python3 reconvert_unread.py            # 列清單 + 互動確認
    python3 reconvert_unread.py --dry-run  # 只列清單、不轉
    python3 reconvert_unread.py --yes      # 不問直接全轉
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SUMMARIES_DIR = BASE_DIR / "summaries"
# Kobo 同步出來的原始重點檔（kobo-highlight 直接輸出）。比 vault 副本更即時。
HIGHLIGHTS_DIRS = [
    Path.home() / "Library" / "CloudStorage" / "Dropbox" / "Kobo Highlights",
    Path.home() / "Dropbox" / "Kobo Highlights",
    Path.home() / "Dropbox" / "Greens Obsidian" / "1 Sources" / "Highlights",
]


def _norm(name: str) -> str:
    n = name.removesuffix(".md").removesuffix(".kepub.epub").removesuffix(".epub")
    n = n.replace("_", "")
    n = re.sub(r"\s+", " ", n).strip().lower()
    return n


def load_summaries() -> dict:
    """回 {normalized_title: (video_id, title, url, channel)}"""
    out = {}
    for p in SUMMARIES_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        title = d.get("video", {}).get("title", "")
        url = d.get("video", {}).get("url", "")
        if not title or not url:
            continue
        out[_norm(title)] = {
            "video_id": p.stem,
            "title": title,
            "url": url,
            "channel": d.get("channel", ""),
            "saved_at": d.get("saved_at", ""),
        }
    return out


def list_kobo_epubs() -> list:
    """回 [{name, path}]。優先掃本地 Dropbox 資料夾、找不到再用 API。"""
    candidates = [
        Path.home() / "Library" / "CloudStorage" / "Dropbox" / "應用程式" / "Rakuten Kobo",
        Path.home() / "Dropbox" / "應用程式" / "Rakuten Kobo",
    ]
    for local_dir in candidates:
        if not local_dir.exists():
            continue
        out = []
        for p in local_dir.iterdir():
            if not p.is_file():
                continue
            n = p.name.lower()
            if not (n.endswith(".epub") or n.endswith(".kepub.epub")):
                continue
            out.append({"name": p.name, "path": str(p)})
        return out

    # fallback: Dropbox API
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    sys.path.insert(0, str(BASE_DIR))
    import dropbox_uploader
    return dropbox_uploader.list_kobo_files()


_HIGHLIGHT_NORMS_CACHE = None


def _all_highlight_norms() -> set:
    global _HIGHLIGHT_NORMS_CACHE
    if _HIGHLIGHT_NORMS_CACHE is not None:
        return _HIGHLIGHT_NORMS_CACHE
    out = set()
    for d in HIGHLIGHTS_DIRS:
        if not d.exists():
            continue
        for f in d.glob("*.md"):
            if f.name.lower() == "readme.md":
                continue
            out.add(_norm(f.name))
    _HIGHLIGHT_NORMS_CACHE = out
    return out


def has_highlights(norm_title: str) -> bool:
    return norm_title in _all_highlight_norms()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只列、不轉")
    ap.add_argument("--yes", action="store_true", help="不互動，全部重轉")
    ap.add_argument("--remote", default="", help="SSH 目標（如 root@1.2.3.4），透過遠端 server 跑 yt2epub.py")
    ap.add_argument("--orphans", action="store_true", help="處理對不到 summary 的 orphan：用 yt-dlp 搜 URL")
    args = ap.parse_args()

    print("→ 載入 Pharos summaries...")
    summaries = load_summaries()
    print(f"  共 {len(summaries)} 筆 summary")

    print("→ 列 Dropbox Kobo 資料夾...")
    try:
        kobo_files = list_kobo_epubs()
    except Exception as e:
        print(f"❌ 讀 Dropbox 失敗：{e}")
        sys.exit(1)
    print(f"  共 {len(kobo_files)} 個 epub")

    candidates = []
    skipped_highlighted = []
    skipped_no_summary = []
    for f in kobo_files:
        nt = _norm(f["name"])
        summary = summaries.get(nt)
        if not summary:
            # 名字對不到 summary（可能是手動加的書，或 title 改過）
            skipped_no_summary.append(f["name"])
            continue
        if has_highlights(nt):
            skipped_highlighted.append(summary["title"])
            continue
        candidates.append(summary)

    print()
    print(f"✅ 候選重轉（沒畫重點）：{len(candidates)} 筆")
    print(f"⏭  已畫重點、跳過：{len(skipped_highlighted)} 筆")
    print(f"❓ 對不到 summary、跳過：{len(skipped_no_summary)} 筆")
    print()

    if not candidates:
        print("沒有可重轉的書。")
        return

    print("=== 候選清單 ===")
    for i, c in enumerate(candidates, 1):
        print(f"{i:3}. [{c['channel']}] {c['title'][:70]}")

    if skipped_no_summary:
        print()
        print("=== 對不到 summary 的（要手動處理）===")
        for n in skipped_no_summary:
            print(f"  • {n}")

    if args.dry_run:
        print("\n(dry-run，不重轉)")
        return

    if not args.yes:
        ans = input(f"\n要重轉這 {len(candidates)} 本嗎？[y/N] ").strip().lower()
        if ans != "y":
            print("取消")
            return

    fail = 0
    for i, c in enumerate(candidates, 1):
        print(f"\n[{i}/{len(candidates)}] 轉檔: {c['title'][:60]}")
        if args.remote:
            import shlex
            inner = (
                f"cd /home/yt2epub/pharos && python3 yt2epub.py "
                f"{shlex.quote(c['url'])} --title {shlex.quote(c['title'])}"
            )
            if c["channel"]:
                inner += f" --podcast-name {shlex.quote(c['channel'])}"
            # 用 yt2epub user 跑（root 沒裝 anthropic 等套件）
            remote_cmd = f"sudo -u yt2epub bash -lc {shlex.quote(inner)}"
            cmd = ["ssh", args.remote, remote_cmd]
        else:
            cmd = [
                sys.executable, "-u",
                str(BASE_DIR / "yt2epub.py"),
                c["url"],
                "--title", c["title"],
            ]
            if c["channel"]:
                cmd += ["--podcast-name", c["channel"]]
        rc = subprocess.call(cmd, cwd=str(BASE_DIR))
        if rc != 0:
            fail += 1
            print(f"  ❌ rc={rc}")
        else:
            print(f"  ✅")

    print(f"\n完成。成功 {len(candidates) - fail} / 失敗 {fail}")


if __name__ == "__main__":
    main()
