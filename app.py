"""FomoFinder – Solana wallet identity lookup. Run: uvicorn app:app --host 127.0.0.1 --port 8787 --proxy-headers"""
import mimetypes, os, re, sys, time
from collections import defaultdict, deque
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # identity.py ships alongside (copy of ca-tracker-bot/identity.py)
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import identity
mimetypes.add_type("image/webp", ".webp")
app = FastAPI(title="FomoFinder", docs_url=None, redoc_url=None, openapi_url=None)
HERE = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
SOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
USER = re.compile(r"^@?[A-Za-z0-9_.]{2,32}$")
PUMP_URL = re.compile(r"^https?://(www\.)?pump\.fun/profile/@?[A-Za-z0-9_.]{2,44}/?$")
RATE = int(os.environ.get("RATE_PER_MIN", "10"))
_hits, _cache = defaultdict(deque), {}

def client_ip(req: Request):
    return req.headers.get("cf-connecting-ip") or (req.headers.get("x-forwarded-for") or "").split(",")[0].strip() or req.client.host

@app.get("/")
def index(): return FileResponse(os.path.join(HERE, "index.html"), headers={"Cache-Control": "no-cache"})

@app.get("/healthz")
def health(): return {"ok": True}

@app.get("/api/lookup")
async def lookup(q: str, request: Request):
    q = q.strip()
    if len(q) > 120 or not (SOL.match(q) or USER.match(q) or PUMP_URL.match(q)):
        return JSONResponse({"error": "Enter a Solana wallet, pump.fun profile URL, or pump.fun username."}, status_code=400)
    now = time.time()
    if q in _cache and now - _cache[q][0] < 300: return _cache[q][1]      # cached results don't count against the limit
    dq = _hits[client_ip(request)]
    while dq and now - dq[0] > 60: dq.popleft()
    if len(dq) >= RATE: return JSONResponse({"error": "Too many lookups, wait a minute."}, status_code=429)
    dq.append(now)
    r = await identity.lookup(q)
    if r.get("error"): return JSONResponse(r, status_code=404)
    _cache[q] = (now, r)
    if len(_cache) > 500: _cache.pop(next(iter(_cache)))
    return r
