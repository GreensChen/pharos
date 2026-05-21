#!/usr/bin/env python3
"""
yt2epub — YouTube 影片轉雙語 ePub
===================================
YouTube URL → 抓英文字幕 → Claude 翻譯繁中 → 雙語 ePub → Dropbox → Kobo

使用方式:
    python yt2epub.py "https://www.youtube.com/watch?v=VIDEO_ID"
    python yt2epub.py "https://www.youtube.com/watch?v=VIDEO_ID" --title "DOAC EP1"
    python yt2epub.py "https://www.youtube.com/watch?v=VIDEO_ID" --speakers "Steven Bartlett（主持人）,Mo Gawdat（來賓）"

也支援本地音檔（自動 fallback 到 AssemblyAI 轉錄）:
    python yt2epub.py episode.mp3

環境變數:
    ANTHROPIC_API_KEY      - 必須（翻譯用）
    ASSEMBLYAI_API_KEY     - 選用（僅音檔 fallback 時需要）
"""

import argparse
import anthropic
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from ebooklib import epub
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass


# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────

# Mac 上 Dropbox 桌面版同步資料夾（中文 locale 預設叫「應用程式」，英文叫 Apps）
# 若你的 Dropbox 介面是英文，改 .env 的 DROPBOX_KOBO_LOCAL_PATH 或自行修改路徑
DROPBOX_KOBO_PATH = Path(
    os.environ.get(
        "DROPBOX_KOBO_LOCAL_PATH",
        str(Path.home() / "Dropbox" / "應用程式" / "Rakuten Kobo"),
    )
)
OUTPUT_DIR = Path.home() / "yt2epub_output"

# LLM provider：gemini | claude（環境變數 LLM_PROVIDER 可覆蓋）
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini").lower()
CLAUDE_MODEL = "claude-sonnet-4-6"
GEMINI_MODEL = "gemini-3-flash-preview"  # 速度比 pro 快 6-8 倍、成本 1/10
# thinking budget（Gemini 3）：1024≈low / 4096≈medium / 16384≈high / -1=auto
GEMINI_THINKING_TRANSLATE = 1024  # 翻譯：簡單任務，低 thinking
GEMINI_THINKING_CHAPTER = 4096    # 分章節：需要推理

TRANSLATION_SYSTEM_PROMPT = """你是一位專業的英翻繁體中文翻譯。請遵守以下規則：

1. 翻譯成自然流暢的繁體中文書面語（適合閱讀，不是逐字逐句的口語稿）
2. **省略所有語助詞與口吻贅字**，例如：
   - 英文：uh、um、er、you know、I mean、like（語助詞用法）、actually（贅字）、so（贅字）、well、right
   - 中文：嗯、痾、啊、喔、那個、你知道、我是說、就是、然後（贅字）
   翻譯時這些直接拿掉，**不要保留也不要轉寫**。
3. 重要的英文專業術語、產品名稱、技術概念，在中文後面用括號保留英文原文
   例如：「模型上下文協議（Model Context Protocol）」
4. 人名保留英文原文
5. 保持原文的語氣（正式/輕鬆/幽默），但要讓文字精煉、像書面文章
6. 內容意義完整保留（事實、數據、論點），只刪語助詞與重複贅字
7. 只回傳翻譯結果，不要加任何解釋或前言"""

CHAPTER_SYSTEM_PROMPT = """你是一位 podcast / 訪談內容分析師。請分析以下逐字稿，做兩件事：
1. 找出對話的所有發言者（主持人、來賓）
2. 將對話按照討論主題分成章節

請回傳 JSON 格式：
{
  "participants": [
    {"name": "Patrick O'Shaughnessy", "role": "主持人"},
    {"name": "Ben Horowitz", "role": "來賓"}
  ],
  "chapters": [
    {
      "title_en": "English chapter title",
      "title_zh": "繁體中文章節標題",
      "start_index": 0,
      "end_index": 5
    }
  ]
}

participants 規則：
- 從上下文找出每個人的全名（保留英文）
- role 用「主持人」「來賓」「共同主持人」等簡單分類
- 不確定就只列名字，role 留空字串
- 只列實際有發言的人，不要把「被提到」的人列進來

chapters 規則：
- start_index 和 end_index 是對話段落的索引（從 0 開始，包含頭尾）
- 每個章節大約包含 5-15 段對話
- 章節標題要簡潔有力

只回傳 JSON，不要加任何其他文字。"""


# ─────────────────────────────────────────────
# 工具函式
# ─────────────────────────────────────────────

