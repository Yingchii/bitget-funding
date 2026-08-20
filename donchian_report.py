# -*- coding: utf-8 -*-
"""唐奇安通道每日 LINE 日報（無狀態雲端版，跑在 GitHub Actions）。

只通知、不下單。與本機 trading-model 的差異：這裡看不到本機的持倉記錄檔，
所以只報模型狀態與進出場線；訊號「翻轉當天」會加 ⚠️ 提醒。
實際進出場請在本機跑 trading-exec\\model_order.py。

    python donchian_report.py                # 算訊號並發 LINE
    python donchian_report.py --dry-run      # 只印不發
    python donchian_report.py --only-action  # 翻轉當天才發
    python donchian_report.py --state-file state/report_state.json
        # 同一日 K + 同一動作只發一次，外部 cron 與 GitHub 排程重複觸發也不會重複發

LINE 設定沿用本 repo 慣例：環境變數 LINE_CHANNEL_ACCESS_TOKEN / LINE_TO
（GitHub Secrets），本機測試時退回讀 line_config.json。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE_URL = "https://api.bitget.com"
DAY_MS = 24 * 3600_000
FEE_RATE = 0.001  # 單邊手續費（Bitget 現貨 0.1%；BGB 折抵會更低，保守取整）


def fmt_price(v: float) -> str:
    """BTC 等大價位取整；HYPE 等小價位保留小數。"""
    if v >= 1000:
        return f"{v:,.0f}"
    return f"{v:,.2f}" if v >= 1 else f"{v:.4g}"


def fetch_daily_candles(symbol: str, bars: int = 300) -> list[dict]:
    """回傳由舊到新的已收盤日 K：[{ts, high, low, close}, ...]"""
    rows: list[list] = []
    r = requests.get(f"{BASE_URL}/api/v2/spot/market/candles",
                     params={"symbol": symbol, "granularity": "1day", "limit": 200},
                     timeout=15)
    r.raise_for_status()
    data = r.json().get("data") or []
    rows = list(data)
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


def donchian_positions(candles: list[dict], entry: int, exit_: int) -> list[bool]:
    """與 trading-model/strategies.py 的 donchian_breakout 同邏輯（視窗不含當根）。"""
    pos, holding = [], False
    for i, c in enumerate(candles):
        upper = max(x["high"] for x in candles[i - entry:i]) if i >= entry else None
        lower = min(x["low"] for x in candles[i - exit_:i]) if i >= exit_ else None
        if not holding and upper is not None and c["close"] > upper:
            holding = True
        elif holding and lower is not None and c["close"] < lower:
            holding = False
        pos.append(holding)
    return pos


def fetch_position() -> tuple[bool, float, float] | None:
    """從 secret gist 讀本機同步上來的 (holding, size, avg_price)；讀不到回 None。"""
    gid = os.environ.get("POSITION_GIST_ID", "").strip()
    if not gid:
        return None
    try:
        r = requests.get(
            f"https://gist.githubusercontent.com/Yingchii/{gid}/raw/model_position.json",
            timeout=15)
        if r.status_code != 200:
            print(f"！讀持倉 gist 失敗 HTTP {r.status_code}", file=sys.stderr)
            return None
        st = r.json()
        return (bool(st.get("holding")), float(st.get("size", 0)),
                float(st.get("avg_price", 0)))
    except Exception as e:
        print(f"！讀持倉 gist 失敗：{e}", file=sys.stderr)
        return None


def fetch_mvrv() -> tuple[float, float | None] | None:
    """CoinMetrics 免費 API 抓 BTC MVRV，回 (最新值, 前一日值)；失敗回 None。

    最新一兩天的值可能還是 null（他們算得慢），取最近的非空值。
    """
    try:
        start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 86400))
        r = requests.get(
            "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics",
            params={"assets": "btc", "metrics": "CapMVRVCur", "frequency": "1d",
                    "sort": "time", "page_size": 10, "start_time": start},
            timeout=15)
        if r.status_code != 200:
            print(f"！MVRV 抓取失敗 HTTP {r.status_code}", file=sys.stderr)
            return None
        vals = [float(row["CapMVRVCur"]) for row in r.json().get("data", [])
                if row.get("CapMVRVCur") is not None]
        if not vals:
            return None
        return vals[-1], (vals[-2] if len(vals) >= 2 else None)
    except Exception as e:
        print(f"！MVRV 抓取失敗：{e}", file=sys.stderr)
        return None


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--entry", type=int, default=55)
    ap.add_argument("--exit", type=int, default=20, dest="exit_")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only-action", action="store_true")
    ap.add_argument("--state-file", default="",
                    help="去重記錄檔；同一日 K + 同一動作只發一次")
    args = ap.parse_args()

    candles = fetch_daily_candles(args.symbol)
    pos = donchian_positions(candles, args.entry, args.exit_)
    last = candles[-1]
    upper = max(x["high"] for x in candles[-args.entry - 1:-1])
    lower = min(x["low"] for x in candles[-args.exit_ - 1:-1])
    # K 線以 UTC+8 午夜切日，ts 是開盤時間：+8h 即為該日 K 的台灣日期
    date = time.strftime("%m/%d", time.gmtime(last["ts"] / 1000 + 8 * 3600))

    # gist 只記 BTC 模型的持倉，其他幣種一律當未知
    position = fetch_position() if args.symbol == "BTCUSDT" else None
    lines = [
        f"📈 唐奇安 {args.symbol} 日報（{date} 收盤・雲端）",
        f"狀態：{'做多' if pos[-1] else '空手'}｜收盤 {fmt_price(last['close'])}",
        f"進場線 {fmt_price(upper)}｜出場線 {fmt_price(lower)}",
    ]
    if position is not None:
        holding, size, avg_price = position
        if holding and avg_price > 0:
            # 現在出場的淨損益：扣進場費＋出場費各一次
            net = last["close"] * (1 - FEE_RATE) / (avg_price * (1 + FEE_RATE)) - 1
            lines.append(f"持倉：{size:.6f} 顆｜均價 {fmt_price(avg_price)}"
                         f"（含費 {net:+.1%}）")
        else:
            lines.append(f"持倉：{f'{size:.6f} 顆' if holding else '無'}")
        if pos[-1] and not holding:
            action = "⚠️ 模型做多但未持倉 → 考慮在本機跑 model_order.py 進場"
        elif not pos[-1] and holding:
            action = f"⚠️ 模型翻空手但仍持有 {size:.6f} 顆 → 考慮在本機跑 model_order.py 出場"
        else:
            action = "✅ 與持倉一致，不需動作"
    else:
        flipped = len(pos) >= 2 and pos[-1] != pos[-2]
        # model_order.py 的持倉記錄只跟 BTC 綁定，其他幣種提示手動下單
        tool = ("請在本機跑 model_order.py " if args.symbol == "BTCUSDT"
                else "請自行在本機手動")
        if flipped and pos[-1]:
            action = f"⚠️ 今日翻多 → 若要跟單，{tool}進場"
        elif flipped:
            action = f"⚠️ 今日翻空手 → 若仍持倉，{tool}出場"
        else:
            action = "今日無翻轉"
    mvrv = fetch_mvrv() if args.symbol == "BTCUSDT" else None
    if mvrv is not None:
        cur, prev = mvrv
        trend = f"，前日 {prev:.2f}" if prev is not None else ""
        lines.append(f"鏈上 MVRV：{cur:.2f}{trend}（>3 過熱 / <1 低估）")
    lines.append(action)
    msg = "\n".join(lines)

    print(msg)
    need_action = action.startswith("⚠️")
    if args.dry_run or (args.only_action and not need_action):
        print("（未發送）")
        return

    # 去重：外部 cron 與 GitHub 排程可能都觸發同一天的同一則通報
    state_path = Path(args.state_file) if args.state_file else None
    key = f"{args.symbol}|{date}|{action}"
    if state_path and state_path.exists():
        try:
            if json.loads(state_path.read_text(encoding="utf-8")).get("last_key") == key:
                print("（同一則通報已發過，略過）")
                return
        except Exception as e:
            print(f"！去重記錄讀取失敗，照常發送：{e}", file=sys.stderr)

    push_line(msg)
    print("已發 LINE。")
    if state_path:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"last_key": key}, ensure_ascii=False),
                              encoding="utf-8")


if __name__ == "__main__":
    main()
