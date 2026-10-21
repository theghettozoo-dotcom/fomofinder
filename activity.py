"""Parsed on-chain activity for a Solana wallet: swaps, transfers, Fomo co-signer detection, linked wallets, PnL.
Everything is derived from public RPC data; results are capped + cached and labeled partial where the cap bites."""
import asyncio, os, re, time, collections, aiohttp
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
# Casino / CEX payout wallets are not owners. visitflipgg.sol (Flip.gg) resolves to this address.
PAYOUT_WALLETS = {"BmFdpraQhkiDQE6SnfG5omcA1VwzqfXrwtNYBwWTymy6": "Flip.gg payout (visitflipgg.sol)"}
INFRA = set(RELAYERS) | set(FOMO_INFRA) | set(FOMO_SIGNERS) | set(PAYOUT_WALLETS)
PAYOUT_RE = re.compile(r"visitflipgg|flip\.?gg|binance|coinbase|kraken|bybit|\bokx\b|kucoin|crypto\.com|rollbit|roobet|stake\.com|bc\.game", re.I)
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

def _agg(rows, skip):
    """Counterparties with counts and the largest inbound SOL transfer (the funding evidence tx)."""
    out = {}
    for r in rows:
        for t in r["transfers"]:
            c = t["counterparty"]
            if not c or c in skip: continue
            a = out.setdefault(c, {"wallet": c, "in": 0, "out": 0, "sol_in": 0.0, "sol_out": 0.0,
                                   "fund_sol": 0.0, "fund_tx": None, "example_tx": r["sig"]})
            a[t["dir"]] += 1
            if t["mint"] == WSOL:
                a["sol_" + t["dir"]] += t["amount"]
                if t["dir"] == "in" and t["amount"] >= a["fund_sol"]:
                    a["fund_sol"], a["fund_tx"] = t["amount"], r["sig"]
            if not a["example_tx"]: a["example_tx"] = r["sig"]
    return out

def _fomo_touch(rows):
    """Did this wallet send to the inferred Fomo collection account, or get a Fomo fee-payer co-sign?"""
    hit = None
    for r in rows:
        for t in r["transfers"]:
            if t["counterparty"] in FOMO_INFRA and t["dir"] == "out" and t["mint"] == WSOL and t["amount"] >= 0.05:
                if not hit or t["amount"] > hit["sol"]:
                    hit = {"sol": t["amount"], "tx": r["sig"]}
    fee = fomo_check(rows)
    return hit, bool(fee.get("verified"))

def _score(sol_in, n_in, n_out, fomo_sol, fomo_fee, hop):
    score, parts = 0, []
    if hop == 1 and sol_in >= 1:
        score += 50
        parts.append(f"Sent {sol_in:.2f} SOL to this wallet. Funding it is a strong signal.")
    elif hop == 1 and sol_in >= 0.1:
        score += 25
        parts.append(f"Sent {sol_in:.2f} SOL to this wallet.")
    if hop == 1 and n_in >= 1 and n_out >= 1 and n_in + n_out >= 3:
        score += 30
        parts.append(f"Transfers run both ways ({n_in} in, {n_out} out). Repeated two-way transfers are a medium signal.")
    elif hop == 1 and n_in >= 1 and n_out >= 1:
        score += 15
        parts.append(f"They both sent and received ({n_in} in, {n_out} out).")
    if hop == 1 and n_in + n_out <= 1 and sol_in < 0.1:
        score += 5
        parts.append("The only connection is a one-off transfer, which is a weak signal.")
    if fomo_sol:
        score += 20
        parts.append(f"They also send {fomo_sol:.2f} SOL to the same inferred Fomo collection account this wallet uses, which adds weight.")
    elif fomo_fee:
        score += 10
        parts.append("The Fomo fee payer co-signed at least one of their recent transactions, which adds weight.")
    if score >= 70 and hop == 1: conf = "High"
    elif score >= 35: conf = "Medium"
    else: conf = "Low"
    if hop == 2 and conf == "High": conf = "Medium"
    return score, conf, parts

async def _who(s, w):
    import identity
    p, sns, kol = await asyncio.gather(
        identity._get(s, f"{identity.PUMP}/users/{w}"),
        identity.sns_section(s, w),
        identity.kolscan_section(s, w))
    p = p if isinstance(p, dict) else {}
    domains = [d for d in (sns.get("domains") or []) if d]
    if sns.get("primary") and sns["primary"] not in domains: domains.insert(0, sns["primary"])
    ident = {"username": p.get("username") or None, "x": p.get("x_username") or None,
             "domains": domains, "kol": kol.get("name") if kol.get("is_kol") else None,
             "kol_link": kol.get("link") if kol.get("is_kol") else None,
             "pump_link": f"https://pump.fun/profile/{w}" if p.get("username") else None,
             "x_link": f"https://x.com/{p['x_username']}" if p.get("x_username") else None}
    blob = " ".join([ident["username"] or "", ident["x"] or "", ident["kol"] or ""] + domains)
    ident["payout"] = bool(PAYOUT_RE.search(blob))
    ident["has"] = bool(ident["username"] or ident["x"] or domains or ident["kol"])
    return ident

