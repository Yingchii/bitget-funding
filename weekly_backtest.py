# -*- coding: utf-8 -*-
r"""每週回測週報（無狀態雲端版，跑在 GitHub Actions）。

用最新資料重跑三個策略的完整回測，與買進持有對照，發 LINE 週報。
目的：定期檢查實盤模型（唐奇安 55/20）是否仍然健康，而不是等它壞掉才發現。
邏輯與 trading-model 的引擎一致：訊號隔根生效、部位變動收單邊費用。

    python weekly_backtest.py                # 回測並發 LINE
    python weekly_backtest.py --dry-run      # 只印不發

LINE 設定沿用本 repo 慣例：環境變數 LINE_CHANNEL_ACCESS_TOKEN / LINE_TO
（GitHub Secrets），本機測試時退回讀 line_config.json。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import requests

BASE_URL = "https://api.bitget.com"
DAY_MS = 24 * 3600_000


# ---------- 資料 ----------

def fetch_daily_candles(symbol: str, bars: int) -> list[dict]:
    """回傳由舊到新的已收盤日 K：[{ts, high, low, close}, ...]"""
    rows: list[list] = []
    r = requests.get(f"{BASE_URL}/api/v2/spot/market/candles",
                     params={"symbol": symbol, "granularity": "1day", "limit": 200},
                     timeout=15)
    r.raise_for_status()
    rows = list(r.json().get("data") or [])
    if rows and int(rows[-1][0]) + DAY_MS > int(time.time() * 1000):
        rows.pop()  # 丟掉未收盤的當日 K 線

    end_time = int(rows[0][0]) - 1 if rows else int(time.time() * 1000)
    while len(rows) < bars:
        r = requests.get(f"{BASE_URL}/api/v2/spot/market/history-candles",
                         params={"symbol": symbol, "granularity": "1day",
                                 "endTime": end_time,
                                 "limit": min(200, bars - len(rows))},
                         timeout=15)
        r.raise_for_status()
        page = r.json().get("data") or []
        if not page:
            break
        rows = page + rows
        end_time = int(page[0][0]) - 1
        time.sleep(0.15)

    if not rows:
        sys.exit(f"抓不到 {symbol} 的 K 線")
    seen: dict[int, list] = {int(x[0]): x for x in rows}
    return [{"ts": ts, "high": float(v[2]), "low": float(v[3]), "close": float(v[4])}
            for ts, v in sorted(seen.items())]


# ---------- 策略（與 trading-model/strategies.py 同邏輯） ----------

def strat_donchian(candles: list[dict], entry: int = 55, exit_: int = 20) -> list[float]:
    pos, holding = [], False
    for i, c in enumerate(candles):
        upper = max(x["high"] for x in candles[i - entry:i]) if i >= entry else None
        lower = min(x["low"] for x in candles[i - exit_:i]) if i >= exit_ else None
        if not holding and upper is not None and c["close"] > upper:
            holding = True
        elif holding and lower is not None and c["close"] < lower:
            holding = False
        pos.append(1.0 if holding else 0.0)
    return pos


def strat_sma_cross(candles: list[dict], fast: int = 20, slow: int = 60) -> list[float]:
    closes = [c["close"] for c in candles]
    pos = []
    for i in range(len(closes)):
        if i + 1 < slow:
            pos.append(0.0)
            continue
        fast_ma = sum(closes[i + 1 - fast:i + 1]) / fast
        slow_ma = sum(closes[i + 1 - slow:i + 1]) / slow
        pos.append(1.0 if fast_ma > slow_ma else 0.0)
    return pos


def strat_rsi_reversion(candles: list[dict], period: int = 14,
                        buy_below: float = 30, sell_above: float = 55) -> list[float]:
    closes = [c["close"] for c in candles]
    alpha = 1 / period
    gain_ema = loss_ema = 0.0
    pos, holding = [0.0], False
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain_ema += alpha * (max(delta, 0) - gain_ema)
        loss_ema += alpha * (max(-delta, 0) - loss_ema)
        rsi = 100 - 100 / (1 + gain_ema / (loss_ema or 1e-12))
        if not holding and rsi < buy_below:
            holding = True
        elif holding and rsi > sell_above:
            holding = False
        pos.append(1.0 if holding else 0.0)
    return pos


STRATEGIES = {
    "唐奇安 55/20": strat_donchian,
    "均線交叉 20/60": strat_sma_cross,
    "RSI 逆勢 14": strat_rsi_reversion,
}


# ---------- 回測引擎（與 trading-model/backtest.py 同邏輯的純 Python 版） ----------

def run_backtest(closes: list[float], position: list[float], fee: float) -> dict:
    """訊號隔一根生效；部位每變動 1 單位收一次單邊費用。回傳指標 dict。"""
    n = len(closes)
    equity, eq = [], 1.0
    prev_pos = 0.0
    rets = []
    trades: list[float] = []          # 每筆交易淨報酬
    entry_price = None
    for i in range(n):
        pos_eff = position[i - 1] if i >= 1 else 0.0
        ret = closes[i] / closes[i - 1] - 1 if i >= 1 else 0.0
        turnover = abs(pos_eff - prev_pos)
        strat_ret = pos_eff * ret - turnover * fee
        eq *= 1 + strat_ret
        equity.append(eq)
        rets.append(strat_ret)
        # 逐筆進出場價取「部位生效那根」的收盤，與 trading-model/backtest.py 一致
        if prev_pos == 0.0 and pos_eff > 0.0:
            entry_price = closes[i]
        elif prev_pos > 0.0 and pos_eff == 0.0 and entry_price:
            gross = closes[i] / entry_price - 1
            trades.append((1 + gross) * (1 - fee) ** 2 - 1)
            entry_price = None
        prev_pos = pos_eff
    if entry_price:  # 尚未平倉的部位以最後收盤價結算
        gross = closes[-1] / entry_price - 1
        trades.append((1 + gross) * (1 - fee) ** 2 - 1)

    years = n / 365.0
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    vol = math.sqrt(var) * math.sqrt(365)
    peak, maxdd, dd_now = 0.0, 0.0, 0.0
    for e in equity:
        peak = max(peak, e)
        dd_now = e / peak - 1
        maxdd = min(maxdd, dd_now)
    return {
        "equity": equity,
        "total": eq - 1,
        "cagr": eq ** (1 / years) - 1 if years > 0 else 0.0,
        "sharpe": mean * 365 / vol if vol > 0 else 0.0,
        "maxdd": maxdd,
        "dd_now": dd_now,
        "n_trades": len(trades),
        "win_rate": sum(1 for t in trades if t > 0) / len(trades) if trades else 0.0,
        "in_market": position[-1] > 0,
    }


def window_return(equity: list[float], days: int) -> float | None:
    if len(equity) <= days:
        return None
    return equity[-1] / equity[-1 - days] - 1


# ---------- LINE ----------

def push_line(text: str) -> None:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    to = os.environ.get("LINE_TO", "").strip()
    if not token:
        cfg_path = Path(__file__).with_name("line_config.json")
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            token = str(cfg.get("channel_access_token", "")).strip()
            to = to or str(cfg.get("to", "")).strip()
    if not token:
        sys.exit("找不到 LINE token（環境變數或 line_config.json）")

    endpoint = f"https://api.line.me/v2/bot/message/{'push' if to else 'broadcast'}"
    payload: dict = {"messages": [{"type": "text", "text": text[:4900]}]}
    if to:
        payload["to"] = to
    r = requests.post(endpoint, headers={"Authorization": f"Bearer {token}"},
                      json=payload, timeout=15)
    if r.status_code != 200:
        sys.exit(f"LINE 推送失敗 HTTP {r.status_code}: {r.text[:300]}")


# ---------- 主程式 ----------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--bars", type=int, default=1000, help="回測日 K 根數")
    ap.add_argument("--fee", type=float, default=0.001, help="單邊手續費+滑價")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    candles = fetch_daily_candles(args.symbol, args.bars)
    closes = [c["close"] for c in candles]
    # K 線以 UTC+8 午夜切日，ts 是開盤時間：+8h 即為該日 K 的台灣日期
    d0 = time.strftime("%Y/%m/%d", time.gmtime(candles[0]["ts"] / 1000 + 8 * 3600))
    d1 = time.strftime("%m/%d", time.gmtime(candles[-1]["ts"] / 1000 + 8 * 3600))

    bh = run_backtest(closes, [1.0] * len(closes), fee=0.0)
    lines = [f"📊 每週回測 {args.symbol}（{d0}~{d1}，{len(candles)} 根日K）"]
    for name, fn in STRATEGIES.items():
        m = run_backtest(closes, fn(candles), fee=args.fee)
        w90 = window_return(m["equity"], 90)
        live = "（實盤模型）" if name.startswith("唐奇安") else ""
        lines += [
            f"— {name}{live} —",
            f"總報酬 {m['total']:+.0%}｜CAGR {m['cagr']:+.1%}｜Sharpe {m['sharpe']:.2f}",
            f"最大回撤 {m['maxdd']:.0%}（目前 {m['dd_now']:.0%}）"
            f"｜{m['n_trades']} 筆勝率 {m['win_rate']:.0%}",
            f"近90天 {w90:+.1%}｜目前{'做多' if m['in_market'] else '空手'}"
            if w90 is not None else f"目前{'做多' if m['in_market'] else '空手'}",
        ]
    w90 = window_return(bh["equity"], 90)
    lines += [
        "— 買進持有 —",
        f"總報酬 {bh['total']:+.0%}｜CAGR {bh['cagr']:+.1%}｜最大回撤 {bh['maxdd']:.0%}",
        f"近90天 {w90:+.1%}" if w90 is not None else "",
    ]
    msg = "\n".join(x for x in lines if x)

    print(msg)
    if args.dry_run:
        print("（未發送）")
        return
    push_line(msg)
    print("已發 LINE。")


if __name__ == "__main__":
    main()
