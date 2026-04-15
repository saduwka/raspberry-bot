import html
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from database import get_trade_stats, get_open_position, get_trade_state
from config import PAPER_MODE, TRADE_PAIR, TRADE_QTY
import trade_engine

logger = logging.getLogger(__name__)

def trade_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Статистика", callback_data="trade_stats"),
         InlineKeyboardButton("🔄 Обновить", callback_data="trade_refresh")],
        [InlineKeyboardButton("🧠 Сигнал", callback_data="trade_signal"),
         InlineKeyboardButton("📈 Меню", callback_data="trade_menu")],
    ])

async def show_trade_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"📈 <b>Трейдинг: {TRADE_PAIR}</b>\n\n"
        "Управление торговым модулем через кнопки."
    )
    if update.callback_query:
        await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=trade_keyboard())
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=trade_keyboard())

async def trade_signal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает текущий технический сигнал и решение Gemini."""
    try:
        df = await trade_engine.fetch_ohlcv()
        if df is None or df.empty:
            text = "❌ Не удалось получить рыночные данные."
        else:
            df = trade_engine.calc_indicators(df)
            last = df.iloc[-1]
            technical_signal = await get_trade_state("last_trade_signal")
            gemini_action = await get_trade_state("last_gemini_action")
            gemini_confidence = await get_trade_state("last_gemini_confidence")
            final_decision = await get_trade_state("last_trade_decision")
            gemini_reason = await get_trade_state("last_gemini_reason")
            risk_exit = await get_trade_state("last_risk_exit_reason")

            confidence_text = f"{float(gemini_confidence):.2f}" if gemini_confidence is not None else "0.00"
            text = (
                f"🧠 <b>Текущий сигнал: {TRADE_PAIR}</b>\n\n"
                f"Цена: <code>{float(last['close']):.2f}</code>\n"
                f"EMA Fast: <code>{float(last['ema_fast']):.2f}</code>\n"
                f"EMA Slow: <code>{float(last['ema_slow']):.2f}</code>\n"
                f"RSI: <code>{float(last['rsi']):.2f}</code>\n\n"
                f"Техсигнал: <code>{html.escape(str(technical_signal or 'N/A'))}</code>\n"
                f"Gemini: <code>{html.escape(str(gemini_action or 'N/A'))}</code> "
                f"(<code>{confidence_text}</code>)\n"
                f"Итог: <code>{html.escape(str(final_decision or 'HOLD'))}</code>\n"
                f"Риск-выход: <code>{html.escape(str(risk_exit or 'нет'))}</code>\n"
                f"Причина: <code>{html.escape(str(gemini_reason or 'нет объяснения'))}</code>"
            )

        if update.callback_query:
            try:
                await update.callback_query.message.edit_text(text, parse_mode="HTML", reply_markup=trade_keyboard())
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=trade_keyboard())
    except Exception as e:
        logger.error(f"Error in trade_signal_handler: {e}", exc_info=True)
        error_msg = "❌ Ошибка при получении торгового сигнала."
        if update.callback_query:
            await update.callback_query.message.reply_text(error_msg)
        else:
            await update.message.reply_text(error_msg)

async def trade_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику торгов и текущие индикаторы."""
    try:
        # 1. Получаем индикаторы
        logger.info("Fetching OHLCV for stats...")
        df = await trade_engine.fetch_ohlcv()
        indicators_text = ""
        if df is not None:
            logger.info("Calculating indicators for stats...")
            df = trade_engine.calc_indicators(df)
            if df is not None and not df.empty:
                last = df.iloc[-1]
                indicators_text = (
                    f"\n📊 <b>Текущие индикаторы:</b>\n"
                    f"Цена: <code>{last['close']:.2f}</code>\n"
                    f"EMA Fast: <code>{last['ema_fast']:.2f}</code>\n"
                    f"EMA Slow: <code>{last['ema_slow']:.2f}</code>\n"
                    f"RSI: <code>{last['rsi']:.2f}</code>\n"
                )

        # 2. Получаем статику за 7 и 30 дней
        logger.info("Fetching trade stats from DB...")
        stats7 = await get_trade_stats(7)
        stats30 = await get_trade_stats(30)
        
        # Распаковка с защитой от None
        count7, pnl7 = stats7 if stats7 else (0, 0.0)
        count30, pnl30 = stats30 if stats30 else (0, 0.0)
        
        # Гарантируем, что это числа, а не None
        pnl7 = float(pnl7) if pnl7 is not None else 0.0
        pnl30 = float(pnl30) if pnl30 is not None else 0.0
        count7 = int(count7) if count7 is not None else 0
        count30 = int(count30) if count30 is not None else 0
        
        logger.info("Checking open position...")
        current_pos = await get_open_position()
        pos_text = "💰 В позиции" if current_pos == "in_position" else "💤 Вне рынка"
        last_trade_signal = await get_trade_state("last_trade_signal")
        last_gemini_action = await get_trade_state("last_gemini_action")
        last_gemini_confidence = await get_trade_state("last_gemini_confidence")
        last_gemini_reason = await get_trade_state("last_gemini_reason")
        last_trade_decision = await get_trade_state("last_trade_decision")
        last_risk_exit_reason = await get_trade_state("last_risk_exit_reason")
        entry_price = await get_trade_state("entry_price")
        position_qty = await get_trade_state("position_qty")
        
        mode_text = "🧪 PAPER" if PAPER_MODE else "💰 LIVE"
        position_text = ""
        if current_pos == "in_position" and df is not None and not df.empty and entry_price is not None:
            qty = float(position_qty) if position_qty is not None else TRADE_QTY
            current_price = float(df.iloc[-1]["close"])
            entry_price_value = float(entry_price)
            unrealized_pnl = (current_price - entry_price_value) * qty
            position_text = (
                f"\n💼 <b>Открытая позиция:</b>\n"
                f"Вход: <code>{entry_price_value:.2f}</code>\n"
                f"Объем: <code>{qty}</code>\n"
                f"Плавающий PnL: <code>{unrealized_pnl:.2f}</code>\n"
            )
        gemini_text = ""
        if last_trade_signal or last_gemini_action or last_trade_decision:
            confidence_text = (
                f"{float(last_gemini_confidence):.2f}"
                if last_gemini_confidence is not None else "0.00"
            )
            reason_text = html.escape(str(last_gemini_reason or "нет объяснения"))
            risk_exit_text = html.escape(str(last_risk_exit_reason or "нет"))
            gemini_text = (
                f"\n🤖 <b>Gemini в решении:</b>\n"
                f"Техсигнал: <code>{html.escape(str(last_trade_signal or 'N/A'))}</code>\n"
                f"Gemini: <code>{html.escape(str(last_gemini_action or 'N/A'))}</code> "
                f"(conf <code>{confidence_text}</code>)\n"
                f"Итог: <code>{html.escape(str(last_trade_decision or 'HOLD'))}</code>\n"
                f"Риск-выход: <code>{risk_exit_text}</code>\n"
                f"Причина: <code>{reason_text}</code>\n"
            )
        
        text = (
            f"📈 <b>Торговая статистика: {TRADE_PAIR}</b>\n\n"
            f"Текущий статус: <b>{pos_text}</b>\n"
            f"Режим: <code>{mode_text}</code>\n"
            f"{position_text}"
            f"{indicators_text}\n"
            f"{gemini_text}"
            f"📅 <b>За 7 дней:</b>\n"
            f"Сделок: <code>{count7}</code>\n"
            f"PnL: <code>{pnl7:.2f}</code>\n\n"
            f"📅 <b>За 30 дней:</b>\n"
            f"Сделок: <code>{count30}</code>\n"
            f"PnL: <code>{pnl30:.2f}</code>"
        )
        
        if update.callback_query:
            try:
                await update.callback_query.message.edit_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=trade_keyboard(),
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=trade_keyboard())
            
    except Exception as e:
        import traceback
        logger.error(f"Error in trade_stats_handler: {e}\n{traceback.format_exc()}")
        error_msg = "❌ Ошибка при получении статистики."
        if update.callback_query:
            await update.callback_query.message.reply_text(error_msg)
        else:
            await update.message.reply_text(error_msg)
