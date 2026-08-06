#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bitget 資金費率套利掃描器 (funding rate arbitrage scanner)

掃描 Bitget USDT 本位永續合約，找出「歷史資金費率持續偏高 + 現貨深度足夠」
的幣種，並計算扣掉手續費之後的預估年化報酬。

策略前提（正費率模式）：
    現貨買入 N USDT 的幣  +  同幣種永續合約開 N USDT 空單
    → delta 中性，收益來源只有每期的資金費

只用公開行情 API，不需要 API key，不會下任何單。

用法：
    py -X utf8 bitget_funding_scan.py
    py -X utf8 bitget_funding_scan.py --min-rate 0.0003 --hold-days 5 --top 30
    py -X utf8 bitget_funding_scan.py --negative          # 掃深度負費率（反向做法）
    py -X utf8 bitget_funding_scan.py --csv out.csv --json out.json

LINE 通報：
    py -X utf8 bitget_funding_scan.py --test-notify       # 先測通道
    py -X utf8 bitget_funding_scan.py --notify --watch 30 --quiet
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field
from pathlib import Path
from statistics import median
from typing import Any

try:
    import requests
except ImportError:
    sys.exit("需要 requests 套件：py -m pip install requests")

BASE = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"
DAY_MS = 86_400_000

LINE_API = "https://api.line.me/v2/bot"
DEFAULT_CONFIG = Path(__file__).with_name("line_config.json")
DEFAULT_STATE = Path(__file__).with_name(".funding_state.json")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Content-Type": "application/json",
        "locale": "zh-CN",
        "User-Agent": "funding-scanner/1.0",
    })
    return s


def api_get(session: requests.Session, path: str, params: dict | None = None,
            retries: int = 4) -> Any:
    """呼叫 Bitget 公開端點，處理 429 與業務錯誤碼。"""
    url = BASE + path
    delay = 0.5
    last_err = ""
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=15)
        except requests.RequestException as e:
            last_err = f"連線錯誤: {e}"
        else:
            if r.status_code == 429:
                last_err = "429 rate limited"
            elif r.status_code != 200:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            else:
                try:
                    body = r.json()
                except ValueError:
                    last_err = f"回應非 JSON: {r.text[:200]}"
                else:
                    if body.get("code") == "00000":
                        return body.get("data")
                    last_err = f"code={body.get('code')} msg={body.get('msg')}"
        time.sleep(delay)
        delay *= 2
    raise RuntimeError(f"{path} 失敗（重試 {retries} 次）：{last_err}")


def to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# 資料抓取
# --------------------------------------------------------------------------

def fetch_futures_tickers(session) -> dict[str, dict]:
    data = api_get(session, "/api/v2/mix/market/tickers",
                   {"productType": PRODUCT_TYPE}) or []
    return {d["symbol"]: d for d in data}


def fetch_contracts(session) -> dict[str, dict]:
    data = api_get(session, "/api/v2/mix/market/contracts",
                   {"productType": PRODUCT_TYPE}) or []
    return {d["symbol"]: d for d in data}


def fetch_spot_tickers(session) -> dict[str, dict]:
    data = api_get(session, "/api/v2/spot/market/tickers") or []
    return {d["symbol"]: d for d in data}


def fetch_funding_history(session, symbol: str, periods: int) -> list[float]:
    """回傳最近 N 期資金費率（新→舊）。"""
    rates: list[float] = []
    page = 1
    while len(rates) < periods and page <= 3:
        data = api_get(session, "/api/v2/mix/market/history-fund-rate", {
            "symbol": symbol,
            "productType": PRODUCT_TYPE,
            "pageSize": 100,
            "pageNo": page,
        }) or []
        if not data:
            break
        rates.extend(to_float(d.get("fundingRate")) for d in data)
        if len(data) < 100:
            break
        page += 1
    return rates[:periods]


def fetch_spot_depth(session, symbol: str, max_slippage: float) -> float:
    """回傳現貨賣單簿在 max_slippage 之內可以吃掉多少 USDT。"""
    data = api_get(session, "/api/v2/spot/market/orderbook", {
        "symbol": symbol, "type": "step0", "limit": 150,
    }) or {}
    asks = data.get("asks") or []
    if not asks:
        return 0.0
    best = to_float(asks[0][0])
    if best <= 0:
        return 0.0
    limit_price = best * (1 + max_slippage)
    total = 0.0
    for row in asks:
        price, size = to_float(row[0]), to_float(row[1])
        if price > limit_price:
            break
        total += price * size
    return total


