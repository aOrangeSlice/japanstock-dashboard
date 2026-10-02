# -*- coding: utf-8 -*-
"""
日股工作台 · データパイプライン（取数层）

数据源（Phase 1 探针实测确定，见 probe/probe_report.md）：
  · 主源 yfinance（Yahoo Finance）：17 業種 ETF + 指数 + 個別銘柄 + 小時線
  · ^TOPX 在 Yahoo 上不存在 → TOPIX 用 1306.T（TOPIX ETF）代理
  · 小時線可回溯 730 天 → 前場/後場可回補約 2 年历史
  · 1m 線不可得，5m 線仅 1 个月

子命令：
  init      : 建库建表（japanstock.db）
  backfill  : 回填历史（業種/指数日 K + 個別銘柄 + 前場後場 + 市場宽度）
  sync      : 增量同步（close 收盘档 / noon 前場後場の間档）
  stats     : 各表行数与日期范围
  klines    : 仅業種/指数日 K
  stocks    : 仅個別銘柄ユニバース（含市場宽度重算）
  intraday  : 仅前場/後場（小時線聚合）
  breadth   : 仅由庫內個別銘柄データ重算市場宽度

设计要点（沿用 A 股工作台铁律）：
  · 全部事実表 PRIMARY KEY(date,code) + UPSERT → 幂等，重跑不产生脏数据
  · **按日快照表**（fact_stock_daily）写入前必须先删当日再插，
    否则盘中快照的个股在收盘后会残留成幽灵行（不可逆）
  · 交易日取「庫内 ^N225 の日 K 日期」（无上限）；无数据时才退回 yfinance 历法
  · yfinance 批量调用是限流重灾区 → 统一走 _download() 分批 + 间隔 + 退避
"""
import argparse
import json
import sqlite3
import sys
import time
import traceback
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import jpcommon  # noqa: E402  （必须先于 yfinance：修复 libcurl CA 信任链）

import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402
import logging  # noqa: E402

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

from jpcommon import DB_PATH, IND17, INDICES_JP, RAW_DIR  # noqa: E402
from universe_seed import UNIVERSE_SEED, IND_NAME_TO_CODE  # noqa: E402

E8 = 1e8
BATCH = 40           # 每批标的上限（探针实测 27 只/3.8s 无限流，留足余量）
SLEEP = 1.2          # 批次间隔（秒）
RETRY = 3


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- yfinance 封装
def _clean(df, symbols):
    """把 yf.download 的多层列 DataFrame 拆成 {symbol: DataFrame(Open..Volume)}"""
    out = {}
    if df is None or df.empty:
        return out
    multi = isinstance(df.columns, pd.MultiIndex)
    for s in symbols:
        try:
            sub = df[s].copy() if multi else df.copy()
        except Exception:
            continue
        if sub is None or sub.empty:
            continue
        sub = sub.dropna(subset=["Close"])
        if not sub.empty:
            out[s] = sub
    return out


def download(symbols, start=None, end=None, period=None, interval="1d",
             group_errors=None):
    """分批下载 + 指数退避重试。返回 {symbol: DataFrame}

    ⚠️ yfinance 的批量下载是限流重灾区；本函数按 BATCH 分批、批次间 sleep，
       失败退避重试。取不到的标的直接跳过（不阻塞整体）。
    """
    result = {}
    symbols = list(dict.fromkeys(symbols))
    for i in range(0, len(symbols), BATCH):
        batch = symbols[i:i + BATCH]
        for attempt in range(RETRY + 1):
            try:
                kw = dict(interval=interval, group_by="ticker", progress=False,
                          threads=False, auto_adjust=False)
                if period:
                    kw["period"] = period
                else:
                    kw["start"] = start
                    if end:
                        kw["end"] = (date.fromisoformat(end)
                                     + timedelta(days=1)).isoformat()  # end 为开区间
                df = yf.download(batch, **kw)
            except Exception as e:
                df = None
                if group_errors is not None:
                    group_errors.append(f"{batch[0]}… {type(e).__name__}: {e}"[:160])
            got = _clean(df, batch)
            if len(got) >= max(1, int(len(batch) * 0.6)):
                result.update(got)
                break
            if attempt < RETRY:
                wait = 3 * (attempt + 1) ** 2
                log(f"    [retry {attempt+1}/{RETRY}] 本批仅 {len(got)}/{len(batch)} "
                    f"→ 等待 {wait}s")
                time.sleep(wait)
            else:
                result.update(got)
        time.sleep(SLEEP)
    missing = [s for s in symbols if s not in result]
    if missing:
        log(f"  [warn] 未取得数据 {len(missing)} 只：{missing[:12]}"
            f"{' …' if len(missing) > 12 else ''}")
    return result


