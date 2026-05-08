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

User 在 Obsidian vault 累積了大量讀書筆記跟訪談摘要，會跟你持續對話。
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


SUMMARY_PROMPT = """User 已經 /endchat 結束這輪對話。請依下面格式寫一份完整的對話摘要：

## 我們聊了什麼
3-5 個 bullet，講 user 在這輪對話探索的主題。

## 我給你的觀察
3-5 個 bullet，整理你給 user 的判斷 / 挑戰 / 引用過的關鍵 [[書名]] [[訪談]]。

## 待釐清 / 可繼續挖
1-3 個 bullet，這次沒講完、user 之後可以追問的方向。

只回上述三段，不要前言、不要結語。
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


def build_first_user_turn(question: str) -> str:
    """組第一輪 user message — corpus + 初始問題。之後追問就不用再帶 corpus。"""
    corpus = _build_corpus()
    return (
        f"以下是我 Obsidian vault 裡所有 books / interviews overview，"
        f"當作你回答時的 context：\n\n"
        f"{corpus}\n\n"
        f"---\n\n"
        f"我的問題：{question}\n\n"
        f"請依 vault 內容回答。我之後可能會繼續追問。"
    )


def chat_turn(history: list) -> str:
    """history 是 [{'role':'user'|'model', 'text':'...'}, ...]。
    回 Gemini 對最後一輪的回應。"""
    from google.genai import types

    contents = []
    for turn in history:
        role = turn.get("role", "user")
        # Gemini API 用 'model' 表示 assistant
        if role not in ("user", "model"):
            role = "user"
        contents.append(
            types.Content(
                role=role,
                parts=[types.Part(text=turn.get("text", ""))],
            )
        )

    client = _get_gemini_client()
    config = types.GenerateContentConfig(
        system_instruction=CHAT_SYSTEM_PROMPT,
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


def _safe_filename(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[/\\:*?"<>|\r\n\t]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:max_len].rstrip()


def _yaml_str(s) -> str:
    if s is None:
        return '""'
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def save_conversation_log(
    history: list, started_at: datetime, end_summary: str = "",
) -> str:
    """寫整段多輪對話 log + 結尾摘要到 Dropbox vault。回傳遠端路徑。

    history 第一輪 user message 會包含 corpus prefix（「以下是我 Obsidian vault...」），
    存 log 時把那段拿掉、只保留實際問題。
    """
    from dropbox_uploader import _get_client
    from dropbox.files import WriteMode

    now = datetime.now()
    ts_compact = now.strftime("%Y%m%d-%H%M")

    # 第一輪實際問題（剝掉 corpus prefix）
    first_user = next((h for h in history if h.get("role") == "user"), {"text": ""})
    first_q = first_user.get("text", "")
    # 找「我的問題：」之後的內容
    m = re.search(r"我的問題[:：]\s*(.+?)(?:\n\n請依 vault|\Z)", first_q, re.DOTALL)
    first_q_clean = m.group(1).strip() if m else first_q

    title_seed = first_q_clean.split("\n", 1)[0][:40] or "conversation"
    safe_title = _safe_filename(title_seed)
    filename = f"{safe_title}_{ts_compact}.md"
    remote_path = f"{DROPBOX_VAULT_CONVERSATIONS_PATH}/{filename}"

    fm = [
        "---",
        "type: conversation",
        f"started_at: {_yaml_str(started_at.strftime('%Y-%m-%dT%H:%M:%S'))}",
        f"ended_at: {_yaml_str(now.strftime('%Y-%m-%dT%H:%M:%S'))}",
        f"turns: {len([h for h in history if h.get('role') == 'user'])}",
        f"first_question: {_yaml_str(first_q_clean[:100])}",
        "tags: [conversation, ask, multi-turn]",
        "generated_by: gemini-vault-aware",
        "---",
        "",
        f"# {first_q_clean[:60]}",
        "",
    ]

    if end_summary.strip():
        fm.append("## 結尾摘要")
        fm.append("")
        fm.append(end_summary.strip())
        fm.append("")
        fm.append("---")
        fm.append("")

    fm.append("## 對話 log")
    fm.append("")

    turn_idx = 0
    for h in history:
        role = h.get("role")
        text = h.get("text", "").strip()
        if role == "user":
            turn_idx += 1
            # 第一輪剝掉 corpus prefix
            if turn_idx == 1:
                text = first_q_clean
            fm.append(f"### Q{turn_idx}")
            fm.append("")
            fm.append(text)
            fm.append("")
        elif role == "model":
            fm.append(f"### A{turn_idx}")
            fm.append("")
            fm.append(text)
            fm.append("")

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


def generate_end_summary(history: list) -> str:
    """user /endchat 後、用既有 history 跑一次 Gemini 拿摘要。"""
    end_history = list(history)
    end_history.append({"role": "user", "text": SUMMARY_PROMPT})
    return chat_turn(end_history)


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
