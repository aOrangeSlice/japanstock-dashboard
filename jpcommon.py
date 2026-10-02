# -*- coding: utf-8 -*-
"""
日股工作台 · 公共模块

当前只有一件事：**修复 libcurl 的 CA 信任链**。

背景（Phase 1 实测踩坑）：
  本项目路径含中文（`日股工作台`），libcurl（yfinance 1.7 的默认 HTTP 后端
  是 curl_cffi）**无法从含非 ASCII 字符的路径加载 CA 文件**，报错：
      curl: (77) error adding trust anchors from locations: CAfile: ...日股工作台/...
  表现为所有请求失败、被误判成「数据源不可用」或限流。

对策：把 certifi 的 cacert.pem 复制到系统临时目录（ASCII 路径）后，
      通过 CURL_CA_BUNDLE / SSL_CERT_FILE 环境变量指过去。
      `import jpcommon` 即自动完成，无需手工设置环境变量。
"""
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def ensure_ca():
    """把 CA 包放到 ASCII 路径并注入环境变量。必须在 import yfinance 之前调用。"""
    try:
        import certifi
    except ImportError:
        return None
    src = Path(certifi.where())
    dst = Path(tempfile.gettempdir()) / "jp_wb_cacert.pem"
    try:
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copyfile(src, dst)
    except OSError:
        return None
    os.environ.setdefault("CURL_CA_BUNDLE", str(dst))
    os.environ.setdefault("SSL_CERT_FILE", str(dst))
    return str(dst)


CA_BUNDLE = ensure_ca()

# 目录约定（与 A 股工作台保持一致）
DB_PATH = ROOT / "japanstock.db"
RAW_DIR = ROOT / "raw"
PROBE_DIR = ROOT / "probe"

# ---------------------------------------------------------------- 交易日历锚
# ⚠️ 双锚（2026-10-03 云部署首跑实测踩坑）：Yahoo 的 ^N225 指数日线在收盘后
#    存在「bar 在但 Close=NaN」的滞后窗口（官方收盘延迟发布）→ _clean() 的
#    dropna 会把该日整行删掉，导致日历缺日 → render 日期轴停在前一日、
#    前場/後場被日历守卫误杀。1306.T（TOPIX ETF）是实时行情源，close 永不缺。
#    → 交易日历一律取 (^N225 ∪ 1306.T) 的日期并集。
CALENDAR_CODES = ("^N225", "1306.T")

# ---------------------------------------------------------------- 字典表
# TOPIX-17 系列 ETF（NEXT FUNDS TOPIX-17 シリーズ）→ 行业代理
# ⚠️ 代码 ↔ 行业名的对应关系已由 Phase 1 探针实测确认（见 probe/probe_report.md）
IND17 = {
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

# 指数与宽基（顶部状态条 + 风格面板）
# ⚠️ Phase 1 探针实测：Yahoo 上 **^TOPX 不存在**（返回空）→ TOPIX 用
#    1306.T（NEXT FUNDS TOPIX ETF）代理，页面口径须标注「ETF 代理」。
INDICES_JP = {
    "^N225": "日経平均株価",
    "1306.T": "TOPIX",           # ← 1306.T 代理
    "2513.T": "プライム市場指数",
    "2516.T": "グロース市場250",
    "JPY=X": "ドル円",
}

# 日経225 主要成分（个股榜 universe；成交额ランキング用，非全量 225 只，
# 由 tools/build_universe.py 从公开清单生成后落到 narrative 侧）
DEFAULT_UNIVERSE_FILE = ROOT / "universe_jp225.json"