async def possible_owners(s, w):
    """Unconfirmed owner hunches from funders and counterparties, one or two hops. Never a confirmed match."""
    import identity
    me = await _who(s, w)
    if me["has"]:
        return {"shown": False, "candidates": [], "reason": "This wallet already has a public profile, X handle, or .sol name."}
    fetched = await fetch(s, w)
    direct = _agg(fetched["rows"], INFRA | {w})
    funders = [c for c, a in sorted(direct.items(), key=lambda kv: -kv[1]["sol_in"]) if a["sol_in"] >= 0.5][:3]
    subs = await asyncio.gather(*[fetch(s, c, n=40) for c in funders]) if funders else []
    fomo_for, fee_for, hop2 = {}, {}, []
    for c, sub in zip(funders, subs):
        touch, fee = _fomo_touch(sub["rows"])
        if touch: fomo_for[c] = touch
        if fee: fee_for[c] = True
        via = direct[c]
        for o, b in _agg(sub["rows"], INFRA | {w, c}).items():
            if b["sol_in"] < 0.5 and not (b["in"] >= 1 and b["out"] >= 1 and b["in"] + b["out"] >= 3): continue
            hop2.append((o, b, c, via))
    # Identity on the closest counterparties, plus a couple of second-hop wallets behind funders that may themselves be unnamed.
    want = [c for c, a in sorted(direct.items(), key=lambda kv: -(kv[1]["sol_in"] * 10 + kv[1]["in"] + kv[1]["out"]))][:6]
    extra = []
    for o, b, c, via in sorted(hop2, key=lambda t: -t[1]["sol_in"]):
        if o in direct or o in extra: continue
        extra.append(o)
        if len(extra) >= 3: break
    idents = await asyncio.gather(*[_who(s, c) for c in want + extra])
    named = dict(zip(want + extra, idents))
    leads = []
    for c in want:
        ident = named[c]
        if not ident["has"] or ident["payout"] or c in INFRA: continue
        a = direct[c]
        touch = fomo_for.get(c)
        score, conf, parts = _score(a["sol_in"], a["in"], a["out"], touch["sol"] if touch else None, bool(fee_for.get(c)), 1)
        if not parts: continue
        leads.append(_lead(c, ident, a, score, conf, parts, 1, None, touch))
    for o, b, c, via in hop2:
        ident = named.get(o)
        if not ident or not ident["has"] or ident["payout"] or o in INFRA: continue
        if named.get(c, {}).get("has"): continue  # the closer wallet already carries an identity
        via_ident = named.get(c) or {}
        via_name = via_ident.get("username") or (via["wallet"][:4] + "…" + via["wallet"][-4:])
        score, conf, parts = _score(0, 0, 0, None, False, 2)
        score += 25 if b["sol_in"] >= 1 else 15
        conf = "Medium" if score >= 35 else "Low"
        parts.insert(0, f"They did not pay this wallet directly. They sent {b['sol_in']:.2f} SOL to {via_name}, who sent {via['sol_in']:.2f} SOL here. That extra hop is a weaker signal.")
        leads.append(_lead(o, ident, b, score, conf, parts, 2, via, None))
    leads.sort(key=lambda L: (-L["score"], -L["sol_in"]))
    strong = [L for L in leads if L["score"] >= 25]
    show = (strong or leads)[:3]
    return {"shown": True, "candidates": show, "checked_counterparties": len(direct),
            "txs_parsed": len(fetched["rows"]),
            "method": "Unconfirmed hunches only, ranked from public transfers. Funding this wallet is strong, repeated two-way transfers are medium, a one-off transfer is weak. Sending to the same Fomo collection account adds weight. The Fomo fee payer, the Fomo collection account, relayers, gas sponsors, and known casino payouts such as Flip.gg (visitflipgg.sol) are excluded."}

