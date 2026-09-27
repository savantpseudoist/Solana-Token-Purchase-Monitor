# Architecture

This document describes the system as it is implemented. Where an earlier design
was replaced, the reason is recorded so the choice is not re-litigated blindly.

## Layers

Dependencies point in one direction only; nothing below depends on anything
above it.

```
solana_monitor/
├── domain/          value objects, error hierarchy, address validation
│                    (no I/O, no third-party libraries at all)
├── helius/          transport + wire schemas for the Helius APIs
├── monitoring/      business rules: detection, state, poll loop, ownership
├── telegram/        presentation + framework adapters
├── config.py        typed settings, validated from the environment
├── logging_setup.py structured logging with secret redaction
├── resilience.py    retry/backoff/rate limiting primitives
└── cli.py           process entry point and composition
```

| Layer | Responsibility | Depends on |
| --- | --- | --- |
| `domain` | models, errors, base58 validation | stdlib only |
| `helius` | HTTP client, pydantic wire schemas, metadata cache | config, domain, resilience |
| `monitoring` | detection rules, persisted state, poll loop, monitor ownership | domain, helius (types only), config |
| `telegram` | command logic, rendering, PTB wiring, alert delivery | domain, monitoring, helius (types only) |
| `cli` | argument parsing, logging setup, wiring | everything |

`monitoring` and `telegram` depend on *protocols*, not concrete collaborators, so
either side can be replaced with a double in tests (and, if the system ever grows
a second frontend, with another implementation).

## Data flow of one poll

1. `WalletMonitor.sync_once` asks the source for a page range. If there is no
   cursor yet it primes; otherwise it asks for everything *after* the cursor in
   ascending order.
2. Each transaction goes through `PurchaseDetector.detect`, a pure function of
   `(transaction, wallet)`:
   - failed transactions are ignored;
   - net token deltas come from `tokenBalanceChanges`, falling back to netting
     `tokenTransfers`;
   - positive deltas become acquisitions unless the mint is ignored, looks like
     an NFT, or the policy disables receipts;
   - each acquisition is classified as a `PURCHASE` (SOL or another token left
     the wallet) or a `RECEIPT` (no observable payment).
3. Suppression: a mint is alerted on its first sighting only, unless
   `MONITOR_ALERT_ON_REPEAT` is enabled.
4. Metadata is resolved only when the transaction provided neither symbol nor
   decimals, and only through the bounded TTL cache.
5. Delivery goes through the alert sink, which retries transient Telegram
   failures.
6. State is persisted atomically.

## Decisions and their reasons

**Balance deltas, not transaction types.** Helius types a purchase as `SWAP` only
when it recognises the program. A swap through a new DEX is frequently typed
`TRANSFER` or `UNKNOWN`, so type-based detection misses real buys. Balance deltas
are what actually happened, and they also give the receipt/purchase distinction
for free.

**Polling, not websockets.** The previous websocket code subscribed with
`accountSubscribe`, which delivers account-change notifications rather than
parsed transactions, and it was never wired into the run path. The Enhanced
Transactions endpoint provides exactly the parsed data the bot needs and supports
server-side time windows, so the design is one client and one poll loop.

**Incremental sync with an ascending cursor.** The first poll sets the cursor from
the newest signature; later polls use `after-signature` with `sort-order=asc`.
The cursor advances per transaction, and only past transactions whose alerts were
delivered, so a mid-batch failure cannot skip activity and a Telegram outage
causes a retry rather than a lost purchase.

**State is per chat, bounded and durable.** `WatchStore` keeps one `WatchState`
per chat with FIFO caps on alerted mints and processed signatures, and writes the
file atomically (temp file plus `os.replace`). A corrupt file is quarantined
rather than crashing the bot, which is the right trade-off for a cache-like
artifact.

**Unknown is not zero.** `sol_spent_lamports()` and token decimals return `None`
when Helius did not provide the data, and the UI prints "unknown" instead of a
misleading number.

**Retries are policy, not string matching.** Errors carry `retryable` and
`status`; `resilience.retry_async` decides. `Retry-After` is honoured, backoff is
exponential with jitter, and authentication failures back off hard because
retrying a bad key quickly cannot help.

**One command registry.** `telegram/registry.py` is the single source of truth for
command names, help text, the Telegram command menu and admin requirements, so
those four things cannot drift apart.

## Things that were removed

| Removed | Why |
| --- | --- |
| `config.py` with empty secrets | Configuration is validated environment input, not committed source |
| `BotState` global | Global mutable state made concurrency, testing and per-chat support impossible |
| Websocket monitor path | Never functional (`accountSubscribe` is not parsed transactions) and never called |
| `previously_seen_tokens`, `token_cache`, `known_tokens`, `historical_tokens`, `pending_alerts`, `transaction_queue`, `executor` | Duplicated, overlapping state with no owner and no clear semantics |
| `asyncio` PyPI dependency | A Python 3.4-era backport that breaks modern interpreters; the stdlib has had it since 3.4 |
| `websockets` dependency | No remaining websocket usage |
| `pytz`, `dateutil`, `certifi`, `charset-normalizer`, `idna`, `yarl`, `multidict`, `attrs`, `frozenlist`, `aiosignal`, `async-timeout` | Transitive dependencies of aiohttp/httpx; pinning them at the top level is how you get a broken install |
| `WindowsSelectorEventLoopPolicy` switch | Unnecessary on modern Python and removed in 3.14 |

## Testing strategy

- **Unit** – pure logic: address validation, balance-delta maths, detection rules,
  state serialisation, formatting/escaping, authorisation, backoff maths.
- **Adapter** – the Helius client against a scripted HTTP double (request shape,
  error mapping, pagination, caching) and the sink against a scripted Telegram
  double (retry classification, keyboard construction).
- **Service** – the command service over a real store and manager (authorisation,
  validation, analysis windows, deduplication).
- **Integration** – the full wiring with only HTTP faked: configure, prime, alert,
  deduplicate, restart, resume.

Time, sleep and randomness are injected, so the suite is fast, deterministic and
network-free.

## Extending the system

- **New command**: add a `CommandSpec` to `telegram/registry.py`, implement the
  handler in `CommandService` and register it in `_dispatch`. Help text and the
  Telegram menu update automatically.
- **New detection rule**: add it to `PurchaseDetector` behind a policy flag; no
  other layer changes, because alerting, formatting and persistence all consume
  `TokenAcquisition`.
- **New data source**: implement `TransactionSource.collect_history` and pass it
  to `WalletMonitor`; the monitor and everything above it are unchanged.