# ---------------------------------------------------------------- schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS dim_industry(
  code TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT DEFAULT 'etf17');
CREATE TABLE IF NOT EXISTS dim_index(
  code TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dim_stock(
  code TEXT PRIMARY KEY, name TEXT, ind_code TEXT);

-- 業種（17 ETF 代理）日次
-- attention = 資金注目度 = 当日出来高 ÷ 過去20日平均出来高（量比）
CREATE TABLE IF NOT EXISTS fact_industry_daily(
  date TEXT NOT NULL, code TEXT NOT NULL,
  close REAL, pct_chg REAL, amount REAL, volume REAL, attention REAL,
  PRIMARY KEY(date, code));
CREATE INDEX IF NOT EXISTS idx_fid_date ON fact_industry_daily(date);

CREATE TABLE IF NOT EXISTS fact_index_daily(
  date TEXT NOT NULL, code TEXT NOT NULL,
  close REAL, pct_chg REAL, amount REAL,
  PRIMARY KEY(date, code));

-- 市場宽度（由個別銘柄ユニバース算出，口径诚实标注为 universe 内）
CREATE TABLE IF NOT EXISTS fact_market_daily(
  date TEXT PRIMARY KEY,
  up INTEGER, down INTEGER, flat INTEGER,
  limit_up INTEGER, limit_down INTEGER,
  total_amount REAL, avg_pct REAL, payload TEXT);

-- 個別銘柄（毎日スナップショット；写入前先删当日）
CREATE TABLE IF NOT EXISTS fact_stock_daily(
  date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, ind_code TEXT,
  close REAL, pct_chg REAL, amount REAL, volume REAL,
  PRIMARY KEY(date, code));
CREATE INDEX IF NOT EXISTS idx_fsd_date ON fact_stock_daily(date);

-- 前場/後場（小時線聚合）
CREATE TABLE IF NOT EXISTS fact_intraday_daily(
  date TEXT NOT NULL, code TEXT NOT NULL, kind TEXT,
  prev_close REAL, am_close REAL, pm_close REAL,
  am_pct REAL, pm_pct REAL, am_amount REAL, pm_amount REAL, bars INTEGER,
  PRIMARY KEY(date, code));
CREATE INDEX IF NOT EXISTS idx_intra_date ON fact_intraday_daily(date, kind);

CREATE TABLE IF NOT EXISTS raw_fetch(
  ts TEXT, cmd TEXT, nrows INTEGER, note TEXT);
"""


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def cmd_init():
    con = connect()
    con.executescript(SCHEMA)
    con.executemany("INSERT OR REPLACE INTO dim_industry(code,name,source) "
                    "VALUES(?,?,'etf17')", IND17.items())
    con.executemany("INSERT OR REPLACE INTO dim_index(code,name) VALUES(?,?)",
                    INDICES_JP.items())
    rows = [(c, n, IND_NAME_TO_CODE.get(ind)) for c, n, ind in UNIVERSE_SEED]
    con.executemany("INSERT OR REPLACE INTO dim_stock(code,name,ind_code) "
                    "VALUES(?,?,?)", rows)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM dim_stock").fetchone()[0]
    con.close()
    log(f"init OK -> {DB_PATH}")
    log(f"  業種 {len(IND17)} / 指数 {len(INDICES_JP)} / 個別銘柄 {n}")


def log_fetch(con, cmd, nrows, note=""):
    con.execute("INSERT INTO raw_fetch(ts,cmd,nrows,note) VALUES(?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), cmd, nrows, note))
    con.commit()


# ---------------------------------------------------------------- 交易日
def trading_days(start, end):
    """交易日列表：优先取庫内 ^N225 の日 K 日期（最准且无上限）"""
    con = connect()
    rows = con.execute("SELECT date FROM fact_index_daily WHERE code='^N225' "
                       "AND date>=? AND date<=? ORDER BY date",
                       (start, end)).fetchall()
    con.close()
    return [r[0] for r in rows]


def is_trading_day(d):
    """单日守卫：庫内已有该日的 ^N225 行即视为已确认交易日；
    庫内无该行时用 yfinance 现拉一次 ^N225 判断（节假日会返回空）。"""
    con = connect()
    hit = con.execute("SELECT 1 FROM fact_index_daily WHERE code='^N225' AND date=?",
                      (d,)).fetchone()
    con.close()
    if hit:
        return True
    got = download(["^N225"], start=d, end=d)
    return bool(got)


# ---------------------------------------------------------------- 取数阶段
def fetch_industry(start, end, period=None):
    """17 業種 ETF の日 K（含 資金注目度 量比計算）"""
    symbols = list(IND17)
    got = download(symbols, start=start, end=end, period=period)
    con = connect()
    total = 0
    for code, df in got.items():
        close = df["Close"].astype(float)
        vol = df["Volume"].astype(float)
        pct = close.pct_change() * 100
        # 資金注目度：当日出来高 ÷ 過去20日平均（含自身），窗内不足 5 日则置空
        ma20 = vol.rolling(20, min_periods=5).mean()
        rows = []
        prev = None
        for ts in df.index:
            d = ts.date().isoformat()
            c = float(close.loc[ts])
            p = None if prev is None else (c / prev - 1) * 100
            prev = c
            v = float(vol.loc[ts]) if pd.notna(vol.loc[ts]) else None
            m = ma20.loc[ts]
            att = round(v / float(m), 2) if (v and pd.notna(m) and m > 0) else None
            amt = round(c * v, 0) if v else None      # 概算売買代金（円）
            rows.append((d, code, round(c, 2),
                         round(p, 2) if p is not None else None,
                         amt, v, att))
        # pct_chg 首日由库内前值补：交给 ON CONFLICT 的既有行保留
        con.executemany(
            "INSERT INTO fact_industry_daily(date,code,close,pct_chg,amount,"
            "volume,attention) VALUES(?,?,?,?,?,?,?) ON CONFLICT(date,code) "
            "DO UPDATE SET close=excluded.close, "
            "pct_chg=COALESCE(excluded.pct_chg, fact_industry_daily.pct_chg), "
            "amount=excluded.amount, volume=excluded.volume, "
            "attention=COALESCE(excluded.attention, fact_industry_daily.attention)",
            rows)
        con.commit()
        total += len(rows)
        log_fetch(con, f"kline {code}", len(rows))
    con.close()
    log(f"  industry: {len(got)}/{len(symbols)} 只，{total} 行")
    return total


def fetch_indices(start, end, period=None):
    symbols = list(INDICES_JP)
    got = download(symbols, start=start, end=end, period=period)
    con = connect()
    total = 0
    for code, df in got.items():
        close = df["Close"].astype(float)
        vol = df["Volume"].astype(float)
        rows = []
        prev = None
        for ts in df.index:
            c = float(close.loc[ts])
            p = None if prev is None else (c / prev - 1) * 100
            prev = c
            v = float(vol.loc[ts]) if pd.notna(vol.loc[ts]) else 0.0
            rows.append((ts.date().isoformat(), code, round(c, 2),
                         round(p, 2) if p is not None else None,
                         round(c * v, 0) if v else None))
        con.executemany(
            "INSERT INTO fact_index_daily(date,code,close,pct_chg,amount) "
            "VALUES(?,?,?,?,?) ON CONFLICT(date,code) DO UPDATE SET "
            "close=excluded.close, "
            "pct_chg=COALESCE(excluded.pct_chg, fact_index_daily.pct_chg), "
            "amount=excluded.amount", rows)
        con.commit()
        total += len(rows)
        log_fetch(con, f"index {code}", len(rows))
    con.close()
    log(f"  index: {len(got)}/{len(symbols)} 只，{total} 行")
    return total


def fetch_stocks(start=None, end=None, period=None, snapshot=None):
    """個別銘柄ユニバースの日次スナップショット

    ⚠️ 按日快照表：写入前必须先删当日再插（否则盘中快照残留为幽灵行，
       且 UPSERT 不会删行 → 不可逆）。取不到数据则不删不写。
    ⚠️ 入库日期一律取**行情返回的最后交易日**，不取请求日期（节假日跑时
       请求日期无数据，按请求日期入库会把上一交易日错位记到今天）。
    """
    con = connect()
    meta = {c: (n, ic) for c, n, ic in
            con.execute("SELECT code,name,ind_code FROM dim_stock")}
    got = download(list(meta), start=start, end=end, period=period)
    if not got:
        con.close()
        log("  stocks: 無データ → スキップ（当日は削除しない）")
        return 0

    # 日期轴：快照模式只写最后交易日；区间模式写区间内每个交易日
    last = max(df.index[-1] for df in got.values() if len(df))
    if period or snapshot:
        dates = [last.date().isoformat()]
    else:
        dates = trading_days(start, end)

    n_total = 0
    for d in dates:
        con.execute("DELETE FROM fact_stock_daily WHERE date=?", (d,))
        rows = []
        for code, df in got.items():
            sub = df[[t.date().isoformat() == d for t in df.index]]
            if sub.empty:
                continue
            nm, ic = meta.get(code, (None, None))
            r = sub.iloc[-1]
            c = float(r["Close"]) if pd.notna(r["Close"]) else None
            v = float(r["Volume"]) if pd.notna(r["Volume"]) else None
            rows.append((d, code, nm, ic, round(c, 2) if c else None, None,
                         round(c * v, 0) if (c and v) else None, v))
        if not rows:
            continue
        con.executemany(
            "INSERT INTO fact_stock_daily(date,code,name,ind_code,close,pct_chg,"
            "amount,volume) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(date,code) DO UPDATE SET name=excluded.name, "
            "ind_code=excluded.ind_code, close=excluded.close, "
            "pct_chg=excluded.pct_chg, amount=excluded.amount, "
            "volume=excluded.volume", rows)
        con.commit()
        n_total += len(rows)
        log_fetch(con, f"stocks {d}", len(rows))
    con.close()
    log(f"  stocks: {n_total} 行 / {len(dates)} 个交易日")
    return n_total


def recalc_stocks_daily():
    """用庫内個別銘柄の連続日 K 补齐 pct_chg（下载层不做 pct，统一在这里算）

    口径：pct_chg = 当日 close ÷ 前一日 close − 1（前一日取庫内最近一条）。
    """
    con = connect()
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM fact_stock_daily")]
    n = 0
    for k, code in enumerate(codes):
        rows = con.execute(
            "SELECT date,close FROM fact_stock_daily WHERE code=? AND close IS NOT NULL "
            "ORDER BY date", (code,)).fetchall()
        prev = None
        for d, c in rows:
            if prev is not None and prev > 0:
                p = round((c / prev - 1) * 100, 2)
                con.execute("UPDATE fact_stock_daily SET pct_chg=? "
                            "WHERE date=? AND code=?", (p, d, code))
                n += 1
            prev = c
        # ⚠️ 必须定期提交：215 銘柄 × 488 営業日 = 約 10 万 UPDATE を 1 トランザクション
        #    で抱えると書き込みロックを数分間保持し、並行する intraday 側が
        #    「database is locked」で落ちる（実測）。
        if k % 20 == 19:
            con.commit()
    con.commit()
    con.close()
    log(f"  pct_chg 補完: {n} 行")
    return n


def recalc_breadth():
    """由個別銘柄ユニバース重算市場宽度（上がり/下がり/変わらず家数）

    口径诚实标注：ユニバース内（約 160 銘柄）ベースの腾落，非全市场。
    """
    con = connect()
    days = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM fact_stock_daily ORDER BY date")]
    n = 0
    for d in days:
        rows = con.execute(
            "SELECT pct_chg, amount FROM fact_stock_daily WHERE date=?", (d,)).fetchall()
        vals = [(p, a) for p, a in rows if p is not None]
        if not vals:
            continue
        up = sum(1 for p, _ in vals if p > 0)
        down = sum(1 for p, _ in vals if p < 0)
        flat = sum(1 for p, _ in vals if p == 0)
        amt = sum(a or 0 for _, a in vals)
        avg = sum(p for p, _ in vals) / len(vals)
        payload = json.dumps({"n": len(vals)}, ensure_ascii=False)
        con.execute(
            "INSERT INTO fact_market_daily(date,up,down,flat,limit_up,limit_down,"
            "total_amount,avg_pct,payload) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET up=excluded.up, down=excluded.down, "
            "flat=excluded.flat, total_amount=excluded.total_amount, "
            "avg_pct=excluded.avg_pct, payload=excluded.payload",
            (d, up, down, flat, None, None, amt, round(avg, 2), payload))
        n += 1
    con.commit()
    log_fetch(con, "breadth", n)
    con.close()
    log(f"  breadth: {n} 个交易日")
    return n


# ---------------------------------------------------------------- 前場/後場
BARS_FULL_DAY = 7   # 探针实测：東証の小時足は 1 日 7 本（09/10/11/12/13/14/15 時）


def _intraday_prev_closes(con, codes):
    """各コードの日足終値を読み、指定日より前の直近終値を引けるクロージャを返す"""
    daily = {}
    for code in codes:
        rows = con.execute(
            "SELECT date,close FROM fact_industry_daily WHERE code=? "
            "AND close IS NOT NULL ORDER BY date", (code,)).fetchall()
        if not rows:
            rows = con.execute(
                "SELECT date,close FROM fact_index_daily WHERE code=? "
                "AND close IS NOT NULL ORDER BY date", (code,)).fetchall()
        daily[code] = rows

    def prev_of(code, d):
        rows = daily.get(code) or []
        lo, hi = 0, len(rows)
        while lo < hi:                      # 二分探索：date < d の最後の行
            mid = (lo + hi) // 2
            if rows[mid][0] < d:
                lo = mid + 1
            else:
                hi = mid
        return rows[lo - 1][1] if lo > 0 else None

    return prev_of


def fetch_intraday(start=None, end=None):
    """前場(09:00–11:30) / 後場(12:30–15:30) の集計

    ⚠️ 実装方針（A 股版との違い）：A 股版は westock の m60 が**多日リクエストで
       限流**されたため逐日取得していたが、yfinance は区間一括取得が安定して
       通る → **一括取得**に変更。逐日 488 回（実測 25 分超）→ 1 回 9 秒。
    ⚠️ **`start`/`end` では 1h の長期区間が取れない**（実測：22 銘柄で全滅）。
       `period="730d"` なら通る（実測 17,480 本 / 22 銘柄 / 9.3 秒）→ period 方式で
       取得し、日付はローカルで絞る。
    ⚠️ 時間バケット：探针で 09/10/11/12/13/14/15 時の 7 本を確認。
       前場 = 11:30 以前、後場 = 12:00 以降（12:00 のバーは後場の初回）。
    """
    today = date.today()
    end = end or today.isoformat()
    floor = (today - timedelta(days=730)).isoformat()
    start = max(start or floor, floor)
    # ⚠️ 日内集計の対象から **JPY=X（ドル円）を除外**：FX は 24 時間取引で
    #    1 日 24 本のバーが返り、しかも日曜から取引が始まるため「週末の行」を
    #    大量に生む（実測：104 の週末日付が混入）→ 株・指数のみを対象にする。
    # ⚠️ 日付は必ず **Asia/Tokyo に変換してから** 切り出す（銘柄によっては
    #    別タイムゾーンのバーが返り、UTC 基準だと日付が 1 日ずれる）。
    codes = list(IND17) + [c for c in INDICES_JP if c != "JPY=X"]
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 3
    got = download(codes, period="%dd" % min(730, max(2, span)), interval="1h")
    if not got:
        log("  intraday: 無データ → スキップ")
        return 0

    # 営業日ガード：庫内 ^N225 の日 K にある日付のみを書く（週末・祝日の混入防止）
    con = connect()
    cal = {r[0] for r in con.execute(
        "SELECT date FROM fact_index_daily WHERE code='^N225'")}

    # 日付 → コード → [(HH:MM, row), ...]
    per = {}
    for code, df in got.items():
        for ts, row in df.iterrows():
            try:
                ts2 = ts.tz_convert("Asia/Tokyo") if ts.tzinfo else ts
            except (TypeError, AttributeError):
                ts2 = ts
            d = ts2.date().isoformat()
            if not (start <= d <= end) or d not in cal:
                continue
            per.setdefault(d, {}).setdefault(code, []).append(
                (ts2.strftime("%H:%M"), row))

    prev_of = _intraday_prev_closes(con, codes)

    # ⚠️ 基準終値は「小時足シリーズ自身の前日最終バー終値」を優先する。
    #    日足終値（fact_*_daily）と小時足の 15:00 バー終値は数十ポイントずれる
    #    ことがあり（実測：^N225 で約 110 ポイント）、日足を基準にすると
    #    「前場 × 後場」が日足の騰落率と厳密に一致しなくなる。
    #    小時足基準なら (1+前場)(1+後場) = その日の最終バー ÷ 前日最終バー が
    #    定義上必ず成立する（口径が閉じる）。日足は範囲先頭日のみフォールバック。
    last_bar = {}          # code -> {date: その日の最終バー終値}
    for d, m in per.items():
        for code, bars in m.items():
            bars_sorted = sorted(bars, key=lambda x: x[0])
            last_bar.setdefault(code, {})[d] = float(bars_sorted[-1][1]["Close"])

    def base_close(code, d):
        series = last_bar.get(code) or {}
        prev_days = [x for x in series if x < d]
        if prev_days:
            return series[max(prev_days)]
        return prev_of(code, d)          # 範囲先頭日は日足にフォールバック
    total = 0
    for d in sorted(per):
        buckets = per[d]
        n = 0
        for code, bars in buckets.items():
            bars.sort(key=lambda x: x[0])
            am = [b for b in bars if b[0] <= "11:30"]
            pm = [b for b in bars if b[0] >= "12:00"]
            am_close = float(am[-1][1]["Close"]) if am else None
            pm_close = float(pm[-1][1]["Close"]) if pm else None
            prev_close = base_close(code, d)
            am_vol = float(sum(b[1]["Volume"] or 0 for b in am)) if am else 0.0
            pm_vol = float(sum(b[1]["Volume"] or 0 for b in pm)) if pm else 0.0
            am_pct = ((am_close / prev_close - 1) * 100) if (am_close and prev_close) else None
            pm_pct = ((pm_close / am_close - 1) * 100) if (pm_close and am_close) else None
            con.execute(
                "INSERT INTO fact_intraday_daily(date,code,kind,prev_close,am_close,"
                "pm_close,am_pct,pm_pct,am_amount,pm_amount,bars) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(date,code) DO UPDATE SET "
                "kind=excluded.kind, prev_close=excluded.prev_close, "
                "am_close=excluded.am_close, pm_close=excluded.pm_close, "
                "am_pct=excluded.am_pct, pm_pct=excluded.pm_pct, "
                "am_amount=excluded.am_amount, pm_amount=excluded.pm_amount, "
                "bars=excluded.bars WHERE excluded.bars >= fact_intraday_daily.bars",
                (d, code, "ind" if code in IND17 else "idx", prev_close, am_close,
                 pm_close,
                 round(am_pct, 2) if am_pct is not None else None,
                 round(pm_pct, 2) if pm_pct is not None else None,
                 # 概算：出来高 × 100 株（単元）→ 円。指数は出来高 0 なので None
                 round(am_vol * 100, 0) if am_vol else None,
                 round(pm_vol * 100, 0) if pm_vol else None, len(bars)))
            n += 1
        con.commit()
        total += n
        log_fetch(con, f"intraday {d}", n)
    con.close()
    log(f"  intraday: {len(per)} 営業日 / {total} 行（区間一括取得）")
    return total


# ---------------------------------------------------------------- 编排
def do_backfill(start, end):
    t0 = time.time()
    log(f"=== backfill {start} ~ {end} ===")
    fetch_industry(start, end)
    fetch_indices(start, end)
    fetch_stocks(start=start, end=end)
    recalc_stocks_daily()
    recalc_breadth()
    fetch_intraday(start, end)
    log(f"=== backfill finished in {(time.time()-t0)/60:.1f} min ===")
    cmd_stats()


def cmd_sync(session="close"):
    today = date.today().isoformat()
    if not is_trading_day(today):
        log(f"[SKIP] {today} は非営業日（または データ未提供）→ 書き込みなし")
        return
    if session == "noon":
        log(f"=== noon 前場後（{today}）===")
        fetch_industry(today, today)
        fetch_indices(today, today)
        fetch_stocks(snapshot=today, period="1mo")
        recalc_stocks_daily()
        recalc_breadth()
        fetch_intraday(start=today, end=today)
        cmd_stats()
        return
    log(f"=== close 大引け後（{today}）===")
    con = connect()
    mx = con.execute("SELECT MAX(date) FROM fact_index_daily "
                     "WHERE code='^N225'").fetchone()[0]
    con.close()
    start = ((date.fromisoformat(mx) - timedelta(days=7)).isoformat()
             if mx else (date.today() - timedelta(days=30)).isoformat())
    fetch_industry(start, today)
    fetch_indices(start, today)
    fetch_stocks(snapshot=today, period="1mo")
    recalc_stocks_daily()
    recalc_breadth()
    fetch_intraday(start=today, end=today)
    cmd_stats()


def cmd_stats():
    con = connect()
    for t in ("fact_industry_daily", "fact_index_daily", "fact_market_daily",
              "dim_stock", "fact_stock_daily", "fact_intraday_daily", "raw_fetch"):
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        rng = ""
        if t != "raw_fetch" and t != "dim_stock":
            r = con.execute(f"SELECT MIN(date),MAX(date) FROM {t}").fetchone()
            if r and r[0]:
                rng = f"  ({r[0]} ~ {r[1]})"
        log(f"{t:<22} {n:>7}{rng}")
    con.close()


def main():
    ap = argparse.ArgumentParser(description="日股工作台 データパイプライン")
    ap.add_argument("command", choices=["init", "backfill", "sync", "stats",
                                        "klines", "stocks", "intraday", "breadth"])
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=str(date.today()))
    ap.add_argument("--session", choices=["close", "noon"], default="close")
    a = ap.parse_args()
    if a.command == "init":
        cmd_init()
    elif a.command == "backfill":
        s = a.start or (date.today() - timedelta(days=730)).isoformat()
        do_backfill(s, a.end)
    elif a.command == "sync":
        cmd_sync(a.session)
    elif a.command == "stats":
        cmd_stats()
    elif a.command == "klines":
        s = a.start or (date.today() - timedelta(days=730)).isoformat()
        fetch_industry(s, a.end)
        fetch_indices(s, a.end)
    elif a.command == "stocks":
        if a.start:
            fetch_stocks(start=a.start, end=a.end)
            recalc_stocks_daily()
        else:
            fetch_stocks(snapshot=a.end, period="2y")
            recalc_stocks_daily()
        recalc_breadth()
    elif a.command == "intraday":
        fetch_intraday(start=a.start, end=a.end)
    elif a.command == "breadth":
        recalc_breadth()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
