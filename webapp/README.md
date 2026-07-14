# gridbot-webapp — cloud dashboard (in development)

Next.js app that will become the Vercel-hosted face of the trading bot.
Current state: **env-based login + dashboard scaffold only** — not connected
to Supabase yet; every data panel is an explicit placeholder. The live bot
still runs on the local stack (`bot/` + localhost:8800).

Planned architecture: Vercel (this app) ⇄ Supabase (state, commands, fills)
⇄ VM backend running the bot, polling Supabase for actions and writing state
updates back.

## Auth (pre-Supabase)

Single admin identity from environment variables; sessions are HMAC-signed
httpOnly cookies (12 h), enforced by `middleware.js` on every route except
`/login`.

| Var | Meaning |
| --- | --- |
| `ADMIN_USERNAME` | login name (currently `prismoid-admin`) |
| `ADMIN_PASSWORD` | login password |
| `SESSION_SECRET` | signs session cookies — rotate to log everyone out |

Local values live in `.env.local` (gitignored). `.env.example` documents the
shape.

## Run locally

```powershell
cd bare-features\webapp
npm install
npm run dev          # http://localhost:3000 -> redirects to /login
```

## Deploy to Vercel

```powershell
cd bare-features\webapp
npm i -g vercel      # once
vercel login         # once
vercel               # link + preview deploy (project root = this directory)

# set the env vars on the project (repeat for each, or use the dashboard)
vercel env add ADMIN_USERNAME
vercel env add ADMIN_PASSWORD
vercel env add SESSION_SECRET

vercel --prod        # production deploy
```

## Layout

```
webapp/
├── middleware.js            # session gate for every page
├── lib/auth.js              # HMAC cookie sign/verify (Web Crypto, edge-safe)
├── app/
│   ├── login/page.js        # sign-in form
│   ├── dashboard/page.js    # scaffold: tiles, params, console (placeholders)
│   └── api/auth/{login,logout}/route.js
├── .env.local               # real credentials (gitignored)
└── .env.example
```
