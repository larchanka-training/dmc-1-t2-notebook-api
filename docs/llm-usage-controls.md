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
| `model_id` | `text` NULL | the model the provider *actually served*; NULL only on an `unknown` row |
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

### 4.2 `users.llm_usage_reservation` — one durable row per planned provider call

**Reworked twice.** Draft 1 referenced a reservation that did not exist. Draft 2 added
one row per *request* with a `calls_used` counter — still wrong, because an aggregate
cannot answer the only questions recovery asks: *which* call was this, did it reach
the provider, and what should its ledger row say? The unit of reservation must be the
unit of accounting, and that unit is **one provider call**.

| column | type | note |
|---|---|---|
| `id` | `uuid` PK | the `reservation_id` the ledger references |
| `user_id` | `uuid` FK → `users.users(id)` | |
| `request_id` | `uuid` | groups the calls of one `/llm/generate` request |
| `call_kind` | `text` | `guard` \| `generator` \| `repair` — known when the row is created |
| `provider` | `text` | the adapter that will serve it, captured at reservation time |
| `state` | `text` | `reserved` → `started` → `settled` \| `released` \| `unknown` |
| `cost_reserved_micros` | `bigint` | this call's conservative upper bound (§6) |
| `created_at` | `timestamptz` | |
| `started_at` | `timestamptz` NULL | set when the request is about to be sent |
| `closed_at` | `timestamptz` NULL | set on `settled` / `released` / `unknown` |

Index on `(state, created_at)` — the reconciliation job's only query.

**The states are what make recovery decidable:**

- `reserved` — authorised, **not yet sent**. If the worker dies here the call provably
  never reached the provider, so reconciliation **releases** it and the quota returns.
- `started` — committed *immediately before* the HTTP request goes out. If the worker
  dies here the provider may well have served and billed it, so reconciliation closes
  it as `unknown` and the quota stays spent. Draft 2 could not tell these two apart,
  which is exactly why it could not keep its own promise.
- `settled` — the call returned and its ledger row was written in the same
  transaction.
- `released` — the call is known not to happen. The ordinary case is a **guard
  rejection**: the prompt is refused, so the generator call will never be made and its
  reservation is released within the request. Draft 2 had no way to express this
  partial outcome, so a rejected prompt silently kept a generator call's quota.

`call_kind` **and `provider`** live here, not only on the ledger, so a synthetic
`unknown` row can be filled in without guessing. `provider` in particular cannot be
recovered later: it comes from deployment configuration, which may have changed
between the crash and the reconciliation run, so a reconciler reading today's config
would attribute an old Bedrock call to OpenRouter. It is captured when the reservation
is written.

The served **model** is genuinely unknowable for an `unknown` row — the router picks
it per request and the reply never arrived — so `llm_usage_event.model_id` is
explicitly **nullable**, and NULL means "authorised, outcome unrecorded". A guess
would be worse than a gap.

A normal generation creates **two** rows (guard, generator) in one reserve
transaction; each repair attempt creates one more, reserved before it runs.

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
spend a provider call and then fail the user — the worst of both.

Concretely, one reserve transaction does both things: **one counter update of `+2`**
and **two `llm_usage_reservation` rows** (§4.2), one per planned call, each carrying
its own `cost_reserved_micros` (§6) so `:cost_micros` is their sum. The counter is the
enforcement state; the rows are what recovery and partial release act on. A guard
rejection later releases the generator's row and decrements the counter by 1 — which
is expressible only because the rows are per call. Repair retries are
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

### 5.2 Start and settle

Each call goes through two extra short transactions of its own:

1. **before sending** — `reserved → started`, `started_at = now()`, COMMIT. This is
   the only thing that distinguishes "never sent" from "may have been billed";
2. **after it returns** — `started → settled` plus its ledger row plus the counter
   update, in one transaction: increment `calls_settled`, and **move** cost by
   subtracting this call's `cost_reserved_micros` and adding its settled
   `estimated_cost_micros`. The window total (`cost_micros + cost_reserved_micros`)
   therefore falls from the pessimistic bound to the real estimate instead of
   double-counting.

