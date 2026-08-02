#!/usr/bin/env python3
"""jiucai-helper 本地持仓观测台服务。

零第三方依赖（标准库）。数据源＝TRACK_DIR 档案 Markdown（唯一事实源，只读不写）；
行情按需经内置脚本刷新（futu 快照优先，A股备源兜底），缓存 quotes.json。

启动：/usr/bin/python3 ~/.codex/skills/jiucai-helper/webapp/server.py
访问：http://localhost:8787
环境变量：JIUCAI_TRACK_DIR / FUTU_OPEND_HOST / JIUCAI_PORT 可覆盖默认。
"""
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
TRACK_DIR = os.environ.get("JIUCAI_TRACK_DIR",
                           os.path.expanduser("~/Documents/jiucai-helper/标的跟踪"))
FUTU_HOST = os.environ.get("FUTU_OPEND_HOST", "127.0.0.1")
FUTU_PORT = int(os.environ.get("FUTU_OPEND_PORT", "11111"))
PORT = int(os.environ.get("JIUCAI_PORT", "8787"))


def runtime_python():
    """优先使用 bootstrap.sh 创建的独立运行环境，不污染系统 Python。"""
    configured = os.environ.get("JIUCAI_PYTHON")
    runtime_dir = os.environ.get(
        "JIUCAI_RUNTIME_DIR",
        os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
                     "jiucai-helper"),
    )
    candidates = [
        configured,
        os.path.join(runtime_dir, "venv", "bin", "python"),
        os.path.join(runtime_dir, "venv", "Scripts", "python.exe"),
        sys.executable,
        "/usr/bin/python3",
    ]
    return next((path for path in candidates if path and os.path.isfile(path)), "python3")


PY = runtime_python()
QUOTES_TTL = 15 * 60
_lock = threading.Lock()
ACCOUNTS_FILE = os.path.join(HERE, "accounts.json")
DEMO_DIR = os.path.join(SKILL_DIR, "demo", "模拟账号")


def load_accounts():
    accounts = []
    try:
        if os.path.exists(ACCOUNTS_FILE):
            accounts = json.load(open(ACCOUNTS_FILE, encoding="utf-8"))
    except Exception:
        accounts = []
    if not isinstance(accounts, list) or not accounts:
        accounts = [{"id": "main", "name": "主组合", "track_dir": TRACK_DIR}]
    if os.path.isdir(DEMO_DIR) and not any(a.get("id") == "demo" for a in accounts):
        accounts.append({"id": "demo", "name": "模拟账号", "track_dir": DEMO_DIR})
    return accounts


def account_dir(aid):
    for a in load_accounts():
        if a.get("id") == aid:
            path = a.get("track_dir") or TRACK_DIR
            if not os.path.isabs(path):
                path = os.path.abspath(os.path.join(HERE, path))
            return path
    return TRACK_DIR


# ---------- Markdown 档案解析 ----------

def parse_frontmatter(text):
    fm = {}
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return fm, text
    for line in m.group(1).splitlines():
        mm = re.match(r"^(\w+):\s*(.*)$", line)
        if mm:
            fm[mm.group(1)] = mm.group(2).strip()
    return fm, text[m.end():]


def split_sections(body):
    """按 '## ' 标题切分，返回 [(heading, content)]。"""
    parts = re.split(r"^## +(.+)$", body, flags=re.MULTILINE)
    out = []
    for i in range(1, len(parts), 2):
        out.append((parts[i].strip(), parts[i + 1] if i + 1 < len(parts) else ""))
    return out


def table_rows(content):
    """解析 markdown 表格行（跳过表头与分隔行），返回单元格列表的列表。"""
    rows = []
    for ln in content.splitlines():
        ln = ln.strip()
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        rows.append(cells)
    return rows


def strip_md(s, limit=None):
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = s.replace("**", "").replace("`", "")
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit] + "…"
    return s


