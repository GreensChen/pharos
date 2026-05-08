#!/usr/bin/env python3
"""
consolidate_vocabulary.py — 跑 vocabulary 整合：找 synonym/重疊、合併、改寫所有受影響檔

跑：
    python3 consolidate_vocabulary.py            # AI 提案 + 自動套用
    python3 consolidate_vocabulary.py --dry-run  # 只列出提案、不動任何檔
    python3 consolidate_vocabulary.py --threshold 5  # 累積 5 個新 tag 才跑（給 cron 用）

建議節奏：每週一次 cron 自動跑、或 manually trigger。
"""

import argparse
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass

from vocabulary_manager import (
    load_vocabulary, save_vocabulary,
    propose_consolidation, apply_consolidation,
    USE_DROPBOX_API,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只列出提案、不動任何檔")
    parser.add_argument("--threshold", type=int, default=0,
                        help="累積 N 個新 tag 才跑（小於 N 直接 skip、給 cron 用）")
    parser.add_argument("--max-age-days", type=int, default=0,
                        help="距上次 consolidation 至少 N 天才跑（給 server fallback 用）")
    args = parser.parse_args()

    mode = "Dropbox API" if USE_DROPBOX_API else "local fs"
    print(f"模式：{mode}")
    vocab = load_vocabulary()
    new_count = vocab.get("new_since_last_consolidation", 0)
    print(f"vocab：{len(vocab.get('tags', {}))} 個 tag、新增自上次整合：{new_count}")

    if args.threshold and new_count < args.threshold:
        print(f"沒到 threshold（{new_count} < {args.threshold}）、跳過")
        return

    if args.max_age_days:
        last = vocab.get("last_consolidation")
        if last:
            from datetime import datetime, date
            try:
                last_d = datetime.strptime(last, "%Y-%m-%d").date()
                age = (date.today() - last_d).days
                if age < args.max_age_days:
                    print(f"距上次 consolidation {age} 天、< {args.max_age_days}、跳過")
                    return
            except Exception:
                pass

    print("\n🤔 Gemini 找 merge 機會中...")
    merges = propose_consolidation(vocab)

    if not merges:
        print("✅ 沒有需要合併的 tag")
        # reset counter + 標記今天已經 attempt 過、避免同天重複跑
        from datetime import datetime
        vocab["new_since_last_consolidation"] = 0
        vocab["last_consolidation"] = datetime.now().strftime("%Y-%m-%d")
        save_vocabulary(vocab)
        return

    print(f"\n提案 {len(merges)} 個合併：")
    for m in merges:
        merged = ", ".join(m.get("merged", []))
        into = m.get("into", "")
        reason = m.get("reason", "")
        print(f"  · {merged} → {into}")
        print(f"      原因：{reason}")

    if args.dry_run:
        print("\n--dry-run、不執行")
        return

    print("\n📝 套用中...")
    n_changed = apply_consolidation(vocab, merges)
    print(f"\n✅ 完成、影響 {n_changed} 個檔。changelog 寫到 vault `2 Atomic Notes/Vocabulary Changelog.md`")


if __name__ == "__main__":
    main()
