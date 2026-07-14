# Cloud architecture: Vercel + Supabase + GCP VM

The local launcher/dashboard stack (localhost 8800/8801) is replaced by:

```
 browser ── Vercel (webapp/, Next.js) ── Supabase (Postgres/PostgREST) ── GCP VM (bot/cloud_agent.py)
                 reads state,                 the only shared state           polls commands, runs bot.py,
                 queues commands                                              writes state + logs back
```

The webapp and the VM never talk to each other directly — Supabase is the
single meeting point, so the VM needs **no open inbound ports** at all.

## Data flow

| Table             | Written by | Read by | Purpose |
|-------------------|-----------|---------|---------|
| `bot_settings`    | webapp    | agent   | defaults: exchange, symbol, strategy params |
| `bot_credentials` | webapp    | agent   | exchange API keys (service-role only) |
| `bot_commands`    | webapp    | agent   | queue: start / stop / check / backtest |
| `bot_state`       | agent     | webapp  | heartbeat, running/mode, @STATS, exchange snapshot |
| `bot_logs`        | agent     | webapp  | console lines, idempotent by `seq`, pruned to 5000 |

The agent polls every 2 s; the dashboard polls its own API every 3 s, so a
button press reaches the bot in ≤ ~5 s. A `bot_state.heartbeat_at` older than
15 s renders as **agent offline** in the dashboard.

## 1. Supabase setup

1. Create a project at supabase.com.
2. SQL Editor → paste and run `bot/supabase_schema.sql`.
3. Note two values from Project Settings → API:
   - Project URL (`SUPABASE_URL`)
   - `service_role` secret key (`SUPABASE_SERVICE_ROLE_KEY`)

RLS is enabled with no policies, so the anon key can read nothing; both
sides authenticate with the service-role key, server-side only.

## 2. GCP VM (backend)

Any small VM works (e2-micro is enough — the bot is one Python process and
a poll loop). Debian/Ubuntu image, no inbound firewall rules needed.

```sh
sudo apt-get update && sudo apt-get install -y python3 git
git clone <this repo> && cd bare-features/bot
```

Run the agent under systemd — `/etc/systemd/system/gridbot-agent.service`:

```ini
[Unit]
Description=Grid bot Supabase agent
After=network-online.target
Wants=network-online.target

[Service]
User=bot
WorkingDirectory=/home/bot/bare-features/bot
ExecStart=/usr/bin/python3 -u cloud_agent.py
Environment=SUPABASE_URL=https://<project>.supabase.co
Environment=SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
Restart=always
RestartSec=5
# SIGTERM -> agent stops the bot first (open orders get cancelled)
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl enable --now gridbot-agent
journalctl -u gridbot-agent -f
```

On shutdown/restart the agent stops the bot gracefully (same STOP-on-stdin
path as the local launcher, so open orders are cancelled) and writes a final
`running=false` state row.

## 3. Vercel (frontend)

Import `webapp/` as the project root. Environment variables:

| Var | Purpose |
|-----|---------|
| `SUPABASE_URL` | same project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | server-side only — used by route handlers, never bundled to the browser |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | first-login bootstrap only (see below) |
| `SESSION_SECRET` | signs the session cookie — always required |

**Auth lives in the DB**: logins are checked against the `webapp_users`
table (PBKDF2-hashed). While that table is empty, the first login matching
`ADMIN_USERNAME`/`ADMIN_PASSWORD` seeds it; from then on the env pair is
ignored and can be removed. To change a password, delete the row in the
Supabase table editor and log in once with the env pair again (or insert a
new hash). Without Supabase configured, the env pair alone is used.

## Local development

```powershell
# terminal 1 — the agent (stands in for the VM)
$env:SUPABASE_URL = "https://<project>.supabase.co"
$env:SUPABASE_SERVICE_ROLE_KEY = "<key>"
py bot\cloud_agent.py

# terminal 2 — the webapp (put the same vars in webapp\.env.local)
cd webapp; npm run dev
```

## Semantics worth knowing

- **Commands, not RPC**: the webapp inserts a `bot_commands` row and returns
  immediately; the agent claims it (`pending → running`, conditional update,
  so a duplicate agent can't double-run it) and marks it `done`/`error` with
  a result message. In-flight commands show as a "queued:" badge.
- **Start = the form you see**: start/check/backtest commands carry the
  dashboard form as params, merged over `bot_settings` by the agent.
  "Save settings" only updates the stored defaults.
- **No console in the UI**: outcomes surface in the dashboard's Activity feed
  (recent `bot_commands` with results). Full console output still streams into
  `bot_logs` for debugging — read it via `/api/bot/logs?since=N` or the
  Supabase table editor. The feed restarts from seq 0 when the agent restarts.
- **Credentials**: one key pair per exchange family (`binance`, `gate` —
  Gate spot and futures share keys), stored in `bot_credentials`, synced to
  the bot process as `BINANCE_*`/`GATE_*` env vars at start, exactly like the
  local api_server did.
