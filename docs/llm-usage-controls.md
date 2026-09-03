# LLM Usage Controls — design (roadmap Step 8e)

> Architecture document. Defines the DB-backed usage ledger and quota contract that
> must exist **before** cloud LLM generation is opened beyond the private developer
> allowlist. **No code, schema or OpenAPI change ships with this document** — Step 8e
> is a "split before coding" step, and this is the split.

Companion documents: `docs/specs/llm-provider-toggle-security-contract.md` (Step 8a
boundary) and `docs/specs/llm-openrouter-replacement-decision.md` (Step 8c/8d) in the
workspace repo; `api/docs/domain-boundaries.md` for the schema placement rule.

## 1. Why this exists

The provider account's quota belongs to the **deployment**, not to a user. Today the
only protections are:

- authentication on `POST /api/v1/llm/generate`;
- `LLM_ALLOWED_EMAILS`, a developer allowlist (Step 8d-1);
- an **in-process** sliding-window rate limiter, 20 requests/min/user
  (`services/rate_limiter.py`).

None of them bound total consumption:

- the rate limiter is **per process and per minute**. It caps how fast one account
  spends the quota, not how much it spends in a day, and it resets on restart. With
  more than one worker process it is not even a single limit.
- the allowlist bounds *who*, not *how much*. It is a stopgap precisely because
  there is no accounting.

**One user generation costs two provider calls** (guard model + generator model), so
any budget expressed in provider calls is roughly half the number of generations a
user perceives. This ratio must be explicit everywhere, or every cap will be wrong by
2×.

## 2. Scope

In scope: per-user and global accounting, reservation before the provider call,
quota errors, and a usage view.

Out of scope: billing, payment, invoicing, and price negotiation. The design must
*allow* a paid tier later (§7) without changing the provider adapter boundary, but
Step 8e does not implement one.

## 3. Placement

All tables live in the **`users` schema**. Usage is attributed to an account, which
is the `users` domain; `notebooks` is deliberately kept free of relational coupling
so it can move to a different store (`domain-boundaries.md` §4). Cross-domain foreign
keys stay forbidden — nothing here references `notebooks`.

## 4. Data model

### 4.1 `users.llm_usage_event` — the ledger

One row per **provider call**, not per user request. A generation writes two rows
(guard, generator); a repair retry writes another.

| column | type | note |
|---|---|---|
| `id` | `uuid` PK | |
| `user_id` | `uuid` FK → `users.users(id)` | attribution |
| `request_id` | `uuid` | groups the calls of one `/llm/generate` request |
| `reservation_id` | `uuid` | links to the reservation that authorised the call (§5) |
| `call_kind` | `text` | `guard` \| `generator` \| `repair` |
| `provider` | `text` | `openrouter` \| `bedrock` — the adapter that served it |
| `model_id` | `text` | the model the provider *actually served* |
| `status` | `text` | `ok` \| `provider_error` \| `timeout` \| `unknown` |
| `prompt_tokens` / `completion_tokens` | `integer` | 0 when the provider omits usage |
| `estimated_cost_micros` | `bigint` | see §6; an estimate, never a billing figure |
| `created_at` | `timestamptz` | |

`unknown` is written **only** by reconciliation (§5.3), for a call that was authorised
and whose outcome cannot be established because the worker died. It exists in this
enum because the reconciliation job needs a status to write; the first draft told the
job to write one that the schema did not define.

Indexes: `(user_id, created_at)` for the usage view, `(request_id)` for tracing.

The ledger is **append-only**. It is evidence, not state: quota decisions read the
counters in §4.2, never `SUM()` over this table. A sum over an unbounded table is
both slow and racy.

### 4.2 `users.llm_usage_reservation` — the durable pre-call record

**Added after review.** The first draft referenced a `reservation_id` from the ledger
but never defined the row it points at, and asked reconciliation to find "reservations
older than a threshold with no ledger row". That is not executable: the counter is an
aggregate and keeps neither reservation ids nor per-reservation timestamps. Without
this table the reconciliation job in §5.3 cannot be written at all.

| column | type | note |
|---|---|---|
| `id` | `uuid` PK | the `reservation_id` the ledger references |
| `user_id` | `uuid` FK → `users.users(id)` | |
| `request_id` | `uuid` | one `/llm/generate` request |
| `call_cost` | `integer` | provider calls **authorised** (2 for a generation, 1 for a repair) |
| `calls_used` | `integer` | provider calls actually **issued** so far, incremented as each one is made |
| `cost_reserved_micros` | `bigint` | conservative upper bound, see §6 |
| `state` | `text` | `reserved` \| `settled` \| `released` \| `abandoned` |
| `created_at` / `settled_at` | `timestamptz` | `settled_at` NULL while open |

