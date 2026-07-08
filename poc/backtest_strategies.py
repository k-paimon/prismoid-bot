"""
Backtest proof-of-concept for the three compiled strategies:

    grid_strike   (generic)              -> GridExecutor simulation
    pmm_simple    (market_making)        -> PositionExecutor simulation
    supertrend_v1 (directional_trading)  -> PositionExecutor simulation, pandas_ta signal

Uses hummingbot's BacktestingEngineBase exactly the way services/backtesting_service.py
does, but imports the controllers from bare-features/strategies instead of bots/.
Candles are fetched live from Binance by the engine's BacktestingDataProvider.

Run inside the API image (needs hummingbot + internet):

    docker run --rm -v ${PWD}:/work -w /work/bare-features/poc `
        --entrypoint python hummingbot/hummingbot-api:latest backtest_strategies.py

Optional args: --pair BTC-USDT --connector binance_perpetual --hours 24 --quote 1000
"""
import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request

# Make the compiled controllers importable as the "controllers" package
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategies"))

from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase  # noqa: E402

CONTROLLERS_MODULE = "controllers"
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")


def fetch_anchor_price(pair: str, start_ms: int) -> float:
    """First 1m close at the backtest start, from Binance futures public klines."""
    symbol = pair.replace("-", "")
    url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}"
           f"&interval=1m&startTime={start_ms}&limit=1")
    with urllib.request.urlopen(url, timeout=30) as resp:
        kline = json.loads(resp.read())[0]
    return float(kline[4])


def build_configs(connector: str, pair: str, anchor: float, quote: float) -> dict:
    """One representative config per strategy, grid bounds anchored to the market."""
    return {
        "grid_strike": {
            "id": "poc-grid-strike",
            "controller_type": "generic",
            "controller_name": "grid_strike",
            "connector_name": connector,
            "trading_pair": pair,
            "side": 1,  # TradeType.BUY
            "start_price": round(anchor * 0.97, 2),
            "end_price": round(anchor * 1.03, 2),
            "limit_price": round(anchor * 0.94, 2),
            "total_amount_quote": quote,
            "min_spread_between_orders": 0.001,
            "min_order_amount_quote": 10,
            "max_open_orders": 3,
            "order_frequency": 10,
            "leverage": 20,
        },
        "pmm_simple": {
            "id": "poc-pmm-simple",
            "controller_type": "market_making",
            "controller_name": "pmm_simple",
            "connector_name": connector,
            "trading_pair": pair,
            "total_amount_quote": quote,
            "buy_spreads": [0.001, 0.003],
            "sell_spreads": [0.001, 0.003],
            "buy_amounts_pct": [1, 1],
            "sell_amounts_pct": [1, 1],
            "executor_refresh_time": 300,
            "cooldown_time": 15,
            "leverage": 20,
            "stop_loss": 0.02,
            "take_profit": 0.01,
            "time_limit": 2700,
        },
        "supertrend_v1": {
            "id": "poc-supertrend-v1",
            "controller_type": "directional_trading",
            "controller_name": "supertrend_v1",
            "connector_name": connector,
            "trading_pair": pair,
            "candles_connector": connector,
            "candles_trading_pair": pair,
            "interval": "3m",
            "length": 20,
            "multiplier": 4.0,
            "percentage_threshold": 0.01,
            "total_amount_quote": quote,
            "max_executors_per_side": 2,
            "cooldown_time": 300,
            "leverage": 20,
            "stop_loss": 0.03,
            "take_profit": 0.02,
            "time_limit": 2700,
        },
    }


async def run_one(engine: BacktestingEngineBase, name: str, config: dict,
                  start: int, end: int) -> dict:
    controller_config = engine.get_controller_config_instance_from_dict(
        config_data=config, controllers_module=CONTROLLERS_MODULE)
    backtest = await engine.run_backtesting(
        controller_config=controller_config,
        trade_cost=0.0006,
        start=start,
        end=end,
        backtesting_resolution="1m",
    )
    results = backtest["results"]
    executors = backtest["executors"]
    filled = [e for e in executors if e.filled_amount_quote and float(e.filled_amount_quote) > 0]
    summary = {
        "strategy": name,
        "executors_created": len(executors),
        "executors_filled": len(filled),
        "net_pnl_quote": round(float(results.get("net_pnl_quote", 0)), 4),
        "net_pnl_pct": round(float(results.get("net_pnl", 0)) * 100, 4),
        "total_volume_quote": round(float(results.get("total_volume", 0)), 2),
        "max_drawdown_usd": round(float(results.get("max_drawdown_usd", 0)), 4),
        "sharpe_ratio": round(float(results.get("sharpe_ratio") or 0), 4),
        "accuracy": round(float(results.get("accuracy", 0)), 4),
        "win_signals": results.get("win_signals", 0),
        "loss_signals": results.get("loss_signals", 0),
    }
    os.makedirs(RESULTS_DIR, exist_ok=True)
    detail_path = os.path.join(RESULTS_DIR, f"{name}.json")
    with open(detail_path, "w", encoding="utf-8") as f:
        json.dump({
            "config": config,
            "summary": summary,
            "executors": [e.to_dict() for e in executors],
        }, f, indent=2, default=str)
    summary["detail_file"] = os.path.relpath(detail_path)
    return summary


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--connector", default="binance_perpetual")
    parser.add_argument("--pair", default="BTC-USDT")
    parser.add_argument("--hours", type=int, default=24, help="backtest window length")
    parser.add_argument("--quote", type=float, default=1000, help="total amount quote per strategy")
    parser.add_argument("--only", default=None, help="run a single strategy by name")
    args = parser.parse_args()

    end = int(time.time()) - 3600          # up to one hour ago
    start = end - args.hours * 3600
    anchor = fetch_anchor_price(args.pair, start * 1000)
    print(f"Backtest window: {args.hours}h ending 1h ago | {args.connector} {args.pair} "
          f"| anchor price at start: {anchor}")

    configs = build_configs(args.connector, args.pair, anchor, args.quote)
    if args.only:
        configs = {args.only: configs[args.only]}

    summaries = []
    for name, config in configs.items():
        print(f"\n=== backtesting {name} ===")
        try:
            summary = await run_one(BacktestingEngineBase(), name, config, start, end)
            summaries.append(summary)
            for k, v in summary.items():
                print(f"  {k:>20}: {v}")
        except Exception as e:
            summaries.append({"strategy": name, "error": f"{type(e).__name__}: {e}"})
            print(f"  FAILED: {type(e).__name__}: {e}")

    print("\n" + "=" * 60)
    failures = [s for s in summaries if "error" in s]
    print(f"SUMMARY: {len(summaries) - len(failures)}/{len(summaries)} strategies backtested OK")
    for s in summaries:
        if "error" in s:
            print(f"  {s['strategy']}: ERROR {s['error']}")
        else:
            print(f"  {s['strategy']}: pnl {s['net_pnl_quote']} quote ({s['net_pnl_pct']}%), "
                  f"{s['executors_created']} executors ({s['executors_filled']} filled)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
