"""Public identity lookup for a Solana wallet. Every field carries its source; nothing is inferred except where marked 'unverified'."""
import asyncio, os, re, time, aiohttp
SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
PUMP = "https://frontend-api-v3.pump.fun"
RPC = os.environ.get("SOLANA_RPC_URL", "https://solana-rpc.publicnode.com")
HIST_RPC = os.environ.get("SOLANA_ARCHIVE_RPC_URL", "https://api.mainnet-beta.solana.com")  # publicnode prunes old history
UA = {"User-Agent": "Mozilla/5.0"}
WSOL = "So11111111111111111111111111111111111111112"

async def _get(s, url, **kw):
    try:
        async with s.get(url, headers=UA, **kw) as r:
            return await r.json(content_type=None) if r.status == 200 else None
    except Exception: return None

async def _get_retry(s, url, tries=4):
    """Retry rate limits and blips. None means the request failed; an empty list is a real empty result."""
    for i in range(tries):
        try:
            async with s.get(url, headers=UA) as r:
                if r.status == 200: return await r.json(content_type=None)
                if r.status not in (429, 500, 502, 503, 504): return None
        except Exception:
            pass
        await asyncio.sleep(0.8 * (2 ** i))
    return None

async def _rpc(s, method, params, url=None):
    for i in range(4):
        try:
            async with s.post(url or RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) as r:
                d = await r.json(content_type=None)
            if "result" in d: return d["result"]
        except Exception: pass
        await asyncio.sleep(0.5 * (i + 1))
    return None

async def resolve_input(s, q):
    """wallet | pump.fun profile URL | pump.fun username -> (wallet, pump_profile|None)"""
    q = q.strip().rstrip("/").split("/")[-1].split("?")[0].lstrip("@")
    prof = await _get(s, f"{PUMP}/users/{q}")
    if isinstance(prof, dict) and prof.get("address"): return prof["address"], prof
    return (q, None) if SOL_RE.match(q) else (None, None)

async def pump_section(s, w, prof):
    prof = prof or await _get(s, f"{PUMP}/users/{w}")
    if not isinstance(prof, dict): prof = None
    raw, coins_error = [], False
    for offset in range(0, 100, 20):
        batch = await _get_retry(s, f"{PUMP}/coins?creator={w}&offset={offset}&limit=20&includeNsfw=true&sort=created_timestamp&order=DESC")
        if not isinstance(batch, list):
            coins_error = True
            break
        if not batch: break
        raw.extend(batch)
        if len(batch) < 20: break
    coins = []
    seen = set()
    for c in raw:
        if c.get("creator") != w or not c.get("mint") or c["mint"] in seen: continue
        seen.add(c["mint"])
        ts = c.get("created_timestamp") or 0
        if ts > 10**11: ts /= 1000
        coins.append({"mint": c["mint"], "name": c.get("name"), "symbol": c.get("symbol"), "image": c.get("image_uri"),
                      "created": ts or None, "mcap": c.get("usd_market_cap") if c.get("usd_market_cap") is not None else c.get("market_cap_usd"),
                      "ath_mcap": c.get("ath_market_cap"), "graduated": bool(c.get("complete")),
                      "link": f"https://pump.fun/coin/{c['mint']}"})
    if not prof and not coins and not coins_error: return None
    p = prof or {}
    unavailable = coins_error and not coins
    return {"source": f"{PUMP}/users/{w}", "username": p.get("username"), "bio": p.get("bio"), "followers": p.get("followers"),
            "following": p.get("following"), "x_username": p.get("x_username"), "evm_wallet": p.get("canonical_evm_wallet"),
            "image": p.get("profile_image"), "link": f"https://pump.fun/profile/{w}",
            "coins_created": coins, "coins_devved": None if unavailable else len(coins),
            "coins_graduated": None if unavailable else sum(1 for c in coins if c["graduated"]),
            "coins_unavailable": unavailable}