def extract_video_id(url: str) -> str:
    """從各種 YouTube URL 格式中提取 video ID。"""
    patterns = [
        r'(?:v=|/v/|youtu\.be/|/shorts/)([a-zA-Z0-9_-]{11})',
        r'(?:embed/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return url  # 假設直接傳入 ID


def format_timestamp(seconds: float) -> str:
    """把秒數轉成 MM:SS 格式。"""
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{minutes:02d}:{secs:02d}"


# YouTube 自動字幕常見的非語音標記，要在翻譯前過濾掉
_NOISE_MARKER_PATTERN = re.compile(
    r"\[\s*("
    r"music|musique|musik|音[樂楽]|"
    r"laughter|laughs?|笑聲|"
    r"applause|clapping|掌聲|"
    r"cheers?|cheering|歡呼|"
    r"inaudible|不清楚|聽不清楚|"
    r"silence|沉默|"
    r"sound effect|聲音效果|"
    r"background\s*music|背景音樂|"
    r"crosstalk|交談聲"
    r")\s*\]",
    flags=re.IGNORECASE,
)


def clean_caption_noise(text: str) -> str:
    """清掉 YouTube 自動字幕的非語音標記與發言者切換符號。"""
    if not text:
        return text
    # 拿掉 [Music] / [Laughter] 等標記
    text = _NOISE_MARKER_PATTERN.sub(" ", text)
    # 拿掉 >> 發言者切換符號（YouTube 自動字幕用來標示換人說話）
    text = re.sub(r">>+\s*", " ", text)
    # 收斂多重空白
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# 中文語助詞 / 贅字。LLM 可能還是會留下一些，post-process 再清。
# 用「孤立詞」匹配：前後是中文標點、句首句尾、空白
_ZH_FILLER_PATTERN = re.compile(
    r"(?:^|(?<=[\s，。！？、；：「」『』（）【】「」]))"
    r"("
    r"嗯+|嗯嗯+|"          # 嗯、嗯嗯、嗯嗯嗯
    r"痾+|啊+|喔+|哦+|噢+|呃+|"  # 各種語助詞單字
    r"那個那個|那個 那個|"   # 重複的「那個」
    r"就是就是|然後然後|"
    r"你知道嗎?|我是說|"
    r"對對對+|是是是+"
    r")"
    r"(?=[\s，。！？、；：「」『』（）【】「」]|$)",
)

# 英文語助詞（在英文原文裡出現的，作為翻譯前清理可選）
_EN_FILLER_PATTERN = re.compile(
    r"\b(?:uh+|um+|er+|hmm+|you know|i mean|kind of|sort of)\b[\s,]*",
    flags=re.IGNORECASE,
)


def clean_zh_fillers(text: str) -> str:
    """清掉繁中翻譯裡殘留的語助詞 / 重複贅字。"""
    if not text:
        return text
    prev = None
    # 多跑幾次處理疊加情況（如「嗯，嗯，」）
    while prev != text:
        prev = text
        text = _ZH_FILLER_PATTERN.sub("", text)
    # 標點疊加修正：「，，」「，。」之類
    text = re.sub(r"，\s*([，。！？、])", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^[，、；。！？\s]+", "", text)  # 開頭多餘標點
    return text.strip()


def merge_short_segments(
    snippets: list[dict],
    min_duration: float = 15.0,
    max_duration: float = 30.0,
) -> list[dict]:
    """
    YouTube 字幕通常是短碎片（每句 2-5 秒）。
    將它們合併成較長的段落，更適合閱讀。

    斷段規則：
      1) 已超過 min_duration 且字串以句末標點結束 → 斷
      2) 已超過 max_duration（自動字幕常無標點，強制保底） → 斷
    """
    if not snippets:
        return []

    merged = []
    current_text = ""
    current_start = snippets[0]["start"]

    for i, snip in enumerate(snippets):
        # 清掉 [Music]/[Laughter]/>> 等噪音再合併
        cleaned = clean_caption_noise(snip["text"])
        if cleaned:
            current_text += " " + cleaned

        elapsed = snip["start"] + snip.get("duration", 0) - current_start
        is_last = (i == len(snippets) - 1)
        ends_sentence = current_text.rstrip().endswith(('.', '?', '!', '。', '？', '!'))

        should_break = is_last \
            or (elapsed >= min_duration and ends_sentence) \
            or (elapsed >= max_duration)

        if should_break:
            text_clean = current_text.strip()
            if text_clean:  # 跳過全空段（如果整段都是 [Music]）
                merged.append({
                    "timestamp": format_timestamp(current_start),
                    "en": text_clean,
                })
            if not is_last:
                current_start = snippets[i + 1]["start"]
                current_text = ""

    last_clean = current_text.strip()
    if last_clean and (not merged or merged[-1]["en"] != last_clean):
        merged.append({
            "timestamp": format_timestamp(current_start),
            "en": last_clean,
        })

    return merged


# ─────────────────────────────────────────────
# Step 1a: YouTube 字幕抓取
# ─────────────────────────────────────────────

def fetch_youtube_transcript(url: str) -> tuple[list[dict], dict]:
    """從 YouTube 抓取英文字幕，回傳 (segments, video_info)。

    區分「真的沒字幕」與「暫時被擋」：
    - NoTranscriptFound / TranscriptsDisabled → 真的沒字幕，直接回 None
    - 其他錯誤（IP block / 網路 / 解析失敗）→ 重試最多 3 次（指數 backoff）
    """
    import time
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (
        NoTranscriptFound,
        TranscriptsDisabled,
        VideoUnavailable,
    )

    video_id = extract_video_id(url)
    print(f"🎬 正在抓取 YouTube 字幕（Video ID: {video_id}）...")

    # 雲端 IP 會被 YouTube 擋；偵測 .env 有 Webshare 憑證就走住宅輪替 proxy
    proxy_user = os.environ.get("WEBSHARE_PROXY_USERNAME")
    proxy_pass = os.environ.get("WEBSHARE_PROXY_PASSWORD")
    if proxy_user and proxy_pass:
        from youtube_transcript_api.proxies import WebshareProxyConfig
        ytt_api = YouTubeTranscriptApi(
            proxy_config=WebshareProxyConfig(
                proxy_username=proxy_user,
                proxy_password=proxy_pass,
            )
        )
        print("   🌐 透過 Webshare 住宅 proxy 抓字幕（自動輪替 IP）")
    else:
        ytt_api = YouTubeTranscriptApi()

    snippets = None
    last_error = None
    for attempt in range(1, 4):  # 最多 3 次
        try:
            transcript_list = ytt_api.list(video_id)
            transcript = None
            try:
                transcript = transcript_list.find_manually_created_transcript(['en', 'en-US', 'en-GB'])
                print("   ✅ 找到手動建立的英文字幕")
            except NoTranscriptFound:
                try:
                    transcript = transcript_list.find_generated_transcript(['en', 'en-US', 'en-GB'])
                    print("   ✅ 找到自動產生的英文字幕")
                except NoTranscriptFound:
                    print("   ❌ 此影片真的沒有英文字幕")
                    return None, {}

            fetched = transcript.fetch()
            snippets = [
                {"text": s.text, "start": s.start, "duration": s.duration}
                for s in fetched.snippets
            ]
            break  # 成功就跳出重試迴圈

        except (TranscriptsDisabled, VideoUnavailable) as e:
            print(f"   ❌ 字幕被關閉或影片不可用: {type(e).__name__}")
            return None, {}

        except Exception as e:
            last_error = e
            wait = 2 ** attempt  # 2s, 4s, 8s
            print(f"   ⚠️ 第 {attempt} 次嘗試失敗（{type(e).__name__}），{wait}s 後重試")
            if attempt < 3:
                time.sleep(wait)

    if snippets is None:
        print(f"   ❌ 重試 3 次都失敗，最後錯誤: {last_error}")
        return None, {}

    # 合併短碎片成可閱讀的段落
    segments = merge_short_segments(snippets)
    print(f"   📝 原始 {len(snippets)} 段字幕 → 合併為 {len(segments)} 段")

    video_info = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }

    return segments, video_info


# ─────────────────────────────────────────────
# Step 1b: AssemblyAI 轉錄（fallback）
# ─────────────────────────────────────────────

def transcribe_audio(audio_source: str) -> list[dict]:
    """用 AssemblyAI 轉錄音檔（fallback 方案）。"""
    import assemblyai as aai

    print("🎙  YouTube 字幕不可用，改用 AssemblyAI 轉錄...")

    api_key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not api_key:
        print("❌ 請設定環境變數 ASSEMBLYAI_API_KEY")
        print("   export ASSEMBLYAI_API_KEY='your-key-here'")
        sys.exit(1)

    aai.settings.api_key = api_key

    config = aai.TranscriptionConfig(
        speaker_labels=True,
        language_code="en",
    )

    transcriber = aai.Transcriber()
    transcript = transcriber.transcribe(audio_source, config=config)

    if transcript.status == aai.TranscriptStatus.error:
        print(f"❌ 轉錄失敗: {transcript.error}")
        sys.exit(1)

    segments = []
    for utterance in transcript.utterances:
        total_seconds = utterance.start // 1000
        timestamp = format_timestamp(total_seconds)

        segments.append({
            "speaker": utterance.speaker,
            "timestamp": timestamp,
            "en": utterance.text,
        })

    speaker_count = len(set(s.get("speaker", "") for s in segments))
    print(f"✅ 轉錄完成，共 {len(segments)} 段，{speaker_count} 位講者")
    return segments


# ─────────────────────────────────────────────
# LLM 抽象（Gemini / Claude）
# ─────────────────────────────────────────────

_gemini_client = None
_anthropic_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            print("❌ 請設定環境變數 GEMINI_API_KEY")
            sys.exit(1)
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("❌ 請設定環境變數 ANTHROPIC_API_KEY")
            sys.exit(1)
        _anthropic_client = anthropic.Anthropic(api_key=api_key, timeout=180.0, max_retries=2)
    return _anthropic_client


def llm_call(system_prompt: str, user_prompt: str, max_tokens: int, *,
             thinking_budget: int = 1024, cache_system: bool = True) -> str:
    """統一 LLM 入口，依 LLM_PROVIDER 切換。"""
    if LLM_PROVIDER == "gemini":
        from google.genai import types
        client = _get_gemini_client()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=max_tokens,
                thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
            ),
        )
        return response.text or ""
    # Claude
    client = _get_anthropic_client()
    system_block = {"type": "text", "text": system_prompt}
    if cache_system:
        system_block["cache_control"] = {"type": "ephemeral"}
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=[system_block],
        messages=[{"role": "user", "content": user_prompt}],
    )
    return response.content[0].text