# --------------------------------------------------------------------------
# 計算
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    symbol: str
    interval_h: float
    current_rate: float          # 當期費率（小數）
    median_rate: float           # 近 N 期中位數
    mean_rate: float
    min_rate_seen: float
    hits: int                    # 近 N 期中達標的期數
    samples: int
    gross_apr_notional: float    # 名目年化（未扣費）
    net_apr_notional: float      # 名目年化（扣手續費，攤在 hold_days）
    net_apr_capital: float       # 以總投入資本計的年化
    breakeven_periods: float     # 需要幾期資金費才回本
    spot_depth_usdt: float
    spot_vol_24h: float
    open_interest_usdt: float
    basis_pct: float             # (合約-現貨)/現貨
    change_24h_pct: float
    age_days: float
    flags: str = ""

    def flag_list(self) -> list[str]:
        return [f for f in self.flags.split(",") if f]


def annualize(rate_per_period: float, interval_h: float) -> float:
    periods_per_year = 24.0 / interval_h * 365.0
    return rate_per_period * periods_per_year


def evaluate(symbol: str, rates: list[float], interval_h: float,
             cfg: argparse.Namespace, sign: float) -> tuple[float, float, float, float, float, int]:
    """回傳 (median, mean, min_seen, gross_apr, breakeven, hits)。

    sign = +1 收正費率（現貨多+合約空）、-1 收負費率（合約多+現貨空）。
    所有費率先乘上 sign，讓「對我有利」永遠是正數。
    """
    signed = [r * sign for r in rates]
    med = median(signed)
    mean = sum(signed) / len(signed)
    lo = min(signed)
    hits = sum(1 for r in signed if r >= cfg.min_rate)

    # 用中位數估算，比平均值更抗插針
    gross_apr = annualize(med, interval_h)

    roundtrip_fee = 2 * cfg.spot_fee + 2 * cfg.futures_fee
    breakeven = roundtrip_fee / med if med > 0 else float("inf")
    return med, mean, lo, gross_apr, breakeven, hits


