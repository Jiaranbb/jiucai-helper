#!/usr/bin/env python3
"""jiucai-helper 备用行情工具（A股 akshare → baostock；港股 Yahoo 延迟行情）。

定位：futu OpenD 不可用时的行情降级方案；其中 valuation 子命令（估值历史分位）
即使 futu 可用也是首选——futu 快照只给当前 PE，历史分位要靠 baostock 日频序列。

自包含：只依赖 pip 包 akshare、baostock（pip3 install akshare baostock）。
支持 A股（SZ.002281 / SH.600519）与港股快照（HK.00941）；美股降级为联网搜索。

用法:
  python3 fallback_quote.py snapshot SZ.002281            # 实时快照（东财）
  python3 fallback_quote.py kline SZ.002281 --start 2025-07-28 --end 2026-07-27
  python3 fallback_quote.py range SZ.002281 [--days 365]  # 区间高低/现价/回撤
  python3 fallback_quote.py valuation SZ.002281 [--years 5]   # peTTM/pbMRQ 现值+历史分位

输出: stdout 最后一行为单行 JSON，含 "source" 字段标注实际命中源（降级留痕）。
失败时输出 {"error": "..."} 并以非零码退出。
"""
import argparse
import contextlib
import datetime as dt
import io
import json
import sys
import warnings
from urllib.parse import quote
from urllib.request import Request, urlopen

warnings.filterwarnings("ignore")


def out(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False, default=str))
    sys.exit(code)


def parse_code(code):
    code = code.strip().upper()
    if "." in code:
        mkt, num = code.split(".", 1)
    else:
        out({"error": f"代码格式应为 SZ.002281 / SH.600519 / HK.00941，收到: {code}"}, 1)
    if mkt not in ("SZ", "SH", "HK"):
        out({"error": f"仅支持 A股与港股快照；{mkt} 市场请用 futu 主通道或联网搜索"}, 1)
    return mkt.lower(), num  # ("sz", "002281")


