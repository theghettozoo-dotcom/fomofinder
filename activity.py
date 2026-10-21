"""Parsed on-chain activity for a Solana wallet: swaps, transfers, Fomo co-signer detection, linked wallets, PnL.
Everything is derived from public RPC data; results are capped + cached and labeled partial where the cap bites."""
import asyncio, os, time, collections, aiohttp
RPCS = [os.environ.get("SOLANA_RPC_URL", "https://solana-rpc.publicnode.com"), os.environ.get("SOLANA_ARCHIVE_RPC_URL", "https://api.mainnet-beta.solana.com")]
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
QUOTES = {WSOL, USDC, USDT}
SWAP_PROGRAMS = {"DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH": "DFlow", "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
                 "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun", "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
                 "proVF4pMXVaYqmy4NjniPh4pqKNfMmsihgd4wdkCX3u": "proVF", "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
                 "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium", "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
                 "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC", "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca"}
# Fee payer that co-signs Fomo app trades. Evidence (live, Oct 20 2026): fee payer on 703/794 txs of user-confirmed Fomo wallet
# GmqpRuMPTC5dAjdNJaYCgfnGJNJk438xGRPqDcYaAkba and 61/300 recent txs of 9z332Pxs4xaz7CdE4sYPEHu9a9qrxcUTkbvMXVo1q1Q8; it co-signs for
# many distinct user wallets (25 distinct co-signers in 25 sampled txs), routing via DFlow/Jupiter.
FOMO_SIGNERS = {"AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51": "Fomo fee payer / co-signer"}
# Inferred Fomo infrastructure: receives USDC from many different wallets in txs paid by the Fomo fee payer (44/60 sampled).
FOMO_INFRA = {"7uTT8Xi5RWXzy7h9XL244GRgEycDYDhLjr3ZyNdXi8pZ": "Fomo-side collection account (inferred)"}
RELAYERS = set(FOMO_SIGNERS) | {"HckQ93Xqjjo8mwt5pNPWvyCTZXQZ858rvzmm7ZRrZg9t", "FHpcNSe6tb2n15bAdq4BkeYWGyZKFD7yLYrH92ng7wCT",
                                 "gasBidSWW5zmwXs3gn8TG2ijzKkrwpyM7ucwjgDQst6"}
TX_CAP = int(os.environ.get("ACTIVITY_TX_CAP", "250"))
_cache = {}

async def _rpc(s, method, params, i=0):
    for a in range(6):
        url = RPCS[(i + a) % len(RPCS)]
        try:
            async with s.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) as r:
                d = await r.json(content_type=None)
            if d.get("result") is not None: return d["result"]
        except Exception: pass
        await asyncio.sleep(0.5 * (a + 1))
    return None

async def _jget(s, url):
    for a in range(4):
        try:
            async with s.get(url) as r:
                if r.status == 200: return await r.json(content_type=None)
        except Exception: pass
        await asyncio.sleep(0.8 * (a + 1))
    return None

_sol = {"t": 0, "v": None}
async def sol_price(s):
    if time.time() - _sol["t"] < 120 and _sol["v"]: return _sol["v"]
    d = await _jget(s, f"https://lite-api.jup.ag/price/v3?ids={WSOL}") or {}
    v = (d.get(WSOL) or {}).get("usdPrice")
    if not v:
        d = await _jget(s, "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd") or {}
        v = (d.get("solana") or {}).get("usd")
    if v: _sol.update(t=time.time(), v=v)
    return v or _sol["v"]

