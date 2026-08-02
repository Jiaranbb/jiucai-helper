#!/usr/bin/env python3
"""jiucai-helper 内置行情工具（富途 OpenAPI）。

自包含：只依赖 pip 包 futu-api 与一台可达的 OpenD（富途行情网关）。
不依赖任何其他 skill。

用法:
  python3 futu_quote.py snapshot SZ.002281 [US.NVDA ...]     # 实时快照
  python3 futu_quote.py kline SZ.002281 --start 2025-07-28 --end 2026-07-27 [--ktype 1d]
  python3 futu_quote.py range SZ.002281 [--days 365]         # 区间高低点/现价/回撤摘要

环境变量:
  FUTU_OPEND_HOST  OpenD 地址（默认 127.0.0.1）
  FUTU_OPEND_PORT  OpenD 端口（默认 11111）

输出: stdout 最后一行为单行 JSON（SDK 日志已抑制；稳妥起见解析时取最后一行）。
失败时输出 {"error": "..."} 并以非零码退出。
代码格式: SZ.002281 / SH.600519 / HK.00700 / US.NVDA

⚠️ 配额警告: kline/range 走 request_history_kline，有滚动限额（约 1000 次/周）。
A股历史 K 线请改用 fallback_quote.py（baostock 免费）；本脚本的 kline/range 仅限
港美股或备源不可用场景。snapshot 不消耗该限额，可放心用。批量/回测禁用本脚本。
"""
import argparse
import datetime as dt
import json
import logging
import os
import sys

logging.disable(logging.CRITICAL)  # 必须在 import futu 之前，抑制 SDK 日志


def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False, default=str))
    sys.exit(code)


def get_ctx():
    try:
        from futu import OpenQuoteContext
    except ImportError:
        out({"error": "futu-api 未安装。运行: pip3 install futu-api"}, 1)
    host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
    port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
    try:
        return OpenQuoteContext(host=host, port=port)
    except Exception as e:
        out({"error": f"无法连接 OpenD ({host}:{port}): {e}。请确认 OpenD 已启动并已登录。"}, 1)


def df_records(df):
    return json.loads(df.to_json(orient="records"))


def fetch_kline(ctx, code, start, end, ktype_str):
    from futu import RET_OK, KLType, AuType, KL_FIELD
    ktype = {"1d": KLType.K_DAY, "1w": KLType.K_WEEK, "1m": KLType.K_MON}.get(ktype_str)
    if ktype is None:
        out({"error": f"不支持的 ktype: {ktype_str}（可选 1d/1w/1m）"}, 1)
    rows, page_key = [], None
    while True:
        ret, data, page_key = ctx.request_history_kline(
            code, start=start, end=end, ktype=ktype, autype=AuType.QFQ,
            fields=[KL_FIELD.ALL], max_count=1000, page_req_key=page_key)
        if ret != RET_OK:
            out({"error": f"request_history_kline 失败: {data}"}, 1)
        rows.extend(df_records(data))
        if page_key is None:
            break
    return rows


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("snapshot")
    sp.add_argument("codes", nargs="+")
    kp = sub.add_parser("kline")
    kp.add_argument("code")
    kp.add_argument("--start", required=True)
    kp.add_argument("--end", required=True)
    kp.add_argument("--ktype", default="1d")
    rp = sub.add_parser("range")
    rp.add_argument("code")
    rp.add_argument("--days", type=int, default=365)
    args = p.parse_args()

    ctx = get_ctx()
    try:
        from futu import RET_OK
        if args.cmd == "snapshot":
            ret, data = ctx.get_market_snapshot(args.codes)
            if ret != RET_OK:
                out({"error": f"get_market_snapshot 失败: {data}"}, 1)
            keep = ["code", "name", "update_time", "last_price", "open_price",
                    "high_price", "low_price", "prev_close_price", "volume",
                    "turnover", "turnover_rate", "pe_ratio", "pe_ttm_ratio",
                    "pb_ratio", "total_market_val", "circular_market_val",
                    "highest52weeks_price", "lowest52weeks_price"]
            recs = []
            for r in df_records(data):
                recs.append({k: r[k] for k in keep if k in r and r[k] not in (None, "N/A")})
            out({"data": recs})
        elif args.cmd == "kline":
            rows = fetch_kline(ctx, args.code, args.start, args.end, args.ktype)
            out({"code": args.code, "ktype": args.ktype, "count": len(rows), "data": rows})
        elif args.cmd == "range":
            end = dt.date.today()
            start = end - dt.timedelta(days=args.days)
            rows = fetch_kline(ctx, args.code, str(start), str(end), "1d")
            if not rows:
                out({"error": "区间内无 K 线数据"}, 1)
            hi = max(rows, key=lambda r: r["high"])
            lo = min(rows, key=lambda r: r["low"])
            last = rows[-1]
            out({"code": args.code, "start": str(start), "end": str(end),
                 "high": hi["high"], "high_date": str(hi["time_key"])[:10],
                 "low": lo["low"], "low_date": str(lo["time_key"])[:10],
                 "last_close": last["close"], "last_date": str(last["time_key"])[:10],
                 "drawdown_from_high_pct": round((last["close"] / hi["high"] - 1) * 100, 1),
                 "gain_from_low_pct": round((last["close"] / lo["low"] - 1) * 100, 1)})
    finally:
        ctx.close()


if __name__ == "__main__":
    main()