def parse_dossier(path):
    text = open(path, encoding="utf-8").read()
    fm, body = parse_frontmatter(text)
    d = {"file": os.path.basename(path), "fm": fm}
    secs = split_sections(body)

    def sec(kw):
        for h, c in secs:
            if kw in h:
                return c
        return ""

    # 一句话状态（跳过引用框与空行）
    s0 = sec("一句话状态")
    lines = [ln.strip() for ln in s0.splitlines()
             if ln.strip() and not ln.strip().startswith(">")]
    d["summary"] = strip_md(" ".join(lines), 500)

    # 白话结论（人话版）
    sp = sec("白话结论")
    plines = [ln.strip() for ln in sp.splitlines() if ln.strip()]
    d["plain"] = strip_md(" ".join(plines), 600) if plines else None

    # 持仓上下文
    ctx = {}
    for cells in table_rows(sec("持仓上下文")):
        if len(cells) >= 2 and cells[0] not in ("项",):
            ctx[strip_md(cells[0])] = strip_md(cells[1], 80)
    cost = None
    if ctx.get("持仓成本"):
        mm = re.search(r"([\d,]+(?:\.\d+)?)", ctx["持仓成本"])
        if mm:
            cost = float(mm.group(1).replace(",", ""))
    shares = None
    if ctx.get("持仓数"):
        mm = re.search(r"([\d,]+)", ctx["持仓数"].replace(",", ""))
        if mm:
            shares = int(mm.group(1))
    d["context"] = ctx
    d["cost"] = cost
    d["shares"] = shares
    ed = None
    dates = []
    for k, v in ctx.items():
        if "建仓" in k:
            for mm in re.finditer(r"(\d{4})[-/年](\d{1,2})(?:[-/月](\d{1,2}))?", v):
                dates.append("%s-%02d-%02d" % (mm.group(1), int(mm.group(2)),
                                               int(mm.group(3) or 15)))
    if dates:
        ed = min(dates)  # 首笔口径：多笔取最早
    d["entry_date"] = ed
    dv = 0.0
    if ctx.get("累计分红"):
        mm = re.search(r"([\d,]+(?:\.\d+)?)", ctx["累计分红"].replace(",", ""))
        if mm:
            dv = float(mm.group(1))
    d["dividends"] = dv
    cc = ctx.get("币种", "")
    d["currency"] = "HKD" if "港" in cc else ("USD" if "美" in cc else "CNY")

    # Falsifier 看板
    fals = []
    for cells in table_rows(sec("Falsifier")):
        if len(cells) < 3 or cells[0] in ("#",):
            continue
        sev = next((c for c in cells if c in ("红", "黄")), "")
        status = ""
        for c in cells:
            for icon in ("🔴", "🟡", "🟢", "⚪"):
                if icon in c:
                    status = icon
                    break
            if status:
                break
        fals.append({"id": strip_md(cells[0], 6), "cond": strip_md(cells[1], 90),
                     "sev": sev, "status": status or "⚪"})
    d["falsifiers"] = fals

    # 最新决议
    rows = [r for r in table_rows(sec("决议记录"))
            if len(r) >= 3 and r[0] not in ("日期",)]
    if rows:
        last = rows[-1]
        d["last_decision"] = {"date": strip_md(last[0], 24),
                              "verdict": strip_md(last[2], 120)}

    # 拥挤度（正文任意位置）
    mm = re.search(r"拥挤度[^\n]{0,80}?[「\*]*(低|中|高|极端)[」\*]", body)
    d["crowding"] = mm.group(1) if mm else None
    return d


def load_dossiers(base=None):
    base = base or TRACK_DIR
    out, errors = [], []
    if not os.path.isdir(base):
        return [], [f"档案目录不存在: {base}"]
    for fn in sorted(os.listdir(base)):
        if not fn.endswith(".md") or fn.startswith("_"):
            continue
        try:
            out.append(parse_dossier(os.path.join(base, fn)))
        except Exception as e:
            errors.append(f"{fn}: {e}")
    return out, errors


def load_methodlog(base=None):
    path = os.path.join(base or TRACK_DIR, "_Method_Log.md")
    rows = []
    if os.path.exists(path):
        text = open(path, encoding="utf-8").read()
        seg = text.split("## 裁定追踪记分")[-1]
        for cells in table_rows(seg):
            if len(cells) >= 6 and cells[0] not in ("裁定日",):
                rows.append([strip_md(c, 70) for c in cells[:9]])
    return rows


def load_common_factors(base=None):
    path = os.path.join(base or TRACK_DIR, "_Index.md")
    rows = []
    if os.path.exists(path):
        text = open(path, encoding="utf-8").read()
        if "组合共同因子" in text:
            seg = text.split("组合共同因子")[-1]
            seg = seg.split("→ 方法后验日志")[0]
            for cells in table_rows(seg):
                if len(cells) >= 4 and cells[0] not in ("共同因子",):
                    rows.append([strip_md(c, 130) for c in cells[:4]])
    return rows


# ---------- 行情 ----------

