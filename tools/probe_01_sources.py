# -*- coding: utf-8 -*-
"""
日股工作台 · Phase 1 数据源探针

验证（全部只读，不写入任何数据库）：
  ① TOPIX-17 系列 ETF（1617.T~1633.T）的日 K 历史完整性
  ② 候选指数（日経225 / TOPIX / グロース等）与代表性 ETF 的可得性
  ③ 小时线（前場/後場聚合用）的历史深度
  ④ yfinance 批量调用的限流边界（分批试探，出现 429 即停止升级）

用法:
  python tools/probe_01_sources.py            # 全量探针
  python tools/probe_01_sources.py --skip-batch   # 跳过限流试探（快速验证）

输出:
  probe/probe_report.md  人读报告
  probe/probe_raw.json   机读明细（供 pipeline 的字典表复用）
"""
import argparse
import json
import sys
import time
import traceback
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import jpcommon  # noqa: E402  （必须先于 yfinance：修复 libcurl 的 CA 信任链）

try:
    import yfinance as yf
except ImportError:
    sys.exit("缺少 yfinance：请先 pip install yfinance pandas")

import logging  # noqa: E402
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

OUT_DIR = ROOT / "probe"

# NEXT FUNDS TOPIX-17 シリーズ ETF（行业代理；代码↔行业名需实测确认）
ETF17 = {
    "1617.T": "食料品",
    "1618.T": "エネルギー資源",
    "1619.T": "建設・資材",
    "1620.T": "素材・化学",
    "1621.T": "医薬品",
    "1622.T": "自動車・輸送機",
    "1623.T": "鉄鋼・非鉄",
    "1624.T": "機械",
    "1625.T": "電機・精密",
    "1626.T": "情報通信・サービスその他",
    "1627.T": "電力・ガス",
    "1628.T": "運輸・物流",
    "1629.T": "商社・卸売",
    "1630.T": "小売",
    "1631.T": "銀行",
    "1632.T": "金融（除く銀行）",
    "1633.T": "不動産",
}

# 候选指数与宽基 ETF（含日経225 / TOPIX / 市場別・規模別的代理）
INDICES = {
    "^N225": "日経平均株価",
    "^TOPX": "TOPIX",
    "^NKX": "日経225（Stooq 口径，Yahoo 上一般不可得，用于确认命名差异）",
    "1306.T": "TOPIX ETF（野村）",
    "1321.T": "日経225 ETF（野村）",
    "1330.T": "TOPIX 高配当40 ETF",
    "2516.T": "東証グロース市場250 指数 ETF",
    "2513.T": "東証プライム市場指数 ETF",
    "1698.T": "TOPIX 銀行業 高配当 ETF（行业交叉校验用）",
    "JPY=X": "米ドル/円（宏观副面板候选）",
}

# 代表性个股（大票流动性交叉校验）
STOCKS = {
    "7203.T": "トヨタ自動車", "6758.T": "ソニーグループ", "9984.T": "ソフトバンクグループ",
    "6861.T": "キーエンス", "8306.T": "三菱UFJフィナンシャル", "9432.T": "NTT",
    "6098.T": "リクルート", "8035.T": "東京エレクトロン", "4063.T": "信越化学工業",
    "8058.T": "三菱商事",
}

REPORT = []
RAW = {"generated": date.today().isoformat(), "etf17": {}, "indices": {},
       "stocks": {}, "batch": {}, "minute": {}}


def log(s=""):
    print(s, flush=True)
    REPORT.append(s)


def hist_stats(sym, period="2y", interval="1d"):
    """拉一次历史，返回统计或错误信息"""
    t0 = time.time()
    try:
        h = yf.Ticker(sym).history(period=period, interval=interval, auto_adjust=False)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:160]}
    dt = round(time.time() - t0, 2)
    if h is None or h.empty:
        return {"error": "empty", "sec": dt}
    idx = [d.date().isoformat() for d in h.index]
    close = h["Close"].dropna()
    return {
        "rows": int(len(h)),
        "first": idx[0],
        "last": idx[-1],
        "last_close": round(float(close.iloc[-1]), 2) if len(close) else None,
        "nonzero_vol_days": int((h["Volume"].fillna(0) > 0).sum()),
        "sec": dt,
    }


