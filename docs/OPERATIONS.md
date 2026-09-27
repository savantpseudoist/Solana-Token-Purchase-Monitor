# Operations

Everything an operator needs to run this in anger: what it costs, what it will
alert on, how it fails, and how to keep it safe.

## Cost model

Helius bills Enhanced Transactions requests by credits (roughly 100 credits per
request), so **the poll interval is the entire cost model**:

| `MONITOR_POLL_INTERVAL_SECONDS` | Requests/day (1 watch) | Credits/day | Credits/month |
| --- | --- | --- | --- |
| 10 | 8,640 | ~864k | ~26M |
| 20 (default) | 4,320 | ~432k | ~13M |
| 60 | 1,440 | ~144k | ~4.3M |
| 300 | 288 | ~29k | ~0.9M |

Plus small amounts for DAS metadata (cached for an hour per mint) and wallet
holdings (read once when monitoring starts). Verify the exact numbers against
your plan on the [Helius pricing page](https://www.helius.dev/pricing); the
figures above are the reasoning, not a quote.

`HELIUS_REQUESTS_PER_SECOND` (default 2) is a client-side ceiling that keeps the
bot inside plan limits, and `/analyze` issues its own windowed queries on demand.

## Alerting policy

What does and does not produce an alert:

| Situation | Alert? |
| --- | --- |
| Wallet receives a token it never held, having paid SOL or another token | Yes (`purchase`) |
| Wallet receives a token with no observable payment (airdrop, transfer) | Yes by default, `receipt` label; disable with `MONITOR_ALERT_ON_RECEIPTS=false` |
| Token the wallet already held when monitoring started | No (baseline) |
| Token already alerted in an earlier run | No, unless `MONITOR_ALERT_ON_REPEAT=true` |
| Sell (token leaves the wallet) | No |
| NFT (`NonFungible`-style token standard, or a single indivisible unit) | No |
| Mints in `MONITOR_IGNORED_MINTS` (wrapped SOL by default) | No |
| Failed transaction | No |
| Amount whose decimals are unknown | Yes, and the alert says "unknown" rather than guessing |

## Failure modes and what you will see

| Failure | Behaviour | Where to look |
| --- | --- | --- |
| Helius 429 (rate limit) | Honours `Retry-After`, exponential backoff, keeps polling | log `helius rate limit reached` |
| Helius 5xx / network error | Retries with jittered backoff, then backs off up to `MONITOR_MAX_BACKOFF_SECONDS` | `/status` shows `degraded` + last error |
| Helius 401/403 | Backs off 5 minutes and logs an error; polling continues | log `helius rejected our credentials` |
| Malformed transaction in a page | Skipped individually, counted in a warning | log `skipped malformed transactions` |
| Telegram unreachable | Alert retried on the next poll; the purchase is *not* lost | log `could not deliver alert` |
| Bot blocked in a chat | Delivery fails permanently, chat ignored | log `bot is not allowed to post here` |
| Unwritable state file | Alerting continues, persistence fails loudly | `cannot write state file` |
| Corrupt state file | Moved aside as `state.json.corrupt`, bot starts fresh | log `state file unreadable` |

Because the cursor only advances past delivered alerts, a long Telegram outage
causes a burst of alerts once it recovers rather than silent data loss.

## Operating the process

- **Signals**: `SIGINT`/`SIGTERM` stop the bot cleanly – monitors are cancelled,
  state is flushed, and the Telegram application is shut down.
- **Restart safety**: `MONITOR_RESUME_ON_START=true` (default) restarts every
  watch that was running when the process exited.
- **State file**: `var/state.json` by default. It is a cache-like artifact (it
  can be deleted, at the cost of re-alerting recent history) and it is written
  atomically, so it should never be corrupt because of a crash.
- **Multiple chats**: each chat has independent state and its own monitor, so
  several groups can watch different wallets from one process.

## Security notes

- Secrets live only in the environment (or `.env`, which is git-ignored). They
  are validated at start-up, stored in `SecretStr`, and excluded from logs by the
  redaction filter, which also masks `api-key=` query parameters and bot tokens
  inside tracebacks.
- **Fail-closed access control**: `/set_wallet`, `/start_monitoring` and
  `/stop_monitoring` require the caller to be listed in
  `TELEGRAM_ADMIN_USER_IDS`. While that list is empty, *nobody* can run them – the
  bot replies with a `/whoami` hint. Set `TELEGRAM_ALLOWED_CHAT_IDS` as well if
  the bot should ignore other chats entirely.
- Every externally supplied value is validated (base58 addresses, whitelisted
  URLs) and every externally sourced string is HTML-escaped before it is sent.
- Alerts are delivered as HTML parse mode with link previews disabled, and the
  bot never constructs a URL from an unvalidated mint.
- Per-user command throttling (`TELEGRAM_COMMANDS_PER_MINUTE`, default 12) keeps
  an abusive or broken client from turning the bot into an API-credit cannon.
- The container image runs as a non-root user and takes secrets only through the
  environment.

## Upgrading

```bash
git pull
pip install -e ".[dev]"     # or rebuild the image
solana-monitor --check-config
```

The state file is versioned. If a future version cannot read it, the file is
moved aside and the bot starts with a fresh baseline rather than refusing to
start.

## Monitoring the bot itself

The bot reports on itself through `/status`: running/stopped, API health, poll
count, transactions scanned, alerts sent, baseline size, last poll, last alert and
the last error. For machine-readable output, run with
`LOG_FORMAT=json` and ship stderr to your log collector; every record carries the
`chat_id`, `wallet` and counters as structured fields.
