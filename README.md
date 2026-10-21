# FomoFinder
Paste a Solana wallet, pump.fun profile URL or username and get a card of everything public tied to it, each with its source.
Shareable links: `/?q=<wallet>`. API: `/api/lookup?q=`. Rate limit: `RATE_PER_MIN` (default 10) per IP; cached results are free.

Sources: pump.fun (profile, followers/following, X, EVM, coins), KOLscan (tracked KOL? name, X, rank/PnL today/7d),
Bonfida SNS (.sol), Jupiter + Solana RPC (holdings, age, gas payer), X (handle + search links; posts need `X_BEARER_TOKEN`),
Fomo (unverified handle guess only: fomo's API requires login). GMGN/Cielo/Birdeye/Arkham are link-only (bot wall or API key needed).

Run locally: `pip install -r requirements.txt && uvicorn app:app --port 8787`. `./start.sh` restarts it on the box.

## Permanent hosting (free tiers)
- **Render** (easiest): push this folder to a GitHub repo, then New → Blueprint (uses `render.yaml`). Free instances sleep after ~15 min idle (cold start ~30–60 s).
- **Railway**: New project → deploy from repo; it reads `Procfile`. Free trial credit, then about $5/mo.
- **Fly.io**: `fly launch`. Needs a card on file.
- Vercel is a poor fit: serverless timeouts are ~10 s and a lookup can take up to ~15 s.
You'd need a GitHub account (for the repo) and a Render/Railway account. Optional env: `X_BEARER_TOKEN`, `SOLANA_ARCHIVE_RPC_URL`, `RATE_PER_MIN`.