async def sns_section(s, w):
    fav = await _get(s, f"https://sdk-proxy.sns.id/favorite-domain/{w}")
    doms = await _get(s, f"https://sdk-proxy.sns.id/domains/{w}")
    res = (doms or {}).get("result") if isinstance(doms, dict) else None
    names = [d["domain"] + ".sol" for d in res if isinstance(d, dict) and d.get("domain")] if isinstance(res, list) else []
    fr = (fav or {}).get("result") if isinstance(fav, dict) else None
    primary = fr.get("reverse") if isinstance(fr, dict) else None
    if not names and not primary: return {"source": "https://sdk-proxy.sns.id", "domains": [], "primary": None}
    return {"source": "https://sdk-proxy.sns.id (Bonfida SNS)", "primary": primary and primary + ".sol", "domains": names,
            "links": [f"https://www.sns.id/domain/{n[:-4]}" for n in names]}

async def holdings_section(s, w):
    bal = await _rpc(s, "getBalance", [w]); sol = (bal or {}).get("value", 0) / 1e9 if bal else None
    h = await _get(s, f"https://lite-api.jup.ag/ultra/v1/holdings/{w}") or {}
    toks = {m: sum(a.get("uiAmount") or 0 for a in accs) for m, accs in (h.get("tokens") or {}).items()}
    toks = {m: v for m, v in toks.items() if v > 0 and m != WSOL}
    ids = list(toks)[:100]
    info = {}
    for i in range(0, len(ids), 20):
        for t in await _get(s, "https://lite-api.jup.ag/tokens/v2/search?query=" + ",".join(ids[i:i + 20])) or []:
            info[t["id"]] = t
    price = await _get(s, f"https://lite-api.jup.ag/price/v3?ids={WSOL}") or {}
    solusd = (price.get(WSOL) or {}).get("usdPrice")
    rows = []
    for m, amt in toks.items():
        t = info.get(m, {}); usd = amt * t["usdPrice"] if t.get("usdPrice") else None
        rows.append({"mint": m, "symbol": t.get("symbol"), "name": t.get("name"), "amount": amt, "usd": usd,
                     "link": f"https://dexscreener.com/solana/{m}"})
    rows.sort(key=lambda r: r["usd"] or 0, reverse=True)
    return {"source": "Solana RPC getBalance + Jupiter lite-api holdings/tokens/price", "sol": sol,
            "sol_usd": sol * solusd if (sol is not None and solusd) else None, "token_count": len(rows), "top": rows[:8]}

async def history_section(s, w, max_pages=int(os.environ.get("ID_MAX_PAGES", "25"))):
    """Wallet age + first funder: walk signatures back (1000/page). Very active wallets may exceed the page budget."""
    before, last, n, oldest_page = None, None, 0, []
    for _ in range(max_pages):
        p = {"limit": 1000}
        if before: p["before"] = before
        sigs = await _rpc(s, "getSignaturesForAddress", [w, p], HIST_RPC)
        if not sigs: break
        n += len(sigs); last = sigs[-1]; before = last["signature"]; oldest_page = sigs
        if len(sigs) < 1000: break
    else:
        return {"source": "Solana RPC getSignaturesForAddress", "complete": False, "tx_count_min": n,
                "oldest_seen": last and last.get("blockTime"), "note": f"more than {n} transactions; oldest shown is a lower bound on age"}
    if not last: return {"source": "Solana RPC", "complete": True, "tx_count": 0}
    out = {"source": "Solana RPC getSignaturesForAddress/getTransaction", "complete": True, "tx_count": n,
           "first_tx": last["signature"], "first_seen": last.get("blockTime"), "first_tx_link": f"https://solscan.io/tx/{last['signature']}"}
    first_payer = None
    # first funding = earliest tx where the wallet's SOL balance went up (the very first tx may be someone else creating a token account for it)
    for sig in [x["signature"] for x in reversed(oldest_page)][:12]:
        tx = await _rpc(s, "getTransaction", [sig, {"maxSupportedTransactionVersion": 1, "encoding": "jsonParsed"}], HIST_RPC)
        if not tx: continue
        keys = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]]
        first_payer = first_payer or keys[0]
        if w not in keys: continue
        i = keys.index(w); gain = (tx["meta"]["postBalances"][i] - tx["meta"]["preBalances"][i]) / 1e9
        if gain <= 0: continue
        src, how = keys[0], "fee payer of the first tx that sent SOL to this wallet"
        inner = [x for g in (tx["meta"].get("innerInstructions") or []) for x in g.get("instructions", [])]
        for ins in (tx["transaction"]["message"].get("instructions") or []) + inner:
            pi = ins.get("parsed") or {}
            if isinstance(pi, dict) and pi.get("type") in ("transfer", "createAccount", "transferWithSeed"):
                info = pi.get("info", {})
                if info.get("destination", info.get("newAccount")) == w: src, how = info.get("source"), "SOL transfer"; break
        out.update(funded_by=src, funded_sol=gain, funded_how=how, funded_at=tx.get("blockTime"),
                   funded_by_link=f"https://solscan.io/account/{src}", funding_tx_link=f"https://solscan.io/tx/{sig}")
        break
    if "funded_by" not in out and first_payer:
        out.update(first_fee_payer=first_payer, first_fee_payer_link=f"https://solscan.io/account/{first_payer}",
                   funding_note="no SOL arrived in its first 12 txs; their fees were paid by another account (looks like sponsored gas, e.g. an app-managed wallet)")
    return out

