#!/usr/bin/env python3
"""
quiz_engine.py — 從書籍 overview + 你的 highlights 出題（server 端 Dropbox API）

obsidian_bot.py 的 /quiz 流程呼叫：
1. list_quizzable_books() — 列出 vault 裡同時有 overview 跟 highlights 的書
2. generate_quiz(book) — 用 Gemini 生 3 選 1 quiz
3. save_quiz_response_to_dropbox(...) — 答完寫到 Obsidian vault
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass


# Dropbox vault paths
DROPBOX_VAULT_BOOKS_PATH = os.environ.get(
    "DROPBOX_VAULT_BOOKS_PATH", "/Greens Obsidian/1 Sources/Books"
)
DROPBOX_VAULT_INTERVIEWS_PATH = os.environ.get(
    "DROPBOX_VAULT_INTERVIEWS_PATH", "/Greens Obsidian/1 Sources/Interviews"
)
DROPBOX_VAULT_HIGHLIGHTS_PATH = os.environ.get(
    "DROPBOX_VAULT_HIGHLIGHTS_PATH", "/Greens Obsidian/1 Sources/Highlights"
)
DROPBOX_VAULT_QUIZZES_PATH = os.environ.get(
    "DROPBOX_VAULT_QUIZZES_PATH", "/Greens Obsidian/2 Atomic Notes/Quizzes"
)

GEMINI_MODEL = "gemini-3-flash-preview"

QUIZ_SYSTEM_PROMPT = """你是一位嚴格但有耐心的家教，幫讀者做 active recall 測驗。

任務：給你一本書的客觀 overview 跟讀者畫的重點，出一道 3 選 1 的選擇題，
測讀者對「作者真正想表達的核心概念」的理解。

出題原則：
1. 題目要考概念理解、不要考記憶細節（例：不要問「作者在第 3 章提了哪個例子」）
2. 三個選項都要看似合理、但只有一個真正符合作者觀點
3. 錯誤選項要反映「常見誤解」或「相近但不對的觀點」，不能太蠢
4. 題目跟選項全部用繁體中文
5. 解析簡潔（2-3 句）、講清楚為什麼正確答案對、其他為什麼錯

只回傳 JSON：
{
  "question": "題目（一句話）",
  "options": [
    {"label": "A", "text": "選項 A"},
    {"label": "B", "text": "選項 B"},
    {"label": "C", "text": "選項 C"}
  ],
  "correct": "A",
  "explanation": "解析"
}
"""

QUIZ_USER_TEMPLATE = """書：{title}

## 客觀 overview
{overview}

---

## 讀者畫的重點（節錄前 3000 字）
{highlights_excerpt}

---

請依上述 overview 出一道概念性 3 選 1。題目盡量挑「讀者畫了重點、但 overview 顯示
作者其實還想強調 X」這種容易誤解的角度。如果讀者重點跟作者主旨一致，就考延伸應用
或關鍵概念辨析。
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


def _list_dropbox_md_files(folder: str) -> dict:
    """list folder, return {filename: server_modified_timestamp}."""
    from dropbox_uploader import _get_client
    dbx = _get_client()
    try:
        result = dbx.files_list_folder(folder)
    except Exception as e:
        print(f"⚠️  list {folder} 失敗: {e}")
        return {}
    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)
    out = {}
    for e in entries:
        if not e.name.endswith(".md"):
            continue
        out[e.name] = e.server_modified.timestamp() if hasattr(e, "server_modified") else 0
    return out


def list_quizzable_books() -> list[dict]:
    """vault 裡同時有 overview（Books 或 Interviews）+ Highlights 的內容。
    依 highlight 最後修改排序。"""
    books_files = _list_dropbox_md_files(DROPBOX_VAULT_BOOKS_PATH)
    interview_files = _list_dropbox_md_files(DROPBOX_VAULT_INTERVIEWS_PATH)
    hl_files = _list_dropbox_md_files(DROPBOX_VAULT_HIGHLIGHTS_PATH)

    quizzable = []
    for fname, overview_dir in (
        [(f, DROPBOX_VAULT_BOOKS_PATH) for f in books_files]
        + [(f, DROPBOX_VAULT_INTERVIEWS_PATH) for f in interview_files]
    ):
        if fname not in hl_files:
            continue
        kind = "interview" if overview_dir == DROPBOX_VAULT_INTERVIEWS_PATH else "book"
        quizzable.append({
            "title": fname.removesuffix(".md"),
            "kind": kind,
            "overview_path": f"{overview_dir}/{fname}",
            "highlights_path": f"{DROPBOX_VAULT_HIGHLIGHTS_PATH}/{fname}",
            "last_activity": hl_files[fname],
        })
    quizzable.sort(key=lambda b: b["last_activity"], reverse=True)
    return quizzable


