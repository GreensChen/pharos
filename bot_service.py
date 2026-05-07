#!/usr/bin/env python3
"""
bot_service.py — Telegram bot 常駐服務

監聽 daily_brief.py 推送的訊息上的按鈕點擊：
- 📝 narrative   → 傳對談摘要
- 📋 transcript  → 傳重點逐字稿（時間戳 + 原文 + 翻譯）
- 🎧 convert     → 跑 yt2epub.py 轉成 epub 同步到 Kobo

啟動方式：
    python3 bot_service.py
（一般由 launchd 自動啟動）
"""

import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=True)
except ImportError:
    pass


# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
SUMMARIES_DIR = BASE_DIR / "summaries"
LOG_FILE = BASE_DIR / "bot_service.log"

# 同時最多跑幾個 epub 轉檔（避免電腦過熱）
MAX_CONCURRENT_CONVERSIONS = 1
conversion_lock = asyncio.Semaphore(MAX_CONCURRENT_CONVERSIONS)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger("bot_service")


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_summary(video_id: str) -> Optional[dict]:
    f = SUMMARIES_DIR / f"{video_id}.json"
    if not f.exists():
        return None
    return json.loads(f.read_text())


# ─────────────────────────────────────────────
# 訊息切段（Telegram 4096 字上限）
# ─────────────────────────────────────────────

def split_messages(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


# ─────────────────────────────────────────────
# 按鈕 handler
# ─────────────────────────────────────────────

# 待確認訂閱：token -> URL（重啟後失效，沒差，使用者重貼即可）
_pending_subs: dict[str, str] = {}

# 偵測「頻道 / playlist」網址（不含單部影片，那個走 YOUTUBE_URL_RE）
CHANNEL_URL_RE = re.compile(
    r"(https?://(?:www\.|m\.)?youtube\.com/(?:@[\w.\-_]+|channel/UC[\w-]+|playlist\?list=[\w-]+))"
)


def build_channels_keyboard(channels: list[dict]) -> InlineKeyboardMarkup:
    """每個訂閱一行 [名稱] [✕]。最後一行 [➕ 新增] [關閉]。"""
    rows = []
    for ch in channels:
        kind_emoji = "📺" if ch["type"] == "channel" else "🎵"
        name = ch["name"][:40]
        rows.append([
            InlineKeyboardButton(f"{kind_emoji} {name}", callback_data="noop"),
            InlineKeyboardButton("✕", callback_data=f"unsub:{ch['type']}:{ch['id']}"),
        ])
    rows.append([
        InlineKeyboardButton("➕ 新增訂閱", callback_data="menu:addhelp"),
        InlineKeyboardButton("關閉", callback_data="menu:close"),
    ])
    return InlineKeyboardMarkup(rows)


def build_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 訂閱清單", callback_data="menu:channels")],
        [InlineKeyboardButton("➕ 新增訂閱", callback_data="menu:addhelp")],
        [InlineKeyboardButton("📋 最近影片", callback_data="menu:list")],
        [InlineKeyboardButton("🚀 立即跑 daily_brief", callback_data="menu:run")],
        [InlineKeyboardButton("❓ 說明", callback_data="menu:help")],
    ])


HELP_TEXT = (
    "<b>yt2epub bot 用法</b>\n\n"
    "• 直接貼 <b>YouTube 影片連結</b> → 立即摘要 + 🎧 轉 epub 按鈕\n"
    "• 直接貼 <b>頻道 / Playlist 連結</b> → 跳訂閱確認按鈕\n"
    "• <code>/menu</code> 開主選單（按鈕操作）\n"
    "• <code>/channels</code> 看訂閱清單（每筆有 ✕ 退訂）\n"
    "• <code>/run</code> 立刻跑 daily_brief\n\n"
    "舊指令仍可用：<code>/sub</code> <code>/unsub</code> <code>/list</code>"
)