async def x_section(s, handle, w):
    """Keyless X search (Nitter/xcancel) was tested and is blocked, so posts need X_BEARER_TOKEN. X: handle from pump.fun socials. With X_BEARER_TOKEN, also profile + recent posts mentioning the wallet."""
    tok = os.environ.get("X_BEARER_TOKEN")
    out = {"handle": handle, "link": handle and f"https://x.com/{handle}", "source": "pump.fun profile x_username" if handle else None,
           "mentions": [], "search_link": f"https://x.com/search?q={w}&f=live"}
    if not tok: out["note"] = "set X_BEARER_TOKEN to pull the X profile and posts that mention this wallet"; return out
    H = {"Authorization": f"Bearer {tok}"}
    if handle:
        u = await _get(s, f"https://api.x.com/2/users/by/username/{handle}?user.fields=description,public_metrics,created_at,location", headers=H)
        if u and u.get("data"): out["profile"] = u["data"]
    r = await _get(s, f"https://api.x.com/2/tweets/search/recent?query={w}&expansions=author_id&user.fields=username&max_results=25", headers=H)
    users = {u["id"]: u["username"] for u in ((r or {}).get("includes") or {}).get("users", [])}
    for t in (r or {}).get("data") or []:
        un = users.get(t.get("author_id"))
        out["mentions"].append({"handle": un, "text": t["text"][:200], "link": f"https://x.com/{un or 'i'}/status/{t['id']}"})
    out["source"] = (out["source"] or "") + " + X API v2"
    return out

_kol = {"t": 0, "rows": {}}
async def kolscan_section(s, w, handle=None):
    """KOLscan embeds its whole tracked-KOL leaderboard (wallet, name, X, profit/wins/losses per timeframe) in the public page data."""
    import json as _j
    if time.time() - _kol["t"] > 600:
        try:
            async with s.get("https://kolscan.io/leaderboard", headers=UA) as r: html = (await r.text()).replace('\\"', '"')
            rows = {}
            for m in re.finditer(r'\{"wallet_address":"([1-9A-HJ-NP-Za-km-z]{32,44})"[^{}]*\}', html):
                try: d = _j.loads(m.group(0))
                except Exception: continue
                e = rows.setdefault(d["wallet_address"], {"name": d.get("name"), "twitter": d.get("twitter"), "telegram": d.get("telegram"), "stats": {}})
                if "profit" in d: e["stats"].setdefault(str(d.get("timeframe")), {"profit_sol": d["profit"], "wins": d.get("wins"), "losses": d.get("losses")})
            # rank per timeframe = position by profit
            for tf in ("1", "7", "30"):
                ranked = sorted([(v["stats"][tf]["profit_sol"], k) for k, v in rows.items() if tf in v["stats"]], reverse=True)
                for i, (_, k) in enumerate(ranked): rows[k]["stats"][tf]["rank"] = i + 1
            if rows: _kol.update(t=time.time(), rows=rows)
        except Exception: pass
    rows = _kol["rows"]
    out = {"source": "https://kolscan.io/leaderboard (page data)", "tracked_kols": len(rows), "is_kol": w in rows}
    if w in rows:
        k = rows[w]; out.update(name=k["name"], twitter=k["twitter"], telegram=k["telegram"], stats=k["stats"], link=f"https://kolscan.io/account/{w}")
    elif handle:
        same = [(a, v) for a, v in rows.items() if (v.get("twitter") or "").rstrip("/").split("/")[-1].lower() == handle.lower()]
        if same: out["same_x_handle_kol_wallets"] = [{"wallet": a, "name": v["name"], "link": f"https://kolscan.io/account/{a}"} for a, v in same]
    return out