def _lead(wallet, ident, stats, score, conf, parts, hop, via, touch):
    title = ident["username"] or (ident["x"] and "@" + ident["x"]) or (ident["domains"][0] if ident["domains"] else None) or ident["kol"] or wallet[:4] + "…" + wallet[-4:]
    tx = stats.get("fund_tx") or stats.get("example_tx")
    parts.append("This is an unconfirmed lead, not a verified owner.")
    out = {"wallet": wallet, "title": title, "hop": hop, "confidence": conf, "score": score,
           "label": "Unconfirmed lead / hunch", "reason": " ".join(parts),
           "sol_in": round(stats.get("sol_in") or stats.get("fund_sol") or 0, 3),
           "tx_link": f"https://solscan.io/tx/{tx}" if tx else None,
           "wallet_link": f"https://solscan.io/account/{wallet}",
           "pump": {"username": ident["username"], "link": ident["pump_link"]} if ident.get("pump_link") else None,
           "x": {"handle": ident["x"], "link": ident["x_link"]} if ident.get("x_link") else None,
           "domains": [{"name": d, "link": f"https://www.sns.id/domain/{d[:-4]}"} for d in ident["domains"]],
           "kol": {"name": ident["kol"], "link": ident["kol_link"]} if ident.get("kol") else None}
    if via: out["via"] = via["wallet"]
    if touch: out["fomo_tx_link"] = f"https://solscan.io/tx/{touch['tx']}"
    return out

async def activity(w, kind="all", limit=50, before=None):
    timeout = 90 if kind == "owner" else 60
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers={"User-Agent": "Mozilla/5.0"}) as s:
        res = {"wallet": w}
        if kind == "owner":
            res["owner"] = await possible_owners(s, w)
            return res
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

_coins = {}

async def coin_meta(s, mint):
    """pump.fun coin name, symbol, and image. Cached so a feed of repeated mints is one lookup each."""
    if not mint: return {}
    hit = _coins.get(mint)
    if hit and time.time() - hit["t"] < 600: return hit
    d = {}
    try:
        async with s.get(f"https://frontend-api-v3.pump.fun/coins-v2/{mint}") as r:
            if r.status == 200: d = await r.json(content_type=None) or {}
    except Exception:
        d = {}
    meta = {"name": d.get("name"), "symbol": d.get("symbol"), "image": d.get("image_uri"), "t": time.time()}
    if meta["name"] or meta["symbol"] or meta["image"]:
        _coins[mint] = meta
        if len(_coins) > 500: _coins.pop(next(iter(_coins)))
    return meta

async def callouts(s, w, limit=50):
    calls, replies = [], []
    cap = min(max(limit, 1), 50)
    async with s.get(f"https://frontend-api-v3.pump.fun/callout/list/{w}?limit={cap}") as r:
        d = await r.json(content_type=None) if r.status == 200 else {}
    for c in (d or {}).get("callouts") or []:
        calls.append({"type": "callout", "mint": c.get("coinMint"), "text": c.get("thesis"), "ts": (c.get("createdAt") or 0) / 1000,
                      "mcap": c.get("marketCap"), "multiple": c.get("multiple"), "max_multiple": c.get("maxMultiplier"), "likes": c.get("likes"),
                      "link": f"https://pump.fun/coin/{c.get('coinMint')}"})
    async with s.get(f"https://frontend-api-v3.pump.fun/callout/replies/list/{w}?limit={cap}") as r:
        d = await r.json(content_type=None) if r.status == 200 else {}
    import datetime
    for x in (d or {}).get("replies") or []:
        rp, co = x.get("reply") or {}, (x.get("callout") or {})
        ts = rp.get("createdAt")
        try: ts = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except Exception: ts = None
        replies.append({"type": "reply", "mint": co.get("coinMint"), "text": rp.get("content"), "ts": ts, "to": co.get("userName"),
                        "link": f"https://pump.fun/coin/{co.get('coinMint')}"})
    sem = asyncio.Semaphore(8)
    async def one(m):
        async with sem: return m, await coin_meta(s, m)
    mints = [m for m in {c.get("mint") for c in calls + replies} if m]
    meta = dict(await asyncio.gather(*[one(m) for m in mints])) if mints else {}
    for c in calls + replies:
        info = meta.get(c.get("mint")) or {}
        c["name"], c["symbol"], c["image"] = info.get("name"), info.get("symbol"), info.get("image")
    calls.sort(key=lambda c: -(c["ts"] or 0)); replies.sort(key=lambda c: -(c["ts"] or 0))
    items = sorted(calls + replies, key=lambda c: -(c["ts"] or 0))
    return {"source": "frontend-api-v3.pump.fun /callout/list + /callout/replies/list + /coins-v2",
            "items": items[:limit], "calls": calls, "replies": replies}

if __name__ == "__main__":
    import sys, json
    print(json.dumps(asyncio.run(activity(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "all")), default=str, indent=1)[:6000])
