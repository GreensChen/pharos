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
DROPBOX_VAULT_ATTACHMENTS_PATH = os.environ.get(
    "DROPBOX_VAULT_ATTACHMENTS_PATH", "/Greens Obsidian/9 Attachments"
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


_INLINE_HASHTAG_RE = re.compile(r"(?<![#\w])#[A-Za-z0-9_一-鿿\-]+\b", re.UNICODE)


def _strip_inline_hashtags(text: str) -> str:
    """拿掉 Gemini OCR 時可能保留的 #AI #Edge 之類 hashtag。
    防止污染 Obsidian tag system。"""
    cleaned = _INLINE_HASHTAG_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


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


def _topical_tags_via_vocab(body: str, source_filename: str) -> list:
    try:
        from vocabulary_manager import apply_tags_to_capture
        return apply_tags_to_capture(body, source_filename)
    except Exception as e:
        print(f"⚠️  vocab tagging 失敗：{e}")
        return []


def _build_tags_line(meta_tags: list, topical: list) -> str:
    seen = set()
    merged = []
    for t in list(meta_tags) + list(topical):
        if t and t not in seen:
            seen.add(t)
            merged.append(t)
    return f"tags: [{', '.join(merged)}]"


def render_article_md(
    title: str,
    summary: str,
    url: str = "",
    original_text: str = "",
    captured_at: str | None = None,
) -> str:
    author, summary_body = _extract_author_line(summary)
    captured_at = captured_at or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    safe_stem = _safe_filename(title)
    topical = _topical_tags_via_vocab(summary_body, safe_stem + ".md")

    fm = [
        "---",
        "type: article",
        f"title: {_yaml_str(title)}",
        f"author: {_yaml_str(author)}",
        f"url: {_yaml_str(url)}",
        f"captured_at: {_yaml_str(captured_at)}",
        _build_tags_line(["article", "capture"], topical),
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
    fm.append(summary_body)
    if original_text and original_text.strip():
        fm.append("")
        fm.append("---")
        fm.append("")
        fm.append("## 原文")
        fm.append("")
        fm.append(original_text.strip())
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
    summary = _strip_inline_hashtags(summary)
    md = render_article_md(
        title or "untitled", summary, url=url, original_text=body,
    )

    safe_title = _safe_filename(title or f"article-{datetime.now().strftime('%Y%m%d-%H%M')}")
    remote_path = f"{DROPBOX_VAULT_ARTICLES_PATH}/{safe_title}.md"
    _upload_md(remote_path, md)
    return {"title": title or "untitled", "remote_path": remote_path}


def save_text_as_article(text: str, title: str = "") -> dict:
    """user 複製貼來的長文字 → 摘要 → 寫 Articles/。原文也保留。"""
    if not title:
        first_line = text.strip().split("\n", 1)[0].strip()
        title = first_line[:80] if first_line else f"text-{datetime.now().strftime('%Y%m%d-%H%M')}"

    summary = summarize_with_gemini(title, text)
    summary = _strip_inline_hashtags(summary)
    md = render_article_md(title, summary, url="", original_text=text)

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


SCREENSHOT_USER_PROMPT = """這是社群文章的截圖（{n_images} 張）。請做以下事：

1. 找出**發文者**（從可見的姓名 / handle / 頭像 / 平台 logo 判斷）
2. **完整 OCR 內文**（按截圖順序拼起來、保留段落分隔；如果是 thread / 回文也全部拼上、別漏）
3. 寫一段**簡短摘要**（純概括、不評論、不延伸）

⚠️ **重要：原文裡的 hashtag（例如 #AI #Edge #NET）不要保留**。
讀者不需要、會污染 Obsidian 的 tag 系統。OCR 時把這些 hashtag 整段拿掉、其他文字保留。

輸出格式（嚴格遵守，每一行的前綴都不能改）：

發文者：姓名 (@handle, 平台名)
平台：Twitter / Threads / Facebook / Instagram / LinkedIn / 微博 / 其他
標題：（用內文前 30 字濃縮一個短標題、用來當檔名、不要含特殊字元 / 引號 / hashtag）

## 摘要

（1 段、80-150 字。純概括發文者在這篇講了什麼、不要評論不要延伸不要加你自己角度。**摘要中不要含 hashtag**）

## 內文

（OCR 拼好的完整文字、保留換行與段落、不翻譯、不修飾、**但 hashtag 拿掉**）

如果發文者完全看不出來就寫「發文者：（不詳）」。

{caption_block}
"""


def _extract_title_line(summary: str) -> tuple[str, str]:
    """抓第一行 `標題：xxx`，回 (title, body_without_first_line)。"""
    lines = summary.split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return "", summary
    first = lines[idx].strip()
    m = re.match(r"^標題[:：]\s*(.+)$", first)
    if not m:
        return "", summary
    title = m.group(1).strip()
    body = "\n".join(lines[idx + 1:]).lstrip("\n")
    return title, body


SCREENSHOT_SYSTEM_PROMPT = """你是一位 OCR 助理。你的工作是看截圖、找發文者、把內文 OCR 出來。
你不要做摘要、不要評論、不要分析。

寫作原則：
1. 全部用原文文字保留（截圖是中文就寫中文、英文就寫英文，不翻譯）
2. 保留發文者原本的段落結構與換行
3. 順序依截圖 1, 2, 3 ... 拼接
4. 嚴格遵守 user 給的輸出格式
"""


def _extract_poster_meta(text: str) -> tuple[str, str, str, str]:
    """從 Gemini 輸出抓 發文者 / 平台 / 標題，
    回 (poster, platform, title, body)。"""
    lines = text.split("\n")
    poster = ""
    platform = ""
    title = ""
    consumed = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            consumed = i + 1
            continue
        m_poster = re.match(r"^發文者[:：]\s*(.+)$", s)
        m_platform = re.match(r"^平台[:：]\s*(.+)$", s)
        m_title = re.match(r"^標題[:：]\s*(.+)$", s)
        if m_poster:
            poster = m_poster.group(1).strip()
            consumed = i + 1
        elif m_platform:
            platform = m_platform.group(1).strip()
            consumed = i + 1
        elif m_title:
            title = m_title.group(1).strip()
            consumed = i + 1
        else:
            break
    body = "\n".join(lines[consumed:]).lstrip("\n")
    return poster, platform, title, body


def summarize_screenshots_with_gemini(
    images: list[bytes], caption: str = "",
) -> str:
    """把多張截圖一次餵 Gemini Vision，純 OCR 不做摘要。"""
    from google.genai import types

    client = _get_gemini_client()
    parts = [
        types.Part.from_bytes(data=img, mime_type="image/jpeg")
        for img in images
    ]
    caption_block = (
        f"使用者貼截圖時附上的補充：\n{caption}" if caption.strip() else ""
    )
    prompt_text = SCREENSHOT_USER_PROMPT.format(
        n_images=len(images), caption_block=caption_block,
    )
    parts.append(types.Part(text=prompt_text))

    config = types.GenerateContentConfig(
        system_instruction=SCREENSHOT_SYSTEM_PROMPT,
        max_output_tokens=4096,
        thinking_config=types.ThinkingConfig(thinking_budget=1024),
    )
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=types.Content(parts=parts),
        config=config,
    )
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("Gemini 回傳空")
    return text


def _upload_image(remote_path: str, content: bytes):
    from dropbox_uploader import _get_client
    from dropbox.files import WriteMode
    dbx = _get_client()
    dbx.files_upload(
        content,
        remote_path,
        mode=WriteMode("overwrite"),
        autorename=False,
        mute=True,
    )


def save_screenshots_as_article(
    images: list[bytes], caption: str = "",
) -> dict:
    """多張截圖 → Gemini OCR（找發文者 + 內文）→ vault Posts + 9 Attachments。"""
    if not images:
        raise RuntimeError("沒有截圖")

    raw = summarize_screenshots_with_gemini(images, caption=caption)
    raw = _strip_inline_hashtags(raw)
    poster, platform, title, body = _extract_poster_meta(raw)
    if not title:
        first_line = body.strip().split("\n", 1)[0].strip() if body else ""
        title = first_line[:40] if first_line else (
            f"post-{datetime.now().strftime('%Y%m%d-%H%M')}"
        )
    safe_title = _safe_filename(title)
    captured_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    safe_stem = _safe_filename(title)
    topical = _topical_tags_via_vocab(body, safe_stem + ".md")
    fm = [
        "---",
        "type: post",
        f"title: {_yaml_str(title)}",
        f"poster: {_yaml_str(poster)}",
        f"platform: {_yaml_str(platform)}",
        f"captured_at: {_yaml_str(captured_at)}",
        _build_tags_line(["post", "social", "capture"], topical),
        "generated_by: gemini-vision-ocr",
        "---",
        "",
        f"# {title}",
    ]
    meta_lines = []
    if poster:
        meta_lines.append(f"**發文者**：{poster}")
    if platform:
        meta_lines.append(f"**平台**：{platform}")
    fm.extend(meta_lines)
    fm.append("")
    if caption.strip():
        fm.append(f"> 📌 補充：{caption.strip()}")
        fm.append("")
    fm.append(body or "(OCR 失敗)")

    md = "\n".join(fm)
    remote_path = f"{DROPBOX_VAULT_ARTICLES_PATH}/{safe_title}.md"
    _upload_md(remote_path, md)
    return {
        "title": title,
        "poster": poster,
        "remote_path": remote_path,
        "image_count": len(images),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: article_to_obsidian.py <url>")
        sys.exit(1)
    print(json.dumps(save_article_from_url(sys.argv[1]), ensure_ascii=False, indent=2))
