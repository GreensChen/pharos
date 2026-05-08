#!/usr/bin/env python3
"""
vocabulary_manager.py — self-curating tag vocabulary

提供：
- load_vocabulary() / save_vocabulary()
- apply_tags_to_capture(content, source_filename) → list[str]
  （新 capture 用：load → AI 挑 or 提新 tag → bump count → save → 回 tag list）
- propose_consolidation(vocab) → list[merge ops]
- apply_consolidation(vocab, merges) → 改寫 vault md + log + save vocab

儲存：~/Dropbox/Greens Obsidian/.vocabulary.json
- Mac 端用本地 file（透過 Dropbox 桌面 sync 同步雲端）
- Server 端用 Dropbox API（自動偵測）
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass


GEMINI_MODEL = "gemini-3-flash-preview"

def _resolve_local_vault() -> Path:
    """找實際 vault 路徑。優先 macOS Dropbox 真實路徑（CloudStorage）、
    再 fallback 到傳統 ~/Dropbox 符號連結。launchd 在 sandbox 下可能無法
    跨 symlink 進 CloudStorage、所以直接用實體路徑最穩。"""
    candidates = [
        Path.home() / "Library" / "CloudStorage" / "Dropbox" / "Greens Obsidian",
        Path.home() / "Dropbox" / "Greens Obsidian",
    ]
    for c in candidates:
        try:
            if c.exists():
                return c
        except (OSError, PermissionError):
            continue
    # 都沒有 → fallback path（會走 Dropbox API 模式）
    return candidates[0]


LOCAL_VAULT = _resolve_local_vault()
LOCAL_VOCAB_PATH = LOCAL_VAULT / ".vocabulary.json"
DROPBOX_VOCAB_PATH = "/Greens Obsidian/.vocabulary.json"


def _local_vault_accessible() -> bool:
    """測 LOCAL_VAULT 是否真的可讀。
    launchd 在 macOS TCC 限制下不能讀 ~/Library/CloudStorage、即便檔案存在。
    這裡嘗試實際讀一個檔來判斷有沒有讀權限、不只是 .exists()。"""
    if not LOCAL_VAULT.exists():
        return False
    # 試讀 vocab 檔（如果有），沒有就試列 vault 目錄
    try:
        if LOCAL_VOCAB_PATH.exists():
            with open(LOCAL_VOCAB_PATH, "rb") as f:
                f.read(1)
        else:
            next(iter(LOCAL_VAULT.iterdir()), None)
        return True
    except (OSError, PermissionError):
        return False


# launchd 跑時 LOCAL_VAULT.exists() 為 True 但讀檔被 TCC 擋、所以要實測
USE_DROPBOX_API = not _local_vault_accessible()


def _empty_vocab() -> dict:
    return {
        "tags": {},
        "merge_log": [],
        "new_since_last_consolidation": 0,
        "last_consolidation": None,
    }


def _load_local() -> dict:
    if not LOCAL_VOCAB_PATH.exists():
        return _empty_vocab()
    try:
        return json.loads(LOCAL_VOCAB_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _empty_vocab()


def _save_local(vocab: dict):
    LOCAL_VOCAB_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_VOCAB_PATH.write_text(
        json.dumps(vocab, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_dropbox() -> dict:
    from dropbox_uploader import _get_client
    dbx = _get_client()
    try:
        _, resp = dbx.files_download(DROPBOX_VOCAB_PATH)
        return json.loads(resp.content.decode("utf-8"))
    except Exception:
        return _empty_vocab()


def _save_dropbox(vocab: dict):
    from dropbox_uploader import _get_client
    from dropbox.files import WriteMode
    dbx = _get_client()
    content = json.dumps(vocab, ensure_ascii=False, indent=2).encode("utf-8")
    dbx.files_upload(
        content,
        DROPBOX_VOCAB_PATH,
        mode=WriteMode("overwrite"),
        autorename=False,
        mute=True,
    )


def load_vocabulary() -> dict:
    return _load_dropbox() if USE_DROPBOX_API else _load_local()


def save_vocabulary(vocab: dict):
    if USE_DROPBOX_API:
        _save_dropbox(vocab)
    else:
        _save_local(vocab)


# ─────────────────────────────────────────────
# Per-capture：選 or 提 tag
# ─────────────────────────────────────────────

SELECT_OR_PROPOSE_PROMPT = """你是知識庫的 tag curator。

目標：給定一份內容，從現有 vocabulary 挑 1-3 個最 relevant 的 tag。
如果現有 tag 確實涵蓋不了核心概念，且這個概念會反覆出現，才提一個新 tag。