def net_returns(med_rate: float, interval_h: float,
                cfg: argparse.Namespace) -> tuple[float, float]:
    """扣掉一趟來回手續費後的名目年化 / 資本年化。"""
    periods_per_day = 24.0 / interval_h
    gross_over_hold = med_rate * periods_per_day * cfg.hold_days
    roundtrip_fee = 2 * cfg.spot_fee + 2 * cfg.futures_fee
    net_over_hold = gross_over_hold - roundtrip_fee
    net_apr_notional = net_over_hold / cfg.hold_days * 365.0
    net_apr_capital = net_apr_notional / cfg.capital_per_notional
    return net_apr_notional, net_apr_capital


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def scan(cfg: argparse.Namespace) -> list[Candidate]:
    session = make_session()
    sign = -1.0 if cfg.negative else 1.0
    now_ms = time.time() * 1000

    print("抓取合約行情 / 合約規格 / 現貨行情 ...", file=sys.stderr)
    fut = fetch_futures_tickers(session)
    contracts = fetch_contracts(session)
    spot = fetch_spot_tickers(session)
    print(f"  合約 {len(fut)} 檔、現貨 {len(spot)} 檔", file=sys.stderr)

    # --- 第一關：當期費率 + 現貨存在 + 現貨成交量 ---------------------------
    stage1: list[str] = []
    for sym, t in fut.items():
        c = contracts.get(sym)
        if not c or c.get("symbolStatus") != "normal":
            continue
        if sym not in spot:
            continue                                    # 沒現貨腿，做不了對沖
        if to_float(spot[sym].get("usdtVolume")) < cfg.min_spot_vol:
            continue
        cur = to_float(t.get("fundingRate")) * sign
        if cur < cfg.min_rate * cfg.current_relax:
            continue
        stage1.append(sym)

    print(f"第一關通過 {len(stage1)} 檔，開始查歷史費率 ...", file=sys.stderr)
    if not stage1:
        return []

    # --- 第二關：歷史費率一致性 --------------------------------------------
    history: dict[str, list[float]] = {}
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futs = {pool.submit(fetch_funding_history, session, s, cfg.periods): s
                for s in stage1}
        for i, f in enumerate(as_completed(futs), 1):
            sym = futs[f]
            try:
                history[sym] = f.result()
            except Exception as e:
                print(f"  ! {sym} 歷史費率失敗: {e}", file=sys.stderr)
            if i % 20 == 0:
                print(f"  ... {i}/{len(stage1)}", file=sys.stderr)

    stage2: dict[str, tuple] = {}
    for sym, rates in history.items():
        if len(rates) < cfg.min_samples:
            continue
        interval_h = to_float(contracts[sym].get("fundInterval"), 8.0) or 8.0
        med, mean, lo, gross, be, hits = evaluate(sym, rates, interval_h, cfg, sign)
        if med < cfg.min_median:
            continue
        if hits / len(rates) < cfg.consistency:
            continue
        if be > cfg.max_breakeven:
            continue
        stage2[sym] = (rates, interval_h, med, mean, lo, gross, be, hits)

    print(f"第二關通過 {len(stage2)} 檔，開始查現貨深度 ...", file=sys.stderr)
    if not stage2:
        return []

    # --- 第三關：現貨深度 ---------------------------------------------------
    depths: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futs = {pool.submit(fetch_spot_depth, session, s, cfg.max_slippage): s
                for s in stage2}
        for f in as_completed(futs):
            sym = futs[f]
            try:
                depths[sym] = f.result()
            except Exception as e:
                print(f"  ! {sym} 深度失敗: {e}", file=sys.stderr)
                depths[sym] = 0.0

    # --- 組裝結果 -----------------------------------------------------------
    out: list[Candidate] = []
    for sym, (rates, interval_h, med, mean, lo, gross, be, hits) in stage2.items():
        depth = depths.get(sym, 0.0)
        if depth < cfg.min_depth:
            continue

        t, c, sp = fut[sym], contracts[sym], spot[sym]
        mark = to_float(t.get("markPrice")) or to_float(t.get("lastPr"))
        spot_px = to_float(sp.get("lastPr"))
        basis = (mark - spot_px) / spot_px if spot_px else 0.0
        oi_usdt = to_float(t.get("holdingAmount")) * mark
        chg = to_float(t.get("change24h")) * 100
        launch = to_float(c.get("launchTime"))
        age = (now_ms - launch) / DAY_MS if launch > 0 else float("inf")

        net_n, net_c = net_returns(med, interval_h, cfg)
        if net_c < cfg.min_net_apr:
            continue

        flags = []
        if age < 30:
            flags.append("NEW")                          # 新上線，費率不穩、易插針
        if depth < cfg.min_depth * 3:
            flags.append("THIN")                         # 深度勉強，注意平倉滑價
        if abs(chg) > 20:
            flags.append("VOL")                          # 24h 劇烈波動
        if oi_usdt < cfg.min_oi:
            flags.append("LOWOI")                        # 未平倉量小，容易被操縱
        if lo < 0:
            flags.append("NEG")                          # 期間內曾出現對你不利的費率

        out.append(Candidate(
            symbol=sym,
            interval_h=interval_h,
            current_rate=to_float(t.get("fundingRate")) * sign,
            median_rate=med, mean_rate=mean, min_rate_seen=lo,
            hits=hits, samples=len(rates),
            gross_apr_notional=gross,
            net_apr_notional=net_n,
            net_apr_capital=net_c,
            breakeven_periods=be,
            spot_depth_usdt=depth,
            spot_vol_24h=to_float(sp.get("usdtVolume")),
            open_interest_usdt=oi_usdt,
            basis_pct=basis * 100,
            change_24h_pct=chg,
            age_days=age,
            flags=",".join(flags),
        ))

    out.sort(key=lambda x: x.net_apr_capital, reverse=True)
    return out


# --------------------------------------------------------------------------
# 輸出
# --------------------------------------------------------------------------

