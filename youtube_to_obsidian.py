#!/usr/bin/env python3
"""
youtube_to_obsidian.py — 把 YouTube 影片直接餵給 Gemini 摘要、存進 Obsidian vault

用途：obsidian_bot 收到 YouTube URL 時的「輕量 capture」流程，跟 yt2epub
（重度逐字稿閱讀 + Kobo 畫重點）區分開。

呼叫流程：
1. fetch_youtube_meta(url) — oEmbed 抓 title / channel
2. summarize_video_with_gemini(url) — Gemini 原生 YouTube ingestion 出結構化摘要
3. render_video_md + 上傳 Dropbox `/Greens Obsidian/1 Sources/Videos/`

回傳：dict with {title, channel, remote_path}
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

DROPBOX_VAULT_VIDEOS_PATH = os.environ.get(
    "DROPBOX_VAULT_VIDEOS_PATH", "/Greens Obsidian/1 Sources/Videos"
)


SYSTEM_PROMPT = """你是一位資深 podcast / 影片摘要編輯，專門幫讀者把 YouTube 影片
轉成「個人知識庫條目」。

任務：給你一支 YouTube 影片，產出一份簡潔但有資訊密度的摘要，幫使用者快速
capture 影片精華進個人 Obsidian vault。

寫作原則：
1. 全部用繁體中文
2. 客觀中立 — 寫講者的觀點、不是你的評論
3. 簡潔精煉 — 每段都要有資訊量，禁止套話
4. 結構固定，方便日後回查 / cross-link
5. 不要重述影片每一段內容，重點是抓「為什麼這支影片值得記」

絕對不要：
- 評論「值不值得看」
- 加上你自己的角度
- 寫成 listicle 式的條列細節
"""


USER_PROMPT = """請為這支 YouTube 影片寫摘要。

請依下面格式輸出。**第一行必須是 `講者：person1（身份）/ person2（身份）` 格式**，
方便系統解析寫進 frontmatter。例如：

講者：Naval Ravikant（主持人）/ Andrej Karpathy（來賓）

## 一句話摘要
30-60 字，抓影片核心主旨。

## 講者背景
**1-2 段、共 120-200 字**，介紹主要講者：
- 身份與專業領域
- 為什麼這個人講這題有 credibility
- 其他重要公開作品（書 / podcast / 論文）

## 主要重點
3-5 個 bullet，每個 1-2 句，覆蓋影片最重要的：
- 論點 / 主張
- 具體案例 / 數據
- 反直覺 / 值得記的觀察

## 關鍵概念詞表
- **概念 A**：講者怎麼定義 / 怎麼用（1-2 句）
（5-8 個重要術語，未來 quiz 會考、cross-reference 用）

## 為什麼值得記
1-2 句，回答：為什麼這支影片值得收進個人知識庫？提出新角度？罕見訪談？整合得好？

## 同主題延伸
- **資源（作者/來源）**：跟這支影片的關聯（一句話）
（3-5 個。可以是 podcast / 論文 / 書 / 推文）

只輸出上述內容，不要前言、不要結語、不要 ```markdown``` 包裝。
"""


def extract_video_id(url: str) -> str:
    patterns = [
        r"(?:v=|/v/|youtu\.be/|/shorts/)([a-zA-Z0-9_-]{11})",
        r"(?:embed/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return ""


def fetch_youtube_meta(url: str) -> dict:
    """oEmbed 抓 title + author_name。"""
    oembed = (
        f"https://www.youtube.com/oembed?url={urllib.parse.quote(url, safe='')}"
        f"&format=json"
    )
    req = urllib.request.Request(oembed, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


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


def summarize_video_with_gemini(url: str) -> str:
    """Gemini 原生 YouTube ingestion：直接餵 URL 出摘要。"""
    from google.genai import types

    client = _get_gemini_client()
    contents = types.Content(
        parts=[
            types.Part(
                file_data=types.FileData(file_uri=url, mime_type="video/*")
            ),
            types.Part(text=USER_PROMPT),
        ]
    )
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        max_output_tokens=4096,
        thinking_config=types.ThinkingConfig(thinking_budget=2048),
    )
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
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


def _extract_speakers_line(summary: str) -> tuple[str, str]:
    """抓第一行 `講者：xxx`，回 (speakers, body_without_first_line)。"""
    lines = summary.split("\n")
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return "", summary
    first = lines[idx].strip()
    m = re.match(r"^講者[:：]\s*(.+)$", first)
    if not m:
        return "", summary
    speakers = m.group(1).strip()
    body = "\n".join(lines[idx + 1:]).lstrip("\n")
    return speakers, body


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


def render_video_md(
    title: str,
    channel: str,
    url: str,
    video_id: str,
    summary: str,
) -> str:
    speakers, body = _extract_speakers_line(summary)
    captured_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    safe_stem = re.sub(r'[/\\:*?"<>|]', "", title)[:120].strip()
    topical = _topical_tags_via_vocab(body, safe_stem + ".md")

    fm = [
        "---",
        "type: video",
        f"title: {_yaml_str(title)}",
        f"channel: {_yaml_str(channel)}",
        f"url: {_yaml_str(url)}",
        f"video_id: {_yaml_str(video_id)}",
        f"speakers: {_yaml_str(speakers)}",
        f"captured_at: {_yaml_str(captured_at)}",
        _build_tags_line(["video", "capture"], topical),
        "generated_by: gemini",
        "---",
        "",
        f"# {title}",
    ]
    if speakers:
        fm.append(f"*{speakers}*")
    elif channel:
        fm.append(f"*{channel}*")
    fm.append("")
    fm.append(f"[▶️ 原始影片]({url})")
    fm.append("")
    fm.append(body)

    return "\n".join(fm)


def save_video_summary(url: str) -> dict:
    """top-level：抓 meta、Gemini 摘要、上傳 Dropbox。
    回傳 {title, channel, remote_path, video_id}。"""
    from dropbox_uploader import _get_client
    from dropbox.files import WriteMode

    try:
        meta = fetch_youtube_meta(url)
    except Exception as e:
        meta = {}
        print(f"⚠️  oEmbed 失敗 {url}: {e}")

    title = meta.get("title") or extract_video_id(url) or "untitled"
    channel = meta.get("author_name", "") or ""
    video_id = extract_video_id(url)

    summary = summarize_video_with_gemini(url)
    md = render_video_md(title, channel, url, video_id, summary)

    safe_title = _safe_filename(title, max_len=120)
    filename = f"{safe_title}.md"
    remote_path = f"{DROPBOX_VAULT_VIDEOS_PATH}/{filename}"

    dbx = _get_client()
    dbx.files_upload(
        md.encode("utf-8"),
        remote_path,
        mode=WriteMode("overwrite"),
        autorename=False,
        mute=True,
    )

    return {
        "title": title,
        "channel": channel,
        "video_id": video_id,
        "remote_path": remote_path,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: youtube_to_obsidian.py <youtube_url>")
        sys.exit(1)
    result = save_video_summary(sys.argv[1])
    print(json.dumps(result, ensure_ascii=False, indent=2))
