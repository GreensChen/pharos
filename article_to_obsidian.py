#!/usr/bin/env python3
"""
article_to_obsidian.py — 把網路文章 / 複製貼來的文字摘要進 Obsidian vault

兩種入口：
1. save_article_from_url(url) — 抓網頁、Gemini 摘要、寫 Articles/
2. save_text_as_article(text, title=None) — 直接餵長文字（複製貼來的全文）給 Gemini

短 spark（< 500 字）用 save_text_as_spark 走 0 Inbox。
"""

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass


GEMINI_MODEL = "gemini-3-flash-preview"

DROPBOX_VAULT_ARTICLES_PATH = os.environ.get(
    "DROPBOX_VAULT_ARTICLES_PATH", "/Greens Obsidian/1 Sources/Articles"
)
DROPBOX_VAULT_INBOX_PATH = os.environ.get(
    "DROPBOX_OBSIDIAN_INBOX_PATH", "/Greens Obsidian/0 Inbox"
)


SYSTEM_PROMPT = """你是一位資深編輯，專門把網路文章 / 長文摘要進個人知識庫。

任務：給你一篇文章的標題與內文，產出簡潔有資訊密度的摘要，幫使用者快速
capture 進 Obsidian vault。

寫作原則：
1. 全部用繁體中文
2. 客觀中立 — 寫作者的觀點、不是你的評論
3. 簡潔精煉 — 每段都要有資訊量，禁止套話
4. 結構固定，方便日後 cross-link
5. 不要逐段重述、重點是抓「為什麼這篇值得記」

絕對不要：
- 評論「值不值得讀」
- 加上你自己的角度
- 逐段照抄文章內容
"""


USER_PROMPT_TEMPLATE = """請為這篇文章寫摘要。

文章標題：{title}
{source_line}
---
文章內文：

{body}

---

請依下面格式輸出。**第一行必須是 `作者：name（身份）` 格式**（找不到就寫 `作者：（不詳）`）：

作者：張三（業界記者）

## 一句話摘要
30-60 字，抓文章核心觀點。

## 作者 / 來源背景
**1 段、80-150 字**：作者身份、為什麼值得讀（領域權威、一手經歷、獨特角度等）。
若是匿名 / 部落格 / 一般媒體，描述來源刊物或內容類型即可。

## 主要論點
3-5 個 bullet，每個 1-2 句，覆蓋文章最重要的：
- 觀察 / 主張
- 具體案例 / 數據
- 反直覺結論

## 關鍵概念詞表
- **概念 A**：作者怎麼定義 / 怎麼用（1-2 句）
（5-8 個重要術語）

## 為什麼值得記
1-2 句，回答：這篇為什麼值得收藏？提出新角度、罕見資訊、整合得好？

## 同主題延伸
- **資源（作者/來源）**：跟本篇的關聯（一句話）
（3-5 個。可以是論文、書、podcast、其他文章）

只輸出上述內容，不要前言、不要結語、不要 ```markdown``` 包裝。
"""


_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("缺 GEMINI_API_KEY")
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def _fetch_html(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    # 嘗試從 Content-Type / meta 找 encoding
    return raw.decode("utf-8", errors="replace")


def _strip_html(html: str, max_chars: int = 60000) -> tuple[str, str]:
    """簡單 strip HTML 抓 title + body。"""
    title_m = re.search(
        r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE | re.DOTALL,
    )
    title = title_m.group(1).strip() if title_m else ""

    # og:title 通常更乾淨
    og_m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        html, re.IGNORECASE,
    )
    if og_m:
        title = og_m.group(1).strip()

    # 拿掉 script / style / nav / footer / aside
    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", "", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<(nav|footer|aside|header)[^>]*>.*?</\1>", "", text, flags=re.IGNORECASE | re.DOTALL)
    # 段落保留換行
    text = re.sub(r"<(p|br|div|h[1-6]|li)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|h[1-6]|li)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    # decode entities
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return title, text[:max_chars]


def summarize_with_gemini(title: str, body: str, source_url: str = "") -> str:
    from google.genai import types

    client = _get_gemini_client()
    source_line = f"來源 URL：{source_url}" if source_url else ""
    user_prompt = USER_PROMPT_TEMPLATE.format(
        title=title or "（無標題）",
        source_line=source_line,
        body=body,
    )
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        max_output_tokens=4096,
        thinking_config=types.ThinkingConfig(thinking_budget=2048),
    )
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_prompt,
        config=config,
    )
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("Gemini 回傳空")
    return text