def human(n: float) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def print_table(rows: list[Candidate], cfg: argparse.Namespace) -> None:
    mode = "負費率（合約做多 + 現貨借幣賣出）" if cfg.negative else "正費率（現貨買入 + 合約做空）"
    print()
    print(f"模式：{mode}")
    print(f"假設：持有 {cfg.hold_days} 天、現貨費率 {cfg.spot_fee*100:.3f}%、"
          f"合約費率 {cfg.futures_fee*100:.3f}%、資本 = 名目 × {cfg.capital_per_notional}")
    print(f"取樣：近 {cfg.periods} 期資金費率　時間：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    if not rows:
        print("沒有符合條件的標的。可以放寬 --min-rate / --min-median / --min-net-apr 再試。")
        return

    hdr = (f"{'SYMBOL':<16}{'INT':>4}{'NOW%':>8}{'MED%':>8}{'HIT':>8}"
           f"{'淨年化(資本)':>14}{'回本期':>8}{'深度':>9}{'現貨量':>9}{'OI':>9}"
           f"{'基差%':>8}  FLAGS")
    print(hdr)
    print("-" * 118)

    for r in rows[:cfg.top]:
        print(f"{r.symbol:<16}"
              f"{r.interval_h:>3.0f}h"
              f"{r.current_rate*100:>8.4f}"
              f"{r.median_rate*100:>8.4f}"
              f"{f'{r.hits}/{r.samples}':>8}"
              f"{r.net_apr_capital*100:>13.1f}%"
              f"{r.breakeven_periods:>8.1f}"
              f"{human(r.spot_depth_usdt):>9}"
              f"{human(r.spot_vol_24h):>9}"
              f"{human(r.open_interest_usdt):>9}"
              f"{r.basis_pct:>8.2f}"
              f"  {r.flags}")

    print()
    print("欄位：INT=結算間隔　NOW%=當期費率　MED%=近N期中位數　HIT=達標期數／取樣數")
    print(f"　　　深度=現貨賣單簿 {cfg.max_slippage*100:.1f}% 內可買金額　"
          f"回本期=需收幾期資金費才蓋過來回手續費")
    print("FLAGS：NEW=上線未滿30天　THIN=深度勉強　VOL=24h波動>20%　"
          "LOWOI=未平倉量偏低　NEG=期間內出現過反向費率")
    print()
    print("提醒：務必開啟統一帳戶/組合保證金，讓現貨計入保證金；合約端槓桿不要超過 2~3x。")
    print("　　　此腳本只讀公開行情，不下單，也不構成投資建議。")


# --------------------------------------------------------------------------
# LINE 通報
# --------------------------------------------------------------------------

def load_line_config(cfg: argparse.Namespace) -> tuple[str, str]:
    """回傳 (channel_access_token, to)。to 為空字串代表用 broadcast。

    優先序：環境變數 > 設定檔。
    """
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    to = os.environ.get("LINE_TO", "").strip()

    path = Path(cfg.line_config) if cfg.line_config else DEFAULT_CONFIG
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"! 讀取 {path} 失敗：{e}", file=sys.stderr)
        else:
            token = token or str(data.get("channel_access_token", "")).strip()
            to = to or str(data.get("to", "")).strip()

    if not token:
        raise RuntimeError(
            f"找不到 LINE channel access token。\n"
            f"請設環境變數 LINE_CHANNEL_ACCESS_TOKEN，或建立 {path}：\n"
            f'{{"channel_access_token": "xxx", "to": ""}}')
    return token, to


def push_line(token: str, to: str, text: str) -> None:
    """推送文字訊息。to 為空則 broadcast 給所有好友（自用最方便）。"""
    endpoint = f"{LINE_API}/message/{'push' if to else 'broadcast'}"
    payload: dict[str, Any] = {"messages": [{"type": "text", "text": text[:4900]}]}
    if to:
        payload["to"] = to

    r = requests.post(endpoint, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }, json=payload, timeout=15)

    if r.status_code != 200:
        raise RuntimeError(f"LINE 推送失敗 HTTP {r.status_code}: {r.text[:300]}")


def line_quota(token: str) -> str:
    """查免費額度用量（輕用量方案每月 200 則）。"""
    h = {"Authorization": f"Bearer {token}"}
    try:
        q = requests.get(f"{LINE_API}/message/quota", headers=h, timeout=10).json()
        c = requests.get(f"{LINE_API}/message/quota/consumption",
                         headers=h, timeout=10).json()
    except (requests.RequestException, ValueError) as e:
        return f"（額度查詢失敗：{e}）"
    if q.get("type") == "limited":
        return f"本月已用 {c.get('totalUsage', '?')} / {q.get('value', '?')} 則"
    return f"本月已用 {c.get('totalUsage', '?')} 則（無上限方案）"