Both transitions are conditional on the current state (`WHERE state = 'reserved'` /
`WHERE state = 'started'`), so they are **idempotent**: a retried settle affects zero
rows and writes nothing.

Settlement never re-checks the cap. The authorisation already happened, and
re-checking could refuse to record spending that has already occurred — the one thing
the ledger must never do.

### 5.3 Release, and what happens on a crash

A reservation is **released** only when its call provably will not happen. Two cases,
both ordinary:

- **guard rejection** — the guard refuses the prompt, so the generator call will never
  be made; its `reserved` row is released and the counter decremented within the
  request. Without this, every rejected prompt would permanently consume a
  generator's worth of quota;
- a later scope's check fails during reservation (§5.1), which is a rollback of an
  uncommitted transaction rather than a compensation.

A call in `started` is **never** released, including on timeout: the provider may have
served and billed it.

**Ordinary provider failures settle; they do not release.** `openrouter_client` raises
for three real paths — timeout, connection failure, and an HTTP error status — and the
contract must say what each does, or the implementation will invent it:

| outcome | reservation | ledger `status` | counter |
|---|---|---|---|
| success | `started → settled` | `ok` | `calls_settled +1`; cost moved to the settled estimate |
| HTTP error status (4xx/5xx from the provider) | `started → settled` | `provider_error` | `calls_settled +1`; **cost moved to 0** |
| timeout | `started → settled` | `timeout` | `calls_settled +1`; cost moved to the **full reserved bound**, not 0 |
| never sent (config error, guard rejection, refused later scope) | `reserved → released` | none | decremented |

The two failure rows differ on purpose. An HTTP error status is a **reply**: the
provider rejected the request and, for the error classes this adapter maps
(`llm_access_denied`, `llm_internal`, `llm_throttled`, `llm_unavailable`), did not
run a model, so the estimate is 0. A **timeout is not a reply** — the request may have
been served and billed in full — so the conservative bound stands. Releasing on
timeout would let a user mint quota by triggering timeouts.

A connection failure before the socket is established is indistinguishable, from the
host's side, from a request that arrived; it is treated as a timeout. The call is
still `started`, so the conservative direction applies.

Failed calls consume a provider call from the quota. That is deliberate: a retry
storm against a failing provider is exactly what the global cap exists to stop.

**Crash recovery is decidable because the states carry the distinction:**

| state at crash | what it means | reconciliation |
|---|---|---|
| `reserved` | request never sent | close as `released`, decrement the counter |
| `started` | may have been served and billed | close as `unknown`, counter unchanged, append one ledger row with `status = 'unknown'`, `call_kind` from the reservation, `model_id` NULL |

The job selects reservations older than a threshold in either open state. It must
never simply decrement a `started` row, or crashing becomes a way to mint quota; and
it must not leave `reserved` rows charged, or a dead worker permanently taxes the
user. Because every transition is conditional on state, a reservation that settles
late cannot be double-counted.

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

