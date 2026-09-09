import html
import logging
import os
import subprocess
import sys

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler

from config import ADMIN_ID
from core.auth import admin_only
from core.db import cleanup_old_data
from core.states import ADD_KW, ADD_RSS, INPUT_SCROLL_TEXT
from core.ui import reply_keyboard
import json
import urllib.error
import urllib.request

from news.repo import get_blocked_tags, get_gaming_keywords, get_rss_feeds, remove_keyword, remove_rss_feed
from system.health import get_stats, restart_bot
from system.repo import set_oled_config

_MUSIC_ROOT = "/root"
if _MUSIC_ROOT not in sys.path:
    sys.path.insert(0, _MUSIC_ROOT)
from music.bluetooth import connect_device, list_paired_devices

logger = logging.getLogger(__name__)

OLED_MODE_SCRIPT = "/usr/local/bin/oled-mode"
OLED_DIR = "/root/oled"
if OLED_DIR not in sys.path:
    sys.path.insert(0, OLED_DIR)



MUSIC_API = "http://127.0.0.1:8080/api/music"
STATUS_API = "http://127.0.0.1:8080/api/status"


def music_api(action: str, **extra) -> dict:
    payload = {"action": action, **extra}
    req = urllib.request.Request(
        MUSIC_API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            return {"ok": False, "error": body or str(exc)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def music_now_playing_text() -> str:
    try:
        with urllib.request.urlopen(STATUS_API, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        m = data.get("music") or {}
    except Exception as exc:
        return f"ℹ️ Музыка недоступна: {html.escape(str(exc))}"

    if m.get("buffering"):
        return "⏳ Запуск волны…"
    if not m.get("playing"):
        title = m.get("title") or ""
        artists = m.get("artists") or ""
        if title:
            return f"⏹ Плеер остановлен\n🎵 {html.escape(artists)} - {html.escape(title)}"
        return "ℹ️ Сейчас ничего не играет."
    play_state = "⏸ Пауза" if m.get("paused") else "▶️ Играет"
    return (
        f"{play_state}\n"
        f"🎵 {html.escape(m.get('artists') or '')} - {html.escape(m.get('title') or '')}\n"
        f"⏱ {m.get('time_pos', 0)} / {m.get('duration', 0)} сек"
    )


def system_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статус RPi", callback_data="cmd_status"),
         InlineKeyboardButton("🤖 Local AI", callback_data="cmd_ai")],
        [InlineKeyboardButton("🎵 Музыка", callback_data="music_menu"),
         InlineKeyboardButton("📺 OLED Дисплей", callback_data="oled_menu")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="set_back")],
        [InlineKeyboardButton("🔄 Перезапуск", callback_data="set_restart")],
    ])


def get_oled_mode() -> str:
    try:
        result = subprocess.run(
            [OLED_MODE_SCRIPT, "status"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in result.stdout.splitlines():
            if line.startswith("Current OLED mode:"):
                return line.split(":", 1)[1].strip()
    except Exception as exc:
        logger.warning("Failed to detect OLED mode: %s", exc)
    return "dashboard"


def oled_mode_keyboard(mode: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🖥 Linux Terminal", callback_data="oled_mode_console"),
         InlineKeyboardButton("📊 Dashboard", callback_data="oled_mode_dashboard")],
    ]
    if mode == "console":
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_system")])
        return InlineKeyboardMarkup(rows)

    rows.extend([
        [InlineKeyboardButton("🔋 Вкл", callback_data="oled_pwr_on"),
         InlineKeyboardButton("🔌 Выкл", callback_data="oled_pwr_off")],
        [InlineKeyboardButton("🔄 Авто-цикл", callback_data="oled_scr_-1"),
         InlineKeyboardButton("⚡️ Рестарт Сервиса", callback_data="oled_restart")],
        [InlineKeyboardButton("1: Sys", callback_data="oled_scr_0"),
         InlineKeyboardButton("2: Wth", callback_data="oled_scr_1"),
         InlineKeyboardButton("3: Jira", callback_data="oled_scr_2")],
        [InlineKeyboardButton("💬 Бегущая строка", callback_data="oled_scroll_set")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_system")],
    ])
    return InlineKeyboardMarkup(rows)


@admin_only
async def show_system_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "⚙️ <b>Системное меню</b>\n\nПроверка состояния оборудования и управление основными настройками бота."
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=system_keyboard())
        except Exception:
            await update.callback_query.message.delete()
            await context.bot.send_message(
                update.effective_chat.id, text, parse_mode="HTML", reply_markup=system_keyboard()
            )
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=system_keyboard())