def links(w, pump_user=None, x=None):
    L = {"Solscan": f"https://solscan.io/account/{w}", "pump.fun": f"https://pump.fun/profile/{w}", "GMGN": f"https://gmgn.ai/sol/address/{w}",
         "Birdeye": f"https://birdeye.so/profile/{w}?chain=solana", "DexScreener": f"https://dexscreener.com/solana?maker={w}",
         "Jupiter portfolio": f"https://jup.ag/portfolio/{w}"}
    if x: L["X"] = f"https://x.com/{x}"
    return L

X_NOTE = "Unconfirmed matches can be wrong. A name that only looks the same is never marked confirmed."
_X_HAND = re.compile(r"(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})", re.I)
_CRYPTO = re.compile(r"pump\.fun|pumpfun|\bfomo\b|solana|memecoin|\bdegen\b|\bcrypto\b|\bweb3\b|\bnft\b|\$sol\b|\bca:|contract address", re.I)
_fx_cache = {}

def handle_variants(name):
    """Exact handle, then digits/underscores stripped, then 0/O swaps. X handles are at most 15 characters."""
    raw = re.sub(r"[^A-Za-z0-9_]", "", (name or "").strip().lstrip("@"))
    if not raw: return []
    seen, out = set(), []
    def add(h):
        h = re.sub(r"[^A-Za-z0-9_]", "", h or "")
        key = h.lower()
        if not (4 <= len(h) <= 15) or key in seen: return
        seen.add(key); out.append(h)
    add(raw)
    stripped = re.sub(r"[\d_]", "", raw)
    add(stripped)
    for base in (raw, stripped):
        add(base.replace("0", "O").replace("o", "O"))
        add(base.replace("0", "o"))
        add(re.sub(r"[\d_]", "", base.replace("0", "O")))
    return out[:8]

def _handles_in(text):
    return {m.group(1).lower() for m in _X_HAND.finditer(text or "")}

def _flat(*parts):
    bits = []
    for p in parts:
        if isinstance(p, dict): bits += [p.get("url") or "", p.get("display_url") or ""]
        elif p: bits.append(str(p))
    return "\n".join(bits)

