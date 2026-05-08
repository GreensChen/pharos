#!/usr/bin/env python3
"""
add_tags_to_overviews.py — 對既有 vault 檔案加上主題 tags，
讓 Books / Interviews / Transcripts / Articles / Videos 之間能透過共同 tag 關聯。

讀每個 .md 的 body、用 Gemini Flash 產 3-5 個 kebab-case 英文主題 tag、
merge 進 frontmatter `tags:` 陣列。已有「非預設」tag 的檔自動跳過、不重複 enrich。

跑法：
    python3 add_tags_to_overviews.py            # 全跑
    python3 add_tags_to_overviews.py --only Books
    python3 add_tags_to_overviews.py --refresh  # 強制重生 tag（覆蓋既有）

需要 GEMINI_API_KEY。
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass

VAULT_ROOT = Path.home() / "Dropbox" / "Greens Obsidian"
SOURCES = {
    "Books": VAULT_ROOT / "1 Sources" / "Books",
    "Interviews": VAULT_ROOT / "1 Sources" / "Interviews",
    "Articles": VAULT_ROOT / "1 Sources" / "Articles",
    "Transcripts": VAULT_ROOT / "1 Sources" / "Transcripts",
    "Videos": VAULT_ROOT / "1 Sources" / "Videos",
}

GEMINI_MODEL = "gemini-3-flash-preview"

# 預設 metadata tag（檔案類型而非主題） — 不算「已 enrich」依據
DEFAULT_META_TAGS = {
    "book", "interview", "overview", "article", "capture",
    "post", "social", "transcript", "yt2epub", "video",
    "spark", "inbox",
}

TAG_SYSTEM_PROMPT = """你是知識庫標籤助理。看一份內容、產出 3-5 個英文 kebab-case 主題標籤，
方便讀者跨檔搜尋與關聯。

原則：
1. **kebab-case 英文**（例：ai, business-strategy, design-systems, productivity, leadership）
2. 涵蓋核心領域、不要過度細節
3. 不要重複（不同詞但同義就只取一個）
4. 不要技術 meta tag（不要 book / interview / overview / article / video / transcript — 那是檔案類型）
5. 不要太泛的 tag（不要 thinking / knowledge / general）

只回 JSON 陣列：
["ai", "agents", "software-engineering"]
"""


_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            print("❌ 缺 GEMINI_API_KEY")
            sys.exit(1)
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def get_topical_tags(content: str) -> list[str]:
    """送內容給 Gemini Flash、回 3-5 個 kebab-case tag。"""
    from google.genai import types
    client = _get_gemini_client()
    config = types.GenerateContentConfig(
        system_instruction=TAG_SYSTEM_PROMPT,
        max_output_tokens=512,
        thinking_config=types.ThinkingConfig(thinking_budget=512),
        response_mime_type="application/json",
    )
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=content[:8000],  # 截短省 tokens
        config=config,
    )
    text = (resp.text or "").strip()
    if not text:
        if os.environ.get("DEBUG_TAGS"):
            print(f"  [debug] empty response. resp={resp}", file=sys.stderr)
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            # 確保都是 kebab-case 字串、移除空白與重複
            seen = set()
            out = []
            for t in data:
                if not isinstance(t, str):
                    continue
                slug = t.strip().lower().replace(" ", "-").replace("_", "-")
                slug = re.sub(r"[^a-z0-9\-]", "", slug)
                if slug and slug not in seen:
                    seen.add(slug)
                    out.append(slug)
            return out[:5]
    except Exception:
        pass
    return []


def parse_frontmatter(text: str):
    m = re.match(r"^---\n(.*?\n)---\n(.*)", text, re.DOTALL)
    if not m:
        return "", text
    return m.group(1), m.group(2)


def extract_existing_tags(fm: str):
    """回 (tags_list, matched_line) 或 ([], None) 若 frontmatter 沒 tags 欄位。"""
    m = re.search(r"^tags:\s*\[(.*?)\]\s*$", fm, re.MULTILINE)
    if not m:
        return [], None
    raw = m.group(1)
    return [t.strip() for t in raw.split(",") if t.strip()], m.group(0)


def update_tags(filepath: Path, refresh: bool = False) -> str:
    """讀檔、加 tags、寫回。
    回傳 status 字串：'added' / 'skipped' / 'error' / 'no-fm'。"""
    text = filepath.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    if not fm:
        return "no-fm"

    existing, tags_line = extract_existing_tags(fm)
    has_topical = any(t not in DEFAULT_META_TAGS for t in existing)
    if has_topical and not refresh:
        return "skipped"

    new_tags = get_topical_tags(body)
    if not new_tags:
        return "error"

    if refresh:
        # 重生：保留 default meta tags、把 topical 換成新的
        meta_only = [t for t in existing if t in DEFAULT_META_TAGS]
        merged = meta_only + new_tags
    else:
        merged = list(existing)
        for t in new_tags:
            if t not in merged:
                merged.append(t)

    new_tags_str = f"tags: [{', '.join(merged)}]"
    if tags_line:
        new_fm = fm.replace(tags_line, new_tags_str)
    else:
        new_fm = fm + new_tags_str + "\n"

    new_text = f"---\n{new_fm}---\n{body}"
    filepath.write_text(new_text, encoding="utf-8")
    return "added"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="只處理某個 source（Books/Interviews/Articles/Transcripts/Videos）")
    parser.add_argument("--refresh", action="store_true", help="強制重生 topical tags")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    targets = SOURCES if not args.only else {args.only: SOURCES.get(args.only)}
    total_added = 0
    total_skipped = 0
    total_error = 0

    for name, src in targets.items():
        if not src or not src.exists():
            print(f"⚠️  {name}: 找不到 {src}")
            continue
        files = sorted(src.glob("*.md"))
        if args.limit:
            files = files[:args.limit]
        print(f"\n=== {name}（{len(files)} files）===")
        for i, f in enumerate(files, 1):
            try:
                status = update_tags(f, refresh=args.refresh)
            except Exception as e:
                status = "error"
                print(f"  ✗ [{i}/{len(files)}] {f.name[:60]}: {e}")
                total_error += 1
                continue
            if status == "added":
                total_added += 1
                print(f"  ✓ [{i}/{len(files)}] {f.name[:60]}")
            elif status == "skipped":
                total_skipped += 1
                print(f"  · [{i}/{len(files)}] {f.name[:60]} (已有)")
            elif status == "no-fm":
                print(f"  ? [{i}/{len(files)}] {f.name[:60]} (沒 frontmatter)")
            else:
                total_error += 1
                print(f"  ✗ [{i}/{len(files)}] {f.name[:60]} (Gemini 沒回 tag)")
            time.sleep(0.5)  # 避免 rate limit

    print(f"\n--- summary ---")
    print(f"  added: {total_added}")
    print(f"  skipped: {total_skipped}")
    print(f"  error: {total_error}")


if __name__ == "__main__":
    main()
