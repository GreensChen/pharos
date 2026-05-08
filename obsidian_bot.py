#!/usr/bin/env python3
"""
obsidian_bot.py — Obsidian 知識庫 Telegram bot（跟 yt2epub bot 分開）

職責：
- /review 流程：拉 Dropbox `/Kobo Highlights/` 未 process 的 highlight，
  一條一條卡片推 user → user 文字 reaction → 寫 `/Obsidian/0 Inbox/`
- /skipall：把所有 pending 標記為已處理（清歷史用）
- /pausereview：暫停當前 review batch
- 22:00 daily digest：推送 pending highlights 數量提醒

未來會擴：
- 原生靈感（散步/洗澡/對話冒出）→ Telegram 文字 → 直接進 Obsidian
- 網路文章連結 → 摘要 + 存 Obsidian Inbox
- vault-aware AI 對話（撈 related notes、提問挑戰）

啟動：
    python3 obsidian_bot.py
（一般由 systemd 自動啟動）

需要 .env：
- OBSIDIAN_BOT_TOKEN（不同於 yt2epub 的 TELEGRAM_BOT_TOKEN）
- TELEGRAM_CHAT_ID（共用，daily digest 推這個 chat）
- Dropbox 認證（共用 yt2epub 的 .env，存取 /Kobo Highlights/ + /Obsidian/0 Inbox/）
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
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
LOG_FILE = BASE_DIR / "obsidian_bot.log"

DAILY_REVIEW_DIGEST_HOUR = int(os.environ.get("DAILY_REVIEW_DIGEST_HOUR", "22"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger("obsidian_bot")


def html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ─────────────────────────────────────────────
# Review state（per chat_id）
# ─────────────────────────────────────────────

_review_state: dict[int, dict] = {}
_quiz_state: dict[int, dict] = {}


async def _push_review_card(chat_id: int, bot):
    """推下一張 highlight 卡片，或結束 batch。"""
    state = _review_state.get(chat_id)
    if not state:
        return
    if state["idx"] >= len(state["pending"]):
        total = len(state["pending"])
        del _review_state[chat_id]
        await bot.send_message(
            chat_id, f"✅ 本輪 review 完成（{total} 條）。"
        )
        return

    h = state["pending"][state["idx"]]
    total = len(state["pending"])
    text_lines = [
        f"📖 <b>{html_escape(h.book_title)}</b>",
    ]
    if h.author:
        text_lines.append(f"👤 {html_escape(h.author)}")
    text_lines.append(f"🕒 {h.timestamp}  ({state['idx']+1}/{total})")
    text_lines.append("")
    text_lines.append(f"<blockquote>{html_escape(h.text)}</blockquote>")
    if h.note:
        text_lines.append(f"\n📝 原筆記：{html_escape(h.note)}")
    text_lines.append("\n💭 你的反應？（直接打字，或用按鈕）")

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏭ 跳過", callback_data="rev:skip"),
            InlineKeyboardButton("⏸ 全部之後", callback_data="rev:pause"),
        ],
    ])
    msg = await bot.send_message(
        chat_id,
        "\n".join(text_lines),
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    state["current_msg_id"] = msg.message_id
    state["awaiting_text"] = True


# ─────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "👋 <b>Obsidian bot 上線</b>\n\n"
        "我負責處理你的 Kobo highlights、之後也會接原生靈感跟網路文章。\n\n"
        "<b>常用指令：</b>\n"
        "• /review — 開始 process pending Kobo highlights\n"
        "• /skipall — 把所有 pending 標為已處理（清歷史用）\n"
        "• /pausereview — 暫停當前 review batch\n\n"
        "yt2epub 那支 bot 不變，繼續處理影片連結 → epub。"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/review — 啟動一輪 review。"""
    chat_id = update.effective_chat.id
    if chat_id in _review_state:
        await update.message.reply_text(
            "⚠️ 你正在 review 中。打 /pausereview 暫停或繼續處理當前卡片。"
        )
        return

    await update.message.reply_text("🔍 從 Dropbox 抓 highlights...")
    try:
        from kobo_highlights_reader import list_pending_highlights
        pending = await asyncio.to_thread(list_pending_highlights)
    except Exception as e:
        logger.exception("list_pending_highlights 失敗")
        await update.message.reply_text(f"❌ 讀取失敗：{e}")
        return

    if not pending:
        await update.message.reply_text("📭 沒有新的 highlights。慢慢來。")
        return

    _review_state[chat_id] = {
        "pending": pending,
        "idx": 0,
        "awaiting_text": False,
        "current_msg_id": None,
    }
    book_count = len({h.book_filename for h in pending})
    await update.message.reply_html(
        f"📚 <b>{len(pending)}</b> 條 highlights、跨 <b>{book_count}</b> 本書。\n一條一條來。"
    )
    await _push_review_card(chat_id, context.bot)