def score_x_candidate(subject, user, variant, avatar_match=False, posts_hit=None):
    """Label an X account found for a pump.fun / Fomo name. Confirmed requires hard proof."""
    handle = (user or {}).get("screen_name") or ""
    wallet = subject.get("wallet") or ""
    query = subject.get("handle") or ""
    bio = user.get("description") or ""
    web = _flat(user.get("website"))
    xblob = bio + "\n" + web
    hard, soft, evidence = [], [], []
    src = user.get("_source") or "fxtwitter"
    if wallet and wallet in xblob:
        hard.append({"text": "X bio or website contains wallet " + wallet + ".", "source": src})
    pump_bits = [f"pump.fun/profile/{wallet}", f"pump.fun/{wallet}"]
    if query: pump_bits.append(f"pump.fun/profile/{query}")
    low = xblob.lower()
    if any(b.lower() in low for b in pump_bits if b):
        hard.append({"text": "X bio or website links to this pump.fun profile or wallet.", "source": src})
    pump_bio = subject.get("pump_bio") or ""
    if handle and handle.lower() in _handles_in(pump_bio):
        hard.append({"text": "pump.fun bio links to https://x.com/" + handle + ".", "source": "pump.fun profile"})
    x_user = (subject.get("x_username") or "")
    if handle and x_user and x_user.lower() == handle.lower():
        hard.append({"text": "pump.fun profile x_username is @" + handle + ".", "source": "pump.fun profile"})
    if posts_hit:
        hard.append({"text": "An X post from this account contains the wallet address.", "source": "X API v2"})
    for c in subject.get("coins") or []:
        if c.get("creator") and c.get("creator") != wallet: continue
        link = c.get("twitter") or ""
        if handle and handle.lower() in _handles_in(link):
            sym = c.get("symbol") or "coin"
            hard.append({"text": f"Coin ${sym} created by this wallet links to @{handle}.", "source": "pump.fun coin"})
            break
    exact = bool(handle) and handle.lower() == query.lower()
    if exact: soft.append("exact handle")
    elif handle:
        evidence.append({"text": f"X @{handle} is a normalized variant of {query} (digits/underscores removed or 0/O swapped), not the exact name.", "source": src, "hard": False})
    if avatar_match: soft.append("matching avatar")
    if _CRYPTO.search(bio or ""): soft.append("crypto bio")
    tweets = int(user.get("tweets") or 0)
    active = (not user.get("protected")) and tweets >= 5
    if active: soft.append("account active")
    if exact: evidence.append({"text": "X handle matches " + query + " exactly.", "source": src, "hard": False})
    if avatar_match: evidence.append({"text": "Profile photo matches the pump.fun avatar.", "source": "image hash", "hard": False})
    if "crypto bio" in soft: evidence.append({"text": "X bio mentions crypto, pump.fun, or Fomo.", "source": src, "hard": False})
    if active: evidence.append({"text": f"Account is active ({tweets} posts).", "source": src, "hard": False})
    for h in hard: h["hard"] = True
    evidence = hard + evidence
    label = "Confirmed" if hard else ("Likely" if len(soft) >= 2 else "Possible")
    if label == "Confirmed" and not hard: label = "Possible"
    return {"wallet": wallet, "role": subject.get("role") or "target", "query": query,
            "name_kind": subject.get("name_kind") or "pump.fun username", "handle": handle,
            "link": user.get("url") or f"https://x.com/{handle}", "variant": None if exact else variant,
            "label": label, "evidence": evidence, "soft": soft, "avatar": user.get("avatar_url"),
            "source": src}

