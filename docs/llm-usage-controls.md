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
| `model_id` | `text` NULL | the model the provider *actually served*; NULL on `unknown` rows and on timeout / transport / provider errors where no served model was returned |
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
counters in §4.3, never `SUM()` over this table. A sum over an unbounded table is
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
- `started` — committed *immediately before* the HTTP request goes out, and **after
  the adapter's preflight has passed** (below). If the worker
  dies here the provider may well have served and billed it, so reconciliation closes
  it as `unknown` and the quota stays spent. Draft 2 could not tell these two apart,
  which is exactly why it could not keep its own promise.
- `settled` — the call returned and its ledger row was written in the same
  transaction.
- `released` — the call is known not to happen. Ordinary cases are **guard
  rejection** (the prompt is refused) and **request abort** (guard preflight failure,
  HTTP/transport/timeout failure, unusable provider response, or invalid guard JSON):
  in all these paths the generator call will never be made, and its pre-reserved row
  is deterministically released within the request lifecycle (§5.3). Draft 2 had no
  way to express partial outcome cleanup, so an aborted or rejected guard silently
  kept a generator call's quota until reconciliation.

`call_kind` **and `provider`** live here, not only on the ledger, so a synthetic
`unknown` row can be filled in without guessing. `provider` in particular cannot be
recovered later: it comes from deployment configuration, which may have changed
between the crash and the reconciliation run, so a reconciler reading today's config
would attribute an old Bedrock call to OpenRouter. It is captured when the reservation
is written.

The served **model** is genuinely unknowable whenever the provider reply never
arrives or fails before returning completion metadata — the router picks the model per
request — so `llm_usage_event.model_id` is explicitly **nullable**. NULL means
"authorised, served model unrecorded or unobserved" (covering both synthetic `unknown`
rows written by reconciliation and settled `timeout` / transport / early provider error
rows where no served model was received). A guess would be worse than a gap.

A normal generation creates **two** rows (guard, generator) in one reserve
transaction; each repair attempt creates one more, reserved before it runs.

**Adapters must expose a `preflight()` that runs before `reserved → started`.**
Today the OpenRouter adapter validates its API key *inside* `converse()`, and Bedrock
raises `LlmProviderNotConfiguredError` from its own client construction. A literal
implementation of this contract would commit `started` first and then hit a
configuration error — at which point §5.3 forbids releasing a `started` row, so a
misconfigured deployment would burn the whole day's quota on requests that never left
the process.

`preflight()` performs only checks that need no network: key present, model id
non-blank, adapter constructible. It raises `LlmProviderNotConfiguredError`, which is
the **one typed outcome that proves inference never began** and is therefore the only
path allowed to release a reservation after it was taken. 8e-2 moves the existing
in-`converse()` key check into it.

### 4.3 `users.llm_usage_counter` — the enforcement state

| column | type | constraints | note |
|---|---|---|---|
| `scope` | `text` | NOT NULL | `user` \| `global` |
| `scope_key` | `text` | NOT NULL | the user id, or `'-'` for global |
| `window_kind` | `text` | NOT NULL | `day` \| `month` |
| `window_start` | `date` | NOT NULL | UTC window boundary |
| `calls_reserved` | `integer` | NOT NULL DEFAULT 0 CHECK (calls_reserved >= 0) | incremented **before** the provider call |
| `calls_settled` | `integer` | NOT NULL DEFAULT 0 CHECK (calls_settled >= 0) | incremented after it returns |
| `cost_reserved_micros` | `bigint` | NOT NULL DEFAULT 0 CHECK (cost_reserved_micros >= 0) | conservative upper bound, reserved before the call (§6) |
| `cost_micros` | `bigint` | NOT NULL DEFAULT 0 CHECK (cost_micros >= 0) | settled estimate (§6) |

Primary key `(scope, scope_key, window_kind, window_start)`.