async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/quiz [keyword] — 出一題 3 選 1 active recall。
    沒帶 keyword = 用最近活躍的書；帶 keyword 用 substring 找書。"""
    chat_id = update.effective_chat.id
    if chat_id in _quiz_state:
        await update.message.reply_text(
            "⚠️ 你正在答題中。先選 A/B/C 結束當前題、或 /endquiz 取消。"
        )
        return

    keyword = " ".join(context.args).strip() if context.args else ""
    await update.message.reply_text(
        f"📚 找書中{'（' + keyword + '）' if keyword else ''}..."
    )

    try:
        from quiz_engine import (
            list_quizzable_books, find_book_by_keyword, generate_quiz,
        )
        if keyword:
            books = await asyncio.to_thread(find_book_by_keyword, keyword)
        else:
            books = await asyncio.to_thread(list_quizzable_books)
    except Exception as e:
        logger.exception("list books 失敗")
        await update.message.reply_text(f"❌ 列書失敗：{e}")
        return

    if not books:
        if keyword:
            await update.message.reply_text(
                f"📭 沒找到書名包含「{keyword}」的書（要先有 overview + highlights 才能考）"
            )
        else:
            await update.message.reply_text(
                "📭 vault 裡還沒有可考的書（先跑 generate_book_overviews.py 生 overview）"
            )
        return

    book = books[0]
    await update.message.reply_html(f"🎯 出題：<b>{html_escape(book['title'])}</b>")

    try:
        quiz = await asyncio.to_thread(generate_quiz, book)
    except Exception as e:
        logger.exception("generate_quiz 失敗")
        await update.message.reply_text(f"❌ 出題失敗：{e}")
        return

    _quiz_state[chat_id] = {"book": book, "quiz": quiz}
    await _push_quiz_card(chat_id, context.bot)


async def cmd_endquiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in _quiz_state:
        await update.message.reply_text("（沒在答題中）")
        return
    del _quiz_state[chat_id]
    await update.message.reply_text("⏹ 已取消當前 quiz。")


async def _push_quiz_card(chat_id: int, bot):
    state = _quiz_state.get(chat_id)
    if not state:
        return
    quiz = state["quiz"]
    book = state["book"]

    text_lines = [
        f"📖 <b>{html_escape(book['title'])}</b>",
        "",
        f"<b>{html_escape(quiz['question'])}</b>",
        "",
    ]
    for o in quiz["options"]:
        text_lines.append(f"<b>{o['label']}</b>．{html_escape(o['text'])}")

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(o["label"], callback_data=f"quiz:{o['label']}")
            for o in quiz["options"]
        ],
    ])
    await bot.send_message(
        chat_id,
        "\n".join(text_lines),
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def _handle_quiz_choice(query, choice: str):
    chat_id = query.message.chat_id
    state = _quiz_state.get(chat_id)
    if not state:
        await query.answer("此題已過期", show_alert=True)
        return

    quiz = state["quiz"]
    book = state["book"]
    is_correct = (choice == quiz["correct"])

    # save to vault
    try:
        from quiz_engine import save_quiz_response_to_dropbox
        remote_path = await asyncio.to_thread(
            save_quiz_response_to_dropbox,
            book["title"], quiz, choice, is_correct,
        )
    except Exception as e:
        logger.exception("save quiz 失敗")
        remote_path = f"（存檔失敗：{e}）"

    del _quiz_state[chat_id]
    await query.answer("答對 ✅" if is_correct else "答錯 ❌")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    correct_text = next(
        (o["text"] for o in quiz["options"] if o["label"] == quiz["correct"]),
        "",
    )
    result_emoji = "✅" if is_correct else "❌"
    feedback_lines = [
        f"{result_emoji} {'答對' if is_correct else '答錯'}",
        "",
        f"<b>正解：{quiz['correct']}．{html_escape(correct_text)}</b>",
        "",
        f"💡 {html_escape(quiz['explanation'])}",
        "",
        f"<i>已存：</i> <code>{html_escape(remote_path)}</code>",
        "",
        "再來一題？打 /quiz",
    ]
    await query.message.reply_html("\n".join(feedback_lines))


async def cmd_pausereview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pausereview — 暫停當前 review。state 保留，下次 /review 從頭抓。"""
    chat_id = update.effective_chat.id
    if chat_id not in _review_state:
        await update.message.reply_text("（沒在 review 中）")
        return
    del _review_state[chat_id]
    await update.message.reply_text("⏸ 已暫停。隨時打 /review 重啟（從新的 pending 開始）。")