async def _fx_user(s, handle):
    key = handle.lower()
    hit = _fx_cache.get(key)
    if hit and time.time() - hit["t"] < 600: return hit["u"], hit["err"]
    from urllib.parse import quote
    user, err, not_found = None, None, False
    for base, source in (("https://api.fxtwitter.com/", "fxtwitter"), ("https://api.vxtwitter.com/", "vxtwitter")):
        try:
            async with s.get(base + quote(handle), headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                             timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status == 404:
                    not_found = True
                    continue
                if r.status == 429:
                    err = "rate limited"; await asyncio.sleep(0.8); continue
                if r.status != 200:
                    err = f"{source} {r.status}"; continue
                d = await r.json(content_type=None)
            u = d.get("user") if isinstance(d, dict) else None
            if isinstance(u, dict) and u.get("screen_name"):
                u["_source"] = source
                user, err = u, None
                break
        except Exception:
            err = err or "lookup failed"
        await asyncio.sleep(0.15)
    # A 404 from either host means there is no account. A 403 from the other host is not a failure.
    if not user and not_found: err = None
    _fx_cache[key] = {"t": time.time(), "u": user, "err": err if not user else None}
    if len(_fx_cache) > 500:
        for k in sorted(_fx_cache, key=lambda k: _fx_cache[k]["t"])[:100]: _fx_cache.pop(k, None)
    return user, (None if user else err)

async def _ahash(s, url):
    if not url or "default_profile" in url: return None
    try:
        async with s.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200: return None
            data = await r.read()
    except Exception:
        return None
    if not data: return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-i", "pipe:0", "-frames:v", "1", "-vf", "scale=8:8,format=gray", "-f", "rawvideo", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(data), timeout=8)
    except Exception:
        return None
    if not out or len(out) < 64: return None
    pix = list(out[:64]); avg = sum(pix) / 64; bits = 0
    for i, p in enumerate(pix):
        if p >= avg: bits |= 1 << i
    return bits

def _hamming(a, b):
    return (a ^ b).bit_count()

async def _posts_have_wallet(s, handle, wallet):
    tok = os.environ.get("X_BEARER_TOKEN")
    if not tok or not handle or not wallet: return None
    from urllib.parse import quote
    q = quote(f'from:{handle} "{wallet}"')
    try:
        async with s.get(f"https://api.x.com/2/tweets/search/recent?query={q}&max_results=10",
                         headers={"Authorization": f"Bearer {tok}", "User-Agent": "Mozilla/5.0"},
                         timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200: return False
            d = await r.json(content_type=None)
    except Exception:
        return False
    return bool(isinstance(d, dict) and d.get("data"))

async def _creator_coins(s, w):
    raw = await _get_retry(s, f"{PUMP}/coins?creator={w}&offset=0&limit=20&includeNsfw=true&sort=created_timestamp&order=DESC")
    if not isinstance(raw, list): return []
    return [c for c in raw if isinstance(c, dict) and c.get("creator") == w]

async def x_matches_for(s, wallet, handle, role="target", name_kind="pump.fun username", pump=None, coins=None):
    """Look up one pump.fun or Fomo name, plus normalized variants, and score every X account found."""
    handle = (handle or "").strip().lstrip("@")
    if not handle: return {"matches": [], "miss": None, "note": X_NOTE, "posts_searched": bool(os.environ.get("X_BEARER_TOKEN"))}
    pump = pump if isinstance(pump, dict) else (await _get(s, f"{PUMP}/users/{wallet}") or {})
    if not isinstance(pump, dict): pump = {}
    if coins is None: coins = await _creator_coins(s, wallet)
    subject = {"wallet": wallet, "handle": handle, "role": role, "name_kind": name_kind,
               "pump_bio": pump.get("bio") or "", "x_username": pump.get("x_username") or "",
               "coins": coins, "pump_image": pump.get("profile_image")}
    variants = handle_variants(handle)
    found, errors, checked = [], [], []
    seen_ids = set()
    pump_hash = None
    for i, variant in enumerate(variants):
        if i: await asyncio.sleep(0.2)
        user, err = await _fx_user(s, variant)
        checked.append(variant)
        if err and not user: errors.append(err); continue
        if not user: continue
        uid = str(user.get("id") or user.get("screen_name")).lower()
        if uid in seen_ids: continue
        seen_ids.add(uid)
        posts = await _posts_have_wallet(s, user.get("screen_name"), wallet)
        avatar_match = False
        if subject["pump_image"] and user.get("avatar_url"):
            if pump_hash is None: pump_hash = await _ahash(s, subject["pump_image"])
            xh = await _ahash(s, user.get("avatar_url"))
            avatar_match = pump_hash is not None and xh is not None and _hamming(pump_hash, xh) <= 8
        found.append(score_x_candidate(subject, user, variant, avatar_match, posts))
    rank = {"Confirmed": 0, "Likely": 1, "Possible": 2}
    found.sort(key=lambda m: (rank.get(m["label"], 9), 0 if not m.get("variant") else 1, m.get("handle") or ""))
    miss = None
    if not found:
        miss = {"query": handle, "role": role, "name_kind": name_kind, "wallet": wallet,
                "checked": checked, "error": errors[0] if errors and len(errors) == len(checked) else None}
    return {"matches": found, "miss": miss, "note": X_NOTE, "posts_searched": bool(os.environ.get("X_BEARER_TOKEN"))}

async def x_name_report(s, wallet):
    """Name matches for this wallet's pump.fun username, which is also the only public Fomo-name guess."""
    pump = await _get(s, f"{PUMP}/users/{wallet}")
    pump = pump if isinstance(pump, dict) else {}
    handle = pump.get("username")
    if not handle:
        return {"matches": [], "misses": [], "note": X_NOTE, "posts_searched": bool(os.environ.get("X_BEARER_TOKEN")),
                "empty": "No pump.fun username or Fomo name on this wallet."}
    coins = await _creator_coins(s, wallet)
    rep = await x_matches_for(s, wallet, handle, "target", "pump.fun username · Fomo name guess", pump, coins)
    return {"matches": rep["matches"], "misses": [rep["miss"]] if rep.get("miss") else [], "note": X_NOTE,
            "posts_searched": rep["posts_searched"], "empty": None}

async def enrich_linked(items):
    """Light identity tags for linked wallets (pump.fun profile/X, .sol, KOLscan). Max 6, sequential to stay rate-limit friendly."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as s:
        matched = 0
        for a in items[:6]:
            p = await _get(s, f"{PUMP}/users/{a['wallet']}")
            p = p if isinstance(p, dict) else {}
            sns = await sns_section(s, a["wallet"]); k = await kolscan_section(s, a["wallet"])
            tags = []
            if p.get("x_username"): tags.append({"label": "X @" + p["x_username"], "link": f"https://x.com/{p['x_username']}", "strength": "strong (self-linked on pump.fun)"})
            if p.get("username") and (p.get("followers") or 0) >= 20: tags.append({"label": "pump.fun " + p["username"], "link": f"https://pump.fun/profile/{a['wallet']}", "strength": "medium" if not re.match(r"^[a-z]+\d{3,6}$|^[A-Z][a-z]+[A-Z][a-z]+[A-Z][a-z]+$", p["username"]) else "weak (auto-generated name)"})
            for d in sns.get("domains") or []: tags.append({"label": d, "link": f"https://www.sns.id/domain/{d[:-4]}", "strength": "strong (owner-registered)"})
            if k.get("is_kol"): tags.append({"label": "KOL " + (k.get("name") or ""), "link": k.get("link"), "strength": "strong"})
            a["identity"] = tags
            if p.get("username") and matched < 3:
                matched += 1
                try: a["x_match"] = await x_matches_for(s, a["wallet"], p["username"], "funder", "pump.fun username", p)
                except Exception: a["x_match"] = None
    return items

async def lookup(q):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
        w, prof = await resolve_input(s, q)
        if not w: return {"error": f"not a Solana wallet or pump.fun user: {q}"}
        pump, sns, hold, hist = await asyncio.gather(pump_section(s, w, prof), sns_section(s, w), holdings_section(s, w), history_section(s, w))
        handle = (pump or {}).get("x_username")
        import activity
        async def fomo_onchain():
            try: return activity.fomo_check((await activity.fetch(s, w, n=int(os.environ.get("FOMO_CHECK_TXS", "40"))))["rows"])
            except Exception as e: return {"verified": False, "error": str(e)}
        x, kol, fchk = await asyncio.gather(x_section(s, handle, w), kolscan_section(s, w, handle), fomo_onchain())
        guess = (pump or {}).get("username")
        fomo = {"verified": False, "source": None, "onchain": fchk,
                "note": "fomo.family's API (prod-api.fomo.family) requires login; no public wallet→profile mapping exists",
                "guess_link": guess and f"https://fomo.family/profile/{guess}", "guess_label": "unverified guess (same handle as pump.fun)"}
        if fchk.get("verified"):
            fomo.update(verified=True, source="on-chain: tx co-signed by Fomo fee payer",
                        evidence=f"{fchk['hits']} of the last {fchk['checked_txs']} txs co-signed by Fomo fee payer {fchk['signer']}",
                        example_link=fchk["example_link"], signer_link=fchk["signer_link"],
                        note="Fomo username isn't public (Fomo's API needs login); handle link below is still a guess.")
        out = {"wallet": w, "fetched_at": int(time.time()), "pump": pump, "x": x, "domains": sns, "holdings": hold, "history": hist,
                "fomo": fomo, "labels": {"known_label": None, "note": "no free keyless wallet-label source (Solscan/Arkham labels need an API key or login)"},
                "links": links(w, guess, handle), "kolscan": kol,
                "unavailable": {"GMGN": "Cloudflare bot wall (403) – link only", "Cielo": "profile 404/429 without key – link only",
                                "Birdeye/Arkham": "API key required – link only", "Nitter/xcancel (keyless X search)": "blocked / JS challenge",
                                "AllDomains": "no public API found"}}
        res = out
        res["found_on"] = [n for n, ok in (("pump.fun", bool(pump and pump.get("username"))), ("X", bool(handle)), ("SNS .sol", bool(sns.get("domains"))),
                           ("KOLscan", kol.get("is_kol")), ("KOLscan (same X handle)", bool(kol.get("same_x_handle_kol_wallets"))),
                           ("X mentions", bool(x.get("mentions"))), ("Fomo ✅ (on-chain)", fomo["verified"]), ("Fomo (unverified)", bool(guess) and not fomo["verified"])) if ok]
        return res

def _ago(ts):
    if not ts: return "?"
    d = (time.time() - ts) / 86400
    return f"{time.strftime('%Y-%m-%d', time.localtime(ts))} ({d:.0f}d ago)"

def to_html(r):
    """Telegram HTML summary."""
    import html as H
    if r.get("error"): return H.escape(r["error"])
    w = r["wallet"]; L = []
    L.append(f"🔎 <b>Wallet</b> <code>{w}</code>")
    if r.get("found_on"): L.append("Found on: " + ", ".join(r["found_on"]))
    k = r.get("kolscan") or {}
    if k.get("is_kol"):
        st = k.get("stats", {}).get("1") or next(iter(k.get("stats", {}).values()), {})
        L.append(f"\n<b>KOLscan</b>: <a href=\"{k['link']}\">{H.escape(k.get('name') or '')}</a>" + (f" · #{st.get('rank')} today, {st.get('profit_sol', 0):+.1f} SOL ({st.get('wins')}W/{st.get('losses')}L)" if st else ""))
    p = r.get("pump")
    if p:
        L.append(f"\n<b>pump.fun</b>: <a href=\"{p['link']}\">{H.escape(p.get('username') or w[:6])}</a> · {p.get('followers') or 0:,} followers")
        if p.get("bio"): L.append(f"Bio: {H.escape(p['bio'])}")
        if p.get("evm_wallet"): L.append(f"EVM: <code>{p['evm_wallet']}</code>")
        if p.get("coins_unavailable"): L.append("Coins created: lookup failed")
        else: L.append(f"Coins created: {len(p['coins_created'])}" + "".join(f"\n  • <a href=\"{c['link']}\">${H.escape(c.get('symbol') or '?')}</a> <code>{c['mint']}</code>" for c in p["coins_created"][:5]))
    x = r["x"]
    if x.get("handle"):
        pr = x.get("profile") or {}
        L.append(f"\n<b>X</b>: <a href=\"{x['link']}\">@{x['handle']}</a>" + (f" · {pr['public_metrics']['followers_count']:,} followers" if pr else "") + " (from pump.fun)")
    for m in x.get("mentions", [])[:5]: L.append(f"  • <a href=\"{m['link']}\">@{m['handle']}</a> mentioned this wallet")
    d = r["domains"]
    if d.get("domains"): L.append(f"\n<b>.sol</b>: {', '.join(d['domains'])}" + (f" (primary {d['primary']})" if d.get("primary") else ""))
    h = r["holdings"]
    if h.get("sol") is not None: L.append(f"\n<b>Balance</b>: {h['sol']:.3f} SOL" + (f" (~${h['sol_usd']:,.0f})" if h.get("sol_usd") else "") + f" · {h['token_count']} tokens")
    for t in h["top"][:5]: L.append(f"  • ${H.escape(t.get('symbol') or '?')} " + (f"~${t['usd']:,.0f}" if t.get("usd") else "") + f" <code>{t['mint']}</code>")
    hi = r["history"]
    if hi.get("complete"):
        L.append(f"\n<b>Age</b>: first tx {_ago(hi.get('first_seen'))}, {hi.get('tx_count')} txs")
        if hi.get("first_fee_payer"): L.append(f"Gas paid by <a href=\"{hi['first_fee_payer_link']}\">{hi['first_fee_payer'][:6]}…</a> (sponsored)")
        if hi.get("funded_by"): L.append(f"Funded by <a href=\"{hi['funded_by_link']}\">{hi['funded_by'][:6]}…</a> ({hi['funded_sol']:.3f} SOL)")
    elif hi.get("tx_count_min"): L.append(f"\n<b>Age</b>: older than {_ago(hi.get('oldest_seen'))} (&gt;{hi['tx_count_min']:,} txs)")
    f = r["fomo"]
    if f.get("verified"): L.append(f"\n<b>Fomo</b>: ✅ verified on-chain – <a href=\"{f['example_link']}\">tx co-signed by Fomo fee payer</a>")
    elif f.get("guess_link"): L.append(f"\n<b>Fomo</b>: <a href=\"{f['guess_link']}\">{f['guess_link'].split('/')[-1]}</a> ⚠️ unverified guess")
    L.append("\n" + " | ".join(f'<a href="{u}">{k}</a>' for k, u in r["links"].items()))
    return "\n".join(L)

if __name__ == "__main__":
    import sys, json
    print(json.dumps(asyncio.run(lookup(sys.argv[1])), indent=1, default=str))