async def oled_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    mode = get_oled_mode()
    mode_label = "Linux Terminal" if mode == "console" else "Dashboard"
    text = f"📺 OLED — режим: <b>{mode_label}</b>"
    if mode == "console":
        text += "\n\nDashboard-управление недоступно, пока активен Linux terminal."

    await query.edit_message_text(
        text,
        reply_markup=oled_mode_keyboard(mode),
        parse_mode="HTML",
    )


async def oled_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    if data == "oled_mode_console":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ ДА, ПЕРЕЗАГРУЗИТЬ", callback_data="oled_mode_console_confirm")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="oled_menu")],
        ])
        await query.edit_message_text(
            "⚠️ <b>Linux Terminal на OLED</b>\n\n"
            "Pi перезагрузится. <code>oled_monitor</code> будет отключён.\n"
            "После reboot: <code>chvt 2</code> или Ctrl+Alt+F2 для консоли на OLED.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == "oled_mode_dashboard":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ ДА, ПЕРЕЗАГРУЗИТЬ", callback_data="oled_mode_dashboard_confirm")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="oled_menu")],
        ])
        await query.edit_message_text(
            "⚠️ <b>Dashboard mode</b>\n\n"
            "Pi перезагрузится. Вернётся <code>oled_monitor</code> и управление экранами.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == "oled_mode_console_confirm":
        subprocess.Popen([OLED_MODE_SCRIPT, "console"])
        await query.edit_message_text(
            "⏳ Pi перезагружается в режим <b>Linux Terminal</b>...\n"
            "После загрузки: <code>chvt 2</code> или Ctrl+Alt+F2.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if data == "oled_mode_dashboard_confirm":
        subprocess.Popen([OLED_MODE_SCRIPT, "dashboard"])
        await query.edit_message_text(
            "⏳ Pi перезагружается в режим <b>Dashboard</b>...",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    if get_oled_mode() == "console":
        await query.edit_message_text(
            "ℹ️ Сейчас активен Linux terminal. Переключитесь в Dashboard для управления экранами.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="oled_menu")]]),
        )
        return ConversationHandler.END

    if data == "oled_restart":
        os.system("systemctl restart oled_monitor.service")
        msg = "⚡️ Сервис OLED перезапущен"
    elif data == "oled_scroll_set":
        await query.message.reply_text("Введите текст для бегущей строки (или /cancel):")
        return INPUT_SCROLL_TEXT
    else:
        if data.startswith("oled_pwr_"):
            pwr = data.split("_")[2]
            await set_oled_config("power", pwr)
        elif data.startswith("oled_scr_"):
            scr = data.split("_")[2]
            await set_oled_config("forced_screen", scr)
        msg = f"✅ Команда OLED сохранена ({data})"

    await query.edit_message_text(
        msg, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="oled_menu")]])
    )
    return ConversationHandler.END


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📰 Управление RSS", callback_data="set_rss"),
         InlineKeyboardButton("🔑 Ключевые слова", callback_data="set_kw")],
        [InlineKeyboardButton("🚫 Стоп-теги", callback_data="set_tags"),
         InlineKeyboardButton("🧹 Очистка БД", callback_data="set_cleanup")],
        [InlineKeyboardButton("🔄 ПЕРЕЗАПУСК", callback_data="set_restart"),
         InlineKeyboardButton("❌ Закрыть", callback_data="set_close")],
    ]
    await update.message.reply_text(
        "⚙️ <b>Настройки бота</b>\n\nВыберите раздел для управления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    data = query.data

    if data == "set_close":
        await query.message.delete()
    elif data == "set_rss":
        feeds = await get_rss_feeds()
        text = "📰 <b>RSS-ленты:</b>\n\n" + ("\n".join([f"• {html.escape(f)}" for f in feeds]) if feeds else "Пусто")
        kb = [
            [InlineKeyboardButton("➕ Добавить ленту", callback_data="add_rss_ui")],
            [InlineKeyboardButton("🗑 Удалить ленту", callback_data="del_rss_ui")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="set_back")],
        ]
        await query.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML", disable_web_page_preview=True
        )
    elif data == "set_kw":
        kws = await get_gaming_keywords()
        text = "🔑 <b>Ключевые слова:</b>\n\n" + (", ".join([html.escape(k) for k in kws]) if kws else "Пусто")
        kb = [
            [InlineKeyboardButton("➕ Добавить слово", callback_data="add_kw_ui")],
            [InlineKeyboardButton("🗑 Удалить слово", callback_data="del_kw_ui")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="set_back")],
        ]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    elif data == "set_tags":
        tags = await get_blocked_tags()
        text = "🚫 <b>Заблокированные теги:</b>\n\n" + (", ".join([html.escape(t) for t in tags]) if tags else "Пусто")
        kb = [[InlineKeyboardButton("⬅️ Назад", callback_data="set_back")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    elif data == "set_back":
        keyboard = [
            [InlineKeyboardButton("📰 Управление RSS", callback_data="set_rss"),
             InlineKeyboardButton("🔑 Ключевые слова", callback_data="set_kw")],
            [InlineKeyboardButton("🚫 Стоп-теги", callback_data="set_tags"),
             InlineKeyboardButton("🧹 Очистка БД", callback_data="set_cleanup")],
            [InlineKeyboardButton("🔄 ПЕРЕЗАПУСК", callback_data="set_restart"),
             InlineKeyboardButton("❌ Закрыть", callback_data="set_close")],
        ]
        await query.message.edit_text(
            "⚙️ <b>Настройки бота</b>\n\nВыберите раздел для управления:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
    elif data == "set_restart":
        kb = [
            [InlineKeyboardButton("✅ ДА, ПЕРЕЗАГРУЗИТЬ", callback_data="confirm_restart")],
            [InlineKeyboardButton("⬅️ НАЗАД", callback_data="set_back")],
        ]
        await query.message.edit_text(
            "⚠️ <b>Вы уверены, что хотите перезагрузить бота?</b>",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="HTML",
        )
    elif data == "confirm_restart":
        await query.message.delete()
        await restart_bot(context=context)
    elif data == "set_cleanup":
        await cleanup_old_data()
        await query.message.edit_text(
            "✅ База данных очищена (удалены старые логи и новости).",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="set_back")]]),
        )