def futu_code(ticker):
    # 仅四个可报价市场；OF（场外基金）／BD（债券）／含非 ASCII 的待补码一律跳过
    mm = re.match(r"^(SH|SZ|HK|US)([A-Za-z0-9]+)$", ticker or "")
    return f"{mm.group(1)}.{mm.group(2)}" if mm else None


def run_quote(script, code, extra=None):
    codes = code if isinstance(code, list) else [code]
    cmd = [PY, os.path.join(SKILL_DIR, "scripts", script), "snapshot"] + codes
    env = dict(os.environ, FUTU_OPEND_HOST=FUTU_HOST, FUTU_OPEND_PORT=str(FUTU_PORT))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
        for ln in reversed(r.stdout.strip().splitlines()):
            ln = ln.strip()
            if ln.startswith("{"):
                return json.loads(ln)
    except Exception:
        pass
    return None


def futu_reachable():
    """快速探测 OpenD；不可达时立即走免费源，避免按市场累计等待子进程超时。"""
    try:
        with socket.create_connection((FUTU_HOST, FUTU_PORT), timeout=0.8):
            return True
    except OSError:
        return False


def fetch_quotes(tickers):
    out = {}
    pairs = [(t, futu_code(t)) for t in tickers]
    pairs = [(t, c) for t, c in pairs if c]
    # 按市场分批快照（snapshot 不耗历史 K 线限额），一批失败降级逐只，坏代码不拖累整批
    groups = {}
    for t, c in pairs:
        groups.setdefault(c.split(".")[0], []).append(c)
    snap = {}
    if futu_reachable():
        for mkt, codes in groups.items():
            q = run_quote("futu_quote.py", codes)
            if q and isinstance(q.get("data"), list):
                for s in q["data"]:
                    snap[s.get("code", "")] = s
            elif len(codes) > 1:
                for c in codes:
                    q = run_quote("futu_quote.py", c)
                    if q and isinstance(q.get("data"), list) and q["data"]:
                        snap[c] = q["data"][0]
    for t, c in pairs:
        s = snap.get(c)
        if s:
            price = s.get("last_price")
            prev = s.get("prev_close_price")
            out[t] = {"price": price, "prev_close": prev,
                      "chg_pct": round((price / prev - 1) * 100, 2) if price and prev else None,
                      "pe_ttm": s.get("pe_ttm_ratio"), "src": "futu",
                      "time": s.get("update_time", "")}
    for t, c in pairs:  # A股／港股缺失时走免费源兜底
        if t in out or c.split(".")[0] not in ("SZ", "SH", "HK"):
            continue
        q = run_quote("fallback_quote.py", c)
        if q and isinstance(q.get("data"), dict):
            s = q["data"]
            price = s.get("last_price") or s.get("last_close")
            out[t] = {"price": price, "prev_close": s.get("prev_close"),
                      "chg_pct": s.get("pct_chg"), "pe_ttm": s.get("pe_dynamic"),
                      "src": "backup", "time": s.get("date", "")}
    return out


def _finite(o):
    """递归把 NaN/Infinity 换成 None，保证输出是合法 JSON（send_json 的兜底路径）。"""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_finite(v) for v in o]
    return o


def fetch_fx():
    """取 HKD/CNY 与 USD/CNY；即期报价为空时降级到 ECB 最近工作日参考汇率。"""
    out = {}
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import akshare as ak
        df = ak.fx_spot_quote()
        for pair, key in (("HKD/CNY", "hkdcny"), ("USD/CNY", "usdcny")):
            r = df[df["货币对"] == pair]
            if len(r):
                rate = (float(r.iloc[0]["买报价"]) + float(r.iloc[0]["卖报价"])) / 2
                # 收盘时段买/卖报价可能为 NaN；NaN 是 truthy，会绕过回退缓存，
                # 并让 json.dumps 输出裸 NaN（非法 JSON）导致 /api/quotes 无法解析。
                if math.isfinite(rate):
                    out[key] = rate
    except Exception:
        pass
    if out.get("hkdcny") and out.get("usdcny"):
        out["fx_source"] = "中国外汇交易中心即期报价"
        out["fx_date"] = time.strftime("%Y-%m-%d")
        return out

    # 周末、休市或上游异常时，akshare 可能返回整列 NaN。ECB 每个工作日发布
    # 以 EUR 为基准的官方参考汇率；CNY/HKD 与 CNY/USD 交叉相除即可得到所需口径。
    try:
        from urllib.request import Request, urlopen
        import xml.etree.ElementTree as ET
        req = Request(
            "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
            headers={"User-Agent": "jiucai-helper/1.0"},
        )
        with urlopen(req, timeout=10) as resp:
            root = ET.fromstring(resp.read())
        rates = {
            elem.attrib["currency"]: float(elem.attrib["rate"])
            for elem in root.iter()
            if "currency" in elem.attrib and "rate" in elem.attrib
        }
        if all(rates.get(k) for k in ("CNY", "HKD", "USD")):
            return {
                "hkdcny": rates["CNY"] / rates["HKD"],
                "usdcny": rates["CNY"] / rates["USD"],
                "fx_source": "ECB 官方参考汇率",
                "fx_date": next(
                    (elem.attrib["time"] for elem in root.iter() if "time" in elem.attrib),
                    None,
                ),
            }
    except Exception:
        pass
    return {}