All four aggregate counter columns are `NOT NULL DEFAULT 0` with non-negative check
constraints. An empty window starts with all counters at zero. The reservation `INSERT`
explicitly populates `calls_settled = 0` and `cost_micros = 0` (matching the DDL
defaults), guaranteeing that arithmetic expressions like
`c.cost_micros + c.cost_reserved_micros + :cost_micros` never evaluate to `NULL` on
subsequent operations.

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

One statement per **counter row** — that is, per `(scope, window_kind)` pair, so four
in total for a normal reservation — inside the request transaction, acquired in the
lock order below:

```sql
INSERT INTO users.llm_usage_counter AS c
       (scope, scope_key, window_kind, window_start,
        calls_reserved, calls_settled, cost_reserved_micros, cost_micros)
SELECT :scope, :key, :window_kind, :window_start, :cost, 0, :cost_micros, 0
 WHERE (:call_limit IS NULL OR :cost <= :call_limit)                 -- guards the INSERT path
   AND (:cost_limit_micros IS NULL OR :cost_micros <= :cost_limit_micros)
ON CONFLICT (scope, scope_key, window_kind, window_start) DO UPDATE
   SET calls_reserved       = c.calls_reserved + :cost,
       cost_reserved_micros = c.cost_reserved_micros + :cost_micros
 WHERE (:call_limit IS NULL OR c.calls_reserved + :cost <= :call_limit)          -- guards the UPDATE path
   -- BOTH cost columns: `cost_micros` is money already spent and settled, and
   -- omitting it made the ceiling reset itself on every settlement (see below).
   AND (:cost_limit_micros IS NULL OR c.cost_micros + c.cost_reserved_micros + :cost_micros <= :cost_limit_micros)
RETURNING calls_reserved;
```

No row returned ⇒ a cap would be exceeded ⇒ reject with `429 llm_quota_exceeded`
(§8). The check and the increment are the same statement, so there is no window to
race in.

**Inactive limit dimensions (`NULL` semantics):** Each counter row represents a
specific `(scope, window_kind)` pair. As defined in §7, different scopes and windows
enforce different dimensions (e.g. user/day and user/month enforce call counts but no
cost ceiling; global/month enforces a cost ceiling but no call count limit). An
unconstrained or inactive dimension is represented explicitly as `NULL`
(`:call_limit = NULL` or `:cost_limit_micros = NULL`). The reservation predicate uses
`(:call_limit IS NULL OR ...)` and `(:cost_limit_micros IS NULL OR ...)`. When a limit
parameter is `NULL`, that dimension is unconstrained and passes unconditionally. This
avoids SQL three-valued logic failures (where `x <= NULL` yields `unknown` and rejects
valid calls) without relying on undocumented magic sentinel values.

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
reserved individually, before each retry. If a repair reservation is refused due to
quota exhaustion, the repair loop halts immediately and raises `CodeValidationError`
("Generated code did not pass syntax validation") — preserving the exact existing
behavior of `generation_service.py` when repair attempts cannot proceed. An
unvalidated result must never be returned to the client as validated code.

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

- **downstream cleanup on guard rejection or request abort** — if the guard refuses the
  prompt or exits the pipeline due to preflight failure, transport/HTTP failure, timeout,
  unusable provider output, or invalid guard JSON, the generator call will never be
  made. The pipeline MUST immediately release all downstream reservations for that
  `request_id` that are still in `reserved` state using the atomic release protocol
  below, returning their call and cost capacity immediately. Without this, every guard
  failure would lock up a generator call's quota until background reconciliation;
- a later scope's check fails during reservation (§5.1), which is a rollback of an
  uncommitted transaction rather than a compensation.

A call in `started` is **never** released, including on timeout: the provider may have
served and billed it.

**Ordinary provider failures settle; they do not release.** The adapter raises on
several real paths, and the contract must say what each does or the implementation
will invent it. The governing rule is one sentence:

> **A `started` call keeps its full reserved bound unless a typed outcome proves
> inference never began.**