async def ai_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from ai import client as local_llm

    query = update.callback_query
    waiting = "🤖 Проверяю локальную модель..."
    if query:
        await query.edit_message_text(waiting, parse_mode="HTML")
    else:
        wait_msg = await update.message.reply_text(waiting)

    ok, detail = await local_llm.ping()
    html_detail = html.escape(str(detail)[:400])
    if ok:
        text = (
            f"✅ <b>Локальная модель работает</b>\n\n"
            f"{local_llm.status_text()}\n"
            f"Ответ: <code>{html_detail}</code>"
        )
    else:
        text = (
            f"❌ <b>Локальная модель недоступна</b>\n\n"
            f"{local_llm.status_text()}\n"
            f"Ошибка: <code>{html.escape(str(local_llm.last_error or detail)[:400])}</code>\n\n"
            f"Облако Gemini/Groq отключено — бот не уйдёт в запасной API."
        )

    markup = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_system")]])
    if query:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await wait_msg.edit_text(text, parse_mode="HTML")


@admin_only
async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ai_status_handler(update, context)


def music_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Моя волна", callback_data="music_wave_start")],
        [InlineKeyboardButton("⏯ Пауза", callback_data="music_pause"),
         InlineKeyboardButton("⏭ Далее", callback_data="music_next")],
        [InlineKeyboardButton("⏹ Стоп", callback_data="music_stop"),
         InlineKeyboardButton("ℹ️ Сейчас играет", callback_data="music_status")],
        [InlineKeyboardButton("📶 Bluetooth", callback_data="music_bt_menu")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_system")],
    ])


def bluetooth_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for device in list_paired_devices():
        label = f"{'✅' if device.connected else '📶'} {device.name}"
        rows.append([InlineKeyboardButton(label, callback_data=f"music_bt_connect::{device.mac}")])
    if not rows:
        rows.append([InlineKeyboardButton("Нет спаренных устройств", callback_data="music_bt_refresh")])
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="music_bt_refresh")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="music_menu")])
    return InlineKeyboardMarkup(rows)


async def show_music_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        status_text = music_now_playing_text()
    except Exception as exc:
        status_text = f"ℹ️ Плеер ещё не запущен.\n<code>{html.escape(str(exc))}</code>"

    text = (
        "🎵 <b>Яндекс Музыка</b>\n\n"
        "Управление локальным плеером на Raspberry Pi.\n\n"
        f"{status_text}"
    )
    keyboard = music_keyboard()
    if query:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def show_bluetooth_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    devices = list_paired_devices()
    if devices:
        lines = [
            f"{'✅' if device.connected else '•'} <code>{device.mac}</code> {html.escape(device.name)}"
            for device in devices
        ]
        text = "📶 <b>Bluetooth-устройства</b>\n\n" + "\n".join(lines)
    else:
        text = (
            "📶 <b>Bluetooth-устройства</b>\n\n"
            "Пока нет спаренных колонок. Спарьте колонку через `bluetoothctl`, "
            "после этого она появится здесь."
        )
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=bluetooth_keyboard())
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise


async def process_scroll_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    await set_oled_config("scrolling_text", text)
    await update.message.reply_text("✅ Текст бегущей строки обновлен!", reply_markup=reply_keyboard())
    return ConversationHandler.END


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    query = update.callback_query
    if data == "add_rss_ui":
        await query.message.reply_text("Отправьте URL RSS-ленты (или /cancel):")
        return ADD_RSS
    if data == "add_kw_ui":
        await query.message.reply_text("Отправьте ключевое слово (или /cancel):")
        return ADD_KW
    if data.startswith(("set_", "add_", "del_", "drss_", "dkw_", "confirm_")):
        return await settings_callback(update, context) or True
    if data == "oled_scroll_set":
        await query.message.reply_text("Введите текст для бегущей строки (или /cancel):")
        return INPUT_SCROLL_TEXT
    if data == "music_menu":
        await show_music_menu(update, context)
        return True
    if data == "music_wave_start":
        await query.answer("🎵 Запускаю Мою волну...")
        try:
            result = music_api("wave")
            if not (result.get("ok") or result.get("accepted")):
                raise RuntimeError(result.get("error") or "wave failed")
            m = result.get("music") or {}
            if m.get("buffering"):
                message = "⏳ Запускаю Мою волну…"
            else:
                message = f"▶️ {html.escape(m.get('artists') or '')} - {html.escape(m.get('title') or '')}"
            await query.message.edit_text(
                message,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К музыке", callback_data="music_menu")]]),
            )
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка запуска волны: {html.escape(str(e))}", parse_mode="HTML")
        return True
    if data == "music_pause":
        try:
            result = music_api("pause")
            if not (result.get("ok") or result.get("accepted")):
                raise RuntimeError(result.get("error") or "pause failed")
            m = result.get("music") or {}
            message = "⏸ Пауза" if m.get("paused") else "▶️ Продолжил"
            await query.answer(message)
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка паузы: {html.escape(str(e))}", parse_mode="HTML")
        await show_music_menu(update, context)
        return True
    if data == "music_next":
        await query.answer("⏭ Переключаю трек...")
        try:
            result = music_api("next")
            if not (result.get("ok") or result.get("accepted")):
                raise RuntimeError(result.get("error") or "next failed")
            m = result.get("music") or {}
            message = f"⏭ {m.get('artists') or ''} - {m.get('title') or ''}".strip(" -") or "⏭ Далее"
            await query.message.reply_text(html.escape(message), parse_mode="HTML")
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка next: {html.escape(str(e))}", parse_mode="HTML")
        await show_music_menu(update, context)
        return True
    if data == "music_stop":
        try:
            result = music_api("stop")
            if not (result.get("ok") or result.get("accepted")):
                raise RuntimeError(result.get("error") or "stop failed")
            await query.answer("⏹ Стоп")
            await query.message.reply_text("⏹ Воспроизведение остановлено.", parse_mode="HTML")
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка stop: {html.escape(str(e))}", parse_mode="HTML")
        await show_music_menu(update, context)
        return True
    if data == "music_status":
        await show_music_menu(update, context)
        return True
    if data == "music_bt_menu":
        await show_bluetooth_menu(update, context)
        return True
    if data == "music_bt_refresh":
        await show_bluetooth_menu(update, context)
        return True
    if data.startswith("music_bt_connect::"):
        mac = data.split("::", 1)[1]
        await query.answer("📶 Подключаю колонку...")
        try:
            ok, output = connect_device(mac)
            prefix = "✅ Устройство подключено." if ok else "⚠️ Подключение не подтверждено."
            await query.message.reply_text(
                f"{prefix}\n\n<code>{html.escape(output[-3000:])}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка Bluetooth: {html.escape(str(e))}", parse_mode="HTML")
        await show_bluetooth_menu(update, context)
        return True
    if data == "back_to_system":
        await show_system_menu(update, context)
        return True
    if data == "cmd_status":
        await query.message.edit_text(
            get_stats(),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_system")]]),
        )
        return True
    if data == "cmd_ai":
        await ai_status_handler(update, context)
        return True
    return None


def register(app):
    app.add_handler(CommandHandler("restart", restart_bot))
    app.add_handler(CommandHandler("ai", ai_command))
    app.add_handler(CallbackQueryHandler(oled_menu_handler, pattern="^oled_menu$"))
    app.add_handler(CallbackQueryHandler(oled_callback_handler, pattern="^oled_(pwr|scr|restart|mode_)"))