# ─────────────────────────────────────────────
# Step 2: 翻譯
# ─────────────────────────────────────────────

GLOSSARY_PATH = Path(__file__).parent / "glossary.json"


def _load_glossary() -> tuple[list, list]:
    """讀 glossary.json，回傳 ([(compiled_regex, replacement)...], [canonical_terms])。

    這個檔不存在或解析失敗時回傳空，不影響翻譯流程。
    """
    if not GLOSSARY_PATH.exists():
        return [], []
    try:
        data = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  glossary.json 解析失敗：{e}（略過）")
        return [], []
    rules_dict = data.get("rules", {})
    compiled = [(re.compile(p), r) for p, r in rules_dict.items()]
    canonical = sorted({r for _, r in compiled})
    return compiled, canonical


def _apply_glossary(text: str, compiled_rules) -> str:
    for pat, repl in compiled_rules:
        text = pat.sub(repl, text)
    return text


def translate_segments(segments: list[dict], batch_size: int = 10) -> list[dict]:
    """批次翻譯成繁體中文（依 LLM_PROVIDER 用 Gemini 或 Claude）。"""
    print(f"🌐 正在翻譯成繁體中文（{LLM_PROVIDER}）...")

    glossary_rules, canonical_terms = _load_glossary()
    if glossary_rules:
        # 翻譯前先把字幕辨識錯的專有名詞修對，避免 LLM 再 hallucinate 翻譯
        fixed = 0
        for seg in segments:
            new_en = _apply_glossary(seg.get("en", ""), glossary_rules)
            if new_en != seg.get("en"):
                fixed += 1
            seg["en"] = new_en
        print(f"   📓 套用 glossary：{len(glossary_rules)} 條規則、修正 {fixed} 段 EN")

    # 同時把規範詞注入 system prompt，要求 LLM 原樣保留（防止再被誤譯）
    system_prompt = TRANSLATION_SYSTEM_PROMPT
    if canonical_terms:
        terms_block = "\n".join(f"   - {t}" for t in canonical_terms)
        system_prompt = (
            TRANSLATION_SYSTEM_PROMPT
            + "\n\n8. 以下專有名詞請原樣保留（連空格、大小寫都不要改），不要翻譯也不要重寫拼法：\n"
            + terms_block
        )

    translated = []
    total_batches = (len(segments) + batch_size - 1) // batch_size

    for i in range(0, len(segments), batch_size):
        batch = segments[i:i + batch_size]
        batch_num = i // batch_size + 1
        print(f"   翻譯中... ({batch_num}/{total_batches})", flush=True)

        lines = [f"[{j}] {seg['en']}" for j, seg in enumerate(batch)]
        prompt = (
            "請翻譯以下編號的英文段落成繁體中文。\n"
            "每段翻譯用同樣的編號格式回傳，例如：\n"
            "[0] 翻譯內容\n"
            "[1] 翻譯內容\n\n"
            + "\n".join(lines)
        )

        # max_tokens 給足空間（thinking + 輸出）
        response_text = llm_call(
            system_prompt, prompt,
            max_tokens=8192,
            thinking_budget=GEMINI_THINKING_TRANSLATE,
        )

        translations = {}
        for match in re.finditer(r'\[(\d+)\]\s*(.*?)(?=\n\[|\Z)', response_text, re.DOTALL):
            idx = int(match.group(1))
            translations[idx] = match.group(2).strip()

        for j, seg in enumerate(batch):
            seg_copy = seg.copy()
            zh_text = translations.get(j, f"（翻譯失敗：{seg['en'][:50]}...）")
            seg_copy["zh"] = clean_zh_fillers(zh_text)
            translated.append(seg_copy)

    print(f"✅ 翻譯完成，共 {len(translated)} 段")
    return translated


