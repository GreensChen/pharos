#!/usr/bin/env python3
"""
generate_book_overviews.py — 為 vault 裡每本「畫過重點的書」生成客觀 overview

讀 ~/Dropbox/Greens Obsidian/1 Sources/Highlights/<title>.md，抽 title + author，
用 Gemini + Google Search grounding 生成一份結構化的書籍 overview，輸出到
~/Dropbox/Greens Obsidian/1 Sources/Books/<title>.md。

Overview 純客觀（不參考 user 的 highlights），讓 user 自己對照「我畫的重點」
跟「作者主旨」之間的落差。

跑法：
    python3 generate_book_overviews.py            # 增量、跳過已有
    python3 generate_book_overviews.py --refresh  # 重生全部
    python3 generate_book_overviews.py --only "7大市場力量"  # 只跑符合的
    python3 generate_book_overviews.py --limit 3  # 只跑 3 本（試試品質）

需要環境變數：GEMINI_API_KEY
"""

import argparse
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
HIGHLIGHTS_DIR = VAULT_ROOT / "1 Sources" / "Highlights"
BOOKS_DIR = VAULT_ROOT / "1 Sources" / "Books"

GEMINI_MODEL = "gemini-3-flash-preview"

SYSTEM_PROMPT = """你是一位資深編輯，專門為讀者寫「書籍速讀地圖」。

任務：給定書名與作者，搜尋這本書的客觀資訊，產出一份結構化的書籍 overview，
讓讀者快速理解「作者真正想表達什麼」、「這本書的論證骨架」、以及「這本書在
領域中的位置」。

寫作原則：
1. 全部用繁體中文
2. 客觀中立 — 寫作者的論點、不是你的評論
3. 簡潔精煉 — 每一段都要有資訊量，禁止套話/廢話
4. 結構固定，方便日後對照

如果搜尋後仍找不到此書（可能太冷門或書名翻譯有歧義），就誠實說「無法確認此書，
以下根據書名/作者推測」並標註不確定的部分。

絕對不要：
- 猜內容（找不到就說找不到）
- 評論「值不值得讀」
- 加上你自己的角度
- 引用 user 的閱讀重點（你沒這個 input）
"""

USER_PROMPT_TEMPLATE = """請為這本書生成 overview：

書名：{title}
作者：{author}

請用以下格式輸出（保留 markdown 標題層級）：

## 摘要
寫 **2-3 段、共 250-400 字** 的書籍摘要。內容要回答：
1. 這本書要解決什麼問題、為什麼這個問題重要
2. 作者提出什麼樣的核心解答 / 論點 / 框架
3. 讀者讀完會帶走什麼（最關鍵的 1-2 個 takeaway）
不要寫成大綱式 bullets — 要寫成連貫的段落，像在向沒看過的人介紹這本書。

## 核心主題
- （3-5 個 bullet、每個一句話、抓作者最想傳達的概念）

## 論證骨架
寫 **5-8 段、每段 120-200 字**，把作者整本書的論證脈絡攤開：

第一段：作者從什麼觀察 / 問題出發，為什麼既有答案不夠
中間幾段：作者一步步建立論證 — 每段聚焦一個關鍵 move（提出概念、舉例證明、回應反駁、推論到下一層）
最後一段：論證收尾在哪、把讀者帶到什麼結論或行動

重點：**論證流程**不是章節列表。讀者看完這節應該能複述「作者大概怎麼想出他的結論」。
有具體例子、數據、案例的話帶進來（例如「作者用 X 公司案例證明 Y」）。

## 關鍵概念詞表
- **概念 A**：作者怎麼定義 / 怎麼用（1-2 句）
- **概念 B**：...
（5-10 個重要術語，未來 quiz 會考）

## 適合誰讀 / 不適合誰讀
- 適合：...
- 不適合：...

## 同領域延伸 / 對話書
- **書名（作者）**：跟本書的關聯（一句話）
（3-5 本）

只輸出上述內容，不要前言、不要結語、不要 ```markdown``` 包裝。
"""