Cost `0` is not the default for failure — it is a claim, and it needs evidence.

| outcome | reservation | ledger `status` | cost effect |
|---|---|---|---|
| success with complete, valid usage data | `started → settled` | `ok` | replaced by the settled estimate |
| success with missing or partial usage data | `started → settled` | `ok` | **full bound kept** |
| success, but the body is unusable (invalid JSON, no `choices`, empty text) | `started → settled` | `provider_error` | **full bound kept** |
| HTTP error status | `started → settled` | `provider_error` | **full bound kept** |
| timeout / connection failure | `started → settled` | `timeout` | **full bound kept** |
| refused by preflight, before `started` | `reserved → released` | none | bound returned in full |

**Successful responses without complete usage keep their full bound.** The event
schema permits missing provider usage by storing zero token counts (§4.1), and both
adapters normalize missing usage to zero (`openrouter_client.py:265-273`,
`bedrock_client.py:183-188`). If a provider returns 200 OK with valid text but omits
or truncates usage metadata, replacing the reserved bound with a zero estimate would
settle a real inference for free and reopen the cost ceiling. The governing rule
applies: replace the bound only when all token usage fields required by the configured
pricing formula are present and positive; otherwise settle `ok` while retaining the
full reserved bound (`estimated_cost_micros = cost_reserved_micros`).

**Corrected after review — the previous version was unsafe.** It treated any HTTP
4xx/5xx as proof that no model ran and zeroed the cost. That is not sound: OpenRouter
distinguishes errors raised *before* a provider is tried from `5xx` returned *after*
one or more provider attempts, and the status code alone does not tell them apart.
A model can therefore have run and been billed behind a `5xx`. Zeroing there would
let a failing provider quietly drain the budget while the ceiling reported room.

The same reasoning covers a case the earlier matrix omitted entirely: a **200 with an
unusable body**. `_parse_completion_response` raises on invalid JSON, missing
`choices`, or empty text — the request unquestionably reached the model, so it is a
settled `provider_error` that keeps its bound, not a release.

Refining any of these later requires *evidence*, not a status code: if the provider
exposes trustworthy per-attempt usage metadata, a future revision may zero the cost
for outcomes that metadata proves never reached a model. Until then the conservative
value stands, because the ceiling is the thing being protected.

Failed calls consume a provider call from the quota. That is deliberate: a retry
storm against a failing provider is exactly what the global cap exists to stop.

**Crash recovery is decidable because the states carry the distinction:**

| state at crash | what it means | reconciliation |
|---|---|---|
| `reserved` | request never sent | close as `released`; see the release statement below |
| `started` | may have been served and billed | close as `unknown`, counter unchanged, append one ledger row with `status = 'unknown'`, `call_kind` from the reservation, `model_id` NULL |

**Release is atomic and idempotent under concurrency: state transition gates counters.**
Simply putting counter decrements and a conditional reservation update in the same
transaction is not enough under concurrency: if two workers race to release the same
reservation (for example, an in-request guard abort racing against a background
reconciliation job), both could execute counter decrements before the state check runs,
double-decrementing the counters.

The state transition on `users.llm_usage_reservation` must therefore be the atomic gate
that authorizes counter mutation:

1. **Step 1: Atomic state claim.**
   ```sql
   UPDATE users.llm_usage_reservation
      SET state = 'released',
          closed_at = now()
    WHERE id = :id
      AND state = 'reserved'
   RETURNING cost_reserved_micros;
   ```
   If this query updates **0 rows**, the reservation was already closed or transitioned;
   the releaser terminates immediately without modifying any counter.

