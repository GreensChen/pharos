#!/usr/bin/env python3
"""
patch_kepub_css.py — 直接改 Kobo 同步資料夾裡 .kepub.epub 的 CSS、
不重抓 / 不重翻、秒級完成。

只處理「沒畫重點」的書（依 Kobo Highlights/ 判定），跳過你正在讀的。

固定動作：
- 把 .segment { font-size: 1.4em; ... } 中的 font-size 那行拿掉
- body 若有固定 font-size 也拿掉

用法：
    python3 patch_kepub_css.py --dry-run     # 列要處理的書
    python3 patch_kepub_css.py --yes         # 直接 patch
"""
import argparse
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

HIGHLIGHTS_DIRS = [
    Path.home() / "Library" / "CloudStorage" / "Dropbox" / "Kobo Highlights",
    Path.home() / "Dropbox" / "Kobo Highlights",
    Path.home() / "Dropbox" / "Greens Obsidian" / "1 Sources" / "Highlights",
]
KOBO_DIRS = [
    Path.home() / "Library" / "CloudStorage" / "Dropbox" / "應用程式" / "Rakuten Kobo",
    Path.home() / "Dropbox" / "應用程式" / "Rakuten Kobo",
]


def _norm(name: str) -> str:
    n = name
    for suf in (".kepub.epub", ".epub", ".md"):
        if n.lower().endswith(suf):
            n = n[: -len(suf)]
            break
    n = n.replace("_", "")
    n = re.sub(r"\s+", " ", n).strip().lower()
    return n


def highlighted_norms() -> set:
    out = set()
    for d in HIGHLIGHTS_DIRS:
        if not d.exists():
            continue
        for f in d.glob("*.md"):
            if f.name.lower() == "readme.md":
                continue
            out.add(_norm(f.name))
    return out


def find_kobo_dir() -> Path:
    for d in KOBO_DIRS:
        if d.exists():
            return d
    raise FileNotFoundError("找不到本地 Kobo Dropbox 資料夾")


# CSS 處理：找 .segment { ... } 整塊，拿掉 font-size 那行；body 同樣處理
_BLOCK_RE = re.compile(r"(\.segment\s*\{[^}]*\}|body\s*\{[^}]*\})", re.DOTALL)
_FONTSIZE_RE = re.compile(r"^\s*font-size\s*:[^;]+;\s*\n", re.MULTILINE)


def patch_css_text(css: str) -> tuple:
    """回 (new_css, changed_count)。"""
    changed = 0

    def _strip_block(m):
        nonlocal changed
        block = m.group(0)
        new_block, n = _FONTSIZE_RE.subn("", block)
        if n > 0:
            changed += n
        return new_block

    new_css = _BLOCK_RE.sub(_strip_block, css)
    return new_css, changed


def patch_kepub(path: Path) -> int:
    """patch 單一 kepub.epub，回 changed lines (0 表示無需 patch)。"""
    tmp = Path(tempfile.mkdtemp(prefix="kepub_patch_"))
    try:
        # 解壓
        with zipfile.ZipFile(path, "r") as z:
            z.extractall(tmp)

        # 找所有 CSS、找到我們生成的特徵（.segment 或 body 字型 family Noto）
        css_files = list(tmp.rglob("*.css"))
        total_changed = 0
        for css_file in css_files:
            try:
                text = css_file.read_text(encoding="utf-8")
            except Exception:
                continue
            if ".segment" not in text and "Noto Serif CJK" not in text:
                continue  # 不是 Pharos 生成的 CSS
            new_text, changed = patch_css_text(text)
            if changed > 0:
                css_file.write_text(new_text, encoding="utf-8")
                total_changed += changed

        if total_changed == 0:
            return 0

        # 重新打包：mimetype 必須第一個、未壓縮
        out_tmp = path.with_suffix(path.suffix + ".tmp")
        mimetype_path = tmp / "mimetype"
        with zipfile.ZipFile(out_tmp, "w") as z:
            if mimetype_path.exists():
                z.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)
            for f in tmp.rglob("*"):
                if not f.is_file():
                    continue
                if f == mimetype_path:
                    continue
                rel = f.relative_to(tmp)
                z.write(f, str(rel), compress_type=zipfile.ZIP_DEFLATED)
        # 原子覆蓋
        out_tmp.replace(path)
        return total_changed
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    kobo_dir = find_kobo_dir()
    print(f"→ Kobo 資料夾: {kobo_dir}")

    hl = highlighted_norms()
    print(f"→ 已畫重點: {len(hl)} 筆（會跳過）")

    files = []
    skipped_hl = []
    for p in kobo_dir.iterdir():
        if not p.is_file():
            continue
        if not p.name.lower().endswith(".epub"):
            continue
        nt = _norm(p.name)
        if nt in hl:
            skipped_hl.append(p.name)
            continue
        files.append(p)

    print(f"→ 候選 patch: {len(files)} 本")
    print(f"→ 已重點跳過: {len(skipped_hl)} 本")
    print()

    print("=== 候選清單 ===")
    for i, p in enumerate(files, 1):
        print(f"{i:3}. {p.name}")
    print()
    if skipped_hl:
        print("=== 跳過（已畫重點）===")
        for n in skipped_hl:
            print(f"  • {n}")
        print()

    if args.dry_run:
        print("(dry-run)")
        return

    if not args.yes:
        ans = input(f"要 patch 這 {len(files)} 本嗎？[y/N] ").strip().lower()
        if ans != "y":
            print("取消")
            return

    ok = 0
    skipped_no_change = 0
    fail = 0
    for i, p in enumerate(files, 1):
        try:
            changed = patch_kepub(p)
            if changed > 0:
                ok += 1
                print(f"[{i:3}/{len(files)}] ✅ {p.name}  (-{changed} font-size)")
            else:
                skipped_no_change += 1
                print(f"[{i:3}/{len(files)}] ⏭  {p.name}  (無需 patch)")
        except Exception as e:
            fail += 1
            print(f"[{i:3}/{len(files)}] ❌ {p.name}  {e}")

    print()
    print(f"完成。patched={ok}  no-change={skipped_no_change}  fail={fail}")


if __name__ == "__main__":
    main()
