import logging
from config import (
    BINANCE_API_KEY,
    BINANCE_SECRET,
    TRADE_PAIR,
    PAPER_MODE,
    TRADE_QTY,
    TRADE_TIMEFRAME,
    TRADE_STOP_LOSS_PCT,
    TRADE_TAKE_PROFIT_PCT,
)
from database import save_trade, set_trade_state, get_trade_state

logger = logging.getLogger(__name__)

async def fetch_ohlcv(symbol=TRADE_PAIR, timeframe=TRADE_TIMEFRAME, limit=100):
    """Получает исторические данные (свечи) с Binance."""
    import ccxt.async_support as ccxt
    import pandas as pd
    
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })
    try:
        if PAPER_MODE:
            exchange.set_sandbox_mode(True)
            
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        logger.error(f"Error fetching OHLCV: {e}")
        return None
    finally:
        await exchange.close()

def calc_indicators(df):
    """Рассчитывает EMA5, EMA13 и RSI14."""
    if df is None or df.empty:
        return None
        
    from ta.trend import EMAIndicator
    from ta.momentum import RSIIndicator
    
    df['ema_fast'] = EMAIndicator(close=df['close'], window=5).ema_indicator()
    df['ema_slow'] = EMAIndicator(close=df['close'], window=13).ema_indicator()
    df['rsi'] = RSIIndicator(close=df['close'], window=14).rsi()
    return df

def get_signal(df, sentiment=0):
    """
    Генерирует сигнал.
    Агрессивный режим:
    BUY: EMA5 > EMA13 + RSI < 80 (или пересечение)
    SELL: EMA5 < EMA13 OR RSI > 85
    """
    if df is None or len(df) < 2:
        return "HOLD"
        
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    # Состояние тренда
    is_bullish = last['ema_fast'] > last['ema_slow']
    is_bearish = last['ema_fast'] < last['ema_slow']
    
    # Момент пересечения
    ema_cross_up = prev['ema_fast'] <= prev['ema_slow'] and is_bullish
    ema_cross_down = prev['ema_fast'] >= prev['ema_slow'] and is_bearish
    
    # Вход: если только что пересеклись ИЛИ если тренд уже бычий, но RSI позволяет
    if (ema_cross_up or is_bullish) and last['rsi'] < 80:
        return "BUY"
    # Выход: пересечение вниз или перекупленность
    elif ema_cross_down or last['rsi'] > 85:
        return "SELL"
        
    return "HOLD"

def get_risk_exit_signal(current_price, entry_price):
    """Возвращает причину принудительного выхода по риску, если она сработала."""
    if entry_price is None:
        return None

    stop_loss_price = entry_price * (1 - TRADE_STOP_LOSS_PCT)
    take_profit_price = entry_price * (1 + TRADE_TAKE_PROFIT_PCT)

    if current_price <= stop_loss_price:
        return "STOP_LOSS"
    if current_price >= take_profit_price:
        return "TAKE_PROFIT"
    return None

async def execute_trade(signal, price, sentiment_score=0):
    """Исполняет сделку (в PAPER_MODE или реальную)."""
    if signal == "HOLD":
        return
        
    logger.info(f"Executing {signal} at {price} (Sentiment: {sentiment_score})")
    
    qty = TRADE_QTY
    pnl = 0.0
    
    # Расчет PnL для SELL (разница между ценой продажи и покупки)
    if signal == "SELL":
        entry_price_raw = await get_trade_state("entry_price")
        if entry_price_raw is not None:
            try:
                entry_price = float(entry_price_raw)
                pnl = (price - entry_price) * qty
                logger.info(f"Closed trade with PnL: {pnl:.4f} (Entry: {entry_price}, Exit: {price})")
            except Exception as e:
                logger.error(f"Error calculating PnL: {e}")
        else:
            logger.warning("SELL signal received without entry_price in trade_state")

    await save_trade(
        pair=TRADE_PAIR,
        side=signal,
        price=price,
        qty=qty,
        pnl=pnl,
        signal=f"AGGRESSIVE_{signal}",
        sentiment=str(sentiment_score)
    )
    
    # Сохраняем состояние позиции и цену входа
    if signal == "BUY":
        await set_trade_state("current_position", "in_position")
        await set_trade_state("entry_price", price)
        await set_trade_state("position_qty", qty)
    else:
        await set_trade_state("current_position", "none")
        await set_trade_state("entry_price", None)
        await set_trade_state("position_qty", None)

    return True