2. **Step 2: Counter decrements (only if Step 1 returned a row).**
   Using the exact `cost_reserved_micros` returned from Step 1 (not a recomputed
   estimate, which could diverge if configuration changed), the worker decrements the
   four counter rows, acquired in the mandatory canonical lock order:
   `global/day → global/month → user/day → user/month`:

   ```sql
   UPDATE users.llm_usage_counter AS c
      SET calls_reserved       = c.calls_reserved - 1,
          cost_reserved_micros = c.cost_reserved_micros - :returned_bound
    WHERE (c.scope, c.scope_key, c.window_kind, c.window_start) = (...)
   ```

   (In PostgreSQL this can also be expressed as a single CTE where counter mutations
   are joined directly to the `RETURNING` of the claimed reservation).

Because the reservation transition is claimed first with `RETURNING`, concurrent
releasers cannot double-decrement counters, and both `calls_reserved` and
`cost_reserved_micros` are returned accurately on all four rows.

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

  **The repair call is bounded from its actual prompt, not from a proxy.** A repair
  prompt carries the previously generated code and the validation error, and *both are
  already in hand when the repair reservation is taken* — the generation finished and
  the validator ran. So there is no reason to estimate:

  ```
  input_tokens_max(repair) = tokens(utf8_len(built repair prompt)
                                    + system-prompt allowance for repair)
  ```

  The previous draft used `llm_max_tokens` as a stand-in for the prior completion's
  size. That is a **proxy, not a bound**: with a router-selected model the tokenizer is
  not ours, so a token count from one model's cap says nothing reliable about another
  model's billing. Measuring the bytes actually about to be sent removes the guess.

  This is only computable once the validator's error text is capped.
  `LLM_VALIDATION_ERROR_MAX_BYTES` is implemented in Step 8e-2: `syntax_validator` error
  text is truncated before insertion into the repair prompt (with a `" [truncated]"` marker),
  strictly guaranteeing that the total payload never exceeds the configured byte cap.
  Without it the repair prompt has no maximum size, so the ceiling is unenforceable on
  exactly the path most likely to loop.

  Prices come from the configured map for a pinned model, or from configured
  `LLM_WORST_CASE_PRICE_MICROS_PROMPT` and `LLM_WORST_CASE_PRICE_MICROS_COMPLETION`
  when the model is chosen by the router or missing from the map. An unknown model
  must cost the *worst* assumed price, never 0.

- **`estimated_cost_micros` — the settled estimate**, computed from the actual token
  usage the provider reported. This is the number the usage view shows.

At settle time, if and only if the provider returned complete, valid usage data needed
by the configured price formula, the reservation's upper bound is released and replaced
by the settled estimate. If usage data is absent or partial, the settled estimate
retains the full reserved bound (`estimated_cost_micros = cost_reserved_micros`),
ensuring the window's reserved total converges downward when metered, but never
silently clears unmetered spending.

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

### Configuration settings surface (Step 8e-1 & Step 8e-2)

The following deployment settings in `app/core/config.py` configure quota limits, global cost ceilings, conservative upper-bound pricing parameters, and validation error limits.

> [!IMPORTANT]
> **Provisional Operational Defaults**: All proposed tier limits, global ceilings, and price model parameters are provisional operational defaults for development, testing, and capacity modeling. They are **not** evidence of approved or reserved provider capacity.

> [!NOTE]
> **Enforcement Wiring (Step 8e-2)**: Active runtime enforcement wiring into the generation pipeline (`POST /api/v1/llm/generate`) and reservation orchestration is implemented in Step 8e-2, along with `LLM_VALIDATION_ERROR_MAX_BYTES`.