def get_quotes(force=False, aid="main"):
    cache_path = os.path.join(HERE, f"quotes_{aid}.json")
    with _lock:
        cache = None
        if os.path.exists(cache_path):
            try:
                cache = json.load(open(cache_path, encoding="utf-8"))
            except Exception:
                cache = None
        if cache and not force and time.time() - cache.get("ts", 0) < QUOTES_TTL:
            return cache
        dossiers, _ = load_dossiers(account_dir(aid))
        tickers = [d["fm"].get("ticker") for d in dossiers
                   if d["fm"].get("status") != "已退出"]
        data = fetch_quotes([t for t in tickers if t])
        if not data and cache:  # 全失败时退回旧缓存
            cache["stale"] = True
            return cache
        fx = fetch_fx()
        rate = fx.get("hkdcny") or (cache or {}).get("hkdcny")
        urate = fx.get("usdcny") or (cache or {}).get("usdcny")
        fx_source = fx.get("fx_source") or (cache or {}).get("fx_source")
        fx_date = fx.get("fx_date") or (cache or {}).get("fx_date")
        cache = {"ts": time.time(), "at": time.strftime("%Y-%m-%d %H:%M"),
                 "quotes": data, "hkdcny": rate, "usdcny": urate,
                 "fx_source": fx_source, "fx_date": fx_date}
        json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
        return cache


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        # allow_nan=False：宁可这里报错也不要把裸 NaN/Infinity 发给浏览器——
        # 那是非法 JSON，JSON.parse 会整体抛错，前端表现为「行情待刷新」而无任何报错线索。
        try:
            body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except ValueError:
            body = json.dumps(_finite(obj), ensure_ascii=False,
                              allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            page = open(os.path.join(HERE, "index.html"), encoding="utf-8").read()
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/dossier":
            page = open(os.path.join(HERE, "dossier.html"), encoding="utf-8").read()
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/dossier":
            q = parse_qs(u.query)
            aid = q.get("account", ["main"])[0]
            fn = os.path.basename(q.get("file", [""])[0])
            path = os.path.join(account_dir(aid), fn)
            if not fn.endswith(".md") or not os.path.exists(path):
                self.send_json({"error": "not found"}, 404)
            else:
                self.send_json({"file": fn,
                                "md": open(path, encoding="utf-8").read()})
        elif u.path == "/api/accounts":
            self.send_json({"accounts": [{"id": a["id"], "name": a["name"]}
                                         for a in load_accounts()]})
        elif u.path == "/api/dossiers":
            aid = parse_qs(u.query).get("account", ["main"])[0]
            base = account_dir(aid)
            dossiers, errors = load_dossiers(base)
            self.send_json({"dossiers": dossiers, "errors": errors,
                            "track_dir": base,
                            "today": time.strftime("%Y-%m-%d")})
        elif u.path == "/api/quotes":
            q = parse_qs(u.query)
            force = q.get("force", ["0"])[0] == "1"
            aid = q.get("account", ["main"])[0]
            self.send_json(get_quotes(force=force, aid=aid))
        elif u.path == "/api/methodlog":
            aid = parse_qs(u.query).get("account", ["main"])[0]
            self.send_json({"rows": load_methodlog(account_dir(aid))})
        elif u.path == "/api/common":
            aid = parse_qs(u.query).get("account", ["main"])[0]
            self.send_json({"rows": load_common_factors(account_dir(aid))})
        else:
            self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    print(f"jiucai-helper 观测台 → http://localhost:{PORT}")
    print(f"档案库: {TRACK_DIR}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
