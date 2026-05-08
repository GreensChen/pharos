#!/usr/bin/env python3
"""
chat_engine.py — vault-aware Q&A：把 user 的問題餵 Gemini，連同 vault 裡所有
Books + Interviews overview 當 context，回答 + 存 conversation log 到 vault。

被 obsidian_bot.py 的 /ask 流程呼叫。
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


GEMINI_MODEL = "gemini-3-flash-preview"

DROPBOX_VAULT_BOOKS_PATH = os.environ.get(
    "DROPBOX_VAULT_BOOKS_PATH", "/Greens Obsidian/1 Sources/Books"
)
DROPBOX_VAULT_INTERVIEWS_PATH = os.environ.get(
    "DROPBOX_VAULT_INTERVIEWS_PATH", "/Greens Obsidian/1 Sources/Interviews"
)
DROPBOX_VAULT_CONVERSATIONS_PATH = os.environ.get(
    "DROPBOX_VAULT_CONVERSATIONS_PATH",
    "/Greens Obsidian/2 Atomic Notes/Conversations",
)

# Context size cap — 拉所有 overview 大概 ~80K tokens、Flash 1M 撐得住但避免太貴
MAX_CONTEXT_CHARS = 250000

CHAT_SYSTEM_PROMPT = """你是 user 的個人圖書館員 + 思想夥伴。

User 在 Obsidian vault 累積了大量讀書筆記跟訪談摘要，他會丟問題給你。
你的工作是：

1. **基於他 vault 裡的內容回答**（books + interviews overview）。不要靠你的通用知識。
2. **誠實標記**：如果問題超出 vault 涵蓋範圍，直接說「你 vault 裡沒提到 X」。
3. **引用具體**：提到任何書 / 訪談時，用 `[[書名]]` 格式 wiki 連結（讓 user 在 Obsidian
   裡可以點過去）。
4. **量化、具體**：講「你關心 X」時、列舉 vault 裡哪幾本書/哪幾場訪談支持這個觀察。
5. **長度合理**：問題複雜的話可以寫長一點（500-1500 字），簡短問題就簡短答。
6. **不要套話**：禁止「這是個好問題」「整體來說」之類的開場 / 結語。

寫作風格：
- 全部用繁體中文
- 直接、有 substance、不繞彎
- 像跟一個熟悉你的編輯朋友聊天，不是 ChatGPT 的客套版本
- 偶爾可以挑戰 user（「你重點都在 X，但 Y 那本你只畫了 1 條，這代表...?」）
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


def _list_dropbox_md_files(folder: str) -> list:
    from dropbox_uploader import _get_client
    dbx = _get_client()
    try:
        result = dbx.files_list_folder(folder)
    except Exception as e:
        print(f"⚠️  list {folder} 失敗: {e}")
        return []
    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)
    return [e for e in entries if e.name.endswith(".md")]


def _download_dropbox_text(path: str) -> str:
    from dropbox_uploader import _get_client
    dbx = _get_client()
    _, resp = dbx.files_download(path)
    return resp.content.decode("utf-8", errors="replace")


def _build_corpus() -> str:
    """組所有 Books + Interviews overview 的全文 context。截到 MAX_CONTEXT_CHARS 內。"""
    pieces: list[str] = []
    total = 0

    for folder, kind in [
        (DROPBOX_VAULT_BOOKS_PATH, "📚 書"),
        (DROPBOX_VAULT_INTERVIEWS_PATH, "🎙 訪談"),
    ]:
        for entry in _list_dropbox_md_files(folder):
            if total >= MAX_CONTEXT_CHARS:
                break
            try:
                text = _download_dropbox_text(
                    entry.path_lower or entry.path_display
                )
            except Exception:
                continue
            stem = entry.name.removesuffix(".md")
            piece = f"\n\n=== {kind}：[[{stem}]] ===\n\n{text}"
            if total + len(piece) > MAX_CONTEXT_CHARS:
                # 可以留書名+簡介就好的話，用截斷版
                cap = MAX_CONTEXT_CHARS - total
                piece = piece[:cap]
            pieces.append(piece)
            total += len(piece)
    return "".join(pieces)


def chat_with_vault(question: str) -> str:
    """user 提問 → Gemini 用 vault 回答。回傳 Markdown 字串。"""
    from google.genai import types

    corpus = _build_corpus()
    user_prompt = (
        f"以下是我 Obsidian vault 裡所有 books / interviews overview，"
        f"當作你回答時的 context：\n\n"
        f"{corpus}\n\n"
        f"---\n\n"
        f"我的問題：{question}\n\n"
        f"請依 vault 內容回答。"
    )

    client = _get_gemini_client()
    config = types.GenerateContentConfig(
        system_instruction=CHAT_SYSTEM_PROMPT,
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


def _safe_filename(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[/\\:*?"<>|\r\n\t]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len].rstrip()


def _yaml_str(s) -> str:
    if s is None:
        return '""'
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def save_conversation_to_vault(question: str, answer: str) -> str:
    """寫 Q&A 到 Dropbox vault Conversations folder。回傳遠端路徑。"""
    from dropbox_uploader import _get_client
    from dropbox.files import WriteMode

    now = datetime.now()
    ts_compact = now.strftime("%Y%m%d-%H%M")
    # 用問題前 40 字當 title
    short_q = question.strip().split("\n", 1)[0]
    title_seed = short_q[:40] if short_q else "conversation"
    safe_title = _safe_filename(title_seed)
    filename = f"{safe_title}_{ts_compact}.md"
    remote_path = f"{DROPBOX_VAULT_CONVERSATIONS_PATH}/{filename}"

    fm = [
        "---",
        "type: conversation",
        f"asked_at: {_yaml_str(now.strftime('%Y-%m-%dT%H:%M:%S'))}",
        f"question_preview: {_yaml_str(short_q[:80])}",
        "tags: [conversation, ask]",
        "generated_by: gemini-vault-aware",
        "---",
        "",
        f"# {short_q[:60]}",
        "",
        "## 問題",
        "",
        question.strip(),
        "",
        "## 回答",
        "",
        answer.strip(),
        "",
    ]

    md = "\n".join(fm)
    dbx = _get_client()
    dbx.files_upload(
        md.encode("utf-8"),
        remote_path,
        mode=WriteMode("overwrite"),
        autorename=False,
        mute=True,
    )
    return remote_path


def ask(question: str) -> dict:
    """top-level：讀 vault → Gemini 回答 → 存 vault。回傳 {answer, remote_path}。"""
    answer = chat_with_vault(question)
    remote_path = save_conversation_to_vault(question, answer)
    return {"answer": answer, "remote_path": remote_path}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: chat_engine.py '<your question>'")
        sys.exit(1)
    q = " ".join(sys.argv[1:])
    result = ask(q)
    print(result["answer"])
    print()
    print(f"saved: {result['remote_path']}")