async def fetch(s, w, n=TX_CAP):
    k = (w, n)
    if k in _cache and time.time() - _cache[k][0] < 300: return _cache[k][1]
    sigs, before = [], None
    while len(sigs) < n:
        p = {"limit": min(1000, n - len(sigs))}
        if before: p["before"] = before
        r = await _rpc(s, "getSignaturesForAddress", [w, p], 1) or []
        sigs += r
        if len(r) < p["limit"]: break
        before = r[-1]["signature"]
    sem = asyncio.Semaphore(6)
    async def one(i, x):
        if x.get("err"): return None
        async with sem:
            t = await _rpc(s, "getTransaction", [x["signature"], {"maxSupportedTransactionVersion": 1, "encoding": "jsonParsed"}], i)
        return parse(t, w, x["signature"]) if t else None
    rows = [r for r in await asyncio.gather(*[one(i, x) for i, x in enumerate(sigs)]) if r]
    res = {"rows": rows, "fetched": len(sigs), "complete": len(sigs) < n}
    _cache[k] = (time.time(), res)
    if len(_cache) > 200: _cache.pop(next(iter(_cache)))
    return res

def parse(t, w, sig):
    m, meta = t["transaction"]["message"], t["meta"]
    keys = [k["pubkey"] for k in m["accountKeys"]]
    signers = [k["pubkey"] for k in m["accountKeys"] if k.get("signer")]
    progs = {i.get("programId") for i in m["instructions"]}
    inner = [i for g in meta.get("innerInstructions") or [] for i in g.get("instructions", [])]
    progs |= {i.get("programId") for i in inner}
    def bal(kind): 
        out = collections.Counter()
        for b in meta.get(kind) or []:
            if b.get("owner") == w: out[b["mint"]] += float(b["uiTokenAmount"]["uiAmount"] or 0)
        return out
    pre, post = bal("preTokenBalances"), bal("postTokenBalances")
    deltas = {mt: post[mt] - pre[mt] for mt in set(pre) | set(post) if abs(post[mt] - pre[mt]) > 1e-12}
    i = keys.index(w) if w in keys else None
    sol = ((meta["postBalances"][i] - meta["preBalances"][i]) / 1e9) if i is not None else 0.0
    if keys[0] == w: sol += meta.get("fee", 0) / 1e9          # ignore the network fee when the wallet paid it
    sol += deltas.pop(WSOL, 0)
    usd_stable = deltas.pop(USDC, 0) + deltas.pop(USDT, 0)
    owners = {}
    for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        owners[keys[b["accountIndex"]]] = (b.get("owner"), b["mint"])
    transfers = []
    for ins in list(m["instructions"]) + inner:
        p = ins.get("parsed")
        if not isinstance(p, dict) or p.get("type") not in ("transfer", "transferChecked"): continue
        inf = p.get("info", {})
        if ins.get("program") == "system":
            src, dst, amt, mint = inf.get("source"), inf.get("destination"), (inf.get("lamports") or 0) / 1e9, WSOL
        else:
            src = owners.get(inf.get("source"), (None,))[0] or inf.get("authority")
            dst, mint = owners.get(inf.get("destination"), (None, inf.get("mint")))
            ta = inf.get("tokenAmount") or {}
            amt = float(ta.get("uiAmount") or 0)
            mint = mint or inf.get("mint")
        if src == w and dst and dst != w: transfers.append({"dir": "out", "counterparty": dst, "mint": mint, "amount": amt})
        elif dst == w and src and src != w: transfers.append({"dir": "in", "counterparty": src, "mint": mint, "amount": amt})
    venue = next((SWAP_PROGRAMS[p] for p in progs if p in SWAP_PROGRAMS), None)
    swap = None
    if len(deltas) == 1 and (abs(sol) > 1e-6 or abs(usd_stable) > 1e-6):
        mint, d = next(iter(deltas.items()))
        if (d > 0 and (sol < 0 or usd_stable < 0)) or (d < 0 and (sol > 0 or usd_stable > 0)):
            swap = {"side": "buy" if d > 0 else "sell", "mint": mint, "amount": abs(d), "sol": abs(sol), "usdc": abs(usd_stable), "venue": venue}
    return {"sig": sig, "ts": t.get("blockTime"), "fee_payer": keys[0], "signers": signers, "swap": swap,
            "transfers": [] if (swap or venue) else transfers, "venue": venue}