def probe_group(title, mapping, period="2y", interval="1d"):
    log(f"\n=== {title}（period={period}, interval={interval}）===")
    log(f"{'代码':<10}{'名称':<26}{'行数':>6}{'首日':>12}{'末日':>12}{'成交量>0':>9}{'耗时':>7}  备注")
    ok = bad = 0
    for sym, name in mapping.items():
        st = hist_stats(sym, period, interval)
        if "error" in st:
            bad += 1
            log(f"{sym:<10}{name:<26}{'—':>6}{'—':>12}{'—':>12}{'—':>9}{'—':>7}  ✗ {st['error']}")
        else:
            ok += 1
            note = "" if st["nonzero_vol_days"] > st["rows"] * 0.9 else "⚠ 成交量缺失较多"
            if st["last"] < (date.today() - timedelta(days=10)).isoformat():
                note = (note + " ⚠ 疑似停止更新").strip()
            log(f"{sym:<10}{name:<26}{st['rows']:>6}{st['first']:>12}{st['last']:>12}"
                f"{st['nonzero_vol_days']:>9}{st['sec']:>7}  {note}")
        time.sleep(0.6)          # 温和节流，避免探针本身触发 429
    log(f"→ {title} 小结：可用 {ok} / 失败 {bad}")
    return mapping, ok, bad


def probe_batch():
    """分档试探批量下载的限流边界（失败即停，不继续加压）"""
    log("\n=== 批量调用限流试探（分档，遇 429 立即停止）===")
    universe = list(ETF17) + list(INDICES)[:0] + list(STOCKS)
    universe = list(dict.fromkeys(universe))
    for n in (10, 30, 57):
        batch = universe[:n]
        t0 = time.time()
        try:
            df = yf.download(batch, period="6mo", interval="1d",
                             group_by="ticker", progress=False,
                             threads=False, auto_adjust=False)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"[:200]
            log(f"  n={n:>3}: ✗ {msg}")
            RAW["batch"][str(n)] = {"error": msg}
            break
        got = 0
        if df is not None and not df.empty:
            for sym in batch:
                try:
                    if sym in df.columns.get_level_values(0):
                        sub = df[sym]["Close"].dropna()
                        if len(sub):
                            got += 1
                except Exception:
                    pass
        dt = round(time.time() - t0, 1)
        log(f"  n={n:>3}: 成功 {got}/{len(batch)}，耗时 {dt}s")
        RAW["batch"][str(n)] = {"requested": len(batch), "returned": got, "sec": dt}
        if got < len(batch) * 0.8:
            log("  ⚠ 返回不完整（疑似限流），停止加压")
            break
        time.sleep(3)


def probe_minute():
    """分钟/小时线（前場後場聚合用）历史深度"""
    log("\n=== 分钟线深度（^N225 / 1625.T / 7203.T）===")
    for sym in ("^N225", "1625.T", "7203.T"):
        for interval, period in (("1h", "2y"), ("1h", "730d"), ("5m", "1mo"), ("1m", "1mo")):
            st = hist_stats(sym, period=period, interval=interval)
            if "error" in st:
                log(f"  {sym:<8}{interval:<4}{period:<6} ✗ {st['error']}")
            else:
                log(f"  {sym:<8}{interval:<4}{period:<6} 行数 {st['rows']:>5}  "
                    f"{st['first']} ~ {st['last']}")
                RAW["minute"][f"{sym}|{interval}|{period}"] = st
            time.sleep(0.6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-batch", action="store_true", help="跳过限流试探")
    ap.add_argument("--skip-minute", action="store_true", help="跳过分钟线探测")
    a = ap.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    log(f"yfinance {yf.__version__} · 探针时间 {date.today().isoformat()}")

    for tag, mapping in (("ETF17", ETF17), ("INDICES", INDICES), ("STOCKS", STOCKS)):
        _, ok, bad = probe_group(tag, mapping)
        RAW[{"ETF17": "etf17", "INDICES": "indices", "STOCKS": "stocks"}[tag]] = {
            "members": mapping, "ok": ok, "fail": bad}

    if not a.skip_minute:
        probe_minute()
    if not a.skip_batch:
        probe_batch()

    log("\n=== 结论与后续动作 ===")
    log("1. ETF17 中失败的标的 → 用对应 TOPIX-33 業種龙头个股或 Stooq 补齐（写入 dim_industry_jp 的 note 列）")
    log("2. 批量档位结果 → 决定 pipeline 的 batch size 与 sleep（写入 pipeline_jp.py 常量）")
    log("3. 指数可得性 → 决定顶部状态条与宽基面板的标的选择")
    log("4. 分钟线深度 → 决定前場/後場历史可回补窗口")

    (OUT_DIR / "probe_report.md").write_text(
        "# 日股工作台 · Phase 1 数据源探针报告\n\n```\n" + "\n".join(REPORT) + "\n```\n",
        encoding="utf-8")
    (OUT_DIR / "probe_raw.json").write_text(
        json.dumps(RAW, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\n报告已写入：{OUT_DIR / 'probe_report.md'}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
