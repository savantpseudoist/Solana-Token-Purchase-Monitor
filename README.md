# Solana Token Purchase Monitor

A Telegram bot that watches Solana wallets and alerts a chat the moment a
watched wallet acquires a new token, using the
[Helius Enhanced Transactions API](https://www.helius.dev/docs/api-reference/enhanced-transactions/gettransactionsbyaddress).

- **Correct detection** – purchases are derived from on-chain *balance deltas*
  rather than from Helius' transaction type, so buys on unrecognised programs are
  not missed, and sells, NFTs and wrapped SOL do not alert.
- **No duplicate noise** – duplicate suppression is persisted, so a restart does
  not re-alert the wallet's recent history.
- **At-least-once delivery** – if Telegram is unreachable, the alert is retried on
  the next poll instead of being lost.
- **Fail-closed access control** – only configured administrators can change what
  is monitored.
- **Typed and tested** – strict `mypy`, `ruff`, and a test suite that never
  touches the network.

## How it works

1. `/set_wallet` stores the wallet to watch (validated as a real base58 Solana
   address) and `/start_monitoring` begins polling.
2. The first poll establishes a **baseline**: tokens the wallet already holds
   never alert, so starting the bot does not flood the chat.
3. Every later poll fetches only what is newer than the stored cursor, turns each
   transaction into zero or more *acquisitions*, and sends one alert per newly
   acquired mint.
4. State (cursor, alerted mints, baseline) is written atomically to
   `var/state.json`, so restarts and crashes do not cause duplicate alerts.

```
Telegram  ──commands──▶  CommandService  ──▶  WatchManager  ──▶  WalletMonitor
                                 │                                      │
                                 │                                      ├─▶ HeliusClient (transactions, DAS)
                                 │                                      ├─▶ PurchaseDetector (pure rules)
                                 │                                      ├─▶ TokenMetadataResolver (cached)
                                 │                                      └─▶ TelegramAlertSink ──▶ Telegram
                                 └────────────── WatchStore (var/state.json)
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design and
[docs/OPERATIONS.md](docs/OPERATIONS.md) for running it in anger.

## Requirements

- Python 3.11+
- A [Telegram bot token](https://t.me/BotFather)
- A [Helius API key](https://dashboard.helius.dev)

## Quick start

```bash
git clone https://github.com/savantpseudoist/Solana-Token-Purchase-Monitor.git
cd Solana-Token-Purchase-Monitor

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e ".[dev]"            # or: pip install -r requirements.txt

cp .env.example .env               # then edit .env
```

Fill in `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:AA...        # from @BotFather
HELIUS_API_KEY=00000000-1111-2222-3333-444444444444
TELEGRAM_ADMIN_USER_IDS=11111111          # your Telegram user id
```

Validate the configuration without contacting anyone:

```bash
solana-monitor --check-config
```

Then start the bot and, in Telegram:

```
/set_wallet <solana_address>     # the wallet to watch
/start_monitoring
/status
```

## Commands

| Command | Who | Description |
| --- | --- | --- |
| `/set_wallet <address>` | admin | Choose (or change) the wallet to watch |
| `/start_monitoring` | admin | Start alerting; existing holdings become the baseline |
| `/stop_monitoring` | admin | Stop alerting (state is kept) |
| `/status` | anyone in the chat | Watch state, poll health, counters, last error |
| `/analyze <1h\|1d\|1w>` | anyone in the chat | Purchases in a time window (server-side query) |
| `/whoami` | anyone | Your Telegram user and chat id (for `TELEGRAM_ADMIN_USER_IDS`) |
| `/help`, `/start` | anyone | Command list |

Example alert:

```
🟢 New token purchase

Token: BONK
Amount: 1,234.5
Spent: 0.5 SOL
Wallet: 9WzD...tAWWM
CA: DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263
Time: 2026-09-27 11:00:00 UTC
```

## Configuration

Every setting is an environment variable (a `.env` file is read automatically).
`.env.example` documents all of them; these are the ones you will actually
change:

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | – | **Required.** Bot token from @BotFather |
| `HELIUS_API_KEY` | – | **Required.** Helius API key |
| `TELEGRAM_ADMIN_USER_IDS` | – | Comma separated user ids allowed to change the watch. **Empty disables those commands for everyone.** |
| `TELEGRAM_ALLOWED_CHAT_IDS` | – | Comma separated chat ids the bot answers in (empty = any chat) |
| `MONITOR_POLL_INTERVAL_SECONDS` | `20` | Seconds between polls. Main cost driver: each poll costs roughly 100 Helius credits. |
| `MONITOR_ALERT_ON_RECEIPTS` | `true` | Alert on tokens that arrive without a payment (airdrops, transfers) |
| `MONITOR_ALERT_ON_REPEAT` | `false` | Alert again when a previously alerted mint is bought again |
| `MONITOR_RESUME_ON_START` | `true` | Resume monitoring automatically after a restart |
| `MONITOR_IGNORED_MINTS` | wrapped SOL | Comma separated mints that never alert |
| `STATE_FILE` | `var/state.json` | Where cursors and alerted mints are stored |
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO`, `text` | Logging verbosity and `text`/`json` output |

Configuration is validated at start-up: a missing or malformed value fails
immediately with a message that names the variable and never echoes a secret.

## Running it

```bash
solana-monitor                      # console script
python -m solana_monitor            # module entry point
python main.py                      # compatibility shim

solana-monitor --check-config       # validate configuration and exit
solana-monitor --log-level DEBUG --log-format json
```

With Docker:

```bash
cp .env.example .env   # fill it in first
docker compose up -d
docker compose logs -f
```

State lives in the `monitor-state` volume; losing it means re-alerting the
wallet's recent history, so do not delete it casually.

## Development

```bash
pip install -e ".[dev]"

ruff check .            # lint
ruff format .           # format
mypy                    # strict type check
pytest                  # tests (no network required)
pytest --cov            # with coverage
pre-commit install      # optional: run checks on every commit
```

The test suite is offline by design: HTTP, Telegram and the clock are injected
doubles, so retry/backoff, rate limiting and duplicate suppression can be
asserted deterministically instead of by sleeping.

Layout:

```
src/solana_monitor/
├── config.py            # typed settings, validated from the environment
├── logging_setup.py     # structured logging with secret redaction
├── resilience.py        # retry, backoff, rate limiting
├── domain/              # value objects, errors, address validation
├── helius/              # wire schemas, HTTP client, metadata cache
├── monitoring/          # detection, persisted state, poll loop
├── telegram/            # command service, rendering, PTB wiring
└── cli.py               # entry point
```

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `/start_monitoring` says no wallet | Run `/set_wallet <address>` first |
| "No administrators are configured" | Set `TELEGRAM_ADMIN_USER_IDS`; use `/whoami` to find your id, then restart |
| Bot replies "not configured for this bot" | The chat is not in `TELEGRAM_ALLOWED_CHAT_IDS` |
| No alerts, `/status` shows errors | Check `HELIUS_API_KEY` and quota; `API: degraded` plus the last error is the quickest clue |
| Alerts stop after a restart | Confirm the state file path is writable and persisted (Docker volume) |
| Amounts show as raw units | Token decimals are unknown; the alert says so instead of guessing |

## Further reading

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) – layers, data flow, decisions
- [docs/OPERATIONS.md](docs/OPERATIONS.md) – cost model, alerting policy,
  failure modes, security notes

## License

MIT – see [LICENSE](LICENSE).
