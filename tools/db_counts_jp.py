# -*- coding: utf-8 -*-
"""日股工作台 · 店卸後確認ツール（読み取り専用）

各表の最新日・行数・カバレッジを一覧表示し、異常（欠損・幽灵行）を検出する。

  python tools/db_counts_jp.py
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import jpcommon  # noqa: E402

from jpcommon import DB_PATH, IND17, INDICES_JP  # noqa: E402
from universe_seed import UNIVERSE_SEED  # noqa: E402


def main():
    con = sqlite3.connect(DB_PATH)
    print(f"DB: {DB_PATH}")
    print(f"サイズ: {DB_PATH.stat().st_size / 1024:.0f} KB\n")

    print("=== 行数と期間 ===")
    for t, col in (("dim_industry", None), ("dim_index", None), ("dim_stock", None),
                   ("fact_industry_daily", "date"), ("fact_index_daily", "date"),
                   ("fact_market_daily", "date"), ("fact_stock_daily", "date"),
                   ("fact_intraday_daily", "date"), ("raw_fetch", None)):
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        rng = ""
        if col:
            r = con.execute(f"SELECT MIN({col}),MAX({col}) FROM {t}").fetchone()
            if r and r[0]:
                rng = f"  {r[0]} 〜 {r[1]}"
        print(f"{t:<22}{n:>8}{rng}")

    print("\n=== 業種 ETF カバレッジ（直近 10 営業日）===")
    days = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM fact_industry_daily ORDER BY date DESC LIMIT 10")]
    for d in sorted(days):
        n = con.execute("SELECT COUNT(*) FROM fact_industry_daily WHERE date=? "
                        "AND close IS NOT NULL", (d,)).fetchone()[0]
        att = con.execute("SELECT COUNT(*) FROM fact_industry_daily WHERE date=? "
                          "AND attention IS NOT NULL", (d,)).fetchone()[0]
        flag = "" if n == len(IND17) else f"  ⚠ 欠損（期待 {len(IND17)}）"
        print(f"  {d}  終値 {n:>2}/{len(IND17)}  注目度 {att:>2}{flag}")

    print("\n=== 個別銘柄スナップショット（直近 5 営業日）===")
    for d in [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM fact_stock_daily ORDER BY date DESC LIMIT 5")]:
        n = con.execute("SELECT COUNT(*) FROM fact_stock_daily WHERE date=?",
                        (d,)).fetchone()[0]
        ni = con.execute("SELECT COUNT(DISTINCT ind_code) FROM fact_stock_daily "
                         "WHERE date=? AND ind_code IS NOT NULL", (d,)).fetchone()[0]
        p = con.execute("SELECT COUNT(*) FROM fact_stock_daily WHERE date=? "
                        "AND pct_chg IS NOT NULL", (d,)).fetchone()[0]
        print(f"  {d}  {n:>3} 銘柄 / {ni:>2} 業種 / 前日比あり {p:>3}")

    print("\n=== 前場・後場カバレッジ（直近 5 営業日）===")
    # ⚠️ 日中の集計対象は株・株式指数のみ（JPY=X は 24 時間取引のため除外）
    exp = len(IND17) + len([c for c in INDICES_JP if c != "JPY=X"])
    for d in [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM fact_intraday_daily ORDER BY date DESC LIMIT 5")]:
        n = con.execute("SELECT COUNT(*) FROM fact_intraday_daily WHERE date=?",
                        (d,)).fetchone()[0]
        bars = con.execute("SELECT DISTINCT bars FROM fact_intraday_daily WHERE date=?",
                           (d,)).fetchall()
        flag = "" if n >= exp * 0.9 else f"  ⚠ 欠損（期待 {exp}）"
        print(f"  {d}  {n:>2}/{exp}  bars={[b[0] for b in bars]}{flag}")

    print("\n=== 幽灵行チェック（当該日の銘柄数がユニバース数を超えていないか）===")
    un = len(UNIVERSE_SEED)
    bad = con.execute("SELECT date, COUNT(*) c FROM fact_stock_daily "
                      "GROUP BY date HAVING c > ?", (un + 5,)).fetchall()
    print("  なし" if not bad else f"  ⚠ {bad}")

    n = con.execute("SELECT COUNT(*) FROM raw_fetch WHERE note LIKE '%SKIP%' "
                    "OR cmd LIKE '%no-data%'").fetchone()[0]
    print(f"\n取数ログのスキップ件数: {n}（raw_fetch 参照）")
    con.close()


if __name__ == "__main__":
    main()
