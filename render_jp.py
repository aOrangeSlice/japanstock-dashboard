# -*- coding: utf-8 -*-
"""
日股工作台 · ダッシュボード・レンダラ（全量データパック方式）

A 股工作台 render.py の設計を踏襲：
  · 庫内の全履歴を 1 個の JS データパック `var D` として HTML に埋め込み、
    フロント側が「選択日 × セッション」で即時再計算する
  · 庫で計算できないもの（総括コメント・口径説明）は narrative_jp.json から注入

データ量の方針（HTML の肥大化を避けるため）：
  · 業種 / 指数の日 K：全期間（約 2 年）
  · 前場・後場（小時線集計）：全期間（約 2 年、probe で 730 日取得可能を実測）
  · 個別銘柄：直近 250 営業日 × 売買代金上位 25 銘柄 / 日
  · 業種別の個別銘柄一覧：最新営業日のみ（ドロワー用）

使い方:
  python render_jp.py                    # 庫内最新営業日を既定選択日にする
  python render_jp.py --date 2026-10-01  # 既定選択日を指定
  python render_jp.py --check            # 数値チェックのみ（書き出しなし）
"""
import argparse
import json
import re
import sqlite3
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "japanstock.db"
TPL_PATH = ROOT / "dashboard_jp.template.html"
NARR_PATH = ROOT / "narrative_jp.json"
OUT_PATH = ROOT / "日本株セクター・ローテーション監視.html"

WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]
E8 = 1e8                    # 円 → 億円
BARS_FULL_DAY = 7           # 小時足の 1 日分（09〜15 時）＝ 完全な取引日
STOCK_WINDOW = 250          # 個別銘柄を収録する営業日数
RANK_PER_DAY = 25           # 1 日あたりの売買代金上位銘柄数
IND_TOP_N = 8               # ドロワー用：業種あたりの最新上位銘柄数


def q(con, sql, args=()):
    return con.execute(sql, args).fetchall()


def align(con, table, code, dates, col, scale=1.0, nd=2):
    """标的の 1 字段を dates に整列（欠損は None、scale は**除数**）"""
    m = {}
    for d, v in q(con, f"SELECT date,{col} FROM {table} WHERE code=? "
                       f"AND {col} IS NOT NULL", (code,)):
        m[d] = round(v / scale, nd)
    return [m.get(d) for d in dates]