_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            print("❌ 缺 GEMINI_API_KEY，環境變數沒設")
            sys.exit(1)
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def parse_highlight_file(path: Path) -> dict:
    """從 highlight md 抓 title + author。kobo-highlight 格式：
    第一行 # title，第二/三行 **作者:** author。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    title = path.stem
    author = ""
    for line in lines[:10]:
        s = line.strip()
        if s.startswith("# ") and not title.startswith(s[2:]):
            title = s[2:].strip()
        m = re.match(r"\*\*作者[:：]\*\*\s*(.+)", s)
        if m:
            author = m.group(1).strip()
            break
    return {"title": title, "author": author, "filename": path.name}


def _safe_filename(name: str) -> str:
    name = re.sub(r'[/\\:*?"<>|\r\n\t]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120].rstrip()


def _yaml_str(s) -> str:
    if s is None:
        return '""'
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def call_gemini_with_grounding(title: str, author: str, max_retries: int = 2) -> str:
    """用 Gemini + Google Search grounding 生成 overview。"""
    from google.genai import types
    client = _get_gemini_client()

    user_prompt = USER_PROMPT_TEMPLATE.format(title=title, author=author or "（不詳）")
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(google_search=types.GoogleSearch())],
        max_output_tokens=6144,
        thinking_config=types.ThinkingConfig(thinking_budget=2048),
    )

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=user_prompt,
                config=config,
            )
            text = (resp.text or "").strip()
            if text:
                return text
            last_err = "empty response"
        except Exception as e:
            last_err = str(e)
        if attempt < max_retries:
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"Gemini 失敗（{max_retries+1} 次重試）: {last_err}")


def render_book_md(title: str, author: str, overview: str, source_filename: str) -> str:
    fm = [
        "---",
        "type: book",
        f"title: {_yaml_str(title)}",
        f"author: {_yaml_str(author or '')}",
        "tags: [book, overview]",
        "generated_by: gemini-grounded",
        "---",
        "",
        f"# {title}",
    ]
    if author:
        fm.append(f"*{author}*")
    fm.append("")
    # 連回 highlights file（建立 backlink）
    highlight_link = f"[[{source_filename.removesuffix('.md')}]]"
    fm.append(f"> 📌 我的 highlights: {highlight_link}")
    fm.append("")
    fm.append(overview)
    return "\n".join(fm)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="重生已存在的書 overview")
    parser.add_argument("--only", help="檔名包含此字串才處理（substring match）")
    parser.add_argument("--limit", type=int, default=0, help="最多處理幾本（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="只列出要處理的書、不呼叫 LLM")
    args = parser.parse_args()

    if not HIGHLIGHTS_DIR.exists():
        print(f"❌ 找不到 {HIGHLIGHTS_DIR}")
        sys.exit(1)

    BOOKS_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(HIGHLIGHTS_DIR.glob("*.md"))
    if args.only:
        files = [f for f in files if args.only in f.name]

    todo = []
    for f in files:
        if f.name == "README.md":
            continue
        meta = parse_highlight_file(f)
        safe = _safe_filename(meta["title"])
        out_path = BOOKS_DIR / f"{safe}.md"
        if out_path.exists() and not args.refresh:
            continue
        todo.append((f, meta, out_path))
        if args.limit and len(todo) >= args.limit:
            break

    print(f"待處理 {len(todo)} 本（總共 {len(files)} 本書、跳過已存在）")
    for _, meta, out in todo:
        print(f"  · {meta['title']}  →  {out.name}")

    if args.dry_run:
        return

    if not todo:
        print("✅ 沒事可做")
        return

    print()
    for i, (src, meta, out_path) in enumerate(todo, 1):
        title = meta["title"]
        author = meta["author"]
        print(f"[{i}/{len(todo)}] 生成 {title}（作者：{author or '不詳'}）...", flush=True)
        try:
            overview = call_gemini_with_grounding(title, author)
        except Exception as e:
            print(f"  ❌ 失敗：{e}")
            continue
        md = render_book_md(title, author, overview, meta["filename"])
        out_path.write_text(md, encoding="utf-8")
        print(f"  ✓ {out_path.name}  ({len(md)//1024} KB)")
        # 不要太快，避免 rate limit
        time.sleep(1)

    print(f"\n✅ {len(todo)} 本完成")


if __name__ == "__main__":
    main()