async def cb_convert(query, video_id: str):
    """🎧 跑 yt2epub.py。"""
    data = load_summary(video_id)
    if not data:
        await query.answer("❌ 找不到摘要檔", show_alert=True)
        return

    url = data["video"]["url"]
    title = data["video"]["title"]
    channel = data.get("channel", "")
    safe_title = html_escape(title)

    # 標記為轉檔中
    await query.answer("⏳ 排隊中...")
    await query.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⏳ 轉檔中...", callback_data="noop"),
        ]])
    )

    async with conversion_lock:
        logger.info(f"開始轉檔: {video_id}  {title}")
        await query.message.reply_html(f"🎬 開始轉檔: <b>{safe_title}</b>")

        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_BASE_URL"}
        env["PYTHONUNBUFFERED"] = "1"

        # 直接寫到 log 檔避免 PIPE 在連續 API call 卡死的怪事
        log_path = BASE_DIR / f"yt2epub_run_{video_id}.log"
        log_fp = open(log_path, "wb")
        logger.info(f"subprocess log: {log_path}")
        cmd = [
            sys.executable, "-u",
            str(BASE_DIR / "yt2epub.py"),
            url,
            "--title", title,
        ]
        if channel:
            cmd += ["--podcast-name", channel]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(BASE_DIR),
            stdout=log_fp,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        await proc.wait()
        log_fp.close()
        output = log_path.read_text(errors="replace")
        logger.info(f"yt2epub exit={proc.returncode}\n{output[-2000:]}")

    # 長時間任務後 callback query 已過期，改用 chat_id+message_id 直接編輯訊息
    chat_id = query.message.chat_id
    message_id = query.message.message_id
    bot = query.get_bot()

    async def safe_edit_markup(markup):
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=markup,
            )
        except Exception as e:
            logger.warning(f"無法更新原訊息按鈕（可能訊息太舊）: {e}")

    if proc.returncode == 0:
        await safe_edit_markup(InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 已轉檔到 Kobo", callback_data="noop"),
        ]]))
        await query.message.reply_html(
            f"✅ <b>{safe_title}</b>\n轉檔完成，已同步到 Dropbox/Kobo。"
        )
    else:
        # 失敗 → 還原按鈕讓使用者重試
        await safe_edit_markup(InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 重試轉 epub", callback_data=f"convert:{video_id}")],
        ]))
        # 錯誤訊息只回傳最後 500 字
        err_tail = output[-500:].strip()
        await query.message.reply_html(
            f"❌ <b>轉檔失敗</b>: {safe_title}\n<pre>{html_escape(err_tail)}</pre>"
        )


# ─────────────────────────────────────────────
# 主分派
# ─────────────────────────────────────────────

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    if data == "noop":
        await query.answer()
        return

    if ":" not in data:
        await query.answer()
        return

    action, payload = data.split(":", 1)
    logger.info(f"按鈕點擊: action={action}  payload={payload}")

    try:
        if action == "convert":
            await cb_convert(query, payload)
        elif action == "sub":
            await cb_sub(query, payload)
        elif action == "subno":
            await cb_subno(query, payload)
        elif action == "unsub":
            await cb_unsub(query, payload)
        elif action == "menu":
            await cb_menu(query, payload)
        else:
            await query.answer(f"未知動作: {action}")
    except Exception as e:
        logger.exception(f"按鈕 handler 失敗: {e}")
        try:
            await query.message.reply_text(f"❌ 處理失敗: {e}")
        except Exception:
            pass


# ─────────────────────────────────────────────
# 訂閱 / 退訂 / 主選單按鈕
# ─────────────────────────────────────────────