def yahoo_hk_snapshot(num):
    """标准库读取 Yahoo 港股延迟快照；免费源仅作降级展示，关键价格需复核。"""
    yahoo_num = num.lstrip("0").zfill(4)
    symbol = f"{yahoo_num}.HK"
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(symbol)}?range=5d&interval=1d"
    )
    req = Request(url, headers={"User-Agent": "jiucai-helper/1.0"})
    with urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise RuntimeError("Yahoo 港股快照返回空")
    meta = result.get("meta") or {}
    price = meta.get("regularMarketPrice")
    closes = (((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
    closes = [float(value) for value in closes if value is not None]
    prev = closes[-2] if len(closes) >= 2 else meta.get("previousClose")
    if price is None:
        raise RuntimeError("Yahoo 港股快照缺少价格")
    pct = round((price / prev - 1) * 100, 2) if prev else None
    stamp = meta.get("regularMarketTime")
    at = dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat() if stamp else None
    return {
        "source": "Yahoo Finance 港股延迟行情（免费降级源）",
        "data": {
            "code": f"HK.{num}",
            "name": meta.get("shortName") or meta.get("longName") or symbol,
            "last_price": float(price),
            "prev_close": float(prev) if prev else None,
            "pct_chg": pct,
            "date": at,
        },
    }


def bs_kline(mkt, num, start, end, fields="date,open,high,low,close,volume"):
    import baostock as bs
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            f"{mkt}.{num}", fields, start_date=start, end_date=end,
            frequency="d", adjustflag="2")  # 2=前复权
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(dict(zip(rs.fields, rs.get_row_data())))
        if rs.error_code != "0":
            raise RuntimeError(f"baostock 取数失败: {rs.error_msg}")
        return rows
    finally:
        with contextlib.redirect_stdout(buf):
            bs.logout()


def ak_kline(num, start, end):
    import akshare as ak
    df = ak.stock_zh_a_hist(symbol=num, period="daily",
                            start_date=start.replace("-", ""),
                            end_date=end.replace("-", ""), adjust="qfq")
    if df is None or df.empty:
        raise RuntimeError("akshare 日K 返回空")
    return [{"date": str(r["日期"]), "open": float(r["开盘"]), "high": float(r["最高"]),
             "low": float(r["最低"]), "close": float(r["收盘"]),
             "volume": float(r["成交量"])} for _, r in df.iterrows()]


def get_kline_chain(mkt, num, start, end):
    try:
        return ak_kline(num, start, end), "akshare:stock_zh_a_hist(前复权)"
    except Exception as e1:
        try:
            rows = bs_kline(mkt, num, start, end)
            for r in rows:
                for k in ("open", "high", "low", "close", "volume"):
                    r[k] = float(r[k] or 0)
            return rows, f"baostock:query_history_k_data_plus(前复权)[akshare失败:{e1}]"
        except Exception as e2:
            out({"error": f"源链全败 akshare:{e1} | baostock:{e2}"}, 1)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("snapshot"); sp.add_argument("code")
    kp = sub.add_parser("kline"); kp.add_argument("code")
    kp.add_argument("--start", required=True); kp.add_argument("--end", required=True)
    rp = sub.add_parser("range"); rp.add_argument("code")
    rp.add_argument("--days", type=int, default=365)
    vp = sub.add_parser("valuation"); vp.add_argument("code")
    vp.add_argument("--years", type=int, default=5)
    args = p.parse_args()
    mkt, num = parse_code(args.code)

    if mkt == "hk":
        if args.cmd != "snapshot":
            out({"error": "免费港股源仅支持 snapshot；历史 K 线请使用 futu 或联网取证"}, 1)
        try:
            out(yahoo_hk_snapshot(num))
        except Exception as e:
            out({"error": f"港股免费源失败: {e}"}, 1)

    if args.cmd == "snapshot":
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            row = df[df["代码"] == num]
            if row.empty:
                raise RuntimeError(f"东财实时表中未找到 {num}")
            r = row.iloc[0]
            out({"source": "akshare:stock_zh_a_spot_em(东财实时)",
                 "data": {"code": args.code, "name": str(r["名称"]),
                          "last_price": float(r["最新价"]), "pct_chg": float(r["涨跌幅"]),
                          "pe_dynamic": float(r["市盈率-动态"]), "pb": float(r["市净率"]),
                          "total_market_val": float(r["总市值"]),
                          "circular_market_val": float(r["流通市值"]),
                          "turnover_rate": float(r["换手率"])}})
        except SystemExit:
            raise
        except Exception as e1:
            end = str(dt.date.today()); start = str(dt.date.today() - dt.timedelta(days=10))
            rows = bs_kline(mkt, num, start, end)
            if not rows:
                out({"error": f"akshare 失败({e1})且 baostock 无近日数据"}, 1)
            last = rows[-1]
            out({"source": f"baostock 最新收盘(非实时)[akshare失败:{e1}]",
                 "data": {"code": args.code, "date": last["date"],
                          "last_close": float(last["close"])}})
    elif args.cmd == "kline":
        rows, src = get_kline_chain(mkt, num, args.start, args.end)
        out({"source": src, "code": args.code, "count": len(rows), "data": rows})
    elif args.cmd == "range":
        end = dt.date.today(); start = end - dt.timedelta(days=args.days)
        rows, src = get_kline_chain(mkt, num, str(start), str(end))
        if not rows:
            out({"error": "区间内无数据"}, 1)
        hi = max(rows, key=lambda r: r["high"]); lo = min(rows, key=lambda r: r["low"])
        last = rows[-1]
        out({"source": src, "code": args.code, "start": str(start), "end": str(end),
             "high": hi["high"], "high_date": hi["date"],
             "low": lo["low"], "low_date": lo["date"],
             "last_close": last["close"], "last_date": last["date"],
             "drawdown_from_high_pct": round((last["close"] / hi["high"] - 1) * 100, 1),
             "gain_from_low_pct": round((last["close"] / lo["low"] - 1) * 100, 1)})
    elif args.cmd == "valuation":
        end = dt.date.today(); start = end - dt.timedelta(days=args.years * 365)
        try:
            rows = bs_kline(mkt, num, str(start), str(end),
                            fields="date,close,peTTM,pbMRQ,psTTM")
            src = "baostock:query_history_k_data_plus(peTTM/pbMRQ 日频)"
        except Exception as e:
            out({"error": f"baostock 估值序列失败: {e}；可改用 akshare stock_zh_valuation_baidu 手工兜底"}, 1)
        series = [(r["date"], float(r["peTTM"] or 0), float(r["pbMRQ"] or 0))
                  for r in rows if r.get("peTTM")]
        if not series:
            out({"error": "估值序列为空"}, 1)
        cur_date, cur_pe, cur_pb = series[-1]
        pes = sorted(x[1] for x in series if x[1] > 0)
        pbs = sorted(x[2] for x in series if x[2] > 0)
        def pct(v, arr):
            return round(sum(1 for x in arr if x <= v) / len(arr) * 100, 1) if arr else None
        out({"source": src, "code": args.code, "as_of": cur_date, "years": args.years,
             "pe_ttm": cur_pe, "pe_ttm_percentile": pct(cur_pe, pes),
             "pb_mrq": cur_pb, "pb_mrq_percentile": pct(cur_pb, pbs),
             "note": "分位数为计算派生；亏损期 peTTM 失真时改看 pbMRQ；周期股分位可能反向误导"})


if __name__ == "__main__":
    main()