1. **Three short transactions, never one long one:**
   `reserve → COMMIT` → `start → COMMIT` → *provider call* → `settle → COMMIT`.
   The middle one is not optional bookkeeping: `start` is what §5.3 reads to tell
   "never sent" from "may have been billed", and a `start` that is not committed
   before the request goes out records nothing.
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
  and enforced by the same atomic statement as the call count (§5.1). Because §4.2
  reserves **per call**, each row carries the bound for *its own* call and the request
  total is simply their sum — a generation reserves `guard + generator`, not one
  call's worth.

  Per call, both token directions (providers bill input as well as output):

  ```
  bound(call) = input_tokens_max(call)  * prompt_price_micros_per_1k     / 1000
              + output_tokens_max(call) * completion_price_micros_per_1k / 1000
  ```

  `input_tokens_max` is **not** `LLM_MAX_TOTAL_BYTES` alone. That setting bounds the
  HTTP request body; the server then adds its own system prompt, and — for the guard
  — a serialised, truncated copy of the notebook context. The bound must therefore be

  ```
  input_tokens_max(call) = tokens(LLM_MAX_TOTAL_BYTES)
                         + tokens(configured system-prompt allowance for that call)
  ```

  with a deliberately pessimistic bytes-per-token ratio. Draft 2 used the body cap
  only and applied it once, so the "upper bound" could be under the real cost twice
  over: it ignored the server-side prompt, and it priced one call while the pipeline
  makes two.

  `output_tokens_max` is `llm_max_tokens` for the generator and repair calls; the
  guard's own cap is smaller and configured separately, since it returns a one-field
  JSON verdict rather than code.

  **The repair call needs its own input term, and one prerequisite.** A repair prompt
  is not the user's request: it carries the *previously generated code* plus the
  *validation error*. The code side is bounded — it was model output, so at most
  `llm_max_tokens`. The error side is **not bounded today**: `syntax_validator` returns
  esbuild's raw `stderr`/`stdout` verbatim, which is however long esbuild decides to
  make it. So:

  ```
  input_tokens_max(repair) = input_tokens_max(generator)
                           + llm_max_tokens                  # the code being repaired
                           + tokens(LLM_VALIDATION_ERROR_MAX_BYTES)
  ```

  `LLM_VALIDATION_ERROR_MAX_BYTES` does not exist yet. **8e-2 must add it and truncate
  the validator's error text**, because without a cap on that string the repair call
  has no computable upper bound and the ceiling is unenforceable for exactly the path
  most likely to loop. This is a small product change, not just accounting: an
  unbounded compiler error was already being sent to a paid model.

  Prices come from the configured map for a pinned model, or from a configured
  `LLM_WORST_CASE_PRICE_MICROS` when the model is chosen by the router or missing from
  the map. An unknown model must cost the *worst* assumed price, never 0.

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
  with the `llm_quota_exceeded` error and its OpenAPI + ui contract sync. Also adds
  `LLM_VALIDATION_ERROR_MAX_BYTES` and truncates the validator error text (§6): the
  repair call has no computable upper bound until that string is capped.

  Required tests, each against a **real database** — a mocked counter cannot
  demonstrate any of these:

  1. two simultaneous generations, with the counter pre-set to
     **`limit - call_cost`** (not `limit - 1`), ⇒ exactly one succeeds. The unit
     matters: a generation reserves **two** provider calls, so at `limit - 1`
     *neither* request can pass and the test would prove nothing while looking green;
  2. the first request of a window is refused when its cost exceeds the limit,
     starting from an **empty table** so the `INSERT` path is exercised — a test that
     pre-seeds the counter row would pass against an unguarded insert;
  3. settlement is idempotent — settling twice counts once;
  4. reservations are **committed before** the provider call, and survive the worker
     being killed mid-call;
  5. the cost ceiling refuses a request whose reserved upper bound would exceed it,
     including when the served model is unknown to the price map;
  6. the ceiling **still binds after settlement** — settle, then assert the next
     request is refused because `cost_micros` counts toward the limit. A test that
     only ever reserves would pass against a check reading the reserved column alone;
  7. **a guard rejection releases the generator's reservation** — quota returns, and
     a second generation is still possible at `limit - call_cost`;
  8. **crash in `reserved` vs crash in `started` diverge**: the first is reconciled to
     `released` with the counter decremented, the second to `unknown` with the counter
     unchanged and exactly one ledger row written carrying the reservation's
     `call_kind`. One test per state — a single "crash" test cannot show the
     distinction the design turns on.

- **8e-3** — the usage view endpoints, and the reconciliation job that transitions
  stale open reservations per §5.3 — `reserved` → `released` with the counter
  decremented, `started` → `unknown` with the counter unchanged and one ledger row
  appended. (`abandoned` was a state in an earlier draft and no longer exists;
  turning a `reserved` row into `unknown` would charge a user for a call that was
  never sent.)

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
