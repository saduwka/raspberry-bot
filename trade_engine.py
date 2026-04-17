import logging
import ccxt.async_support as ccxt
import pandas as pd
from config import (
    BINANCE_API_KEY,
    BINANCE_SECRET,
    PAPER_MODE,
    TRADE_QTY,
    TRADE_TIMEFRAME,
    TRADE_STOP_LOSS_PCT,
    TRADE_TAKE_PROFIT_PCT,
)
from database import save_trade, set_trade_state, get_trade_state

logger = logging.getLogger(__name__)

async def fetch_ohlcv(symbol, timeframe=TRADE_TIMEFRAME, limit=100):
    """Получает исторические данные (свечи) с Binance с повторными попытками."""
    exchange = ccxt.binance({
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_SECRET,
        'enableRateLimit': True,
        'timeout': 20000,
        'options': {'defaultType': 'spot'}
    })
    
    try:
        if PAPER_MODE:
            exchange.set_sandbox_mode(True)
            
        for attempt in range(3):
            try:
                ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                return df
            except Exception as e:
                if attempt == 2:
                    raise e
                logger.warning(f"Attempt {attempt+1} failed to fetch OHLCV: {e}")
                import asyncio
                await asyncio.sleep(1)
                
    except Exception as e:
        logger.error(f"Error fetching OHLCV after retries: {e}")
        return None
    finally:
        await exchange.close()

def calc_indicators(df):
    """Рассчитывает EMA3, EMA8 и RSI14."""
    if df is None or df.empty:
        return None
        
    from ta.trend import EMAIndicator
    from ta.momentum import RSIIndicator
    
    df['ema_fast'] = EMAIndicator(close=df['close'], window=3).ema_indicator()
    df['ema_slow'] = EMAIndicator(close=df['close'], window=8).ema_indicator()
    df['rsi'] = RSIIndicator(close=df['close'], window=14).rsi()
    return df

def get_signal(df, sentiment=0):
    """
    Генерирует сигнал.
    Агрессивный режим:
    BUY: EMA3 > EMA8 + RSI < 80 (или пересечение)
    SELL: EMA3 < EMA8 OR RSI > 85
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

async def execute_trade(signal, price, pair, sentiment_score=0):
    """Исполняет сделку (в PAPER_MODE или реальную)."""
    if signal == "HOLD":
        return
        
    logger.info(f"Executing {signal} for {pair} at {price} (Sentiment: {sentiment_score})")
    
    qty = TRADE_QTY
    pnl = 0.0
    
    # Расчет PnL для SELL (разница между ценой продажи и покупки)
    if signal == "SELL":
        entry_price_raw = await get_trade_state("entry_price", pair)
        if entry_price_raw is not None:
            try:
                entry_price = float(entry_price_raw)
                pnl = (price - entry_price) * qty
                logger.info(f"Closed trade for {pair} with PnL: {pnl:.4f} (Entry: {entry_price}, Exit: {price})")
            except Exception as e:
                logger.error(f"Error calculating PnL for {pair}: {e}")
        else:
            logger.warning(f"SELL signal received for {pair} without entry_price in trade_state")

    await save_trade(
        pair=pair,
        side=signal,
        price=price,
        qty=qty,
        pnl=pnl,
        signal=f"AGGRESSIVE_{signal}",
        sentiment=str(sentiment_score)
    )
    
    # Сохраняем состояние позиции и цену входа
    if signal == "BUY":
        await set_trade_state("current_position", "in_position", pair)
        await set_trade_state("entry_price", price, pair)
        await set_trade_state("position_qty", qty, pair)
    else:
        await set_trade_state("current_position", "none", pair)
        await set_trade_state("entry_price", None, pair)
        await set_trade_state("position_qty", None, pair)

    return True
