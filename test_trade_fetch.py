import asyncio
import ccxt.async_support as ccxt
import pandas as pd
import os
from dotenv import load_dotenv

load_dotenv()

async def test():
    symbol = os.getenv("TRADE_PAIR", "BTC/USDT")
    timeframe = os.getenv("TRADE_TIMEFRAME", "15m")
    paper = os.getenv("PAPER_MODE", "True").lower() == "true"
    
    print(f"Testing fetch for {symbol} on {timeframe} (Paper: {paper})")
    
    exchange = ccxt.binance({
        'apiKey': os.getenv("BINANCE_API_KEY"),
        'secret': os.getenv("BINANCE_SECRET"),
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })
    
    try:
        if paper:
            print("Enabling Sandbox mode...")
            exchange.set_sandbox_mode(True)
            
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=10)
        print("Success! First row:")
        print(ohlcv[0])
    except Exception as e:
        print(f"FAILED: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(test())