def build_bundle(con, narr):
    # ① 営業日軸：^N225 ∪ 1306.T 双锚并集（^N225 官方收盘有滞后、Close 可能
    #    缺失 → 单锚会漏掉最新交易日；1306.T ETF 実時行情補位。缺值日 N225
    #    KPI 由前端降级显示「—」）
    dates = [r[0] for r in q(con, "SELECT DISTINCT date FROM fact_index_daily "
                                  "WHERE code IN ('^N225','1306.T') "
                                  "AND close IS NOT NULL ORDER BY date")]
    # ② 業種（17 ETF 代理）
    ind = {}
    for code, name in q(con, "SELECT code,name FROM dim_industry ORDER BY code"):
        ind[code] = {
            "n": name,
            "c": align(con, "fact_industry_daily", code, dates, "close"),
            "p": align(con, "fact_industry_daily", code, dates, "pct_chg"),
            "a": align(con, "fact_industry_daily", code, dates, "amount", E8, 3),
            "t": align(con, "fact_industry_daily", code, dates, "attention"),
        }
    # ③ 指数
    idx = {}
    for code, name in q(con, "SELECT code,name FROM dim_index ORDER BY code"):
        idx[code] = {
            "n": name,
            "c": align(con, "fact_index_daily", code, dates, "close"),
            "p": align(con, "fact_index_daily", code, dates, "pct_chg"),
        }
    # ④ 市場宽度（ユニバース内 ≈160 銘柄ベース、口径はページに明記）
    mkt = {}
    for d, up, down, flat, amt, avg in q(
            con, "SELECT date,up,down,flat,total_amount,avg_pct "
                 "FROM fact_market_daily ORDER BY date"):
        mkt[d] = [up or 0, down or 0, flat or 0,
                  round((amt or 0) / E8, 3), round(avg or 0, 2)]
    # ⑤ 前場・後場
    intra_dates = [r[0] for r in q(con, "SELECT DISTINCT date FROM fact_intraday_daily "
                                        "ORDER BY date")]
    ipos = {d: i for i, d in enumerate(intra_dates)}
    intra = {"dates": intra_dates, "ind": {}, "idx": {}}
    for kind, key in (("ind", "ind"), ("idx", "idx")):
        tmp = {}
        for code, d, am, pm, aa, pa in q(
                con, "SELECT code,date,am_pct,pm_pct,am_amount,pm_amount "
                     "FROM fact_intraday_daily WHERE kind=?", (kind,)):
            if d not in ipos:
                continue
            arr = tmp.setdefault(code, [None] * len(intra_dates))
            arr[ipos[d]] = [round(am, 2) if am is not None else None,
                            round(pm, 2) if pm is not None else None,
                            round((aa or 0) / E8, 3), round((pa or 0) / E8, 3)]
        intra[key] = tmp

    # ⑥ 個別銘柄：売買代金上位（直近 STOCK_WINDOW 営業日）
    stock_dates = [r[0] for r in q(
        con, "SELECT DISTINCT date FROM fact_stock_daily ORDER BY date DESC "
             "LIMIT ?", (STOCK_WINDOW,))]
    stock_dates = sorted(stock_dates)
    rank, ind_top = {}, {}
    for d in stock_dates:
        rows = q(con, "SELECT code,name,ind_code,close,pct_chg,amount "
                      "FROM fact_stock_daily WHERE date=? AND amount IS NOT NULL "
                      "ORDER BY amount DESC LIMIT ?", (d, RANK_PER_DAY))
        rank[d] = [[c, n, ic, round(cl, 1) if cl else None,
                    round(p, 2) if p is not None else None,
                    round((a or 0) / E8, 3)] for c, n, ic, cl, p, a in rows]
    # ドロワー用：最新営業日の業種別上位銘柄
    if stock_dates:
        d0 = stock_dates[-1]
        for (ic,) in q(con, "SELECT DISTINCT ind_code FROM fact_stock_daily "
                            "WHERE date=? AND ind_code IS NOT NULL", (d0,)):
            rows = q(con, "SELECT code,name,close,pct_chg,amount FROM fact_stock_daily "
                          "WHERE date=? AND ind_code=? ORDER BY amount DESC LIMIT ?",
                     (d0, ic, IND_TOP_N))
            ind_top[ic] = [[c, n, round(cl, 1) if cl else None,
                            round(p, 2) if p is not None else None,
                            round((a or 0) / E8, 3)] for c, n, cl, p, a in rows]

    # ⑦ 時間整合：partial（前場引けスナップショット）判定
    today = date.today().isoformat()
    bars_max = 0
    if dates and dates[-1] == today:
        r = q(con, "SELECT MAX(bars) FROM fact_intraday_daily WHERE date=?", (today,))
        bars_max = (r[0][0] or 0) if r else 0
    is_partial = bool(dates) and dates[-1] == today and bars_max < BARS_FULL_DAY

    meta = {
        "latest": dates[-1] if dates else None,
        "indStart": dates[0] if dates else None,
        "indEnd": dates[-1] if dates else None,
        "indDays": len(dates),
        "intraStart": intra_dates[0] if intra_dates else None,
        "intraEnd": intra_dates[-1] if intra_dates else None,
        "intraDays": len(intra_dates),
        "stockStart": stock_dates[0] if stock_dates else None,
        "stockEnd": stock_dates[-1] if stock_dates else None,
        "stockDays": len(stock_dates),
        "stockDate": stock_dates[-1] if stock_dates else None,
        "narrativeDate": narr.get("narrative_date"),
        "generated": today,
        "partial": is_partial,
        "bars": bars_max,
    }
    return {"meta": meta, "dates": dates, "ind": ind, "idx": idx, "mkt": mkt,
            "intra": intra, "rank": rank, "indTop": ind_top, "narr": narr}


def _kv(arr, i, fmt):
    v = arr[i] if (arr and i < len(arr)) else None
    return fmt.format(v) if v is not None else "—"