def fomo_check(rows):
    hits = [r for r in rows if r["fee_payer"] in FOMO_SIGNERS or any(x in FOMO_SIGNERS for x in r["signers"])]
    if not hits: return {"verified": False, "checked_txs": len(rows)}
    h = hits[0]; who = h["fee_payer"] if h["fee_payer"] in FOMO_SIGNERS else next(x for x in h["signers"] if x in FOMO_SIGNERS)
    return {"verified": True, "checked_txs": len(rows), "hits": len(hits), "signer": who, "signer_label": FOMO_SIGNERS[who],
            "example_tx": h["sig"], "example_link": f"https://solscan.io/tx/{h['sig']}", "signer_link": f"https://solscan.io/account/{who}"}

def linked(rows, top=6):
    agg = {}
    for r in rows:
        for t in r["transfers"]:
            c = t["counterparty"]
            if c in RELAYERS: continue
            a = agg.setdefault(c, {"wallet": c, "in": 0, "out": 0, "sol_in": 0.0, "sol_out": 0.0, "usd_in": 0.0, "usd_out": 0.0, "first_tx": r["sig"], "first_ts": r["ts"]})
            a[t["dir"]] += 1
            if t["mint"] == WSOL: a["sol_" + t["dir"]] += t["amount"]
            if t["mint"] in (USDC, USDT): a["usd_" + t["dir"]] += t["amount"]
            if (r["ts"] or 0) < (a["first_ts"] or 1e18): a["first_tx"], a["first_ts"] = r["sig"], r["ts"]
    rows = sorted(agg.values(), key=lambda a: -(a["sol_in"] + a["sol_out"]) * 150 - (a["usd_in"] + a["usd_out"]) - (a["in"] + a["out"]))
    for a in rows:
        a["first_tx_link"] = f"https://solscan.io/tx/{a['first_tx']}"
        if a["wallet"] in FOMO_INFRA: a["label"] = FOMO_INFRA[a["wallet"]]
    return rows[:top]

def pnl(rows, sol_usd, complete, oldest_ts):
    now = time.time(); out = {}
    for name, secs in (("1D", 86400), ("1W", 7 * 86400), ("1M", 30 * 86400), ("1Y", 365 * 86400)):
        start = now - secs
        per = collections.defaultdict(lambda: {"bq": 0.0, "bu": 0.0, "sq": 0.0, "su": 0.0})
        trades = 0
        for r in rows:
            sw = r["swap"]
            if not sw or (r["ts"] or 0) < start: continue
            usd = sw["sol"] * (sol_usd or 0) + sw["usdc"]
            p = per[sw["mint"]]; trades += 1
            if sw["side"] == "buy": p["bq"] += sw["amount"]; p["bu"] += usd
            else: p["sq"] += sw["amount"]; p["su"] += usd
        realized, wins, closed, unmatched = 0.0, 0, 0, 0
        for mint, p in per.items():
            if p["sq"] <= 0: continue
            if p["bq"] <= 0: unmatched += 1; continue        # sold tokens bought before the window / outside fetched history
            q = min(p["sq"], p["bq"]); r_ = p["su"] * (q / p["sq"]) - p["bu"] / p["bq"] * q
            realized += r_; closed += 1; wins += r_ > 0
        out[name] = {"realized_usd": realized, "realized_sol": realized / sol_usd if sol_usd else None, "trades": trades,
                     "tokens_closed": closed, "win_rate": (wins / closed) if closed else None,
                     "badge": "profitable" if realized > 0 else "unprofitable" if closed else "no closed trades",
                     "sells_without_basis": unmatched,
                     "partial": (not complete) and (oldest_ts or now) > start}
    return out