# ─────────────────────────────────────────────
# Step 3: 自動分章節（Claude API）
# ─────────────────────────────────────────────

def detect_chapters(segments: list[dict]) -> list[dict]:
    """自動偵測主題，將逐字稿分章節（依 LLM_PROVIDER 用 Gemini 或 Claude）。"""
    print(f"📑 正在自動分章節（{LLM_PROVIDER}）...")

    condensed = []
    for i, seg in enumerate(segments):
        preview = seg["en"][:120] + ("..." if len(seg["en"]) > 120 else "")
        speaker = seg.get("speaker", "")
        prefix = f"Speaker {speaker}: " if speaker else ""
        condensed.append(f"[{i}] [{seg['timestamp']}] {prefix}{preview}")

    prompt = (
        f"以下是一段影片/podcast 逐字稿，共 {len(segments)} 段。\n"
        "請找出對談人並依主題分章節。\n\n"
        + "\n".join(condensed)
    )

    response_text = llm_call(
        CHAPTER_SYSTEM_PROMPT, prompt,
        max_tokens=8192,
        thinking_budget=GEMINI_THINKING_CHAPTER,
    )
    response_text = re.sub(r'^```json\s*', '', response_text.strip())
    response_text = re.sub(r'\s*```$', '', response_text.strip())

    try:
        data = json.loads(response_text)
        chapters = data["chapters"]
        participants = data.get("participants", [])
        print(f"✅ 自動分成 {len(chapters)} 個章節，{len(participants)} 位對談人")
        return {"chapters": chapters, "participants": participants}
    except (json.JSONDecodeError, KeyError) as e:
        print(f"⚠️  章節偵測失敗 ({e})，將所有內容放入單一章節")
        return {
            "chapters": [{
                "title_en": "Full Episode",
                "title_zh": "完整內容",
                "start_index": 0,
                "end_index": len(segments) - 1,
            }],
            "participants": [],
        }


# ─────────────────────────────────────────────
# Step 4: 生成 ePub
# ─────────────────────────────────────────────

EPUB_CSS = """
body {
    font-family: "Noto Serif CJK TC", "Source Han Serif TC", "Georgia", serif;
    /* 不指定 font-size 與 padding：跟商業電子書一樣，完全交給 Kobo 的字級
       與「邊界」slider 控制，避免內文離邊太遠。*/
    line-height: 1.2;
    color: #1a1a1a;
}

a {
    text-decoration: none;
    color: inherit;
}

nav ol, nav ul {
    list-style: none;
    padding-left: 0;
    line-height: 1.2;
}

/* 目錄頁字級（Kobo 會用這個渲染 nav.xhtml，Books 因 linear="no" 跳過）*/
nav h2 {
    font-size: 1.1em;
    margin-bottom: 0.6em;
}
nav li {
    font-size: 0.85em;
    line-height: 1.6;
    margin: 0.3em 0;
}
nav a {
    color: #1a1a1a;
}

h1 {
    /* 預設 h1 給簡介頁用、與內文同大。章節標題另外加大。*/
    font-size: 1em;
    font-weight: bold;
    color: #222;
    border-bottom: 2px solid #c0392b;
    padding-bottom: 0.3em;
    margin-top: 1.5em;
    margin-bottom: 0.3em;
}

/* 章節標題：在 Kobo 上強制換頁。字級不要太大，與內文層級感由副標
   （chapter-zh）下沉到內文等高、英文標題用 1.3em 微大即可。*/
h1.chapter-start {
    font-size: 1.3em;
    page-break-before: always;
    break-before: page;
}
h1.chapter-start:first-of-type {
    page-break-before: avoid;
    break-before: avoid;
}

h1 .chapter-zh {
    display: block;
    /* 用 rem 直接對齊 root（=body）字級，不會因 h1 放大而被 cascade 拉大 */
    font-size: 1rem;
    color: #555;
    margin-top: 0.2em;
    font-weight: normal;
}

.speaker {
    font-weight: bold;
    color: #c0392b;
    margin-top: 1.5em;
    margin-bottom: 0.2em;
    border-left: 3px solid #c0392b;
    padding-left: 0.6em;
}

.speaker-b { color: #2c3e50; border-left-color: #2c3e50; }
.speaker-c { color: #27ae60; border-left-color: #27ae60; }
.speaker-d { color: #8e44ad; border-left-color: #8e44ad; }

.timestamp {
    /* 視覺上隱藏；保留 koboSpan 結構讓 Cat Wu 等老書重建時 highlight 不破 */
    display: none;
    font-size: smaller;
    color: #999;
    font-family: monospace;
    margin-left: 0.5em;
    font-weight: normal;
}

.zh {
    color: #1a1a1a;
    line-height: 1.4;
    margin: 0.3em 0 1em 0;
}

.segment {
    /* 不指定 font-size：跟商業電子書一樣交給 Kobo 字級 slider 控制，
       使用者調整字級時，逐字稿才會跟著縮放、不會永遠超大。*/
    margin-bottom: 1em;
}

.cover-title {
    font-size: 1.3em;
    font-weight: bold;
    text-align: center;
    margin-top: 2em;
    line-height: 1.3;
    color: #c0392b;
}

.cover-subtitle {
    font-size: 1em;
    text-align: center;
    color: #555;
    margin-top: 0.8em;
}

.cover-meta {
    text-align: center;
    margin-top: 2em;
    color: #777;
    line-height: 1.4;
}

.episode-info {
    border: 1px solid #ddd;
    padding: 1em;
    margin: 1.5em 0;
    background-color: #fafafa;
}

.episode-info h2 {
    /* 簡介頁的「對談人」「簡介」副標題：與內文同大，靠粗體 + 顏色區隔 */
    font-size: 1em;
    font-weight: bold;
    margin-top: 0;
    margin-bottom: 0.5em;
    color: #c0392b;
}

/* 對談人 / 簡介 區塊內的條目（姓名、角色、簡介內容）字級壓到跟
   "Stripe · 2026-04-30" 副標一致，視覺上比標題低一階 */
.episode-info p {
    font-size: 0.85em;
}

.source-link {
    text-align: center;
    margin-top: 1em;
    font-size: smaller;
    color: #999;
}

/* 簡介頁字級規則：
   - "簡介" h1 與 "對談人" h2 同高（h1 1.05em / h2 1.05em，預設值已設）
   - "Stripe · 2026-04-30" 副標：比內文再小一階
   - 對談人姓名 + 角色標籤：內文同大（不另外縮小）
*/
body.info-page h1 .chapter-zh {
    font-size: 0.85em;
}
"""