def load_state(cfg: argparse.Namespace) -> dict:
    path = Path(cfg.state_file) if cfg.state_file else DEFAULT_STATE
    if not path.exists():
        return {"alerted": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"alerted": {}}


def save_state(cfg: argparse.Namespace, state: dict) -> None:
    path = Path(cfg.state_file) if cfg.state_file else DEFAULT_STATE
    cutoff = time.time() - 30 * 86400
    state["alerted"] = {k: v for k, v in state.get("alerted", {}).items()
                        if v > cutoff}
    try:
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError as e:
        print(f"! 寫入狀態檔失敗：{e}", file=sys.stderr)


def build_alert(rows: list[Candidate], cfg: argparse.Namespace) -> str:
    mode = "負費率" if cfg.negative else "正費率"
    lines = [f"🔔 Bitget 資費異常（{mode}）",
             time.strftime("%m/%d %H:%M"), ""]

    for r in rows[:cfg.alert_max]:
        warn = f"  ⚠ {r.flags}" if r.flags else ""
        lines += [
            f"▸ {r.symbol}",
            f"  費率 {r.current_rate*100:.4f}%/{r.interval_h:.0f}h"
            f"（中位 {r.median_rate*100:.4f}%）",
            f"  淨年化 {r.net_apr_capital*100:.0f}%｜回本 {r.breakeven_periods:.1f} 期",
            f"  深度 {human(r.spot_depth_usdt)}｜達標 {r.hits}/{r.samples}{warn}",
            "",
        ]

    if len(rows) > cfg.alert_max:
        lines.append(f"（另有 {len(rows) - cfg.alert_max} 檔未列出）")
    lines.append(f"門檻：淨年化≥{cfg.min_net_apr*100:.0f}%、回本≤{cfg.max_breakeven:.0f}期")
    return "\n".join(lines)


def maybe_alert(rows: list[Candidate], cfg: argparse.Namespace) -> None:
    """只對「冷卻期已過」的標的發報，避免同一檔洗頻。"""
    if not rows:
        return

    state = load_state(cfg)
    alerted = state.setdefault("alerted", {})
    now = time.time()
    cooldown = cfg.cooldown_hours * 3600

    fresh = [r for r in rows if now - alerted.get(r.symbol, 0) > cooldown]
    if not fresh:
        print(f"有 {len(rows)} 檔達標，但都在冷卻期內，不重複發報。")
        return

    try:
        token, to = load_line_config(cfg)
        push_line(token, to, build_alert(fresh, cfg))
    except RuntimeError as e:
        print(f"! 通報失敗：{e}", file=sys.stderr)
        return

    for r in fresh:
        alerted[r.symbol] = now
    save_state(cfg, state)
    print(f"已發 LINE 通報：{', '.join(r.symbol for r in fresh)}")