async def activity(w, kind="all", limit=50, before=None):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), headers={"User-Agent": "Mozilla/5.0"}) as s:
        res = {"wallet": w}
        if kind in ("all", "tx", "transfers", "pnl", "linked", "fomo"):
            f = await fetch(s, w)
            rows = sorted(f["rows"], key=lambda r: -(r["ts"] or 0))
            oldest = rows[-1]["ts"] if rows else None
            res["coverage"] = {"txs_parsed": len(rows), "complete_history": f["complete"], "oldest_ts": oldest, "cap": TX_CAP}
            if kind in ("all", "tx", "pnl"):
                mints = list({r["swap"]["mint"] for r in rows if r["swap"]})[:100]
                meta = {}
                for i in range(0, len(mints), 20):
                    for t in await _jget(s, "https://lite-api.jup.ag/tokens/v2/search?query=" + ",".join(mints[i:i + 20])) or []: meta[t["id"]] = t
                sol_usd = await sol_price(s)
                res["sol_usd_now"] = sol_usd
            if kind in ("all", "tx"):
                sw = [r for r in rows if r["swap"] and (not before or (r["ts"] or 0) < before)][:limit]
                res["swaps"] = [{"side": r["swap"]["side"], "mint": r["swap"]["mint"], "symbol": meta.get(r["swap"]["mint"], {}).get("symbol"),
                                 "name": meta.get(r["swap"]["mint"], {}).get("name"), "amount": r["swap"]["amount"], "sol": r["swap"]["sol"],
                                 "usdc": r["swap"]["usdc"], "usd_est": r["swap"]["sol"] * (sol_usd or 0) + r["swap"]["usdc"], "venue": r["swap"]["venue"],
                                 "ts": r["ts"], "link": f"https://solscan.io/tx/{r['sig']}"} for r in sw]
            if kind in ("all", "transfers"):
                tr = []
                for r in rows:
                    if before and (r["ts"] or 0) >= before: continue
                    for t in r["transfers"]:
                        if t["counterparty"] in RELAYERS and t["amount"] < 0.01: continue
                        tr.append(dict(t, ts=r["ts"], token="SOL" if t["mint"] == WSOL else "USDC" if t["mint"] == USDC else "USDT" if t["mint"] == USDT else t["mint"],
                                       link=f"https://solscan.io/tx/{r['sig']}"))
                res["transfers"] = tr[:limit]
            if kind in ("all", "pnl"): res["pnl"] = pnl(rows, sol_usd, f["complete"], oldest)
            if kind in ("all", "linked"): res["linked"] = linked(rows)
            if kind in ("all", "fomo"): res["fomo"] = fomo_check(rows)
        if kind in ("all", "callouts"):
            res["callouts"] = await callouts(s, w, limit)
        if kind in ("all", "theses"):
            res["theses"] = {"available": False, "note": "Fomo keeps theses private behind login (prod-api.fomo.family returns 'unauthorized' without a session)."}
        return res

async def callouts(s, w, limit=50):
    out = []
    async with s.get(f"https://frontend-api-v3.pump.fun/callout/list/{w}?limit={min(limit, 50)}") as r:
        d = await r.json(content_type=None) if r.status == 200 else {}
    for c in (d or {}).get("callouts") or []:
        out.append({"type": "callout", "mint": c.get("coinMint"), "text": c.get("thesis"), "ts": (c.get("createdAt") or 0) / 1000,
                    "mcap": c.get("marketCap"), "multiple": c.get("multiple"), "max_multiple": c.get("maxMultiplier"), "likes": c.get("likes"),
                    "link": f"https://pump.fun/coin/{c.get('coinMint')}"})
    async with s.get(f"https://frontend-api-v3.pump.fun/callout/replies/list/{w}?limit={min(limit, 50)}") as r:
        d = await r.json(content_type=None) if r.status == 200 else {}
    import datetime
    for x in (d or {}).get("replies") or []:
        rp, co = x.get("reply") or {}, (x.get("callout") or {})
        ts = rp.get("createdAt")
        try: ts = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception: ts = None
        out.append({"type": "reply", "mint": co.get("coinMint"), "text": rp.get("content"), "ts": ts, "to": co.get("userName"),
                    "link": f"https://pump.fun/coin/{co.get('coinMint')}"})
    out.sort(key=lambda c: -(c["ts"] or 0))
    return {"source": "frontend-api-v3.pump.fun /callout/list + /callout/replies/list", "items": out[:limit]}

if __name__ == "__main__":
    import sys, json
    print(json.dumps(asyncio.run(activity(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "all")), default=str, indent=1)[:6000])
