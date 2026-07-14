-- Supabase schema for the cloud bot architecture.
--
--   Vercel webapp  --writes-->  bot_settings / bot_commands / bot_credentials
--   VM cloud_agent --reads --^  and --writes--> bot_state / bot_logs
--
-- Run this once in the Supabase SQL editor (Dashboard > SQL Editor).
--
-- Access model: RLS is enabled on every table with NO policies, so the anon
-- and authenticated keys can read nothing. Both the webapp (server-side only)
-- and the VM agent use the service-role key, which bypasses RLS. Never ship
-- the service-role key to the browser.

-- Settings the webapp edits and the VM agent reads when starting the bot.
create table if not exists bot_settings (
  id          int primary key default 1 check (id = 1),   -- single row
  exchange    text not null default 'gate_futures',
  symbol      text not null default 'BTCUSDT',
  params      jsonb not null default '{}',  -- strategies, pmm_spreads, leverage, ...
  updated_at  timestamptz not null default now()
);

-- Action queue: the webapp inserts, the agent claims and executes.
create table if not exists bot_commands (
  id            bigint generated always as identity primary key,
  created_at    timestamptz not null default now(),
  action        text not null check (action in ('start', 'stop', 'check', 'backtest')),
  params        jsonb not null default '{}', -- overrides merged over bot_settings
  status        text not null default 'pending'
                check (status in ('pending', 'running', 'done', 'error')),
  result        jsonb,
  requested_by  text,
  handled_at    timestamptz
);
create index if not exists bot_commands_pending on bot_commands (id) where status = 'pending';

-- Live state mirror: upserted by the agent every poll tick. The webapp reads
-- this row and treats a stale heartbeat_at (> ~15 s) as "agent offline".
create table if not exists bot_state (
  id                int primary key default 1 check (id = 1),  -- single row
  running           boolean not null default false,
  mode              text,           -- check | dry-run | trading | backtest
  pid               int,
  started_at        timestamptz,
  credentials_set   boolean not null default false,
  stats             jsonb,          -- last @STATS payload from bot.py
  backtest          jsonb,          -- last @BACKTEST comparison table
  exchange_summary  jsonb,          -- balances / orders / position snapshot
  log_next          bigint not null default 0,
  last_exit         jsonb,          -- {code, tail} when the bot died nonzero
  heartbeat_at      timestamptz,
  agent_version     text
);
-- upgrade for databases created before this column existed
alter table bot_state add column if not exists last_exit jsonb;

-- Console feed. seq is the BotManager line index, so pushes are idempotent.
-- The agent prunes old rows; the webapp polls "seq >= since".
create table if not exists bot_logs (
  seq         bigint primary key,
  line        text not null,
  created_at  timestamptz not null default now()
);

-- Exchange API keys, written by the webapp, read by the agent. Service-role
-- only (RLS below); consider Supabase Vault if you want at-rest encryption.
create table if not exists bot_credentials (
  exchange    text primary key,
  api_key     text not null,
  api_secret  text not null,
  updated_at  timestamptz not null default now()
);

-- Dashboard logins. Seeded automatically: the first successful login with the
-- ADMIN_USERNAME/ADMIN_PASSWORD env pair creates this row (PBKDF2 hash); after
-- that the env vars are no longer consulted. Manage rows in the table editor.
create table if not exists webapp_users (
  username      text primary key,
  password_hash text not null,   -- pbkdf2$<iterations>$<salt-hex>$<hash-hex>
  created_at    timestamptz not null default now()
);

alter table webapp_users    enable row level security;
alter table bot_settings    enable row level security;
alter table bot_commands    enable row level security;
alter table bot_state       enable row level security;
alter table bot_logs        enable row level security;
alter table bot_credentials enable row level security;

insert into bot_settings (id) values (1) on conflict do nothing;
insert into bot_state (id) values (1) on conflict do nothing;