**`calls_used` is what makes recovery possible.** A reservation authorises two calls
(guard, generator), but after a crash the aggregate alone cannot say whether one, both
or neither was issued — so the promise of "a ledger row per provider call" could not be
kept. Each call increments `calls_used` in the same short transaction that appends its
own ledger row, so `call_cost - calls_used` is exactly the number of authorised calls
with no recorded outcome. Reconciliation writes that many `status = 'unknown'` rows
(§5.3) — usually zero or one, never a guess.

Index on `(state, created_at)` — the reconciliation job's only query.

**Settlement is idempotent**: it transitions `reserved → settled` with a conditional
`UPDATE ... WHERE state = 'reserved'`, and writes the ledger rows in the same
transaction. A retried settle affects zero rows and writes nothing, so a duplicate
worker or a retried job cannot double-count.

### 4.3 `users.llm_usage_counter` — the enforcement state

| column | type | note |
|---|---|---|
| `scope` | `text` | `user` \| `global` |
| `scope_key` | `text` | the user id, or `'-'` for global |
| `window_kind` | `text` | `day` \| `month` |
| `window_start` | `date` | UTC window boundary |
| `calls_reserved` | `integer` | incremented **before** the provider call |
| `calls_settled` | `integer` | incremented after it returns |
| `cost_reserved_micros` | `bigint` | conservative upper bound, reserved before the call (§6) |
| `cost_micros` | `bigint` | settled estimate (§6) |

Primary key `(scope, scope_key, window_kind, window_start)`.

`calls_reserved` is the number enforcement compares against. `calls_settled` exists
only for reconciliation and reporting — a gap between the two is the count of calls
that were authorised and never settled (§5.3).

### 4.4 `users.llm_entitlement` — per-user overrides

| column | type | note |
|---|---|---|
| `user_id` | `uuid` PK FK → `users.users(id)` | |
| `tier` | `text` | `free` \| `developer` \| `paid` |
| `daily_call_limit` / `monthly_call_limit` | `integer` NULL | NULL = use the tier default from config |
| `valid_until` | `timestamptz` NULL | NULL = no expiry |

Defaults per tier come from configuration, not from rows, so a deployment can change
free-tier limits without a migration. A row exists only when a user needs something
other than their tier default.

**This table is the seam that lets a paid plan arrive later** without touching the
provider adapter: billing writes entitlements; the adapter never learns that tiers
exist.

## 5. Reservation protocol (the core requirement)

The rule is **reserve, then call** — never call-then-count, and never
read-then-write. A read followed by a write is two statements with a race between
them; under concurrency both requests read `n < cap` and both proceed.

### 5.1 Reserve

One statement per scope, inside the request transaction:

```sql
INSERT INTO users.llm_usage_counter AS c
       (scope, scope_key, window_kind, window_start,
        calls_reserved, cost_reserved_micros)
SELECT :scope, :key, :window_kind, :window_start, :cost, :cost_micros
 WHERE :cost <= :call_limit                 -- guards the INSERT path
   AND :cost_micros <= :cost_limit_micros
ON CONFLICT (scope, scope_key, window_kind, window_start) DO UPDATE
   SET calls_reserved       = c.calls_reserved + :cost,
       cost_reserved_micros = c.cost_reserved_micros + :cost_micros
 WHERE c.calls_reserved + :cost <= :call_limit          -- guards the UPDATE path
   -- BOTH cost columns: `cost_micros` is money already spent and settled, and
   -- omitting it made the ceiling reset itself on every settlement (see below).
   AND c.cost_micros + c.cost_reserved_micros + :cost_micros <= :cost_limit_micros
RETURNING calls_reserved;
```

No row returned ⇒ a cap would be exceeded ⇒ reject with `429 llm_quota_exceeded`
(§8). The check and the increment are the same statement, so there is no window to
race in.

**The cost check must span both columns.** At settlement a reservation's upper bound
moves out of `cost_reserved_micros` and the actual estimate lands in `cost_micros`
(§6). A check that reads only the reserved column therefore forgets every settled
request: after each settlement the window looks empty again, and the "monthly cost
ceiling" would never bind. Summing `cost_micros + cost_reserved_micros + new bound`
is what makes it a ceiling on the window rather than on whatever happens to be
in flight.

**Both paths must be guarded — this was a hole in the first draft.** A plain
`INSERT ... VALUES` with the condition only on `DO UPDATE` inserts the first row of a
window *unconditionally*: with `limit = 1` and `cost = 2`, the very first request of
the day would be admitted, and only the second would be refused. The `INSERT ...
SELECT ... WHERE` form makes the insert conditional too. **The test that proves this
must start from an empty table**, because a test that pre-seeds the counter row never
exercises the insert path and would have passed against the broken version.

