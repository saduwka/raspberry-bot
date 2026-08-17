import asyncio
import fcntl
import logging
import sys
from datetime import time as dt_time
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import ADMIN_ID, BOT_TOKEN, GAMING_KEYWORDS, RSS_FEEDS, TRADE_INTERVAL_SECONDS
from core.auth import admin_only
from core.db import cleanup_old_data, init_db, populate_initial_data
from core.states import ADD_KW, ADD_RSS, INPUT_SCROLL_TEXT, JOB_DISCOVERY_INPUT, JOB_QUERY_INPUT
from core.ui import reply_keyboard
from jobs.handlers import process_job_discovery_step, process_job_query_step, show_jobs_menu
from jobs.handlers import register as register_jobs
from jobs.jobs import check_job_follow_ups, job_fetch_job
from news.handlers import add_kw_step, add_rss_step, show_news_menu
from news.handlers import register as register_news
from news.jobs import send_weekly_digest
from system.backup import backup_db_job
from system.handlers import process_scroll_text_step, show_system_menu
from system.handlers import register as register_system
from system.health import check_health_alert, get_stats
from trade.handlers import show_trade_menu
from trade.jobs import send_daily_trade_analytics, trade_job

logger = logging.getLogger(__name__)
ALMATY_TZ = ZoneInfo("Asia/Almaty")


def _setup_logging():
    log_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    file_handler = RotatingFileHandler("bot.log", maxBytes=5 * 1024 * 1024, backupCount=3)
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.INFO)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(logging.INFO)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])


@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎮 Привет! Управление ботом:", reply_markup=reply_keyboard())


@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(get_stats(), parse_mode="HTML")


@admin_only
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "🔍 Новости":
        await show_news_menu(update, context)
    elif text == "⚙️ Система":
        await show_system_menu(update, context)
    elif text == "📈 Трейдинг":
        await show_trade_menu(update, context)
    elif text == "💼 Вакансии":
        await show_jobs_menu(update, context)
    elif text == "/start":
        await start(update, context)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from config import ADMIN_ID as admin_id
    from jobs import handlers as jobs_handlers
    from news import handlers as news_handlers
    from system import handlers as system_handlers
    from trade import handlers as trade_handlers

    query = update.callback_query
    await query.answer()
    if query.from_user.id != admin_id:
        return
    data = query.data

    for module in (system_handlers, news_handlers, jobs_handlers, trade_handlers):
        result = await module.handle_callback(update, context, data)
        if result is not None:
            return result


async def cancel_interactive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено.", reply_markup=reply_keyboard())
    return ConversationHandler.END


async def on_startup(app):
    from ai import client as local_llm

    await local_llm.startup()
    await populate_initial_data(RSS_FEEDS, GAMING_KEYWORDS)
    try:
        await app.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🚀 <b>Бот запущен!</b>\n\n{get_stats()}",
            parse_mode="HTML",
            reply_markup=reply_keyboard(),
        )
    except Exception as e:
        logger.error("Startup message failed: %s", e)


async def on_shutdown(app):
    from ai import client as local_llm

    await local_llm.shutdown()


def schedule_jobs(app):
    jq = app.job_queue
    jq.run_daily(job_fetch_job, time=dt_time(10, 0, tzinfo=ALMATY_TZ))
    jq.run_daily(job_fetch_job, time=dt_time(18, 0, tzinfo=ALMATY_TZ))
    jq.run_daily(check_job_follow_ups, time=dt_time(11, 0, tzinfo=ALMATY_TZ))
    jq.run_daily(lambda ctx: asyncio.create_task(cleanup_old_data()), time=dt_time(4, 0, tzinfo=ALMATY_TZ))
    jq.run_daily(backup_db_job, time=dt_time(3, 0, tzinfo=ALMATY_TZ))
    jq.run_daily(send_weekly_digest, time=dt_time(20, 0, tzinfo=ALMATY_TZ), days=(6,))
    jq.run_daily(send_daily_trade_analytics, time=dt_time(23, 0, tzinfo=ALMATY_TZ))
    jq.run_repeating(check_health_alert, interval=300, first=10)
    jq.run_repeating(trade_job, interval=TRADE_INTERVAL_SECONDS, first=30)


def main():
    _setup_logging()
    print("--- BOT STARTING VERSION 3.0 ---")

    pid_file = "/tmp/gamebot.pid"
    fp = open(pid_file, "w")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("Bot is already running. Exiting.")
        sys.exit(1)

    asyncio.run(init_db())

    app = Application.builder().token(BOT_TOKEN).build()
    app.post_init = on_startup
    app.post_shutdown = on_shutdown

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                callback_handler,
                pattern="^(add_rss_ui|add_kw_ui|jobs_query_edit|jobs_discovery_edit|oled_scroll_set)",
            )
        ],
        states={
            ADD_RSS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_rss_step)],
            ADD_KW: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_kw_step)],
            JOB_QUERY_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_job_query_step)],
            JOB_DISCOVERY_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_job_discovery_step)],
            INPUT_SCROLL_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_scroll_text_step)],
        },
        fallbacks=[CommandHandler("cancel", cancel_interactive)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("test_analytics", send_daily_trade_analytics))

    register_system(app)
    register_news(app)
    register_jobs(app)

    from jobs.handlers import cover_letter_callback, dismiss_job_callback

    app.add_handler(CallbackQueryHandler(cover_letter_callback, pattern="^cover_job_"))
    app.add_handler(CallbackQueryHandler(dismiss_job_callback, pattern="^(dismiss_job_|apply_job_)"))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    schedule_jobs(app)
    logger.info("Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()
