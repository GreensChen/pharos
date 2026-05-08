#!/usr/bin/env python3
"""
bootstrap_vocabulary.py — 對 vault 既有 md 跑一次 vocabulary_manager 長出 initial vocabulary

走法：
1. 順序掃 1 Sources 各子資料夾的 md（Books / Interviews / Articles / Transcripts / Videos）
2. 每個檔讀 body、呼叫 vocabulary_manager.apply_tags_to_capture
3. AI 根據累積的 vocabulary 挑或提 tag、bump count
4. 寫進該檔 frontmatter（merge 進既有 tags array、避免覆蓋 meta tag）

跑完之後手動跑一次 consolidate_vocabulary.py 收斂 synonym。

跑：
    python3 bootstrap_vocabulary.py            # 全跑
    python3 bootstrap_vocabulary.py --only Books
    python3 bootstrap_vocabulary.py --limit 5  # 試幾本
    python3 bootstrap_vocabulary.py --refresh  # 強制重做（覆蓋既有 topical）
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass

from vocabulary_manager import (
    LOCAL_VAULT, load_vocabulary, save_vocabulary,
    select_or_propose, bump_tag_counts,
)

SOURCES = {
    "Books": LOCAL_VAULT / "1 Sources" / "Books",
    "Interviews": LOCAL_VAULT / "1 Sources" / "Interviews",
    "Articles": LOCAL_VAULT / "1 Sources" / "Articles",
    "Transcripts": LOCAL_VAULT / "1 Sources" / "Transcripts",
    "Videos": LOCAL_VAULT / "1 Sources" / "Videos",
}

DEFAULT_META = {
    "book", "interview", "overview", "article", "capture",
    "post", "social", "transcript", "yt2epub", "video",
    "spark", "inbox", "meta",
}


def parse_frontmatter(text: str):
    m = re.match(r"^---\n(.*?\n)---\n(.*)", text, re.DOTALL)
    if not m:
        return "", text
    return m.group(1), m.group(2)


def extract_existing_tags(fm: str):
    m = re.search(r"^tags:\s*\[(.*?)\]\s*$", fm, re.MULTILINE)
    if not m:
        return [], None
    raw = m.group(1)
    return [t.strip() for t in raw.split(",") if t.strip()], m.group(0)


def has_topical_tags(existing: list) -> bool:
    return any(t not in DEFAULT_META for t in existing)


def update_file(path: Path, vocab: dict, refresh: bool = False) -> str:
    """處理單一檔。回 status: 'tagged' / 'skipped' / 'no-fm' / 'no-tags-from-ai'。"""
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    if not fm:
        return "no-fm"
    existing, tags_line = extract_existing_tags(fm)
    if has_topical_tags(existing) and not refresh:
        return "skipped"

    used_existing, new_added = select_or_propose(body, vocab)
    new_tags = list(used_existing) + list(new_added)
    if not new_tags:
        return "no-tags-from-ai"
    bump_tag_counts(vocab, new_tags, source_filename=path.name)
    if new_added:
        vocab["new_since_last_consolidation"] = (
            vocab.get("new_since_last_consolidation", 0) + len(new_added)
        )

    # merge：keep meta tags + replace topical with new
    if refresh:
        meta_kept = [t for t in existing if t in DEFAULT_META]
        merged = meta_kept + new_tags
    else:
        merged = list(existing) + [t for t in new_tags if t not in existing]

    new_tags_line = f"tags: [{', '.join(merged)}]"
    if tags_line:
        new_fm = fm.replace(tags_line, new_tags_line)
    else:
        new_fm = fm + new_tags_line + "\n"
    new_text = f"---\n{new_fm}---\n{body}"
    path.write_text(new_text, encoding="utf-8")
    return "tagged"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="只處理某個 source（Books/Interviews/Articles/Transcripts/Videos）")
    parser.add_argument("--refresh", action="store_true", help="強制重生（覆蓋既有 topical）")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    targets = SOURCES if not args.only else {args.only: SOURCES.get(args.only)}
    vocab = load_vocabulary()
    initial_tag_count = len(vocab.get("tags", {}))

    n_tagged = n_skipped = n_failed = 0
    for name, src in targets.items():
        if not src or not src.exists():
            continue
        files = sorted(src.glob("*.md"))
        if args.limit:
            files = files[:args.limit]
        print(f"\n=== {name}（{len(files)} files）===")
        for i, f in enumerate(files, 1):
            try:
                status = update_file(f, vocab, refresh=args.refresh)
            except Exception as e:
                print(f"  ✗ [{i}/{len(files)}] {f.name[:60]}: {e}")
                n_failed += 1
                continue
            if status == "tagged":
                n_tagged += 1
                print(f"  ✓ [{i}/{len(files)}] {f.name[:60]}")
            elif status == "skipped":
                n_skipped += 1
                print(f"  · [{i}/{len(files)}] {f.name[:60]} (已有 topical)")
            else:
                print(f"  ? [{i}/{len(files)}] {f.name[:60]} ({status})")
            # 每 5 個存一次 vocab、避免中途 crash 全沒
            if i % 5 == 0:
                save_vocabulary(vocab)
            time.sleep(0.4)

    save_vocabulary(vocab)
    print(f"\n--- summary ---")
    print(f"  tagged: {n_tagged}")
    print(f"  skipped: {n_skipped}")
    print(f"  failed: {n_failed}")
    print(f"  vocab tags: {initial_tag_count} → {len(vocab.get('tags', {}))}")


if __name__ == "__main__":
    main()