def _safe_filename(name: str, max_len: int = 100) -> str:
    name = re.sub(r'[/\\:*?"<>|\r\n\t]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len].rstrip()


def _yaml_str(s) -> str:
    if s is None:
        return '""'
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _extract_author_line(summary: str) -> tuple[str, str]:
    lines = summary.split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return "", summary
    first = lines[idx].strip()
    m = re.match(r"^作者[:：]\s*(.+)$", first)
    if not m:
        return "", summary
    author = m.group(1).strip()
    body = "\n".join(lines[idx + 1:]).lstrip("\n")
    return author, body


def render_article_md(
    title: str,
    summary: str,
    url: str = "",
    captured_at: str | None = None,
) -> str:
    author, body = _extract_author_line(summary)
    captured_at = captured_at or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    fm = [
        "---",
        "type: article",
        f"title: {_yaml_str(title)}",
        f"author: {_yaml_str(author)}",
        f"url: {_yaml_str(url)}",
        f"captured_at: {_yaml_str(captured_at)}",
        "tags: [article, capture]",
        "generated_by: gemini",
        "---",
        "",
        f"# {title}",
    ]
    if author:
        fm.append(f"*{author}*")
    fm.append("")
    if url:
        fm.append(f"[🔗 原始文章]({url})")
        fm.append("")
    fm.append(body)
    return "\n".join(fm)


def _upload_md(remote_path: str, content: str):
    from dropbox_uploader import _get_client
    from dropbox.files import WriteMode
    dbx = _get_client()
    dbx.files_upload(
        content.encode("utf-8"),
        remote_path,
        mode=WriteMode("overwrite"),
        autorename=False,
        mute=True,
    )


def save_article_from_url(url: str) -> dict:
    """抓網頁、摘要、寫 Dropbox。回傳 {title, remote_path}。"""
    html = _fetch_html(url)
    title, body = _strip_html(html)
    if not body or len(body) < 200:
        raise RuntimeError(
            f"網頁內容太短或抓不到（{len(body)} 字），可能需要登入或 JS 渲染"
        )
    summary = summarize_with_gemini(title or url, body, source_url=url)
    md = render_article_md(title or "untitled", summary, url=url)

    safe_title = _safe_filename(title or f"article-{datetime.now().strftime('%Y%m%d-%H%M')}")
    remote_path = f"{DROPBOX_VAULT_ARTICLES_PATH}/{safe_title}.md"
    _upload_md(remote_path, md)
    return {"title": title or "untitled", "remote_path": remote_path}


def save_text_as_article(text: str, title: str = "") -> dict:
    """user 複製貼來的長文字 → 摘要 → 寫 Articles/。"""
    if not title:
        # 從第一行抓 title
        first_line = text.strip().split("\n", 1)[0].strip()
        title = first_line[:80] if first_line else f"text-{datetime.now().strftime('%Y%m%d-%H%M')}"

    summary = summarize_with_gemini(title, text)
    md = render_article_md(title, summary, url="")

    safe_title = _safe_filename(title)
    remote_path = f"{DROPBOX_VAULT_ARTICLES_PATH}/{safe_title}.md"
    _upload_md(remote_path, md)
    return {"title": title, "remote_path": remote_path}


def save_text_as_spark(text: str) -> dict:
    """短 spark（< 500 字）— 不做 AI summary，原樣存 0 Inbox。"""
    captured_at = datetime.now()
    ts = captured_at.strftime("%Y%m%d-%H%M%S")
    fm = [
        "---",
        "type: spark",
        f"captured_at: {_yaml_str(captured_at.strftime('%Y-%m-%dT%H:%M:%S'))}",
        "tags: [spark, inbox]",
        "---",
        "",
        text.strip(),
        "",
    ]
    md = "\n".join(fm)
    remote_path = f"{DROPBOX_VAULT_INBOX_PATH}/spark-{ts}.md"
    _upload_md(remote_path, md)
    return {"remote_path": remote_path}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: article_to_obsidian.py <url>")
        sys.exit(1)
    print(json.dumps(save_article_from_url(sys.argv[1]), ensure_ascii=False, indent=2))