原則：
1. **優先用現有 tag**。新 tag 要有強理由
2. 一份內容最多 3 個 tag、少而精
3. 新 tag 要 kebab-case 英文、3-25 字元、語意具體
4. 新 tag 必須跟所有現有 tag 顯著不同（避免 synonym sprawl）
5. 不要太泛（不要 thinking / general / knowledge / learning 這種沒區辨力的）

只回 JSON：
{
  "use_existing": ["ai", "business-strategy"],
  "propose_new": [
    {"name": "agentic-systems", "definition": "AI 自主代理人架構與設計", "reason": "目前 vocab 缺、本內容主軸"}
  ]
}
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


def _format_vocab_for_prompt(vocab: dict) -> str:
    tags = vocab.get("tags", {})
    if not tags:
        return "(空 — 沒任何 tag、可自由提新的)"
    lines = []
    for name, info in sorted(tags.items()):
        defn = info.get("definition", "")
        cnt = info.get("count", 0)
        lines.append(f"- **{name}** ({cnt})：{defn}")
    return "\n".join(lines)


def select_or_propose(content: str, vocab: dict) -> tuple:
    """送內容 + 現有 vocab 給 Gemini、要它選或提 tag。
    回 (existing_used: list[str], new_added: list[str])。
    新 tag 已經 inplace 加進 vocab["tags"] 但 count 還是 0、由 caller bump。"""
    from google.genai import types

    try:
        client = _get_gemini_client()
    except RuntimeError:
        return [], []

    vocab_block = _format_vocab_for_prompt(vocab)
    prompt = (
        f"現有 vocabulary（依字母序）：\n\n{vocab_block}\n\n"
        f"---\n\n以下是新內容、依規則挑或提 tag：\n\n{content[:5000]}"
    )

    config = types.GenerateContentConfig(
        system_instruction=SELECT_OR_PROPOSE_PROMPT,
        max_output_tokens=512,
        thinking_config=types.ThinkingConfig(thinking_budget=512),
        response_mime_type="application/json",
    )
    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=config,
        )
    except Exception as e:
        print(f"⚠️  Gemini 失敗：{e}")
        return [], []

    text = (resp.text or "").strip()
    if not text:
        return [], []
    try:
        data = json.loads(text)
    except Exception:
        return [], []

    existing_used = []
    for t in data.get("use_existing", []) or []:
        if isinstance(t, str) and t in vocab.get("tags", {}):
            existing_used.append(t)

    new_added = []
    for n in data.get("propose_new", []) or []:
        if not isinstance(n, dict):
            continue
        raw_name = (n.get("name") or "").strip().lower()
        name = re.sub(r"\s+", "-", raw_name).replace("_", "-")
        name = re.sub(r"[^a-z0-9\-]", "", name)
        if not name or len(name) < 3 or len(name) > 25:
            continue
        if name in vocab.get("tags", {}):
            continue
        defn = (n.get("definition") or "").strip()
        today = datetime.now().strftime("%Y-%m-%d")
        vocab.setdefault("tags", {})[name] = {
            "definition": defn,
            "count": 0,
            "first_seen": today,
            "last_seen": today,
            "examples": [],
        }
        new_added.append(name)

    return existing_used, new_added


def bump_tag_counts(vocab: dict, tags: list, source_filename: str = ""):
    today = datetime.now().strftime("%Y-%m-%d")
    for t in tags:
        info = vocab.get("tags", {}).get(t)
        if info is None:
            continue
        info["count"] = info.get("count", 0) + 1
        info["last_seen"] = today
        if source_filename:
            link = f"[[{source_filename.removesuffix('.md')}]]"
            examples = info.get("examples", [])
            if link not in examples:
                examples.append(link)
                info["examples"] = examples[-5:]


def apply_tags_to_capture(content: str, source_filename: str = "") -> list:
    """top-level helper for generators：load → AI tag → bump → save → 回 tag list。"""
    vocab = load_vocabulary()
    existing, new_added = select_or_propose(content, vocab)
    all_tags = list(existing) + list(new_added)
    if not all_tags:
        return []
    bump_tag_counts(vocab, all_tags, source_filename)
    if new_added:
        vocab["new_since_last_consolidation"] = (
            vocab.get("new_since_last_consolidation", 0) + len(new_added)
        )
    save_vocabulary(vocab)
    return all_tags


# ─────────────────────────────────────────────
# Periodic consolidation
# ─────────────────────────────────────────────

