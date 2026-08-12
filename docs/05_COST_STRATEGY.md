# Cost Strategy and Hard Budget

## 1. Policy

Default monthly hard limit: **100 THB**. The system must refuse a potentially billable request before dispatch when the conservative projected monthly total would exceed that limit. Cost checks are application-level safety controls, not a replacement for provider billing limits.

M4 implements this boundary locally with integer micro-THB accounting and a
SQLite ledger at `data/cost/ledger.sqlite3`. The default budget timezone is
`Asia/Bangkok`; all pricing and FX inputs are explicit local snapshots. M5 adds
one dated Gemini production entry but still never fetches pricing or exchange
rates from the network.

The application tracks two useful numbers:

- **Cash estimate:** best estimate of what the provider will actually charge under the configured tier.
- **Shadow/list cost:** what the same request would cost at configured list rates even if a free tier makes the cash estimate zero.

Budget enforcement should default to the greater conservative amount unless the user explicitly configures verified free-tier accounting. This prevents “free” experiments from becoming unexpected spend when quotas or tier state changes.

## 2. Price catalog

Pricing is data, not code. Commit a versioned catalog containing provider/model/tier rate entries, effective/retrieved dates, modality rates, output/thinking rates, cache rates, and an official source URL. At runtime:

- freeze the resolved price entry into the reservation;
- reject unknown model/tier combinations;
- fail closed if `hard_limit=true` and price data is older than `pricing_max_age_days`;
- require an explicit catalog update when a provider changes pricing;
- never scrape live exchange rates automatically in V1.

As verified 2026-08-13 (Asia/Bangkok), Google's official pricing page lists the
stable `gemini-3.5-flash-lite` Standard tier at USD 0.30/M input tokens for
text/image/video/audio and USD 2.50/M output tokens including thinking. M5 stores
that dated source URL and exact model in a versioned local catalog; it does not
enable Batch pricing or silently fall back to another model: [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) and [model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite).

## 3. Conservative preflight estimate

Before upload/generation, calculate estimated usage from:

- video duration and chosen media-resolution token rate;
- audio modality if separately sent;
- prompt, schema, transcript, and metadata tokens;
- configured maximum output tokens, not optimistic average output;
- cached-token behavior only when a confirmed provider cache applies;
- request fees and storage/cache charges when relevant.

Prefer a provider token-count endpoint after upload when available, but do not rely on it as the only preflight mechanism. Use the greater of provider count and local estimate. Apply `estimate_safety_factor` (initial proposal 1.20) and round up to a small THB unit.

Current official Gemini guidance estimates roughly 66 vision tokens/second plus
32 audio tokens/second for low media resolution (about 100 combined tokens per
second). M5 uses those rates for conservative preflight, plus bounded prompt,
schema, visible-output, and thinking ceilings. A 45-minute low-resolution window
would be roughly 178,200 vision + 86,400 audio tokens before prompt/output; a
four-hour VOD must wait for M6 windowing: [Gemini video understanding](https://ai.google.dev/gemini-api/docs/video-understanding).

Illustrative math only, using the M5 rates and 36 THB/USD:

```text
45 min * 60 sec * (66 vision + 32 audio tokens/sec) = 264,600 input tokens
input: 0.2646M * $0.30 = $0.07938
reserved output: 4,000 / 1M * $2.50 = $0.010
subtotal: $0.08938
20% safety: $0.107256
THB at 36/USD: about 3.87 THB per window
```

This example is deliberately conservative and does not promise an actual bill. Batch pricing may lower cost but adds asynchronous operational complexity; benchmark it after synchronous correctness.

## 4. Transactional reservation algorithm

Use SQLite in WAL mode with a short `BEGIN IMMEDIATE` transaction:

1. Determine the Bangkok billing month from request timestamp.
2. Load committed spend plus non-expired reservations for that month.
3. Compute conservative projected THB from an immutable rate/FX snapshot.
4. If `committed + reserved + projected > hard_limit`, insert a `BLOCKED` audit event and refuse dispatch.
5. Otherwise insert a `RESERVED` event and commit the transaction.
6. Dispatch the provider request with a request fingerprint/idempotency key when supported.
7. On definitive response, derive actual/best-known cost from usage metadata and convert reservation to `COMMITTED`.
8. On confirmed no-dispatch or no-charge failure, mark it `RELEASED`.
9. On ambiguous timeout after dispatch, mark `RECONCILE_REQUIRED`; keep the conservative amount counted until manually/provider-reconciled.

Usage metadata is an untrusted input: every modality count is bounded before
arithmetic. A catalog with no output rate cannot quote non-zero output usage.
If settlement proves an actual-cost overage, persist the actual amount and open
a global safety hold. Every new reservation fails closed while that hold is
active; only an explicit owner acknowledgement or reconciliation clears it.

Reservations need an expiry only for requests proven not to have dispatched. They must never disappear merely because the local process crashed.

## 5. Usage and cost accounting

Log, at minimum:

- provider, concrete model, tier, stage, session, window/batch;
- request fingerprint and provider request ID;
- estimated tokens by modality and maximum output;
- actual input, cached, output, and thinking tokens if returned;
- list-rate snapshot, FX snapshot, safety factor;
- estimated THB, actual/best-known THB, and status;
- timestamps and any free-tier assumption.

`highlight cost` should show:

```text
Month: 2026-08 (Asia/Bangkok)
Hard budget: 100.00 THB
Committed/best-known: 38.40 THB
Active/uncertain reservations: 4.10 THB
Available to reserve: 57.50 THB
Shadow/list spend: 42.50 THB
By stage/provider/model: ...
```

Also write a per-session `cost.json` derived from the ledger for portable reporting. SQLite remains the authoritative budget total.

## 6. Cost reduction order

When projected cost is too high, offer choices without silently reducing quality:

1. Reuse valid local/provider caches.
2. Disable Reviewer.
3. Use low media resolution and a cheaper supported Scout alias.
4. Tighten local activity filtering only after recall benchmarks prove it safe.
5. Use asynchronous batch pricing after reliability is implemented.
6. Reduce Scout scope/window count with an explicit recall warning.
7. Stop before the call.

Never silently omit windows to fit the budget and then label the session fully analyzed.

## 7. Provider file and privacy cost lifecycle

Gemini's Files API currently documents automatic deletion after 48 hours and no File API storage fee, but both facts are external and subject to change: [Gemini Files API](https://ai.google.dev/gemini-api/docs/files). Record remote expiry and explicitly delete when practical. Upload reuse may save time but not justify keeping personal footage remotely longer than necessary.

## 8. Benchmarking

Create a small annotated evaluation set and record per model/config:

- cost per source hour;
- useful-candidate precision/recall proxy;
- match-boundary error;
- duplicates and malformed-response rate;
- latency and retry rate;
- cost per useful candidate and, later, published clip.

Do not choose the cheapest model solely on token price. A model that floods the user with weak candidates increases review cost and may miss the product goal.
