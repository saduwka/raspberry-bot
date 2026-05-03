import asyncio
import trade_engine
import pandas as pd
from config import TRADE_PAIRS

async def check_current_status():
    for pair in TRADE_PAIRS:
        print(f"\n--- {pair} ---")
        df = await trade_engine.fetch_ohlcv(pair)
        if df is None:
            print("Failed to fetch data")
            continue
        df = trade_engine.calc_indicators(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        print(f"Price: {last['close']}")
        print(f"EMA Fast (5): {last['ema_fast']:.2f}")
        print(f"EMA Slow (13): {last['ema_slow']:.2f}")
        print(f"EMA Trend (50): {last['ema_trend']:.2f}")
        print(f"RSI: {last['rsi']:.2f}")
        
        is_bullish = last['ema_fast'] > last['ema_slow']
        above_trend = last['close'] > last['ema_trend']
        ema_cross_up = prev['ema_fast'] <= prev['ema_slow'] and is_bullish
        
        print(f"Is Bullish (EMA5 > EMA13): {is_bullish}")
        print(f"Above Trend (Price > EMA50): {above_trend}")
        print(f"EMA Cross Up: {ema_cross_up}")
        print(f"RSI < 50: {last['rsi'] < 50}")
        
        signal = trade_engine.get_signal(df)
        print(f"Technical Signal: {signal}")

if __name__ == "__main__":
    asyncio.run(check_current_status())