def find_book_by_keyword(keyword: str) -> list[dict]:
    """user `/quiz keyword` 用 substring match 找書。"""
    keyword_lower = keyword.lower()
    return [
        b for b in list_quizzable_books()
        if keyword_lower in b["title"].lower()
    ]


def _download_dropbox_text(path: str) -> str:
    from dropbox_uploader import _get_client
    dbx = _get_client()
    _, resp = dbx.files_download(path)
    return resp.content.decode("utf-8", errors="replace")


def generate_quiz(book: dict) -> dict:
    """呼叫 Gemini 出 3 選 1 quiz。回傳 dict with question/options/correct/explanation。"""
    from google.genai import types

    overview = _download_dropbox_text(book["overview_path"])
    highlights = _download_dropbox_text(book["highlights_path"])
    highlights_excerpt = highlights[:3000]

    user_prompt = QUIZ_USER_TEMPLATE.format(
        title=book["title"],
        overview=overview,
        highlights_excerpt=highlights_excerpt,
    )

    client = _get_gemini_client()
    config = types.GenerateContentConfig(
        system_instruction=QUIZ_SYSTEM_PROMPT,
        max_output_tokens=2048,
        thinking_config=types.ThinkingConfig(thinking_budget=1024),
        response_mime_type="application/json",
    )
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_prompt,
        config=config,
    )
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("Gemini 回傳空")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gemini JSON parse 失敗：{e}\nraw: {text[:200]}")

    if not all(k in data for k in ("question", "options", "correct", "explanation")):
        raise RuntimeError(f"Quiz JSON 缺欄位：{list(data.keys())}")
    if len(data.get("options", [])) != 3:
        raise RuntimeError(f"options 應該 3 個，得到 {len(data.get('options', []))}")

    return data


def _safe_filename(name: str) -> str:
    name = re.sub(r'[/\\:*?"<>|\r\n\t]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:60].rstrip()


def _yaml_str(s) -> str:
    if s is None:
        return '""'
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def save_quiz_response_to_dropbox(
    book_title: str,
    quiz: dict,
    user_choice: str,
    is_correct: bool,
) -> str:
    """寫 quiz 紀錄到 Dropbox vault。回傳遠端路徑。"""
    from dropbox_uploader import _get_client
    from dropbox.files import WriteMode

    now = datetime.now()
    ts = now.strftime("%Y%m%d-%H%M")
    short_book = _safe_filename(book_title)
    filename = f"{short_book}_{ts}.md"
    remote_path = f"{DROPBOX_VAULT_QUIZZES_PATH}/{filename}"

    fm = [
        "---",
        "type: quiz",
        f"book: {_yaml_str(book_title)}",
        f"asked_at: {_yaml_str(now.strftime('%Y-%m-%dT%H:%M:%S'))}",
        f"correct_option: {_yaml_str(quiz['correct'])}",
        f"user_choice: {_yaml_str(user_choice)}",
        f"correct: {str(is_correct).lower()}",
        "tags: [quiz, active-recall]",
        "---",
        "",
        f"# Quiz · {book_title}",
        "",
        f"來源：[[{book_title}]]",
        "",
        "## 題目",
        "",
        quiz["question"],
        "",
    ]
    for o in quiz["options"]:
        marker = ""
        if o["label"] == quiz["correct"]:
            marker = " ✓"
        if o["label"] == user_choice:
            if is_correct:
                marker = " ✓ ← 你選的"
            else:
                marker += " ← 你選的"
        fm.append(f"- **{o['label']}**：{o['text']}{marker}")
    fm.append("")
    fm.append("## 結果")
    fm.append("")
    fm.append("✅ 答對" if is_correct else "❌ 答錯")
    fm.append("")
    fm.append("## 解析")
    fm.append("")
    fm.append(quiz["explanation"])

    md_text = "\n".join(fm)

    dbx = _get_client()
    dbx.files_upload(
        md_text.encode("utf-8"),
        remote_path,
        mode=WriteMode("overwrite"),
        autorename=False,
        mute=True,
    )
    return remote_path
