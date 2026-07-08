# bare-features

A compilation of hummingbot-api's core feature implementations, extracted so the
trading strategies, the credential-holding layer, and the database hooks can be
studied and tested in isolation — without the FastAPI app, Docker orchestration,
or Gateway integration around them.

```
bare-features/
├── strategies/            # trading strategy implementations (verbatim copies)
│   ├── controllers/       #   from bots/controllers — the strategy logic bots run
│   │   ├── directional_trading/   bollinger_v1/v2, bollingrid, dman_v3, macd_bb_v1, supertrend_v1
│   │   ├── market_making/         pmm_simple, pmm_dynamic, dman_maker_v2
│   │   └── generic/               pmm variants, grid_strike, stat_arb, arbitrage, xemm, examples/
│   └── scripts/           #   from bots/scripts — v2_with_controllers.py, the entry
│                          #   script every deployed bot container runs
├── database/              # database hooks (verbatim copy of database/)
│   ├── connection.py      #   AsyncDatabaseManager: engine, sessions, migrations
│   ├── models.py          #   SQLAlchemy models (orders, trades, executors, account states…)
│   └── repositories/      #   one repository per aggregate, async-session based
├── credentials/           # credentials holding, trimmed for standalone testing
│   ├── credential_manager.py       # NEW: bare manager (see provenance in docstring)
│   ├── security.py                 # from utils/security.py (imports localized)
│   ├── file_system.py              # from utils/file_system.py (verbatim)
│   ├── hummingbot_api_config_adapter.py  # from utils/ (verbatim)
│   └── templates/master_account/   # account skeleton (conf_client.yml, fee overrides, logs)
├── tests/
│   ├── test_credentials.py  # encrypt → persist → decrypt round trip in a temp dir
│   └── test_database.py     # schema creation + row round trip against compose Postgres
├── poc/                     # grid_strike + pmm_simple + supertrend_v1 proof of concept
│   ├── backtest_strategies.py   # backtests all three via BacktestingEngineBase
│   ├── exchange_test_trade.py   # strategy-derived orders -> Binance /api/v3/order/test
│   ├── store_binance_keys.py    # save API keys into the encrypted credential store
│   └── results/                 # per-strategy backtest output (JSON)
├── bot/                     # standalone testnet trading bot (pure stdlib, runs with `py`)
│   ├── bot.py                   # all three strategies live on Binance Spot Testnet
│   ├── binance_client.py        # REST client + request/rate-limit accounting
│   ├── strategies.py            # live grid_strike / pmm_simple / supertrend_v1
│   ├── gui.py                   # simple tkinter form UI for Grid Strike
│   ├── launcher.py              # XAMPP-style control panel (web UI + bot API services)
│   ├── api_server.py            # bot backend API (port 8801)
│   ├── web_server.py            # dashboard web server (port 8800)
│   ├── web/index.html           # browser dashboard
│   └── build_windows.ps1 / build_macos.sh   # package as .exe / .dmg (PyInstaller)
└── docs/
    └── trading-bot.md       # bot design: strategies, rate limits, request budget
```

## How the pieces hook into the real app

- **Strategies** are not imported by the API process. When the dashboard deploys a
  bot, `services/docker_service.py::create_hummingbot_instance` spawns a
  `hummingbot/hummingbot` container that runs `scripts/v2_with_controllers.py`,
  which loads the controller configs and instantiates the controller classes in
  `controllers/`. Strategy testing therefore happens inside a hummingbot
  environment, not against the API.
- **Credentials** live under `bots/credentials/<account>/`. The API encrypts each
  connector's API keys into `connectors/<connector>.yml` using hummingbot's
  ETH-keyfile encryption, keyed by `CONFIG_PASSWORD`. `credential_manager.py`
  reproduces exactly that storage path (same files, same crypto) with the live
  exchange-connector and balance-polling machinery stripped out.
- **Database hooks**: `AsyncDatabaseManager` (asyncpg + SQLAlchemy async) creates
  the schema on startup and runs lightweight column migrations; services write
  through the `repositories/` classes, one per aggregate.

## Running the tests

Both tests need the `hummingbot` python package (heavy, Linux-oriented), so the
simplest runner on Windows is the API image itself:

```powershell
# credentials round trip (self-contained, uses a temp dir)
docker run --rm -v ${PWD}:/work -w /work/bare-features/tests `
    --entrypoint python hummingbot/hummingbot-api:latest test_credentials.py

# database hooks (needs the compose Postgres running: docker compose up -d postgres)
docker run --rm --network hummingbot-api_emqx-bridge `
    -e DATABASE_URL=postgresql+asyncpg://hbot:hummingbot-api@postgres:5432/hummingbot_api `
    -v ${PWD}:/work -w /work/bare-features/tests `
    --entrypoint python hummingbot/hummingbot-api:latest test_database.py
```

`test_database.py` only needs SQLAlchemy + asyncpg, so it also runs on the host
(`py bare-features\tests\test_database.py`) against `localhost:5432` if you have
those installed.

## Strategy proof of concept (poc/)

Backtesting (fetches real Binance candles; window/pair/size configurable via args):

```powershell
docker run --rm -v ${PWD}:/work -w /work/bare-features/poc `
    --entrypoint python hummingbot/hummingbot-api:latest backtest_strategies.py
```

Exchange test-trade calls — derives one order per strategy from live market data
and submits each to Binance's `POST /api/v3/order/test` (full exchange-side
validation, never executes). Pure stdlib, runs on the host:

```powershell
# keyless: builds and prints the orders, skips the signed calls
py bare-features\poc\exchange_test_trade.py

# with free sandbox keys from https://testnet.binance.vision (GitHub login):
$env:BINANCE_API_KEY='...'; $env:BINANCE_API_SECRET='...'
py bare-features\poc\exchange_test_trade.py --mode testnet
```

Keys can also be pulled from the encrypted credential store instead of env vars
(`store_binance_keys.py` writes them, `--credentials-account <name>` reads them).

Note: `strategies/controllers/directional_trading/supertrend_v1.py` carries a
small backtesting fix over the `bots/` original — pandas_ta's complementary
`SUPERTl_*`/`SUPERTs_*` NaN columns made the engine's `dropna()` empty the whole
frame; the copy drops those columns before publishing features.

## Provenance

| bare-features file | compiled from |
| --- | --- |
| `strategies/controllers/**` | `bots/controllers/**` (verbatim) |
| `strategies/scripts/v2_with_controllers.py` | `bots/scripts/` (verbatim) |
| `database/**` | `database/**` (verbatim) |
| `credentials/security.py` | `utils/security.py` (imports localized, config constant inlined) |
| `credentials/file_system.py` | `utils/file_system.py` (verbatim) |
| `credentials/hummingbot_api_config_adapter.py` | `utils/hummingbot_api_config_adapter.py` (verbatim) |
| `credentials/credential_manager.py` | new — credential methods of `services/accounts_service.py` + the encrypt-and-persist path of `services/unified_connector_service.py::update_connector_keys` |
| `credentials/templates/master_account/` | `bots/credentials/master_account/` (config skeleton only — no `.password_verification`, tests generate their own) |
