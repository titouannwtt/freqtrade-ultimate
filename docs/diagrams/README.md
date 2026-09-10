# Architecture diagrams

Interactive, self-contained HTML diagrams (pan/zoom, light/dark theme, search,
relationship tracing) of how Freqtrade Ultimate's multi-bot infrastructure fits
together. Download an `.html` file and open it in a browser — no server needed.

| Diagram | Type | Covers |
|---|---|---|
| [`architecture-overview.html`](architecture-overview.html) | System architecture | The whole fleet: bot process, `ftcache`/`ftpairlists` daemons, shared-wallet guards, exchange, DB, RPC/Telegram/REST API, FreqUI Ultimate |
| [`ftcache-sequence.html`](ftcache-sequence.html) | Sequence | One candle fetch through `ftcache`: cache check, token-bucket rate limiting, ccxt call, fallback behavior — see [FEATURES.md §1.1](../FEATURES.md#11-ohlcv-cache-daemon-ftcache) |
| [`bot-loop.html`](bot-loop.html) | Workflow | `FreqtradeBot.process()`: entries → order management → exits, with the shared-wallet guard, external-close and liquidation-detection branches — see [FEATURES.md §3](../FEATURES.md#3-hyperliquid-specific) |

Each `.html` ships next to the `.json` specification it was rendered from
(architecture/sequence/workflow [JSON IR](https://github.com/tt-a1i/archify)).
To regenerate after a source change, edit the `.json` and re-render with
[archify](https://github.com/tt-a1i/archify):

```bash
node bin/archify.mjs deliver architecture architecture-overview.architecture.json architecture-overview.html --quality showcase
node bin/archify.mjs deliver sequence ftcache-sequence.sequence.json ftcache-sequence.html --quality showcase
node bin/archify.mjs deliver workflow bot-loop.workflow.json bot-loop.html --quality showcase
```

These diagrams describe fork-specific infrastructure (multi-bot caching,
Hyperliquid safety mechanisms). They are not part of upstream Freqtrade.
