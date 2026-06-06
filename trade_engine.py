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
    """Рассчитывает EMA, RSI, ADX (сила тренда), ATR (волатильность) и SMA объема."""
    if df is None or df.empty:
        return None
        
    from ta.trend import EMAIndicator, ADXIndicator, SMAIndicator
    from ta.momentum import RSIIndicator
    from ta.volatility import AverageTrueRange
    
    # Трендовые и осцилляторы
    df['ema_fast'] = EMAIndicator(close=df['close'], window=5).ema_indicator()
    df['ema_slow'] = EMAIndicator(close=df['close'], window=13).ema_indicator()
    df['ema_trend'] = EMAIndicator(close=df['close'], window=50).ema_indicator()
    df['rsi'] = RSIIndicator(close=df['close'], window=14).rsi()
    
    # Сила тренда (ADX)
    adx_ind = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
    df['adx'] = adx_ind.adx()
    df['adx_pos'] = adx_ind.adx_pos()
    df['adx_neg'] = adx_ind.adx_neg()
    
    # Волатильность (ATR)
    df['atr'] = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=14).average_true_range()
    
    # Объем
    df['volume_sma'] = SMAIndicator(close=df['volume'], window=20).sma_indicator()
    
    return df

def get_signal(df, sentiment=0):
    """
    Улучшенная генерация сигнала.
    BUY: 
      - EMA Fast > EMA Slow (пересечение или удержание)
      - ADX > 20 (есть тренд) и ADX_POS > ADX_NEG
      - RSI < 60 (не перекуплено)
      - Объем > Volume SMA (подтверждение объемом)
      - Sentiment >= -0.1
    SELL: 
      - EMA Fast < EMA Slow
      - ADX_NEG > ADX_POS
      - RSI > 70 (перекупленность при слабости)
      - Уход под EMA50
    """
    if df is None or len(df) < 20:
        return "HOLD"
        
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    # Состояние тренда и силы
    is_bullish = last['ema_fast'] > last['ema_slow']
    is_bearish = last['ema_fast'] < last['ema_slow']
    strong_trend = last['adx'] > 20
    bullish_trend = last['adx_pos'] > last['adx_neg']
    bearish_trend = last['adx_neg'] > last['adx_pos']
    
    above_trend = last['close'] > last['ema_trend']
    below_trend = last['close'] < (last['ema_trend'] * 0.998) 
    
    # Объем: текущий объем выше среднего за 20 свечей
    high_volume = last['volume'] > last['volume_sma']
    
    # Момент пересечения
    ema_cross_up = prev['ema_fast'] <= prev['ema_slow'] and is_bullish
    ema_cross_down = prev['ema_fast'] >= prev['ema_slow'] and is_bearish
    
    # Вход (BUY)
    if above_trend and strong_trend and bullish_trend and sentiment >= -0.1:
        # Входим на пересечении или на сильном импульсе с подтверждением объема
        if (ema_cross_up or (is_bullish and high_volume)) and last['rsi'] < 60:
            return "BUY"

    # Выход (SELL) - технический
    if ema_cross_down or (is_bearish and bearish_trend) or below_trend:
        # Если RSI очень высокий, выходим быстрее
        if last['rsi'] > 75 or is_bearish:
            return "SELL"
        
    return "HOLD"

def get_risk_exit_signal(current_price, entry_price, atr, highest_price=None):
    """
    Динамический выход по риску с использованием ATR и Трейлинг-стопа.
    ATR Multiplier: 2.0 (для стоп-лосса)
    """
    if entry_price is None or atr is None:
        return None

    # Базовый стоп-лосс по ATR (более гибкий, чем статический %)
    # Если ATR высокий (волатильность), стоп дальше
    stop_loss_distance = atr * 2.0
    initial_stop_loss = entry_price - stop_loss_distance
    
    # Трейлинг-стоп: подтягиваем стоп за ценой
    if highest_price is not None:
        trailing_stop = highest_price - (atr * 1.5)
        current_stop = max(initial_stop_loss, trailing_stop)
    else:
        current_stop = initial_stop_loss

    # Тейк-профит (можно оставить статичным или тоже привязать к ATR)
    take_profit_price = entry_price + (atr * 4.0) # Соотношение риск/прибыль 1:2

    if current_price <= current_stop:
        return "TRAILING_STOP" if highest_price and current_stop > initial_stop_loss else "STOP_LOSS_ATR"
    
    if current_price >= take_profit_price:
        return "TAKE_PROFIT_ATR"
        
    return None

async def execute_trade(signal, price, pair, sentiment_score=0, atr=None):
    """Исполняет сделку с динамическим расчетом объема по ATR."""
    if signal == "HOLD":
        return
        
    logger.info(f"Executing {signal} for {pair} at {price} (Sentiment: {sentiment_score})")
    
    from config import TRADE_QTY_MAP, TRADE_RISK_PER_TRADE_USDT
    
    # Расчет объема на основе риска и волатильности (ATR)
    # Формула: Qty = Risk_USDT / (ATR * 2.0)
    if signal == "BUY" and atr and atr > 0:
        try:
            qty = TRADE_RISK_PER_TRADE_USDT / (atr * 2.0)
            logger.info(f"Dynamic sizing for {pair}: ATR={atr}, Risk={TRADE_RISK_PER_TRADE_USDT} -> Qty={qty:.6f}")
        except Exception as e:
            logger.error(f"Error calculating dynamic qty: {e}")
            qty = TRADE_QTY_MAP.get(pair, TRADE_QTY)
    else:
        qty = TRADE_QTY_MAP.get(pair, TRADE_QTY)

    pnl = 0.0
    entry_price = None
    trade_qty = qty
    
    if signal == "SELL":
        entry_price_raw = await get_trade_state("entry_price", pair)
        stored_qty = await get_trade_state("position_qty", pair)
        if entry_price_raw is not None:
            try:
                entry_price = float(entry_price_raw)
                trade_qty = float(stored_qty) if stored_qty is not None else qty
                pnl = (price - entry_price) * trade_qty
                logger.info(f"Closed trade for {pair} with PnL: {pnl:.4f} (Entry: {entry_price}, Exit: {price}, Qty: {trade_qty})")
            except Exception as e:
                logger.error(f"Error calculating PnL for {pair}: {e}")
        else:
            logger.warning(f"SELL signal received for {pair} without entry_price in trade_state")

    await save_trade(
        pair=pair,
        side=signal,
        price=price,
        qty=trade_qty,
        pnl=pnl,
        signal=f"AGGRESSIVE_{signal}",
        sentiment=str(sentiment_score)
    )
    
    if signal == "BUY":
        await set_trade_state("current_position", "in_position", pair)
        await set_trade_state("entry_price", price, pair)
        await set_trade_state("position_qty", trade_qty, pair)
    else:
        await set_trade_state("current_position", "none", pair)
        await set_trade_state("entry_price", None, pair)
        await set_trade_state("position_qty", None, pair)

    return {"success": True, "pnl": pnl, "entry_price": entry_price, "price": price, "qty": trade_qty}
