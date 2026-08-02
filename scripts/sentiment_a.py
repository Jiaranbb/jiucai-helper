#!/usr/bin/env python3
"""jiucai-helper A股情绪与资金面仪表（拥挤度检查）。

框架定位：产出不进 H 证据台账（不参与 LR），只作三处输入——
关二拥挤度／时点段位（热情·预支段判定）／F6 类估值预支 falsifier。

数据源全免费：换手率序列 baostock；融资融券、龙虎榜、人气榜 akshare（东财）。
用法：python3 sentiment_a.py SZ.002281 [--days 250]
输出：stdout 最后一行 JSON；各子项独立容错，取不到记入 gaps。仅 A股。
"""
import argparse
import contextlib
import datetime as dt
import io
import json
import sys
import time
import warnings

warnings.filterwarnings("ignore")


def retry(fn, n=3, wait=2):
    last = None
    for i in range(n):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(wait * (i + 1))
    raise last


def main():
    p = argparse.ArgumentParser()
    p.add_argument("code")
    p.add_argument("--days", type=int, default=250)
    args = p.parse_args()
    code = args.code.strip().upper()
    mkt, num = code.split(".", 1)
    if mkt not in ("SZ", "SH"):
        print(json.dumps({"error": "仅支持 A股"})); sys.exit(1)

    out = {"code": code, "as_of": str(dt.date.today()), "gaps": []}

    # 1. 换手率：现值 + 一年分位（baostock turn 字段）
    try:
        import baostock as bs
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bs.login()
        end = dt.date.today(); start = end - dt.timedelta(days=args.days + 120)
        rs = bs.query_history_k_data_plus(f"{mkt.lower()}.{num}", "date,turn,close",
                                          start_date=str(start), end_date=str(end),
                                          frequency="d", adjustflag="2")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        with contextlib.redirect_stdout(buf):
            bs.logout()
        turns = [(r[0], float(r[1])) for r in rows if r[1]]
        turns = turns[-args.days:]
        if turns:
            cur = turns[-1][1]
            arr = sorted(t for _, t in turns)
            pct = round(sum(1 for x in arr if x <= cur) / len(arr) * 100, 1)
            avg20 = round(sum(t for _, t in turns[-20:]) / min(20, len(turns)), 2)
            out["turnover"] = {"current_pct": cur, "pctile_1y": pct,
                               "avg20d": avg20, "n_days": len(turns)}
        else:
            out["gaps"].append("turnover:无数据")
    except Exception as e:
        out["gaps"].append(f"turnover:{str(e)[:60]}")

    # 2. 融资余额趋势（近 30 个自然日两个采样点）
    try:
        import akshare as ak
        fn_map = {"SZ": ak.stock_margin_detail_szse, "SH": ak.stock_margin_detail_sse}
        def margin_at(target):
            for back in range(8):  # 向前找最近交易日
                d = (target - dt.timedelta(days=back)).strftime("%Y%m%d")
                try:
                    df = retry(lambda: fn_map[mkt](date=d), n=2, wait=2)
                    col_code = "证券代码" if "证券代码" in df.columns else "标的证券代码"
                    row = df[df[col_code].astype(str) == num]
                    if not row.empty:
                        col_bal = [c for c in df.columns if "融资余额" in c][0]
                        return d, float(row.iloc[0][col_bal])
                except Exception:
                    continue
            return None, None
        d1, m_now = margin_at(dt.date.today())
        d0, m_prev = margin_at(dt.date.today() - dt.timedelta(days=30))
        if m_now is not None and m_prev:
            out["margin"] = {"latest_date": d1, "balance": m_now,
                             "prev_date": d0, "chg_30d_pct": round((m_now / m_prev - 1) * 100, 1)}
        elif m_now is not None:
            out["margin"] = {"latest_date": d1, "balance": m_now}
            out["gaps"].append("margin:30日前采样缺失")
        else:
            out["gaps"].append("margin:未取到")
    except Exception as e:
        out["gaps"].append(f"margin:{str(e)[:60]}")

    # 3. 龙虎榜（近 30 日该股上榜记录）
    try:
        import akshare as ak
        end = dt.date.today(); start = end - dt.timedelta(days=30)
        df = retry(lambda: ak.stock_lhb_detail_em(
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")))
        col = "代码" if "代码" in df.columns else "股票代码"
        hits = df[df[col].astype(str) == num]
        out["lhb_30d"] = {"times": int(len(hits)),
                          "dates": hits["上榜日"].astype(str).tolist()[:10] if len(hits) else []}
    except Exception as e:
        out["gaps"].append(f"lhb:{str(e)[:60]}")

    # 4. 人气榜排名（东财股吧人气）
    try:
        import akshare as ak
        df = retry(lambda: ak.stock_hot_rank_latest_em(symbol=f"{mkt}{num}"))
        d = dict(zip(df["item"], df["value"])) if "item" in df.columns else {}
        rank = d.get("rank") or d.get("当前排名") or d.get("排名")
        total = d.get("marketAllCount")
        if rank:
            out["hot_rank"] = {"rank": int(rank)}
            if total: out["hot_rank"]["of"] = int(total)
        elif all(v is None for v in d.values()):
            out["hot_rank"] = None  # ETF 等非个股标的无人气榜
        else:
            out["hot_rank"] = {"raw": str(d)[:200]}
    except Exception as e:
        out["gaps"].append(f"hot_rank:{str(e)[:60]}")

    # 5. 拥挤度定性（规则化初判，供裁定引用；阈值为经验值）
    crowd = []
    def g(key):  # 子项可能显式为 None（如 ETF 无人气榜），取值一律经此归一
        return out.get(key) or {}
    t = g("turnover")
    if t.get("pctile_1y", 0) >= 90: crowd.append("换手率处一年 90%+ 分位")
    if g("margin").get("chg_30d_pct", 0) >= 20: crowd.append("融资余额 30 日增逾 20%")
    if g("lhb_30d").get("times", 0) >= 3: crowd.append("30 日内龙虎榜 ≥3 次")
    if g("hot_rank").get("rank", 9999) <= 50: crowd.append("人气榜前 50")
    out["crowding"] = {"flags": crowd, "level": ["低", "中", "高", "极端"][min(len(crowd), 3)]}

    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