| Environment variable | Type | Default | Units | Validation rules | Description |
|---|---|---|---|---|---|
| `LLM_FREE_TIER_DAILY_CALLS` | int | `20` | provider calls / day | Positive integer | Default daily provider call limit for users on `free` tier (1 generation ~= 2 calls) |
| `LLM_FREE_TIER_MONTHLY_CALLS` | int | `200` | provider calls / month | Positive integer, `>= LLM_FREE_TIER_DAILY_CALLS` | Default monthly provider call limit for users on `free` tier |
| `LLM_DEV_TIER_DAILY_CALLS` | int | `100` | provider calls / day | Positive integer | Default daily provider call limit for users on `developer` tier |
| `LLM_DEV_TIER_MONTHLY_CALLS` | int | `1000` | provider calls / month | Positive integer, `>= LLM_DEV_TIER_DAILY_CALLS` | Default monthly provider call limit for users on `developer` tier |
| `LLM_GLOBAL_DAILY_CALLS` | int | `1000` | provider calls / day | Positive integer | Global daily provider call cap across all users |
| `LLM_GLOBAL_MONTHLY_COST_CEILING_MICROS` | int | `100000000` | micros ($100 = 100,000,000 micros) | Positive integer | Global monthly cost ceiling across all users, enforced against settled + reserved cost |
| `LLM_WORST_CASE_PRICE_MICROS_PROMPT` | int | `5000` | micros per 1,000 tokens ($5 / 1M tokens) | Positive integer | Conservative upper-bound prompt token price for reservation sizing |
| `LLM_WORST_CASE_PRICE_MICROS_COMPLETION` | int | `15000` | micros per 1,000 tokens ($15 / 1M tokens) | Positive integer | Conservative upper-bound completion token price for reservation sizing |
| `LLM_SYSTEM_PROMPT_ALLOWANCE_TOKENS` | int | `1000` | tokens | Positive integer | Token buffer added to input prompt estimation for repair passes |
| `LLM_GUARD_OUTPUT_TOKENS_MAX` | int | `100` | tokens | Positive integer | Maximum output tokens expected from safety guard evaluation pass |
| `LLM_VALIDATION_ERROR_MAX_BYTES` | int | `2048` | bytes | Positive integer | Maximum bytes of syntax validator error text passed to LLM repair prompt (excess truncated with marker) |

## 8. Error contract

A new code, distinct from the existing limiter:

| condition | status | code | `Retry-After` |
|---|---|---|---|
| per-minute burst (existing) | 429 | `rate_limited` | seconds to window slide |
| quota exhausted (new) | 429 | `llm_quota_exceeded` | seconds to the next UTC window boundary |

They must not share a code. "Wait a minute" and "you are done until tomorrow" are
different instructions, and the UI keys off `error.code`.