SPEAKER_CLASSES = [
    "speaker", "speaker speaker-b",
    "speaker speaker-c", "speaker speaker-d",
]
SPEAKER_ICONS = ["🔴", "🔵", "🟢", "🟣"]


def generate_text_cover(title: str, subtitle: str = "") -> bytes:
    """畫乾淨的純文字書封：白底、深灰標題置中、副標題。

    ⚠️ 此函式 Pharos / Anamnesis 兩邊必須 byte-for-byte 一致。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    # 4:3 直立比例，貼合 Kobo 螢幕原生比例，避免休眠時 letterbox 邊框
    W, H = 1500, 2000
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    font_paths = [
        # macOS（PingFang 預設是 Regular 粗細）
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        # Linux：Regular 優先，避免吃到 Bold 讓封面字過粗
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Medium.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        # 最後才 fallback 到 Bold
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    ]
    title_font = None
    sub_font = None
    for fp in font_paths:
        try:
            title_font = ImageFont.truetype(fp, 66)
            sub_font = ImageFont.truetype(fp, 38)
            break
        except Exception:
            continue
    if not title_font:
        title_font = ImageFont.load_default()
        sub_font = ImageFont.load_default()

    # 標題自動換行：英文/混合語言依字元寬度斷詞；純中文段落每行 20 字
    def wrap(text, font, max_w):
        words = text.split()
        lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip()
            if draw.textlength(test, font=font) <= max_w:
                cur = test
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines

    if any('一' <= c <= '鿿' for c in title):
        per_line = 20
        title_lines = [title[i:i + per_line] for i in range(0, len(title), per_line)]
    else:
        title_lines = wrap(title, title_font, W - 140)

    line_h = 90
    total_h = len(title_lines) * line_h

    # 整體（標題 + 副標題）置中，無分隔線
    sub_gap = 60
    sub_h = 40 if subtitle else 0
    block_h = total_h + (sub_gap + sub_h if subtitle else 0)
    start_y = (H - block_h) // 2

    for i, line in enumerate(title_lines):
        tw = draw.textlength(line, font=title_font)
        draw.text(((W - tw) / 2, start_y + i * line_h), line, fill="#1a1a1a", font=title_font)

    if subtitle:
        sub_y = start_y + total_h + sub_gap
        sw = draw.textlength(subtitle, font=sub_font)
        draw.text(((W - sw) / 2, sub_y), subtitle, fill="#444444", font=sub_font)

    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


_SENT_END_CHARS = "。！？.!?"


def _zh_complete(zh: str) -> bool:
    """zh 結尾是否為句末標點。允許結尾有引號/括號/空白。"""
    z = zh.rstrip("\"'」』）)] 　\n\t")
    return bool(z) and z[-1] in _SENT_END_CHARS


def _merge_unfinished_zh(segments, chapters):
    """合併中文沒結尾標點的相鄰 segments。

    YouTube 自動字幕無標點，merge_short_segments 在 30s 處硬切，
    句子被段落邊界切開，視覺上像被插了 newline。這裡在渲染前把
    連續未完句的段落黏回去，並 rebase chapters 索引。

    規則：
      - 上段 zh 不完整 且 speaker 一致 → 合併（en 用空白接、zh 直接接）
      - chapter 邊界（chapter.start_index）強制不合併，避免跨章融合
    """
    if not segments:
        return segments, chapters

    chapter_starts = {ch["start_index"] for ch in chapters} if chapters else set()

    new_segments = []
    old_to_new = {}

    for old_idx, seg in enumerate(segments):
        speaker = seg.get("speaker", "")
        prev = new_segments[-1] if new_segments else None
        can_merge = (
            prev is not None
            and old_idx not in chapter_starts
            and not _zh_complete(prev.get("zh", ""))
            and prev.get("speaker", "") == speaker
        )

        if can_merge:
            prev["en"] = (prev.get("en", "") + " " + seg.get("en", "")).strip()
            prev["zh"] = prev.get("zh", "") + seg.get("zh", "")
            old_to_new[old_idx] = len(new_segments) - 1
        else:
            new_segments.append(dict(seg))
            old_to_new[old_idx] = len(new_segments) - 1

    new_chapters = []
    for ch in chapters or []:
        new_ch = dict(ch)
        new_ch["start_index"] = old_to_new.get(ch["start_index"], ch["start_index"])
        new_ch["end_index"] = old_to_new.get(ch["end_index"], ch["end_index"])
        new_chapters.append(new_ch)

    diff = len(segments) - len(new_segments)
    if diff > 0:
        print(f"   🧷 合併未完句段落：{len(segments)} → {len(new_segments)}（合 {diff} 段）")

    return new_segments, new_chapters


def build_epub(segments, chapters, meta, output_path, render_timestamps: bool = False):
    """組裝雙語 ePub。"""
    print("📖 正在生成 ePub...")
    # 先把被 30s 硬切切壞的句子黏回去，避免章節內出現怪斷行
    segments, chapters = _merge_unfinished_zh(segments, chapters)

    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(meta["title"])
    book.set_language("zh-TW")
    book.add_author(meta.get("podcast_name") or "YouTube")
    book.add_metadata("DC", "date", meta.get("date", datetime.now().strftime("%Y-%m-%d")))

    # 用 Pillow 畫純文字封面圖（給 Kobo 書庫縮圖用），create_page=False 避免 JPEG 變成內頁
    cover_img = generate_text_cover(meta["title"], meta.get("podcast_name", ""))
    if cover_img:
        book.set_cover("cover.jpg", cover_img, create_page=False)
        print(f"   ✅ 書封圖已生成（{len(cover_img) // 1024} KB）")

    css = epub.EpubItem(
        uid="style", file_name="style/main.css",
        media_type="text/css", content=EPUB_CSS.encode("utf-8"),
    )
    book.add_item(css)

    # 講者對照
    unique_speakers = sorted(set(s.get("speaker", "") for s in segments if s.get("speaker")))
    speaker_names = meta.get("speaker_names", {})
    speaker_display = {spk: speaker_names.get(spk, f"Speaker {spk}") for spk in unique_speakers}
    has_speakers = len(unique_speakers) > 0

    # 封面頁
    speakers_html = ""
    if has_speakers:
        speakers_html = "\n".join(
            f"<p>{SPEAKER_ICONS[i % len(SPEAKER_ICONS)]} {speaker_display[spk]}</p>"
            for i, spk in enumerate(unique_speakers)
        )

    desc_block = ""
    if meta.get("description"):
        desc_block = f'<div class="episode-info"><h2>簡介</h2><p>{meta["description"]}</p></div>'

    # 對談人清單（從 chapter 偵測時順便抓到的）
    participants_block = ""
    participants = meta.get("participants", [])
    if participants:
        items = []
        for i, p in enumerate(participants):
            icon = SPEAKER_ICONS[i % len(SPEAKER_ICONS)]
            name = p.get("name", "")
            role = p.get("role", "")
            label = f"{icon} <strong>{name}</strong>"
            if role:
                # role 字級跟內文等高（user 偏好），靠顏色區隔即可
                label += f" <span style=\"color:#777\">— {role}</span>"
            items.append(f"<p>{label}</p>")
        participants_block = (
            f'<div class="episode-info"><h2>對談人</h2>{"".join(items)}</div>'
        )

    source_block = ""
    if meta.get("url"):
        source_block = f'<div class="source-link">原始影片: {meta["url"]}</div>'

    # 第 1 頁：直接顯示 Pillow 生成的封面圖（讓休眠 / 書庫 / 第一頁看到同一張）
    cover_html = """<?xml version='1.0' encoding='utf-8'?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-TW">