async def cb_sub(query, token: str):
    url = _pending_subs.pop(token, None)
    if not url:
        await query.answer("⚠️ 已過期，請重貼連結", show_alert=True)
        return
    await query.answer("⏳ 解析中...")
    await query.edit_message_text(f"⏳ 解析中... <code>{html_escape(url)}</code>", parse_mode=ParseMode.HTML)

    from subscribe import (
        resolve_url, fetch_video_ids,
        load_channels, save_channels,
        load_seen, save_seen,
    )

    try:
        kind, cid, name = await asyncio.to_thread(resolve_url, url)
    except Exception as e:
        await query.edit_message_text(f"❌ 解析失敗：{html_escape(str(e))}", parse_mode=ParseMode.HTML)
        return

    channels = load_channels()
    if any(c["type"] == kind and c["id"] == cid for c in channels):
        await query.edit_message_text(
            f"⚠️ 已訂閱：<b>{html_escape(name)}</b>", parse_mode=ParseMode.HTML
        )
        return

    new_ch = {"type": kind, "id": cid, "name": name}
    channels.append(new_ch)
    save_channels(channels)

    seen_count = -1
    try:
        ids = await asyncio.to_thread(fetch_video_ids, new_ch)
        seen = load_seen()
        seen[f"{kind}:{cid}"] = ids
        save_seen(seen)
        seen_count = len(ids)
    except Exception as e:
        logger.warning(f"標記既有影片失敗: {e}")

    msg = (
        f"✅ 已訂閱：<b>{html_escape(name)}</b>\n"
        f"類型：{'頻道' if kind == 'channel' else 'Playlist'}"
    )
    if seen_count >= 0:
        msg += f"\n標記 {seen_count} 部既有影片為已看，daily_brief 只推新片。"
    await query.edit_message_text(msg, parse_mode=ParseMode.HTML)


async def cb_subno(query, token: str):
    _pending_subs.pop(token, None)
    await query.answer("已取消")
    await query.edit_message_text("❌ 已取消訂閱")


async def cb_unsub(query, payload: str):
    """payload = '<type>:<id>'。退訂後即時刷新清單。"""
    if ":" not in payload:
        await query.answer("資料異常")
        return
    kind, cid = payload.split(":", 1)

    from subscribe import load_channels, save_channels, load_seen, save_seen
    channels = load_channels()
    removed = None
    for i, ch in enumerate(channels):
        if ch["type"] == kind and ch["id"] == cid:
            removed = channels.pop(i)
            break

    if not removed:
        await query.answer("找不到（可能已退訂）", show_alert=True)
        return

    save_channels(channels)
    seen = load_seen()
    seen.pop(f"{kind}:{cid}", None)
    save_seen(seen)
    await query.answer(f"已退訂 {removed['name']}")

    # 刷新訊息
    if channels:
        await query.edit_message_text(
            f"📡 共 <b>{len(channels)}</b> 個訂閱（點 ✕ 退訂）：",
            parse_mode=ParseMode.HTML,
            reply_markup=build_channels_keyboard(channels),
        )
    else:
        await query.edit_message_text(
            "（已全部退訂）\n直接貼 YouTube 頻道 / Playlist 連結就能再訂閱。",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ 怎麼訂閱？", callback_data="menu:addhelp"),
            ]]),
        )