**`:cost` is 2, not 1**, for a normal generation: the pipeline will make a guard call
and a generator call. Reserving 1 and discovering the shortfall halfway through would
spend a provider call and then fail the user — the worst of both. Repair retries are
reserved individually, before each retry, and a refused reservation ends the repair
loop rather than failing the request (the unrepaired result is returned as it would
be on exhausted retries).

A reservation touches up to **four** counter rows — two scopes × two windows — so
"global before user" is not a total order and does not prevent deadlock on its own.
The full order is:

```
global/day → global/month → user/day → user/month
```

Every transaction acquires them in exactly that sequence, so two concurrent requests
can never hold one row while waiting for another the other already holds. If a later
row's check fails, the earlier reservations are released in the same transaction —
they were never committed, so this is a rollback, not compensation.

### 5.2 Settle

After the provider call returns, in one transaction: append the ledger row, increment
`calls_settled`, and **move** cost between the two columns — subtract this
reservation's `cost_reserved_micros` and add the settled `estimated_cost_micros`. The
window total (`cost_micros + cost_reserved_micros`) therefore falls from the
pessimistic bound to the real estimate instead of double-counting the request.

Settlement never re-checks the cap: the authorisation already happened, and re-checking
could refuse to record spending that has already occurred — the one thing the ledger
must never do.

### 5.3 Release, and what happens on a crash

A reservation is released only when the call **provably did not reach the provider**
(a configuration error, or a refusal by a later scope in §5.1). A timeout is **not**
released: the request may well have been served and billed.

If the process dies between reserve and settle, the reservation stands. That is the
fail-closed direction: the deployment slightly over-counts rather than over-spends.

The reconciliation job is now expressible, because §4.2 gives it a row to find: select
`llm_usage_reservation` where `state = 'reserved'` and `created_at` is older than the
threshold, and transition each to `abandoned` while appending exactly
`call_cost - calls_used` ledger rows with `status = 'unknown'` — the calls that were
authorised but whose outcome was never recorded. It must never simply decrement the counter, or a crash becomes a
way to mint quota. Because settlement is idempotent (§4.2), a reservation that settles
late cannot be counted twice.

### 5.4 Transaction and session boundaries

**Added after review; this is a correctness requirement, not a style note.** The
current pipeline makes the obvious implementation wrong:

- `POST /llm/generate` submits the whole guard → generate → validate → repair
  pipeline to a `ThreadPoolExecutor`, and the controller documents that "the in-flight
  worker keeps running until the provider returns" after the HTTP response has
  already been sent on timeout (`controllers/llm_controller.py`);
- `get_db` is a **request-scoped** session whose transaction commits when the *route*
  returns and closes right after (`core/db.py`).

So a reservation written through the request session may be committed — or rolled
back, or its session closed — at a moment that has nothing to do with the provider
call it was supposed to authorise. On the timeout path the route returns 504 while
the worker is still running; anything it then tries to write goes through a session
that is already gone.

The contract is therefore:

1. **Two short transactions, never one long one:**
   `reserve → COMMIT` → *provider call* → `settle → COMMIT`.
2. **No transaction is held open across the provider call.** It can take up to 30s;
   holding a row-locking transaction that long across the whole user base is its own
   outage.
3. **The worker owns its own session**, created from the sessionmaker inside the
   pipeline thread and closed by it. The request-scoped `get_db` session must not be
   passed into the executor — it belongs to a request that may already have returned.
4. The reservation must be **committed before the provider call is made**. A
   reservation that is still uncommitted has authorised nothing.

This also makes the crash semantics in §5.3 real: because the reservation is
committed on its own, it survives the worker dying mid-call, which is exactly the
state reconciliation is written to find.

### 5.5 Database failures deny

If the reservation statement itself fails, the request is **denied**, not allowed
through. The whole purpose is protecting a shared, exhaustible resource; degrading
open would remove the control exactly when the system is unhealthy.

## 6. Cost: reserved as an upper bound, settled as an estimate

**Corrected after review.** The first draft recorded cost only *after* the provider
answered and let an unknown model contribute **0**. That does not produce a ceiling
at all: nothing is reserved before the call, so concurrent requests can all pass the
check and collectively blow past the limit, and an unmapped model spends real money
while counting as free. A limit that can be exceeded is telemetry wearing a limit's
clothes.

Two different numbers, and the distinction matters:

- **`cost_reserved_micros` — a conservative upper bound, reserved BEFORE the call**
  and enforced by the same atomic statement as the call count (§5.1). It is computed
  from **both** token directions at the most expensive price the request could
  incur — providers bill input as well as output:

  ```
  bound = (LLM_MAX_TOTAL_BYTES  as tokens) * prompt_price_micros_per_1k / 1000
        + (llm_max_tokens)               * completion_price_micros_per_1k / 1000
  ```

  The prompt side uses the pipeline's own byte cap (`LLM_MAX_TOTAL_BYTES`, converted
  with a deliberately pessimistic bytes-per-token ratio), because that is the most
  the request is allowed to send. Prices come from the configured map for a pinned
  model, or from a configured `LLM_WORST_CASE_PRICE_MICROS` when the model is chosen
  by the router or missing from the map. An unknown model must cost the *worst*
  assumed price, never 0.