def export(rows: list[Candidate], cfg: argparse.Namespace) -> None:
    fields = list(Candidate.__dataclass_fields__.keys())
    if cfg.csv:
        with open(cfg.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(asdict(r))
        print(f"已寫出 {cfg.csv}")
    if cfg.json:
        with open(cfg.json, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)
        print(f"已寫出 {cfg.json}")


# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bitget 資金費率套利掃描器",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    g = p.add_argument_group("篩選門檻")
    g.add_argument("--min-rate", type=float, default=0.0002,
                   help="單期費率達標門檻（0.0002 = 0.02%%）")
    g.add_argument("--min-median", type=float, default=0.00015,
                   help="近 N 期費率中位數下限")
    g.add_argument("--consistency", type=float, default=0.6,
                   help="近 N 期中至少多少比例要達到 min-rate")
    g.add_argument("--periods", type=int, default=21,
                   help="取樣期數（8h 結算下 21 期約一週）")
    g.add_argument("--min-samples", type=int, default=6,
                   help="歷史資料至少要有幾期才納入")
    g.add_argument("--current-relax", type=float, default=0.5,
                   help="當期費率門檻相對 min-rate 的寬鬆倍數")
    g.add_argument("--max-breakeven", type=float, default=8.0,
                   help="回本所需期數上限")
    g.add_argument("--min-net-apr", type=float, default=0.10,
                   help="扣費後資本年化下限（0.10 = 10%%）")

    g = p.add_argument_group("流動性")
    g.add_argument("--min-spot-vol", type=float, default=2_000_000,
                   help="現貨 24h 成交額下限 (USDT)")
    g.add_argument("--min-depth", type=float, default=20_000,
                   help="現貨賣單簿在滑價內的可買金額下限 (USDT)")
    g.add_argument("--max-slippage", type=float, default=0.003,
                   help="計算深度時容許的滑價 (0.003 = 0.3%%)")
    g.add_argument("--min-oi", type=float, default=5_000_000,
                   help="未平倉量下限 (USDT)，低於此標 LOWOI")

    g = p.add_argument_group("成本假設")
    g.add_argument("--spot-fee", type=float, default=0.001,
                   help="現貨單邊手續費率")
    g.add_argument("--futures-fee", type=float, default=0.0006,
                   help="合約單邊手續費率")
    g.add_argument("--hold-days", type=float, default=3.0,
                   help="預計持有天數（手續費攤提基準）")
    g.add_argument("--capital-per-notional", type=float, default=2.0,
                   help="總投入資本 ÷ 對沖名目金額；統一帳戶可壓到 1.3~1.5")

    g = p.add_argument_group("LINE 通報")
    g.add_argument("--notify", action="store_true",
                   help="有標的達標時發 LINE 通知")
    g.add_argument("--test-notify", action="store_true",
                   help="只發一則測試訊息並查額度，不掃描")
    g.add_argument("--line-config", help=f"LINE 設定檔路徑（預設 {DEFAULT_CONFIG.name}）")
    g.add_argument("--cooldown-hours", type=float, default=12.0,
                   help="同一標的多久內不重複通報")
    g.add_argument("--alert-max", type=int, default=6,
                   help="單則訊息最多列幾檔")
    g.add_argument("--state-file", help=f"已通報記錄檔（預設 {DEFAULT_STATE.name}）")

    g = p.add_argument_group("其他")
    g.add_argument("--negative", action="store_true",
                   help="改掃深度負費率（合約做多 + 現貨端做空）")
    g.add_argument("--watch", type=float, metavar="MIN",
                   help="常駐監控，每 MIN 分鐘掃一次")
    g.add_argument("--quiet", action="store_true",
                   help="不印表格（搭配 --watch 用）")
    g.add_argument("--top", type=int, default=25, help="顯示前幾名")
    g.add_argument("--workers", type=int, default=5, help="併發請求數")
    g.add_argument("--csv", help="輸出 CSV 檔名")
    g.add_argument("--json", help="輸出 JSON 檔名")
    return p.parse_args()


def run_once(cfg: argparse.Namespace) -> None:
    rows = scan(cfg)
    if not cfg.quiet:
        print_table(rows, cfg)
    else:
        print(f"[{time.strftime('%m/%d %H:%M')}] 達標 {len(rows)} 檔"
              + (f"：{', '.join(r.symbol for r in rows[:5])}" if rows else ""))
    export(rows, cfg)
    if cfg.notify:
        maybe_alert(rows, cfg)


def do_test_notify(cfg: argparse.Namespace) -> int:
    try:
        token, to = load_line_config(cfg)
    except RuntimeError as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 1

    print(f"模式：{'push → ' + to if to else 'broadcast（發給所有好友）'}")
    print(f"額度：{line_quota(token)}")
    try:
        push_line(token, to,
                  "🔔 Bitget 資費監控\n測試訊息，設定成功。\n"
                  + time.strftime("%Y-%m-%d %H:%M:%S"))
    except RuntimeError as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 1
    print("測試訊息已送出，去 LINE 看看。")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    cfg = parse_args()

    if cfg.test_notify:
        return do_test_notify(cfg)

    try:
        if cfg.watch:
            print(f"監控模式：每 {cfg.watch} 分鐘掃一次，Ctrl+C 結束。", file=sys.stderr)
            while True:
                try:
                    run_once(cfg)
                except RuntimeError as e:
                    print(f"! 本輪失敗，稍後重試：{e}", file=sys.stderr)
                time.sleep(cfg.watch * 60)
        else:
            run_once(cfg)
    except RuntimeError as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中斷", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