def render(target_date=None, out=OUT_PATH, check_only=False):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = None
    if not q(con, "SELECT COUNT(*) FROM fact_index_daily WHERE code='^N225'"):
        raise SystemExit("庫内にデータがありません → 先に pipeline_jp.py backfill")

    narr = json.loads(NARR_PATH.read_text(encoding="utf-8")) \
        if NARR_PATH.exists() else {}
    bundle = build_bundle(con, narr)
    d = target_date if target_date in bundle["dates"] else bundle["meta"]["latest"]
    i = bundle["dates"].index(d)

    # narrative の HTML 断片を {{X}} 形式で埋め込む
    data_js = ("/* generated by render_jp.py · source japanstock.db · 既定選択日 %s */\n"
               "  var D = %s;\n") % (
        d, json.dumps(bundle, ensure_ascii=False, separators=(",", ":")))

    d0 = date.fromisoformat(d)
    tok = {
        "DATE": d,
        "WEEKDAY": WEEKDAYS[d0.weekday()],
        "NARR_DATE": narr.get("narrative_date") or d,
        "IND_START": bundle["meta"]["indStart"] or "—",
        "GENERATED": bundle["meta"]["generated"],
    }
    doc = TPL_PATH.read_text(encoding="utf-8")
    doc = doc.replace("/*__DATA__*/", data_js)
    for k, v in narr.items():
        if isinstance(v, str) and ("{{" + k + "}}") in doc:
            doc = doc.replace("{{" + k + "}}", v)
    for k, v in tok.items():
        doc = doc.replace("{{%s}}" % k, str(v))
    left = sorted(set(re.findall(r"\{\{[A-Z_]+\}\}", doc)))

    # ---------- 校验出力（日文ラベル）----------
    ind = bundle["ind"]
    elec = ind.get("1625.T")     # 電機・精密
    bank = ind.get("1631.T")     # 銀行
    att = sorted(((v["n"], v["t"][i]) for v in ind.values()
                  if v["t"][i] is not None), key=lambda x: -x[1])
    mkt = bundle["mkt"].get(d)
    print(f"既定選択日    : {d}（庫内最新 {bundle['meta']['latest']}）")
    print(f"営業日軸      : {len(bundle['dates'])} 日 "
          f"{bundle['dates'][0]} ~ {bundle['dates'][-1]}")
    print(f"業種/指数     : {len(ind)} 業種 / {len(bundle['idx'])} 指数")
    print(f"前場後場      : {bundle['meta']['intraDays']} 日 "
          f"({bundle['meta']['intraStart']} ~ {bundle['meta']['intraEnd']})")
    print(f"個別銘柄      : {bundle['meta']['stockDays']} 日 "
          f"({bundle['meta']['stockStart']} ~ {bundle['meta']['stockEnd']})")
    print(f"--- 検証値（{d}）---")
    print(f"日経平均      : {_kv(bundle['idx']['^N225']['c'], i, '{:,.2f}')}  "
          f"{_kv(bundle['idx']['^N225']['p'], i, '{:+.2f}%')}")
    print(f"TOPIX(1306)   : {_kv(bundle['idx'].get('1306.T', {}).get('c', []), i, '{:,.2f}')}  "
          f"{_kv(bundle['idx'].get('1306.T', {}).get('p', []), i, '{:+.2f}%')}")
    print(f"電機・精密    : 終値 {_kv(elec['c'], i, '{:,.1f}')}  "
          f"{_kv(elec['p'], i, '{:+.2f}%')}  注目度 {_kv(elec['t'], i, '{:.2f}x')}")
    print(f"銀行          : 終値 {_kv(bank['c'], i, '{:,.1f}')}  "
          f"{_kv(bank['p'], i, '{:+.2f}%')}  注目度 {_kv(bank['t'], i, '{:.2f}x')}")
    print(f"騰落/売買代金 : {mkt if mkt else '当該日データなし'}")
    if att:
        print(f"注目度 上位   : {att[0][0]} {att[0][1]:.2f}x ... "
              f"{att[-1][0]} {att[-1][1]:.2f}x")
    r = bundle["rank"].get(d)
    print(f"売買代金上位  : {r[0][1] if r else '—'} "
          f"（{len(r) if r else 0} 銘柄収録）")
    print(f"未置換スロット: {left if left else 'なし'}")

    if not check_only:
        out.write_text(doc, encoding="utf-8")
        print(f"出力          : {out.name}  {len(doc.encode()) / 1024:.1f} KB")
    con.close()
    return doc


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="日股ダッシュボード・レンダラ")
    ap.add_argument("--date", default=None)
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    render(a.date, Path(a.out), a.check)
