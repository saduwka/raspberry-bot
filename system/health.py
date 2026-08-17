import asyncio
import logging
import os
import subprocess

import psutil
from telegram import Update
from telegram.ext import ContextTypes

from config import ADMIN_ID
from core.db import log_event

logger = logging.getLogger(__name__)

TEMP_WARN = 65.0
TEMP_CRIT = 75.0
already_alerted = {"temp": False, "undervoltage": False}


def get_cpu_temp():
    try:
        result = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True, text=True)
        temp_str = result.stdout.strip().replace("temp=", "").replace("'C", "")
        return float(temp_str)
    except Exception:
        return None


def get_throttled_data():
    try:
        result = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True)
        val = result.stdout.strip().replace("throttled=", "")
        code = int(val, 16)
        flags = {
            "undervoltage": bool(code & 0x1),
            "frequency_capped": bool(code & 0x2),
            "throttling": bool(code & 0x4),
        }

        desc = []
        if flags["undervoltage"]:
            desc.append("⚡ Undervoltage!")
        if flags["frequency_capped"]:
            desc.append("🔻 Частота урезана")
        if flags["throttling"]:
            desc.append("🌡 Троттлинг")

        if code == 0:
            desc_str = "✅ Всё норм"
        elif not desc:
            desc_str = "📜 Исторические флаги (" + val + ")"
        else:
            desc_str = " | ".join(desc)

        return flags, desc_str
    except Exception:
        return {}, "N/A"


def get_uptime():
    try:
        result = subprocess.run(["uptime", "-p"], capture_output=True, text=True)
        return result.stdout.strip()
    except Exception:
        return "N/A"


def get_stats():
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    temp = get_cpu_temp()
    _, throttled = get_throttled_data()
    uptime = get_uptime()

    ram_used = (ram.total - ram.available) // (1024 * 1024)
    ram_total = ram.total // (1024 * 1024)
    disk_used = disk.used // (1024 * 1024 * 1024)
    disk_total = disk.total // (1024 * 1024 * 1024)
    temp_display = f"{temp}°C" if temp is not None else "N/A"

    return (
        f"🖥 <b>Состояние малинки</b>\n\n"
        f"🌡 Температура: <code>{temp_display}</code>\n"
        f"⚙️ CPU: <code>{cpu}%</code>\n"
        f"💾 RAM: <code>{ram_used} / {ram_total} MB</code> ({ram.percent}%)\n"
        f"💿 Диск: <code>{disk_used} / {disk_total} GB</code> ({disk.percent}%)\n"
        f"⏱ Uptime: <code>{uptime}</code>\n"
        f"🔋 Питание: <code>{throttled}</code>"
    )


async def check_health_alert(context: ContextTypes.DEFAULT_TYPE):
    temp = get_cpu_temp()
    flags, _ = get_throttled_data()
    uv = flags.get("undervoltage", False)

    await log_event("health", {"temp": temp, "uv": uv})

    if temp is not None:
        if temp >= TEMP_CRIT and not already_alerted["temp"]:
            await context.bot.send_message(
                chat_id=ADMIN_ID, text=f"🔴 <b>КРИТИЧНО: температура {temp}°C!</b>", parse_mode="HTML"
            )
            already_alerted["temp"] = True
        elif temp >= TEMP_WARN and not already_alerted["temp"]:
            await context.bot.send_message(
                chat_id=ADMIN_ID, text=f"🟡 <b>Предупреждение: температура {temp}°C</b>", parse_mode="HTML"
            )
            already_alerted["temp"] = True
        elif temp < TEMP_WARN - 5:
            already_alerted["temp"] = False

    if flags.get("undervoltage") and not already_alerted["undervoltage"]:
        await context.bot.send_message(
            chat_id=ADMIN_ID, text="⚡ <b>Undervoltage detected!</b> Проверьте блок питания.", parse_mode="HTML"
        )
        already_alerted["undervoltage"] = True
    elif not flags.get("undervoltage"):
        already_alerted["undervoltage"] = False


async def restart_bot(update: Update = None, context: ContextTypes.DEFAULT_TYPE = None):
    """Отправляет сообщение о перезапуске и инициирует его через systemctl."""
    msg_text = "🔄 <b>Инициирую перезапуск через systemd...</b>"
    if update and update.message:
        await update.message.reply_text(msg_text, parse_mode="HTML")
    elif context:
        await context.bot.send_message(chat_id=ADMIN_ID, text=msg_text, parse_mode="HTML")

    await asyncio.sleep(1)
    subprocess.Popen(["sudo", "systemctl", "restart", "gamebot.service"])
    os._exit(0)