async def cb_menu(query, action: str):
    """主選單分派。"""
    if action == "close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            await query.edit_message_text("（已關閉）")
        return

    if action == "channels":
        from subscribe import load_channels
        channels = load_channels()
        await query.answer()
        if not channels:
            await query.edit_message_text(
                "（沒有訂閱）\n直接貼 YouTube 頻道 / Playlist 連結就能訂閱。",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ 怎麼訂閱？", callback_data="menu:addhelp"),
                ]]),
            )
            return
        await query.edit_message_text(
            f"📡 共 <b>{len(channels)}</b> 個訂閱（點 ✕ 退訂）：",
            parse_mode=ParseMode.HTML,
            reply_markup=build_channels_keyboard(channels),
        )
        return

    if action == "addhelp":
        await query.answer()
        await query.edit_message_text(
            "<b>新增訂閱</b>：直接把這類網址貼進聊天室即可，會跳確認按鈕：\n\n"
            "• <code>https://youtube.com/@AcquiredFM</code>\n"
            "• <code>https://youtube.com/channel/UCxxx</code>\n"
            "• <code>https://youtube.com/playlist?list=PLxxx</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "list":
        files = sorted(SUMMARIES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        await query.answer()
        if not files:
            await query.edit_message_text("沒有摘要紀錄")
            return
        lines = ["最近 10 部："]
        for f in files[:10]:
            try:
                d = json.loads(f.read_text())
                ts = datetime.fromisoformat(d["saved_at"]).strftime("%m/%d")
                lines.append(f"• {ts} [{d['channel']}] {d['video']['title'][:50]}")
            except Exception:
                continue
        await query.edit_message_text("\n".join(lines))
        return

    if action == "run":
        await query.answer("已觸發")
        await query.edit_message_text("🚀 已觸發 daily_brief，請稍候新訊息推送...")
        asyncio.create_task(_run_daily_brief_bg(query.message.chat_id, query.get_bot()))
        return

    if action == "help":
        await query.answer()
        await query.edit_message_text(HELP_TEXT, parse_mode=ParseMode.HTML)
        return

    await query.answer(f"未知選單: {action}")


def _extract_brief_summary(output: str) -> str:
    """從 daily_brief stdout 抓最後一行有意義的結語。"""
    interesting = ("✅ 完成", "📭 沒有新影片", "找到", "❌")
    for line in reversed(output.splitlines()):
        s = line.strip()
        if not s:
            continue
        # 去掉前面的時間戳 [YYYY-MM-DD HH:MM:SS]
        if s.startswith("[") and "] " in s:
            s = s.split("] ", 1)[1]
        if any(k in s for k in interesting):
            return s
    return "（無摘要）"


async def _run_daily_brief_bg(chat_id: int, bot):
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(BASE_DIR / "daily_brief.py"),
        cwd=str(BASE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        await bot.send_message(chat_id, f"❌ daily_brief 失敗:\n{output[-1000:]}")
    else:
        await bot.send_message(chat_id, f"✅ daily_brief 完成：{_extract_brief_summary(output)}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "👋 yt2epub bot 上線\n\n"
        "<b>最簡單的用法</b>：直接把網址貼進來\n"
        "• 影片連結 → 立即摘要 + 🎧 轉 epub\n"
        "• 頻道 / Playlist 連結 → 跳訂閱確認按鈕\n\n"
        "<b>不想記指令？</b> 用 <code>/menu</code> 或左下角藍色選單按鈕，\n"
        "全部都可以用按鈕點。",
        reply_markup=build_main_menu(),
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = sorted(SUMMARIES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        await update.message.reply_text("沒有摘要紀錄")
        return
    lines = ["最近 10 部："]
    for f in files[:10]:
        try:
            data = json.loads(f.read_text())
            ts = datetime.fromisoformat(data["saved_at"]).strftime("%m/%d")
            lines.append(f"• {ts} [{data['channel']}] {data['video']['title'][:50]}")
        except Exception:
            continue
    await update.message.reply_text("\n".join(lines))


# ─────────────────────────────────────────────
# 即時 URL 摘要：傳 YouTube 連結 → 抓字幕 → Claude 摘要 → 按鈕
# ─────────────────────────────────────────────

YOUTUBE_URL_RE = re.compile(
    r"(https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?[^\s]*v=|shorts/|live/|embed/)|youtu\.be/)[\w\-]{11}[^\s]*)"
)


def extract_youtube_url(text: str) -> Optional[str]:
    m = YOUTUBE_URL_RE.search(text or "")
    return m.group(1) if m else None


def fetch_video_meta(url: str) -> dict:
    """用 YouTube oEmbed 抓 title + author_name（不需 API key）。"""
    oembed = f"https://www.youtube.com/oembed?url={urllib.parse.quote(url, safe='')}&format=json"
    req = urllib.request.Request(oembed, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


async def handle_youtube_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用戶傳 YouTube 連結 → 立即跑摘要 → 推送訊息 + 按鈕。"""
    msg_text = update.message.text or ""
    url = extract_youtube_url(msg_text)
    if not url:
        return

    # daily_brief 跟 yt2epub 都已 import 過，避免循環匯入這裡才載
    from daily_brief import (
        summarize_video,
        build_video_message,
        build_buttons,
        save_summary,
    )
    from yt2epub import extract_video_id

    video_id = extract_video_id(url)
    progress = await update.message.reply_text(f"⏳ 抓字幕 + 摘要中... ({video_id})")

    try:
        meta = fetch_video_meta(url)
        title = meta.get("title", video_id)
        channel = meta.get("author_name", "")
    except Exception as e:
        logger.warning(f"oEmbed 失敗 {url}: {e}")
        title = video_id
        channel = ""

    video = {
        "id": video_id,
        "title": title,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "published": "",
    }

    try:
        summary = await asyncio.to_thread(summarize_video, video, channel)
    except Exception as e:
        logger.exception(f"摘要失敗 {video_id}")
        await progress.edit_text(f"❌ 摘要失敗：{e}")
        return

    item = {"channel": channel, "video": video, "summary": summary}
    save_summary(item)

    text = build_video_message(item)
    if summary.get("no_transcript"):
        await progress.edit_text(text, parse_mode=ParseMode.HTML)
        return

    await progress.delete()
    await update.message.reply_html(
        text,
        reply_markup=build_buttons(video_id),
        disable_web_page_preview=False,
    )


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚀 觸發 daily_brief，請稍候...")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(BASE_DIR / "daily_brief.py"),
        cwd=str(BASE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        await update.message.reply_text(f"❌ daily_brief 失敗:\n{output[-1000:]}")
    else:
        await update.message.reply_text(f"✅ daily_brief 完成：{_extract_brief_summary(output)}")


# ─────────────────────────────────────────────
# 訂閱管理指令（/channels / /sub / /unsub）
# ─────────────────────────────────────────────

async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出全部訂閱頻道（每筆右邊一顆 ✕ 退訂按鈕）。"""
    from subscribe import load_channels
    channels = load_channels()
    if not channels:
        await update.message.reply_html(
            "（沒有訂閱）\n直接貼 YouTube 頻道 / Playlist 連結就能訂閱。",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ 怎麼訂閱？", callback_data="menu:addhelp"),
            ]]),
        )
        return

    text = f"📡 共 <b>{len(channels)}</b> 個訂閱（點 ✕ 退訂）："
    await update.message.reply_html(text, reply_markup=build_channels_keyboard(channels))


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "🎛 <b>yt2epub 主選單</b>\n選一個動作：",
        reply_markup=build_main_menu(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(HELP_TEXT)


async def handle_channel_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """偵測使用者貼上的頻道 / playlist 網址 → 跳訂閱確認按鈕。"""
    text = update.message.text or ""
    # 排除單部影片網址（讓 handle_youtube_url 接手）
    if YOUTUBE_URL_RE.search(text):
        return
    m = CHANNEL_URL_RE.search(text)
    if not m:
        return

    url = m.group(1)
    token = secrets.token_urlsafe(6)
    _pending_subs[token] = url
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 訂閱", callback_data=f"sub:{token}"),
        InlineKeyboardButton("❌ 取消", callback_data=f"subno:{token}"),
    ]])
    await update.message.reply_html(
        f"要訂閱這個嗎？\n<code>{html_escape(url)}</code>",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


async def cmd_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """新增訂閱：/sub <YouTube URL>"""
    if not context.args:
        await update.message.reply_text(
            "用法：/sub <YouTube URL>\n\n"
            "範例：\n"
            "  /sub https://youtube.com/@AcquiredFM\n"
            "  /sub https://youtube.com/playlist?list=PLxxx"
        )
        return

    url = context.args[0]
    await update.message.reply_text(f"⏳ 解析 {url} 中...")

    # 把同步 / 阻塞的 IO 丟到 thread pool，避免卡住 event loop
    try:
        from subscribe import (
            resolve_url, fetch_video_ids,
            load_channels, save_channels,
            load_seen, save_seen,
        )

        kind, cid, fetched_name = await asyncio.to_thread(resolve_url, url)
        channels = load_channels()
        if any(c["type"] == kind and c["id"] == cid for c in channels):
            await update.message.reply_html(
                f"⚠️ 已訂閱：<b>{html_escape(fetched_name)}</b>"
            )
            return

        new_ch = {"type": kind, "id": cid, "name": fetched_name}
        channels.append(new_ch)
        save_channels(channels)

        # 把現有影片標記為已看，下次 daily_brief 才不會一次處理 100 部
        try:
            ids = await asyncio.to_thread(fetch_video_ids, new_ch)
            seen = load_seen()
            seen[f"{kind}:{cid}"] = ids
            save_seen(seen)
            seen_count = len(ids)
        except Exception as e:
            seen_count = -1
            logger.warning(f"標記既有影片失敗: {e}")

        msg = (
            f"✅ 新增訂閱：<b>{html_escape(fetched_name)}</b>\n"
            f"類型：{'頻道' if kind == 'channel' else 'Playlist'}\n"
            f"id: <code>{cid}</code>"
        )
        if seen_count >= 0:
            msg += f"\n\n已標記 {seen_count} 部既有影片為已看，之後 daily_brief 只推新片。"
        await update.message.reply_html(msg)

    except Exception as e:
        logger.exception(f"/sub 失敗: {e}")
        await update.message.reply_text(f"❌ 解析失敗：{e}")


async def cmd_unsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """移除訂閱：/unsub <name 或 id>"""
    if not context.args:
        await update.message.reply_text(
            "用法：/unsub <name 或 id>\n\n"
            "範例：/unsub Acquired\n"
            "提示：先用 /channels 看有哪些訂閱"
        )
        return

    target = " ".join(context.args).strip()
    from subscribe import load_channels, save_channels, load_seen, save_seen

    channels = load_channels()
    for i, ch in enumerate(channels):
        if ch["id"] == target or ch["name"] == target:
            removed = channels.pop(i)
            save_channels(channels)
            seen = load_seen()
            seen.pop(f"{removed['type']}:{removed['id']}", None)
            save_seen(seen)
            await update.message.reply_html(
                f"✅ 已移除：<b>{html_escape(removed['name'])}</b>"
            )
            return

    await update.message.reply_text(
        f"❌ 找不到：{target}\n用 /channels 看有哪些訂閱"
    )


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("❌ 缺少 TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    async def post_init(application: Application):
        """啟動時把指令清單推給 Telegram，使用者左下角藍色選單會自動列出。"""
        await application.bot.set_my_commands([
            BotCommand("menu", "🎛 主選單（按鈕操作）"),
            BotCommand("channels", "📡 訂閱清單（含退訂按鈕）"),
            BotCommand("list", "📋 最近處理過的影片"),
            BotCommand("run", "🚀 立刻跑 daily_brief"),
            BotCommand("sub", "➕ 新增訂閱（也可直接貼網址）"),
            BotCommand("unsub", "➖ 移除訂閱"),
            BotCommand("help", "❓ 用法說明"),
        ])

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("channels", cmd_channels))
    app.add_handler(CommandHandler("sub", cmd_sub))
    app.add_handler(CommandHandler("unsub", cmd_unsub))
    # 影片連結 → 即時摘要
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(YOUTUBE_URL_RE),
            handle_youtube_url,
        )
    )
    # 頻道 / playlist 連結 → 跳訂閱確認
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(CHANNEL_URL_RE),
            handle_channel_url,
        )
    )

    logger.info("✅ bot_service 啟動")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
