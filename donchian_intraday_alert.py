# -*- coding: utf-8 -*-
"""唐奇安盤中預警（GitHub Actions 每 ~15 分鐘跑一次）。

現價「盤中」打到關鍵線就發 LINE：
  - 空手時：現價突破進場線（55 日高）
  - 持有時：現價跌破出場線（20 日低）
持倉狀態讀 gist（讀不到就兩條線都盯）。同型警報 12 小時內不重發。

注意：正式訊號以「日線收盤」為準，盤中觸線只是預警，
收盤前有可能又拉回去；實際動作等每日日報確認。
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

from donchian_report import fetch_daily_candles, fetch_position, fmt_price, push_line

COOLDOWN_H = 12


def get_price(symbol: str) -> float:
    r = requests.get("https://api.bitget.com/api/v2/spot/market/tickers",
                     params={"symbol": symbol}, timeout=15)
    r.raise_for_status()
    return float(r.json()["data"][0]["lastPr"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--entry", type=int, default=55)
    ap.add_argument("--exit", type=int, default=20, dest="exit_")
    ap.add_argument("--state-file", default="state/intraday_state.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    candles = fetch_daily_candles(args.symbol, bars=80)
    upper = max(x["high"] for x in candles[-args.entry:])
    lower = min(x["low"] for x in candles[-args.exit_:])
    price = get_price(args.symbol)

    # gist 只記 BTC 模型的持倉，其他幣種一律當未知（兩條線都盯）
    position = fetch_position() if args.symbol == "BTCUSDT" else None
    holding = position[0] if position is not None else None  # None = 未知，兩條都盯

    state_path = Path(args.state_file)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    now = time.time()

    def cooled(key: str) -> bool:
        return now - state.get(key, 0) > COOLDOWN_H * 3600

    # 冷卻記錄依幣種分開，共用同一個 state 檔
    entry_key = f"{args.symbol}:entry_alert_ts"
    exit_key = f"{args.symbol}:exit_alert_ts"
    exit_hint = ("記得跑 model_order.py 出場" if args.symbol == "BTCUSDT"
                 else "若有持倉記得手動出場")
    alerts: list[tuple[str, str]] = []
    if holding in (False, None) and price > upper and cooled(entry_key):
        alerts.append((entry_key,
                       f"🔔 盤中突破進場線！\n{args.symbol} 現價 {fmt_price(price)} > "
                       f"{args.entry}日高 {fmt_price(upper)}\n正式訊號以今日收盤為準；"
                       f"若收盤站穩，明早日報會提示進場。"))
    if holding in (True, None) and price < lower and cooled(exit_key):
        alerts.append((exit_key,
                       f"🔔 盤中跌破出場線！\n{args.symbol} 現價 {fmt_price(price)} < "
                       f"{args.exit_}日低 {fmt_price(lower)}\n正式訊號以今日收盤為準；"
                       f"若收盤確認跌破，{exit_hint}。"))

    status = f"{args.symbol} 現價 {fmt_price(price)}｜進場線 {fmt_price(upper)}｜" \
             f"出場線 {fmt_price(lower)}｜" \
             f"持倉 {'未知' if holding is None else ('有' if holding else '無')}"
    if not alerts:
        print(f"{status} → 無警報")
    for key, msg in alerts:
        print(f"{status} → 觸發 {key}")
        print(msg)
        if not args.dry_run:
            push_line(msg)
            state[key] = now

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")


if __name__ == "__main__":
    main()