async def cmd_skipall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/skipall — 把所有 pending highlights 標記為 already-processed（不寫 inbox）。"""
    await update.message.reply_text("🧹 抓 pending 列表...")
    try:
        from kobo_highlights_reader import list_pending_highlights, mark_processed
        pending = await asyncio.to_thread(list_pending_highlights)
    except Exception as e:
        logger.exception("list_pending_highlights 失敗")
        await update.message.reply_text(f"❌ 失敗：{e}")
        return

    if not pending:
        await update.message.reply_text("📭 已經沒有 pending highlights")
        return

    by_book: dict[str, str] = {}
    for h in pending:
        prev = by_book.get(h.book_filename, "")
        if h.timestamp > prev:
            by_book[h.book_filename] = h.timestamp

    for book, ts in by_book.items():
        await asyncio.to_thread(mark_processed, book, ts)

    await update.message.reply_html(
        f"✅ 已標記 <b>{len(pending)}</b> 條 highlights 為 processed"
        f"（<b>{len(by_book)}</b> 本書）。\n"
        f"下次 /review 只看之後新畫的。"
    )


# ─────────────────────────────────────────────
# Callback handlers (button presses)
# ─────────────────────────────────────────────

async def cb_review(query, action: str):
    """callback dispatch：rev:skip / rev:pause"""
    chat_id = query.message.chat_id
    state = _review_state.get(chat_id)
    if not state:
        await query.answer("此 review 已過期", show_alert=True)
        return

    if action == "skip":
        h = state["pending"][state["idx"]]
        try:
            from kobo_highlights_reader import mark_processed
            await asyncio.to_thread(mark_processed, h.book_filename, h.timestamp)
        except Exception as e:
            logger.warning(f"mark_processed 失敗: {e}")
        state["idx"] += 1
        await query.answer("已跳過")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _push_review_card(chat_id, query.get_bot())
        return

    if action == "pause":
        del _review_state[chat_id]
        await query.answer("已暫停")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("⏸ 已暫停。隨時打 /review 重啟。")
        return

    await query.answer(f"未知 review 動作: {action}")


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    if ":" not in data:
        await query.answer()
        return
    action, payload = data.split(":", 1)
    logger.info(f"按鈕點擊: action={action}  payload={payload}")
    try:
        if action == "rev":
            await cb_review(query, payload)
        elif action == "quiz":
            await _handle_quiz_choice(query, payload)
        else:
            await query.answer(f"未知動作: {action}")
    except Exception as e:
        logger.exception(f"按鈕 handler 失敗: {e}")
        try:
            await query.message.reply_text(f"❌ 處理失敗: {e}")
        except Exception:
            pass


# ─────────────────────────────────────────────
# Reaction handler (純文字訊息)
# ─────────────────────────────────────────────

async def _process_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             state: dict, reaction_text: str):
    chat_id = update.effective_chat.id
    h = state["pending"][state["idx"]]

    try:
        from obsidian_inbox_writer import write_to_inbox
        from kobo_highlights_reader import mark_processed
        remote_path = await asyncio.to_thread(
            write_to_inbox,
            book_filename=h.book_filename,
            book_title=h.book_title,
            author=h.author,
            highlight_text=h.text,
            highlight_timestamp=h.timestamp,
            user_reaction=reaction_text,
            chapter=h.chapter,
            note=h.note,
        )
        await asyncio.to_thread(mark_processed, h.book_filename, h.timestamp)
    except Exception as e:
        logger.exception("寫 inbox 失敗")
        await update.message.reply_text(f"❌ 寫 Obsidian Inbox 失敗：{e}")
        return

    state["idx"] += 1
    state["awaiting_text"] = False
    await update.message.reply_html(
        f"✅ 已存：<code>{html_escape(remote_path)}</code>"
    )
    await _push_review_card(chat_id, context.bot)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """所有非 command 純文字。目前只處理 review reaction，未來擴增原生靈感 capture。"""
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    text = update.message.text

    state = _review_state.get(chat_id)
    if state and state.get("awaiting_text"):
        await _process_reaction(update, context, state, text)
        return

    # 不在 review 中：暫時 silent。未來這裡接「原生靈感 → Obsidian」
    # 提示 user 可以做什麼
    await update.message.reply_html(
        "💡 收到一段文字，但你現在不在 review 中。\n"
        "未來這裡會把訊息直接存進 Obsidian Inbox 當靈感，目前先 reply 確認。\n"
        "想 process Kobo highlights 打 /review。"
    )


# ─────────────────────────────────────────────
# Daily digest
# ─────────────────────────────────────────────

async def daily_digest_loop(bot, chat_id: int):
    """每天 DAILY_REVIEW_DIGEST_HOUR 點推送 pending 提醒。"""
    while True:
        now = datetime.now()
        target = now.replace(
            hour=DAILY_REVIEW_DIGEST_HOUR, minute=0, second=0, microsecond=0,
        )
        if now >= target:
            target += timedelta(days=1)
        sleep_secs = (target - now).total_seconds()
        logger.info(f"daily digest 下次觸發於 {target}（{int(sleep_secs)}s 後）")
        await asyncio.sleep(sleep_secs)

        try:
            from kobo_highlights_reader import count_pending
            count = await asyncio.to_thread(count_pending)
            if count > 0:
                await bot.send_message(
                    chat_id,
                    f"📚 你還有 <b>{count}</b> 條 highlights 沒 process。\n打 /review 開始。",
                    parse_mode=ParseMode.HTML,
                )
            else:
                logger.info("daily digest: 0 pending, 不推送")
        except Exception as e:
            logger.exception(f"daily digest 失敗: {e}")
            try:
                await bot.send_message(chat_id, f"⚠️ daily digest 失敗：{e}")
            except Exception:
                pass


# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────

def main():
    token = os.environ.get("OBSIDIAN_BOT_TOKEN")
    if not token:
        logger.error("❌ 缺少 OBSIDIAN_BOT_TOKEN（去 @BotFather 建一個新 bot 拿 token）")
        sys.exit(1)

    async def post_init(application: Application):
        await application.bot.set_my_commands([
            BotCommand("review", "📚 process pending Kobo highlights"),
            BotCommand("pausereview", "⏸ 暫停 review"),
            BotCommand("skipall", "🧹 把所有 pending 標記為已處理（清歷史用）"),
            BotCommand("quiz", "🎯 出一題 active recall（[書名關鍵字]）"),
            BotCommand("endquiz", "⏹ 取消當前 quiz"),
            BotCommand("help", "❓ 用法說明"),
        ])

        # 啟動 daily digest 排程
        chat_id_str = os.environ.get("TELEGRAM_CHAT_ID")
        if chat_id_str:
            try:
                chat_id = int(chat_id_str)
                asyncio.create_task(daily_digest_loop(application.bot, chat_id))
                logger.info(f"✅ daily digest task 啟動（每天 {DAILY_REVIEW_DIGEST_HOUR}:00 推送）")
            except ValueError:
                logger.warning(f"TELEGRAM_CHAT_ID 不是有效整數：{chat_id_str}，daily digest 跳過")
        else:
            logger.warning("沒有 TELEGRAM_CHAT_ID，daily digest 跳過")

    app = Application.builder().token(token).post_init(post_init).build()
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("pausereview", cmd_pausereview))
    app.add_handler(CommandHandler("skipall", cmd_skipall))
    app.add_handler(CommandHandler("quiz", cmd_quiz))
    app.add_handler(CommandHandler("endquiz", cmd_endquiz))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("✅ obsidian_bot 啟動")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