- **`estimated_cost_micros` — the settled estimate**, computed from the actual token
  usage the provider reported. This is the number the usage view shows.

At settle time the reservation's upper bound is released and replaced by the settled
estimate, so the window's reserved total converges downward rather than drifting up.

Neither number is a billing figure and neither may be presented as money owed:
provider pricing changes without a deployment, and the free router picks the model
per request. They are good enough to stop spending and to show a user roughly what
they have used — nothing more.

## 7. Limits

| Limit | Scope | Default source |
|---|---|---|
| daily provider calls | user | tier default, overridable per user |
| monthly provider calls | user | tier default, overridable per user |
| daily provider calls | global | deployment config |
| monthly cost ceiling | global | deployment config — enforced on `cost_reserved_micros` (§6), so it is a real bound rather than a post-hoc observation |

Developers on the Step 8d-1 allowlist map to `tier = 'developer'` with higher limits;
the allowlist stays the gate for *access*, entitlements become the gate for *volume*.

Windows are UTC calendar day/month. A rolling window would need per-event scans; a
calendar window is a single row and is what a user-facing "resets at midnight UTC"
message can honestly describe.

## 8. Error contract

A new code, distinct from the existing limiter:

| condition | status | code | `Retry-After` |
|---|---|---|---|
| per-minute burst (existing) | 429 | `rate_limited` | seconds to window slide |
| quota exhausted (new) | 429 | `llm_quota_exceeded` | seconds to the next UTC window boundary |

They must not share a code. "Wait a minute" and "you are done until tomorrow" are
different instructions, and the UI already keys off `error.code`. The response says
which window was exhausted (`day` / `month`) and whether it was the user's or the
deployment's — the latter without disclosing global numbers.

When implemented this is an OpenAPI change and, per `AGENTS.md` §7, a matching
`ui/openapi/llm.openapi.yaml` update.

## 9. Usage view

- `GET /api/v1/llm/usage` — the caller's own current windows: reserved/settled calls,
  limits, window reset times. Any authenticated user.
- Admin/debug view: same data across users, restricted to the developer allowlist.
  Not a public endpoint and not part of the UI's normal flow.

## 10. Implementation split (each a separate PR)

- **8e-1** — Liquibase changesets for the **four** tables (ledger, reservation,
  counter, entitlement — `users` schema, per `domain-boundaries.md`), plus repository
  and settings surface. No behaviour change.
- **8e-2** — the reservation/settlement service, wired into the generation pipeline,
  with the `llm_quota_exceeded` error and its OpenAPI + ui contract sync.

  Required tests, each against a **real database** — a mocked counter cannot
  demonstrate any of these:
  1. two simultaneous requests at cap-1 ⇒ exactly one succeeds;
  2. **the first request of a window is refused when its cost exceeds the limit**
     (starting from an EMPTY table, so the `INSERT` path is exercised — a test that
     pre-seeds the counter row would pass against the broken version);
  3. settlement is idempotent — settling twice counts once;
  4. a reservation is **committed before** the provider call, and survives the worker
     being killed mid-call;
  5. the cost ceiling refuses a request whose *reserved upper bound* would exceed it,
     including when the served model is unknown to the price map;
  6. **the ceiling still binds after settlement** — settle a request, then assert the
     next one is refused because `cost_micros` counts toward the limit. A test that
     only ever reserves would pass against a check that reads the reserved column
     alone;
  7. after a crash with one of two authorised calls issued, reconciliation writes
     **exactly one** `unknown` ledger row, not two and not zero.
- **8e-3** — the usage view endpoints, and the reconciliation job that transitions
  stale `reserved` rows to `abandoned` with an `unknown` ledger entry.

Cloud LLM stays allowlist-only until 8e-2 is deployed.

## 11. Open questions

- **Reconciliation threshold** for stale reservations: too short and a slow provider
  call is double-counted, too long and quota is held hostage. Needs the real p99 of a
  generation once OpenRouter usage data exists.
- **Multi-process counters.** The design is correct for any number of processes
  because enforcement is a single SQL statement, but it adds a DB round-trip to every
  generation. Acceptable at current volume; revisit only with evidence.
- **Whether `repair` retries should count against the user's quota at all.** They are
  caused by a model returning invalid code, not by the user asking for more. Counting
  them is the conservative default chosen here; the alternative is a per-request
  repair allowance outside the quota.
- **Free-tier defaults** cannot be fixed until Step 8c's payment check completes: the
  provider's own 50/day (unpaid) or 1000/day (after a ≥ $10 purchase) bounds anything
  this project can promise.
