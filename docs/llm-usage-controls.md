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
| `status` | `text` | `ok` \| `provider_error` \| `timeout` |
| `prompt_tokens` / `completion_tokens` | `integer` | 0 when the provider omits usage |
| `estimated_cost_micros` | `bigint` | see §6; an estimate, never a billing figure |
| `created_at` | `timestamptz` | |

Indexes: `(user_id, created_at)` for the usage view, `(request_id)` for tracing.

The ledger is **append-only**. It is evidence, not state: quota decisions read the
counters in §4.2, never `SUM()` over this table. A sum over an unbounded table is
both slow and racy.

### 4.2 `users.llm_usage_counter` — the enforcement state

| column | type | note |
|---|---|---|
| `scope` | `text` | `user` \| `global` |
| `scope_key` | `text` | the user id, or `'-'` for global |
| `window_kind` | `text` | `day` \| `month` |
| `window_start` | `date` | UTC window boundary |
| `calls_reserved` | `integer` | incremented **before** the provider call |
| `calls_settled` | `integer` | incremented after it returns |
| `cost_micros` | `bigint` | settled estimate |

Primary key `(scope, scope_key, window_kind, window_start)`.

`calls_reserved` is the number enforcement compares against. `calls_settled` exists
only for reconciliation and reporting — a gap between the two is the count of calls
that were authorised and never settled (§5.3).

### 4.3 `users.llm_entitlement` — per-user overrides

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
       (scope, scope_key, window_kind, window_start, calls_reserved)
VALUES (:scope, :key, :window_kind, :window_start, :cost)
ON CONFLICT (scope, scope_key, window_kind, window_start) DO UPDATE
   SET calls_reserved = c.calls_reserved + :cost
 WHERE c.calls_reserved + :cost <= :limit
RETURNING calls_reserved;
```

No row returned ⇒ the cap would be exceeded ⇒ reject with `429 llm_quota_exceeded`
(§8). The check and the increment are the same statement, so there is no window to
race in.

**`:cost` is 2, not 1**, for a normal generation: the pipeline will make a guard call
and a generator call. Reserving 1 and discovering the shortfall halfway through would
spend a provider call and then fail the user — the worst of both. Repair retries are
reserved individually, before each retry, and a refused reservation ends the repair
loop rather than failing the request (the unrepaired result is returned as it would
be on exhausted retries).

Scopes are reserved in a fixed order — **global, then user** — so concurrent requests
cannot deadlock on the two rows. If the user reservation fails after the global one
succeeded, the global reservation is released in the same transaction.

### 5.2 Settle

After the provider call returns, in one transaction: append the ledger row and
increment `calls_settled` / `cost_micros`. Settlement never re-checks the cap; the
authorisation already happened.

### 5.3 Release, and what happens on a crash

A reservation is released only when the call **provably did not reach the provider**
(a configuration error, or a refusal by a later scope in §5.1). A timeout is **not**
released: the request may well have been served and billed.

If the process dies between reserve and settle, the reservation stands. That is the
fail-closed direction: the deployment slightly over-counts rather than over-spends. A
periodic reconciliation job may convert reservations older than a threshold with no
matching ledger row into a settled `status = 'unknown'` event; it must never simply
decrement, or a crash becomes a way to mint quota.

### 5.4 Database failures deny

If the reservation statement itself fails, the request is **denied**, not allowed
through. The whole purpose is protecting a shared, exhaustible resource; degrading
open would remove the control exactly when the system is unhealthy.

## 6. Cost estimation

`estimated_cost_micros` is computed from a configured per-model price map
(micro-USD per 1K prompt/completion tokens) at settle time. It is explicitly an
**estimate**:

- the free router serves a model chosen per request, so the price is not known before
  the call;
- provider pricing changes without a deployment.

It is adequate for a global "stop at N" ceiling and for a usage view. It is **not** a
billing figure and must never be shown as money owed. A model missing from the price
map contributes 0 and is logged — a wrong price is worse than a known gap.

## 7. Limits

| Limit | Scope | Default source |
|---|---|---|
| daily provider calls | user | tier default, overridable per user |
| monthly provider calls | user | tier default, overridable per user |
| daily provider calls | global | deployment config |
| monthly estimated cost | global | deployment config |

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

- **8e-1** — Liquibase changesets for the three tables (`users` schema, per
  `domain-boundaries.md`), plus repository and settings surface. No behaviour change.
- **8e-2** — the reservation/settlement service, wired into the generation pipeline,
  with the `llm_quota_exceeded` error and its OpenAPI + ui contract sync. Tests must
  cover the concurrency case (two simultaneous requests at cap-1 ⇒ exactly one
  succeeds) with a real database, not a mock — a mocked counter cannot demonstrate
  the property the design exists for.
- **8e-3** — the usage view endpoints, and the reconciliation job for stale
  reservations.

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