CONSOLIDATE_PROMPT = """你是知識庫的 tag curator、要對既有 vocabulary 做整合。

任務：找出「應該合併」的 tag 群組。標準：
1. **同義詞或高度重疊**（例：behavioral-finance + behavioral-economics → behavioral-economics）
2. **過細粒度而沒必要分**（例：apple-products + apple-history + apple → apple）
3. **錯誤命名 / typo / 語意漂移**

不該合併的：
- 真正不同的概念（即便看起來相關）
- 廣義 vs 狹義（例：strategy 跟 business-strategy 不該合）

只回 JSON 陣列、每個 element 是一個合併操作：
[
  {"merged": ["behavioral-finance", "behavioral-economics"], "into": "behavioral-economics", "reason": "..."},
  ...
]

如果沒任何該合併的、回空陣列 []。**保守一點**、不確定就不合。
"""


def propose_consolidation(vocab: dict) -> list:
    from google.genai import types
    try:
        client = _get_gemini_client()
    except RuntimeError:
        return []

    vocab_block = _format_vocab_for_prompt(vocab)
    config = types.GenerateContentConfig(
        system_instruction=CONSOLIDATE_PROMPT,
        max_output_tokens=2048,
        thinking_config=types.ThinkingConfig(thinking_budget=2048),
        response_mime_type="application/json",
    )
    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"目前 vocabulary：\n\n{vocab_block}",
            config=config,
        )
    except Exception as e:
        print(f"⚠️  Gemini consolidation 失敗：{e}")
        return []

    text = (resp.text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _build_rename_map(merges: list) -> dict:
    rename = {}
    for m in merges:
        merged = m.get("merged", []) or []
        into = m.get("into", "")
        if not merged or not into:
            continue
        for old in merged:
            if old != into:
                rename[old] = into
    return rename


def _rewrite_tags_in_text(text: str, rename_map: dict):
    """如果 text 的 tags array 含有要改的 tag、回新 text；否則回 None。"""
    m = re.search(r"^(tags:\s*\[)(.*?)(\]\s*)$", text, re.MULTILINE)
    if not m:
        return None
    tags = [t.strip() for t in m.group(2).split(",") if t.strip()]
    if not any(t in rename_map for t in tags):
        return None
    seen = set()
    new_tags = []
    for t in tags:
        mapped = rename_map.get(t, t)
        if mapped not in seen:
            seen.add(mapped)
            new_tags.append(mapped)
    if new_tags == tags:
        return None
    new_line = f"{m.group(1)}{', '.join(new_tags)}{m.group(3)}"
    return text[:m.start()] + new_line + text[m.end():]


def _apply_consolidation_local(rename_map: dict) -> int:
    files_changed = 0
    for md in LOCAL_VAULT.glob("**/*.md"):
        if md.name.startswith("."):
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        new_text = _rewrite_tags_in_text(text, rename_map)
        if new_text is None:
            continue
        try:
            md.write_text(new_text, encoding="utf-8")
            files_changed += 1
        except Exception:
            continue
    return files_changed


def _apply_consolidation_dropbox(rename_map: dict) -> int:
    """Server 端：Dropbox API 走遍 /Greens Obsidian/、改寫受影響 md。"""
    import time as _time
    from dropbox_uploader import _get_client
    from dropbox.files import WriteMode

    dbx = _get_client()
    files_changed = 0

    try:
        result = dbx.files_list_folder("/Greens Obsidian", recursive=True)
        entries = list(result.entries)
        while result.has_more:
            result = dbx.files_list_folder_continue(result.cursor)
            entries.extend(result.entries)
    except Exception as e:
        print(f"⚠️  Dropbox list_folder 失敗: {e}")
        return 0

    affected = set(rename_map.keys())
    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        path = getattr(entry, "path_display", None) or getattr(entry, "path_lower", None)
        if not path:
            continue
        try:
            _, resp = dbx.files_download(path)
            text = resp.content.decode("utf-8", errors="replace")
        except Exception:
            continue
        # 快速 pre-check：若文字裡都不含 affected tag 字面，跳過 download 的 parse
        if not any(t in text for t in affected):
            continue
        new_text = _rewrite_tags_in_text(text, rename_map)
        if new_text is None:
            continue
        try:
            dbx.files_upload(
                new_text.encode("utf-8"),
                path,
                mode=WriteMode("overwrite"),
                autorename=False,
                mute=True,
            )
            files_changed += 1
            _time.sleep(0.3)  # gentle rate limit
        except Exception as e:
            print(f"⚠️  upload {path} 失敗: {e}")
    return files_changed


def _update_vocab_after_merge(vocab: dict, rename_map: dict, merges: list):
    today = datetime.now().strftime("%Y-%m-%d")
    for old, into in rename_map.items():
        if old in vocab.get("tags", {}):
            old_info = vocab["tags"].pop(old)
            if into not in vocab.get("tags", {}):
                vocab.setdefault("tags", {})[into] = old_info
                vocab["tags"][into]["count"] = old_info.get("count", 0)
            else:
                vocab["tags"][into]["count"] = (
                    vocab["tags"][into].get("count", 0) +
                    old_info.get("count", 0)
                )
                merged_examples = list(dict.fromkeys(
                    (vocab["tags"][into].get("examples") or []) +
                    (old_info.get("examples") or [])
                ))
                vocab["tags"][into]["examples"] = merged_examples[-5:]
    for m in merges:
        vocab.setdefault("merge_log", []).append({
            "date": today,
            "merged": m.get("merged"),
            "into": m.get("into"),
            "reason": m.get("reason"),
        })
    vocab["new_since_last_consolidation"] = 0
    vocab["last_consolidation"] = today


def _build_changelog_block(merges: list, files_changed: int, today: str) -> str:
    lines = [f"## {today}", ""]
    for m in merges:
        lines.append(
            f"- 合併 `{', '.join(m.get('merged', []))}` → `{m.get('into', '')}`"
        )
        lines.append(f"  - 原因：{m.get('reason', '')}")
    lines.append("")
    lines.append(f"**影響檔案**：{files_changed} 個")
    lines.append("")
    return "\n".join(lines) + "\n"


CHANGELOG_HEADER = (
    "---\n"
    "type: meta\n"
    "title: \"Vocabulary Changelog\"\n"
    "tags: [meta]\n"
    "---\n\n"
    "# Vocabulary Changelog\n\n"
    "AI 自動 tag 整合的紀錄。每一段是一次 consolidation 跑出來的合併動作。\n\n"
)


def _write_changelog_local(merges: list, files_changed: int):
    today = datetime.now().strftime("%Y-%m-%d")
    path = LOCAL_VAULT / "2 Atomic Notes" / "Vocabulary Changelog.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else CHANGELOG_HEADER
    path.write_text(
        existing + _build_changelog_block(merges, files_changed, today),
        encoding="utf-8",
    )


def _write_changelog_dropbox(merges: list, files_changed: int):
    from dropbox_uploader import _get_client
    from dropbox.files import WriteMode

    today = datetime.now().strftime("%Y-%m-%d")
    remote = "/Greens Obsidian/2 Atomic Notes/Vocabulary Changelog.md"
    dbx = _get_client()
    try:
        _, resp = dbx.files_download(remote)
        existing = resp.content.decode("utf-8", errors="replace")
    except Exception:
        existing = CHANGELOG_HEADER
    new_text = existing + _build_changelog_block(merges, files_changed, today)
    dbx.files_upload(
        new_text.encode("utf-8"),
        remote,
        mode=WriteMode("overwrite"),
        autorename=False,
        mute=True,
    )


def apply_consolidation(vocab: dict, merges: list) -> int:
    """
    套用 merges：
    - rewrite vault 所有 md 的 tags（Mac 走 local fs、Server 走 Dropbox API）
    - update vocab（合併 count、刪舊 entry）
    - 寫 changelog 到 vault `2 Atomic Notes/Vocabulary Changelog.md`
    - save vocab
    回傳：受影響檔案數
    """
    if not merges:
        return 0
    rename_map = _build_rename_map(merges)
    if not rename_map:
        return 0

    if USE_DROPBOX_API:
        files_changed = _apply_consolidation_dropbox(rename_map)
        _update_vocab_after_merge(vocab, rename_map, merges)
        _write_changelog_dropbox(merges, files_changed)
    else:
        files_changed = _apply_consolidation_local(rename_map)
        _update_vocab_after_merge(vocab, rename_map, merges)
        _write_changelog_local(merges, files_changed)

    save_vocabulary(vocab)
    return files_changed


if __name__ == "__main__":
    # debug：印目前 vocabulary
    v = load_vocabulary()
    print(f"USE_DROPBOX_API: {USE_DROPBOX_API}")
    print(f"tags: {len(v.get('tags', {}))}")
    print(f"new_since_last_consolidation: {v.get('new_since_last_consolidation', 0)}")
    print(f"last_consolidation: {v.get('last_consolidation')}")
    for name, info in sorted(v.get("tags", {}).items())[:20]:
        print(f"  {name} ({info.get('count', 0)})")