When quota is exhausted, the endpoint responds with HTTP 429, the standard error
envelope matching `ApiErrorEnvelope`, and the `Retry-After` header indicating seconds
to the next window boundary:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 7200
Content-Type: application/json
```

```json
{
  "error": {
    "code": "llm_quota_exceeded",
    "message": "Daily generation quota exhausted. Resets at midnight UTC.",
    "fields": {}
  }
}
```

The error message indicates whether the daily or monthly limit was reached.
Machine-readable retry timing is communicated via the standard `Retry-After` HTTP
header rather than custom JSON body fields.

In Step 8e-2, this error code is implemented in the backend API router with HTTP 429
status code and `Retry-After` header. The OpenAPI spec describes the 429 response.
Companion UI error handling (distinguishing `rate_limited` from `llm_quota_exceeded`
in user-facing toasts or dialogs, and consuming `ui/openapi/llm.openapi.yaml`) will be
synchronized in the UI submodule as part of the frontend roadmap prior to monorepo
promotion.

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

  Required test suite:

  **A. PostgreSQL concurrency & invariant suite (real database required — a mocked counter cannot demonstrate these):**

  1. **two simultaneous generations**, with the counter pre-set to
     **`limit - call_cost`** (not `limit - 1`), ⇒ exactly one succeeds. The unit
     matters: a generation reserves **two** provider calls, so at `limit - 1`
     *neither* request can pass and the test would prove nothing while looking green;
  2. **first request on empty table and initialized arithmetic**:
     - the first request of a window is refused when its cost exceeds the limit,
       starting from an **empty table** so the conditional `INSERT` path is
       exercised — a test that pre-seeds the counter row would pass against an
       unguarded insert;
     - starting from an empty table, a valid first request is admitted, zero-initializing
       `calls_settled` and `cost_micros`; settling and then reserving a second request
       proves that subsequent arithmetic (`c.cost_micros + c.cost_reserved_micros + :cost_micros`)
       evaluates correctly against initialized non-null values without failing or rejecting;
  3. **settlement is idempotent** — settling twice counts once;
  4. **pre-call reservation persistence**: reservations are **committed before** the
     provider call, and survive the worker being killed mid-call;
  5. **cost ceiling enforcement**: refuses a request whose reserved upper bound would
     exceed the limit, including when the served model is unknown to the price map;
  6. **ceiling binds after settlement**: settle, then assert the next request is
     refused because `cost_micros` counts toward the limit. A test that only ever
     reserves would pass against a check reading the reserved column alone;
  7. **downstream cleanup by lifecycle point**:
     - **guard preflight failure**: neither call reaches `started`; both reservations are
       released, returning all quota and bounds — starting at `limit - call_cost`
       (where `call_cost = 2`), a second complete generation remains possible;
     - **post-start guard outcomes (rejection, timeout, HTTP error, unusable response)**: the
       guard was attempted and consumes 1 call (settles `ok`, `provider_error`, or
       `timeout`), while the downstream generator's reservation is deterministically
       released (`calls_reserved - 1` and its bound returned). Assert that exactly
       1 call remains consumed and 1 returned: at `limit - 2`, the counter sits at
       `limit - 1` so a second two-call generation is refused; seeded at `limit - 3`,
       a second generation succeeds;
  8. **release returns BOTH columns**: reserve, release, then assert
     `cost_reserved_micros` is back to its previous value — not just `calls_reserved`.
     A test that checks only the call count passes while the cost quota leaks away
     permanently;
  9. **failure paths and partial-usage completions keep full bound**: one case each for
     HTTP error status, timeout, connection failure, a `200` whose body is unusable
     (invalid JSON / no `choices` / empty text), and a `200` whose usage data is
     missing or partial: the reservation settles, the ledger records the status, and
     the window's cost does **not** drop; assert `model_id = NULL` on the ledger row
     for timeout and connection failure where no served model was observable;
  10. **concurrent release idempotency**: two simultaneous releasers attempting to
      release the same `reserved` row (e.g. guard abort racing with reconciliation)
      resolve through the atomic gate, resulting in exactly one state change and
      exactly one counter deduction across all four rows;
  11. **inactive limit dimensions across all four scope/window pairs**: executes reservations across
      all four `(scope, window_kind)` rows where only one dimension is configured and the
      other is inactive (`NULL`) (e.g. user/day and user/month call limits with
      `:cost_limit_micros = NULL`, global/day call limit with `:cost_limit_micros = NULL`,
      and global/month cost ceiling with `:call_limit = NULL`), proving that `NULL`
      unconstrained dimensions admit valid requests and do not reject due to SQL
      three-valued logic.

  **B. Unit & adapter suite (focused checks, no database required):**

  12. **preflight ordering**: with an adapter whose `preflight()` fails, assert the
      reservation ends `released` and never reaches `started`. If `started` were
      committed first, §5.3 would forbid releasing it and a misconfigured deployment
      would burn the day's quota on requests that never left the process;
  13. **`LLM_VALIDATION_ERROR_MAX_BYTES` truncates**: a validator error longer than
      the cap is truncated before it reaches the repair prompt, and the reserved bound
      is computed from the truncated prompt.

- **8e-3** — the usage view endpoints, and the reconciliation job that transitions
  stale open reservations per §5.3 — `reserved` → `released` with both call and cost
  counter columns returned, `started` → `unknown` with the counter unchanged and one
  ledger row appended. (`abandoned` was a state in an earlier draft and no longer
  exists; turning a `reserved` row into `unknown` would charge a user for a call that
  was never sent.)

  Required database tests for 8e-3:
  1. **stale `reserved` reconciliation**: stale `reserved` row transitions to
     `released`, returning both `calls_reserved` and `cost_reserved_micros` on all
     four counter rows;
  2. **stale `started` reconciliation**: stale `started` row transitions to
     `unknown`, counter unchanged, appending exactly one ledger row with
     `status = 'unknown'`, `call_kind` preserved, and `model_id` NULL;
  3. **reconciliation vs late settlement race**: a reservation settling while
     reconciliation inspects it resolves deterministically without double-counting
     or double-releasing.

### 10.1 Operational Runbook: Stale Reservation Reconciliation (Step 8e-3)

#### Delivery & Packaging
The reconciliation tool is packaged in the API codebase and shipped inside the production container image:
- **CLI Entrypoint:** `python scripts/reconcile_llm_usage.py [run] [--stale-seconds N] [--limit M] [--dry-run]`
- **Admin HTTP Endpoint:** `POST /api/v1/llm/admin/reconcile` (guarded by `enforce_llm_admin_access`)
- **Admin Usage View:** `GET /api/v1/llm/admin/usage` (guarded by `enforce_llm_admin_access`)
- **User Usage View:** `GET /api/v1/llm/usage` (returns authenticated user quotas and `resets_at`)

#### Access Control & Security (Fail-Closed)
Admin routes (`/api/v1/llm/admin/*`) require explicit administrative privileges:
1. Users with `"admin"` in their JWT token roles.
2. Users whose verified account email is listed in `LLM_ADMIN_EMAILS` (or `LLM_ALLOWED_EMAILS` fallback).
3. **Fail-closed policy:** If both allowlists are empty, admin endpoints return `HTTP 403 Forbidden` (`llm_admin_access_denied`). Admin access is never open to arbitrary authenticated users, regardless of whether generation access is open to the public.

#### Counter Semantics in Views
In `LlmQuotaWindowView`:
- `calls_reserved`: Total calls admitted into the quota window and currently charged against `call_limit`. This value is never decremented when a call settles.
- `calls_settled`: Count of successfully completed and settled calls within this window.
- `calls_total`: Equivalent to `calls_reserved` (total calls authorized against quota). Outstanding in-flight or unresolved calls equal `calls_reserved - calls_settled`.
- `cost_reserved_micros`: Active reserved cost bound for in-flight requests.
- `cost_micros`: Settled actual cost incurred by completed requests.
- `cost_total_micros`: Combined liability (`cost_reserved_micros + cost_micros`) enforced against `cost_limit_micros`.

#### Safe Threshold Selection
- Pipeline execution timeout is 30 seconds (`LLM_REQUEST_TIMEOUT_SECONDS=30`) with up to 2 validation repair retries, giving a worst-case pipeline latency of approximately 90 seconds.
- `LLM_RECONCILIATION_STALE_SECONDS` defaults to **300 seconds** (5 minutes). This provides a >3x safety buffer over the worst-case pipeline duration, preventing active in-flight calls from being prematurely marked `unknown` or released.
- Non-positive thresholds (`<= 0`) and batch limits (`<= 0`) are rejected with errors by both the CLI parser and service before any database transaction opens.

#### Operational Execution & Dry-Run
Before applying automated mutations, operators can safely inspect candidate backlog size:
```bash
# Non-destructive inspection
python scripts/reconcile_llm_usage.py run --dry-run

# Targeted execution with overrides
python scripts/reconcile_llm_usage.py run --stale-seconds 600 --limit 50
```
Output is returned as structured JSON:
```json
{"dry_run": true, "reconciled_reserved": 0, "reconciled_started": 0, "returned_cost_micros": 0}
```

#### Operational Activation Gate
Tooling and container packaging are delivered and verified in Step 8e-3. Regular automated invocation (e.g. system cron, Kubernetes CronJob, or external orchestrator running every 5–10 minutes) represents an **operational deployment gate** to be scheduled in the production infrastructure environment alongside log monitoring for `llm.reconcile.cli.*` events.

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