<head><title>封面</title></head>
<body style="margin:0;padding:0;text-align:center;">
  <img src="cover.jpg" alt="cover" style="max-width:100%;max-height:100%;display:block;margin:0 auto;"/>
</body>
</html>"""

    cover_page = epub.EpubHtml(title="封面", file_name="cover.xhtml", lang="zh-TW")
    cover_page.content = cover_html.encode("utf-8")
    book.add_item(cover_page)

    # 第 2 頁：簡介（對談人 / 描述 / 原始連結）
    info_inner = participants_block + desc_block + source_block
    info_html = f"""<?xml version='1.0' encoding='utf-8'?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-TW">
<head><title>簡介</title>
<link rel="stylesheet" type="text/css" href="style/main.css"/></head>
<body class="info-page">
    <h1>簡介<span class="chapter-zh">{meta.get('podcast_name', '')} · {meta.get('date', '')}</span></h1>
    {info_inner}
</body>
</html>""" if info_inner else ""

    front_pages = [cover_page]
    if info_html:
        info_page = epub.EpubHtml(title="簡介", file_name="info.xhtml", lang="zh-TW")
        info_page.content = info_html.encode("utf-8")
        info_page.add_item(css)
        book.add_item(info_page)
        front_pages.append(info_page)

    # 每章寫成獨立的 xhtml 檔。Kobo 在 spine 上的檔案邊界會自動斷頁，
    # 比 CSS page-break-before 可靠（先前單檔 + inline page-break style 的版本
    # 在某些 Kobo 機型 / 字級組合下會失效，造成章節接在一起）。
    # tradeoff：跨章節的拖曳畫重點手勢會在章邊界中斷。
    chapter_pages = []
    toc = []

    for ch_idx, ch in enumerate(chapters, 1):
        start = ch["start_index"]
        end = ch["end_index"]
        ch_segments = segments[start:end + 1]

        segments_html = ""
        for seg in ch_segments:
            speaker = seg.get("speaker", "")

            ts_html = f' <span class="timestamp">[{seg["timestamp"]}]</span>' if render_timestamps else ""

            if speaker:
                if speaker in unique_speakers:
                    spk_idx = unique_speakers.index(speaker)
                else:
                    spk_idx = 0
                spk_class = SPEAKER_CLASSES[spk_idx % len(SPEAKER_CLASSES)]
                spk_name = speaker_display.get(speaker, speaker)
                speaker_div = f'<div class="{spk_class}">🎙 {spk_name}{ts_html}</div>'
            elif render_timestamps:
                speaker_div = f'<div class="speaker">{ts_html}</div>'
            else:
                speaker_div = ""

            inner = (speaker_div + "\n        " if speaker_div else "") + f'<div class="zh">{seg.get("zh", "")}</div>'
            segments_html += f"""
    <div class="segment">
        {inner}
    </div>"""

        ch_filename = f"chapter_{ch_idx}.xhtml"
        ch_html = f"""<?xml version='1.0' encoding='utf-8'?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-TW">
<head><title>Ch.{ch_idx} {ch['title_zh']}</title>
<link rel="stylesheet" type="text/css" href="style/main.css"/></head>
<body>
    <h1 class="chapter-start">Ch.{ch_idx} — {ch['title_en']}
        <span class="chapter-zh">{ch['title_zh']}</span>
    </h1>
    {segments_html}
</body>
</html>"""

        ch_page = epub.EpubHtml(
            title=f"Ch.{ch_idx} {ch['title_zh']}",
            file_name=ch_filename,
            lang="zh-TW",
        )
        ch_page.content = ch_html.encode("utf-8")
        ch_page.add_item(css)
        book.add_item(ch_page)
        chapter_pages.append(ch_page)

        toc.append(
            epub.Link(ch_filename, f"Ch.{ch_idx} {ch['title_zh']}", f"ch_{ch_idx:02d}")
        )

    book.toc = toc
    book.add_item(epub.EpubNcx())
    nav_item = epub.EpubNav()
    nav_item.add_item(css)
    book.add_item(nav_item)
    # 順序：封面頁 → (簡介頁) → nav(linear="no") → 各章節
    book.spine = front_pages + [(nav_item, "no")] + chapter_pages

    epub.write_epub(output_path, book, {})

    # 後處理：把 nav.xhtml 的 <ol> 改成 <ul>，避免 Kobo 內建 TOC 顯示 1.2.3.
    _strip_nav_ordered_list(output_path)

    print(f"✅ ePub 已生成: {output_path}")


def _strip_nav_ordered_list(epub_path: str):
    """把 EPUB 內 nav.xhtml 的 <ol> 改成 <ul>，移除目錄編號。"""
    import zipfile
    import shutil
    tmp_path = epub_path + ".tmp"
    with zipfile.ZipFile(epub_path, "r") as zin:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename.endswith("nav.xhtml"):
                    text = data.decode("utf-8")
                    text = text.replace("<ol>", "<ul>").replace("</ol>", "</ul>")
                    data = text.encode("utf-8")
                zout.writestr(item, data)
    shutil.move(tmp_path, epub_path)


# ─────────────────────────────────────────────
# Step 4.5: 轉成 Kobo 私有的 .kepub.epub 格式
# ─────────────────────────────────────────────

def convert_to_kepub(epub_path: str) -> str:
    """用 kepubify 把標準 EPUB 轉成 Kobo 的 .kepub.epub。

    kepub 在每個句子外包 <span class="koboSpan">，這是 Kobo 跨頁畫重點、
    閱讀統計、書籤精準度等功能仰賴的標記。沒有 kepubify 就回傳原 epub。
    """
    # 找 kepubify。launchd / systemd 啟動的 process PATH 通常不含 /opt/homebrew/bin
    # 與 /usr/local/bin，這裡多搜幾個常見位置避免靜默失敗
    kepubify = shutil.which("kepubify")
    if not kepubify:
        for candidate in ("/opt/homebrew/bin/kepubify", "/usr/local/bin/kepubify", "/usr/bin/kepubify"):
            if Path(candidate).exists():
                kepubify = candidate
                break
    if not kepubify:
        raise RuntimeError(
            "❌ 找不到 kepubify，無法產生 Kobo 原生格式（建議：brew install kepubify / apt install kepubify）"
        )

    src = Path(epub_path)
    out_dir = src.parent
    # kepubify 預設輸出 <stem>_converted.kepub.epub；我們用 -o <dir> 由它自己命名
    proc = subprocess.run(
        [kepubify, "-o", str(out_dir), str(src)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"❌ kepubify 失敗（rc={proc.returncode}）: {proc.stderr.strip()[-200:]}")

    # 找出 kepubify 產出的檔案，重新命名為更乾淨的 <原檔名>.kepub.epub
    converted = out_dir / f"{src.stem}_converted.kepub.epub"
    if not converted.exists():
        raise RuntimeError(f"❌ kepubify 沒輸出預期檔案：{converted.name}")

    final = out_dir / f"{src.stem}.kepub.epub"
    converted.replace(final)
    # 中間的純 .epub 不需要保留，刪除避免雙檔
    src.unlink(missing_ok=True)
    print(f"📘 已轉成 kepub: {final.name}")
    return str(final)


# ─────────────────────────────────────────────
# Step 5: Dropbox → Kobo
# ─────────────────────────────────────────────

def copy_to_kobo(epub_path: str) -> bool:
    """同步 epub 到 Dropbox。

    自動偵測：
    - 本地有 Dropbox 桌面版資料夾（Mac）→ 用 shutil.copy2
    - 沒有資料夾但 .env 設了 Dropbox API 憑證（Server）→ 用 API 上傳
    """
    # 路徑 A: 本地 Dropbox 資料夾（Mac 桌面版）
    if DROPBOX_KOBO_PATH.exists():
        dest = DROPBOX_KOBO_PATH / Path(epub_path).name
        shutil.copy2(epub_path, dest)
        print(f"📲 已複製到本地 Dropbox（Kobo）: {dest}")
        return True

    # 路徑 B: Dropbox API（Server 上沒有桌面版）
    try:
        from dropbox_uploader import upload_to_kobo, is_configured
    except ImportError:
        print("⚠️  找不到本地 Dropbox 資料夾，且 dropbox_uploader 模組無法載入")
        return False

    if is_configured():
        try:
            upload_to_kobo(epub_path)
            return True
        except Exception as e:
            print(f"❌ Dropbox API 上傳失敗: {e}")
            return False

    print(f"⚠️  找不到 {DROPBOX_KOBO_PATH}，且 .env 沒有 Dropbox API 憑證")
    print("   Mac：請確認 Dropbox 桌面版已安裝")
    print("   Server：請設定 DROPBOX_APP_KEY/SECRET/REFRESH_TOKEN")
    return False


# ─────────────────────────────────────────────
# 儲存中繼資料
# ─────────────────────────────────────────────

def save_data(segments, chapters, meta, output_dir):
    """儲存 JSON 中繼資料，方便重新生成。"""
    data = {
        "meta": meta, "chapters": chapters,
        "segments": segments,
        "generated_at": datetime.now().isoformat(),
    }
    path = output_dir / f"{meta['safe_filename']}_data.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 資料已儲存: {path}")


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────

def is_youtube_url(s: str) -> bool:
    return any(x in s for x in ["youtube.com", "youtu.be"])


def main():
    parser = argparse.ArgumentParser(
        description="yt2epub — YouTube 影片 / Podcast 轉雙語 ePub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例:
  python yt2epub.py "https://www.youtube.com/watch?v=xxxxx"
  python yt2epub.py "https://youtu.be/xxxxx" --title "DOAC - Mo Gawdat"
  python yt2epub.py "https://youtu.be/xxxxx" --speakers "Steven Bartlett（主持人）,Mo Gawdat（來賓）"
  python yt2epub.py episode.mp3 --title "My Podcast EP1"
  python yt2epub.py --from-json ~/yt2epub_output/xxx_data.json
        """,
    )
    parser.add_argument("source", nargs="?", help="YouTube URL 或音檔路徑")
    parser.add_argument("--title", "-t", help="ePub 標題")
    parser.add_argument("--podcast-name", "-p", help="節目名稱", default="")
    parser.add_argument("--speakers", "-s",
                        help="講者名稱（逗號分隔，依 Speaker A,B,C 順序）")
    parser.add_argument("--description", "-d", help="簡介", default="")
    parser.add_argument("--no-kobo", action="store_true", help="不複製到 Kobo")
    parser.add_argument("--from-json", help="從 JSON 重新生成（跳過轉錄和翻譯）")
    parser.add_argument("--keep-timestamps", action="store_true",
                        help="保留 timestamp HTML 結構（CSS 仍隱藏）。"
                             "重建已畫重點的舊書時用，可確保 koboSpan ID 不變。")
    parser.add_argument("--batch-size", type=int, default=10, help="翻譯批次大小")

    args = parser.parse_args()

    if not args.source and not args.from_json:
        parser.error("請提供 YouTube URL / 音檔路徑，或用 --from-json 重新生成")

    OUTPUT_DIR.mkdir(exist_ok=True)

    if args.from_json:
        print(f"📂 從 JSON 載入: {args.from_json}")
        with open(args.from_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        segments = data["segments"]
        chapters = data["chapters"]
        meta = data["meta"]
        if args.title:
            meta["title"] = args.title
        if args.podcast_name:
            meta["podcast_name"] = args.podcast_name
        # 用最新規則重算檔名
        cleaned = re.sub(r'[/\\:*?"<>|]', '', meta.get("title", ""))
        meta["safe_filename"] = re.sub(r'\s+', ' ', cleaned).strip() or meta.get("safe_filename", "video")
        # 寫回 JSON 保存補齊的欄位
        if args.podcast_name or args.title:
            data["meta"] = meta
            with open(args.from_json, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"💾 已更新 JSON：{args.from_json}")
    else:
        # 建立 meta
        source_name = extract_video_id(args.source) if is_youtube_url(args.source) else Path(args.source).stem
        # 只把檔案系統不允許的字元拿掉，保留空格與中文，連續空格合併成一個
        cleaned = re.sub(r'[/\\:*?"<>|]', '', args.title or source_name)
        safe_filename = re.sub(r'\s+', ' ', cleaned).strip()

        meta = {
            "title": args.title or source_name,
            "podcast_name": args.podcast_name,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "description": args.description,
            "safe_filename": safe_filename,
            "speaker_names": {},
            "url": args.source if is_youtube_url(args.source) else "",
        }

        if args.speakers:
            for i, name in enumerate(args.speakers.split(",")):
                letters = ["A", "B", "C", "D", "E", "F"]
                if i < len(letters):
                    meta["speaker_names"][letters[i]] = name.strip()

        print("=" * 50)
        print(f"🎧 yt2epub — {meta['title']}")
        print("=" * 50)

        # Step 1: 取得逐字稿
        if is_youtube_url(args.source):
            segments, video_info = fetch_youtube_transcript(args.source)
            meta.update(video_info)

            if segments is None:
                print("\n⚠️  YouTube 字幕不可用")
                print("   你可以下載音檔後用以下指令重試：")
                print(f"   python yt2epub.py downloaded_audio.mp3 --title \"{meta['title']}\"")
                sys.exit(1)
        else:
            segments = transcribe_audio(args.source)

        # Step 2: 翻譯
        segments = translate_segments(segments, batch_size=args.batch_size)

        # Step 3: 分章節 + 找對談人
        result = detect_chapters(segments)
        chapters = result["chapters"]
        meta["participants"] = result.get("participants", [])

        # 儲存
        save_data(segments, chapters, meta, OUTPUT_DIR)

    # Step 4: 生成 ePub
    epub_path = str(OUTPUT_DIR / f"{meta.get('safe_filename', 'video')}.epub")
    build_epub(segments, chapters, meta, epub_path, render_timestamps=args.keep_timestamps)

    # Step 4.5: 轉 Kobo 私有的 kepub 格式（跨頁畫重點等功能仰賴此格式）
    # 失敗就直接結束 — 不要悄悄上傳純 epub，那會在 Kobo 上出現巨大字體
    try:
        kobo_path = convert_to_kepub(epub_path)
    except RuntimeError as e:
        print(str(e))
        print("⛔ 中止：未產生 .kepub.epub，不會上傳到 Kobo（避免字體變形）")
        sys.exit(2)

    # Step 5: Kobo
    if not args.no_kobo:
        if copy_to_kobo(kobo_path):
            # 同步成功後清掉本地 epub / kepub，只留 _data.json 當翻譯快取（可用 --from-json 重建）
            for p in (epub_path, kobo_path):
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as e:
                    print(f"⚠️  清理本地檔案失敗 {p}: {e}")

    print()
    print("=" * 50)
    print("🎉 完成！")
    print(f"   ePub: {kobo_path}")
    if not args.no_kobo:
        print("   Kobo: Dropbox 同步後即可下載閱讀")
    print("=" * 50)


if __name__ == "__main__":
    main()
