# Transport, authentication, and refresh architecture review

Implementation and final validation: [completed change record](architecture-implementation-2026-09-05.md).

Reviewed baseline: `d359048513f726408d5dcc2a993c756818f4a4e0`, 5 September 2026.
Owner: `transport_review` sub-agent. This document assigns implementation work;
it does not change runtime behavior. Line references below describe the reviewed
baseline and will move during implementation.

## Assessment

The architecture has useful foundations: Home Assistant supplies HTTP sessions;
browser reads share a concurrency limit with reserved core capacity; optional
families have explicit refresh plans; task groups propagate cancellation;
credential refreshes coalesce; response errors carry sanitized structured
metadata; and startup work has bounded stages. Preserve those contracts.

Three executable reproductions found correctness problems across those
boundaries. Passing component tests alone is insufficient to confirm reliable
reauthentication or safe concurrent publication. Resolve these problems before
large code moves. Performance and decomposition work should follow separately.

## Assigned changes

| ID | Priority | Assignment | Implementation owner | Evidence |
| --- | --- | --- | --- | --- |
| T1 / A01 | P1 | Separate authentication task coordination from the polling lock | `transport_review` | Docker reproduction confirms blocked login |
| T2 / A02 | P1 | Rebuild all authentication-dependent request headers after reauth | `transport_review`, after T1 | Docker reproduction confirms stale retry credentials |
| T3 / A03 | P1 | Publish warmup enrichment without replacing newer charger state | `transport_review` | Docker reproduction confirms old charging state replaces new state |
| T4 / A11, A22 | P2 equality; P3 copying/measurement | Define publication equality and eliminate redundant snapshot traversal | `transport_review`, after T3 | Source-confirmed work; benchmark and behavior contract required |
| T5 / A18 | P3 | Continue transport and endpoint-surface extraction behind the facade | `transport_review`, after T1/T2 | 8,521-line facade and duplicated transport behavior |
| T6 / A24 | P2 | Unify inverter pagination with bounded, explicit completeness semantics | `entity_review`, coordinating API edits with `transport_review` | Duplicate pagination and differing failure behavior |
| T7 / A14, A17 | P2 typing; P3 migration | Clarify refresh/client contracts and migrate runtime state ownership | `transport_review` | Dynamic `Any`/string-based boundaries bypass static checking |

These assignments refer to existing subagents; they do not create additional
agents. T1/T2 share `api.py` and coordinator initialization. T3/T4 share the
coordinator and publication tests. Root coordinates ownership handoffs and
shared-file edits with `lifecycle_review` and `entity_review`.

## T1: Poll-triggered reauthentication waits on its own lock

`coordinator.py:946` creates an ordinary `asyncio.Lock`.
`coordinator.py:4091` acquires that lock around the complete refresh pipeline.
The registered HTTP callback is `_handle_client_unauthorized`
(`coordinator.py:904`), which delegates to `attempt_auto_refresh`
(`coordinator.py:6224`). `auth_refresh_runtime.py:80` then acquires the same lock
before creating a login task. That lock is not reentrant.

Trigger: a polling HTTP request receives 401 while remembered credentials are
available, with neither reusable recent authentication nor an existing auth
task. The callback cannot start a login. The surrounding HTTP timeout eventually
interrupts it, so the poll appears to encounter a timeout instead of recovering
authentication. Calls made in refresh child tasks have the same lock dependency:
the lock owner awaits the child that awaits its lock.

The manual path also uses the polling lock (`auth_refresh_runtime.py:138`),
creating unnecessary coupling to an in-progress poll.

### Safe implementation

Introduce a dedicated auth-task coordination lock owned by `AuthRefreshRuntime`,
or keep the eligibility recheck and task creation atomic without awaiting inside
that section. Preserve shared-task reuse, `asyncio.shield`, success reuse,
credential rejection cooldown, manual retry semantics, and explicit shutdown
cancellation. Authentication network work must never need the poll lock to
start or complete.

### Required behavioral tests

- Exercise coordinator refresh through the real HTTP 401 path and actual
  authentication runtime; demonstrate that login starts and the refresh returns.
- Concurrent 401 responses perform one login and all waiters complete.
- Cancel one waiter; other waiters and the shared login survive.
- Unload cancels and awaits the shared task, including a blocked login.
- A manual attempt during a core refresh shares the in-flight auth task.
- Invalid credentials and MFA retain existing cooldown/reauth behavior.

### Executed reproduction

Run in the pinned `ha-dev` Docker image with the repository mounted by Compose.
The executable probe used the actual `EnphaseEVClient.status()`, its 401
handling, `AuthRefreshRuntime.attempt_auto_refresh()`, an ordinary polling lock,
and the repository's `FakeSession`/`FakeResponse`. Only the login implementation
was replaced with `AsyncMock(return_value=True)`.

```python
async with coord._refresh_lock:
    await asyncio.wait_for(client.status(), timeout=0.05)
```

Observed output:

```text
CONFIRMED actual status→401→reauth deadlock: HTTP requests = 1 login attempts = 0
```

## T2: Successful reauthentication replays old credentials

`_json` intentionally accepts a callable header factory and reevaluates it per
attempt (`api.py:4695`). Many endpoint methods instead pass an already-created
dictionary containing a copy of `self._h`. For example, `status()` passes
`self._today_headers()` at `api.py:5118`; the helper copies the auth headers at
`api.py:2513`.

After a successful reauth callback updates `self._h`, `_json` overlays that new
base with the old extra-header dictionary (`api.py:4702`). The retry therefore
sends the rejected cookie/token again. Fixing T1 alone does not fix this.

### Scope and safe implementation

Audit every `_json` and `_text_response` call site and each wrapper that builds
auth-bearing headers before awaiting a request. Convert these to factories that
rebuild the exact endpoint-specific header policy per attempt. Relevant families
include status/summary, BatteryConfig reads and writes, tariff, scheduler and
charger controls, inventory/history, system dashboard, and the extracted site
surface. Existing HEMS/VPP callable patterns provide a local precedent.

Do not blindly overlay fresh base headers last: BatteryConfig and Activation
deliberately remove some default auth headers or derive alternate bearer/session
values. Preserve `None` removal, explicit cookie-only policies, compatibility
variant selection, and per-attempt XSRF requirements. For writes, review whether
reauthentication invalidates bootstrap XSRF state and rebuild it at the correct
boundary. Identify any auth-derived request body/query fields as part of the
same audit.

### Required behavioral tests

- Status 401, credential rotation, successful retry: assert the retry sends the
  new token and cookie, not merely that the callback was invoked.
- Cover representative ordinary JSON, text, bearer-derived scheduler,
  BatteryConfig, and stateless-cookie request families.
- Preserve deliberate header removals across retry.
- A second 401 stops after the supported retry budget.
- Preserve bounded `enphase_error_status` and the real HTTP-to-battery-runtime
  `409 ALREADY_PROCESSED` contract when touching error handling.

### Executed reproduction

The actual `status()` method received a fake 401 then a success. Its real reauth
callback called `update_credentials` with synthetic replacement credentials.
No auth runtime or lock was involved, isolating this defect from T1.

```text
Actual status request auth before and after successful refresh = ['old-token', 'old-token']
Client current auth = new-token
```

## T3: Startup warmup overwrites state published while it awaits

`refresh_runner.py:385` takes a shallow copy of all charger data once, before
awaiting startup power and each optional stage. Lines 407 and 415 publish this
same dictionary after those waits. Warmup does not hold the polling lock, and
normal refreshes or user commands can publish newer charging state while warmup
waits. The old dictionary then replaces that state, potentially repeatedly
through the 60-second optional stages.

### Safe implementation

Model warmup output as enrichment with explicit field ownership. At publication,
merge into the latest authoritative charger data. For fields that can also be
changed by normal refreshes or commands, use a revision/precondition or a
three-way merge against the stage's starting values. Preserve newer changes,
removed chargers, and explicit field removals. Prefer a single synchronous
publication helper after the await so the read/merge/publish step is atomic on
the event loop.

Do not hold the polling lock across optional network stages. That would suppress
the race by introducing startup latency and blocking core acquisition. Keep
manager-only publication, including VPP, functional when charger data is empty.

### Required behavioral tests

- Pause warmup, publish a newer status/charging change, then resume: it survives.
- A newer command changes the same field warmup enriched: preserve the command.
- Independent enrichment still lands after an interleaved core refresh.
- A charger removed during warmup is not reintroduced.
- Site-only and VPP-only changes still notify listeners.
- Cancellation and deadline expiry preserve the last authoritative state.

### Executed reproduction

The actual `_async_startup_warmup_runner_impl()` started with
`charging=False`. Its awaited startup task published `charging=True` into the
coordinator. Subsequent refresh plans were stubbed to do no work so the probe
isolated publication from endpoint parsing.

```text
Concurrent update published charging = True
Warmup publications charging = [False, False, False, False, False]
```

## T4: Publication equality and snapshot copying need an explicit contract

The root review identified that `coordinator.py:4952` inserts
`fetched_at_utc` on every poll. `IntegrationSnapshot.chargers` includes the whole
charger mapping, so identical device telemetry with a new acquisition timestamp
compares unequal. `CoordinatorData.__eq__` delegates to aggregate snapshot
equality (`integration_snapshot.py:68`). This defeats suppression based on
unchanged charger state, although acquisition/health timestamps may themselves
be needed by diagnostic entities.

Additionally, `freeze_charger_data` freezes each charger payload and then freezes
the containing mapping again (`integration_snapshot.py:24`).
`snapshot_helpers.py:12` recursively handles every `Mapping`, including an
already-frozen mapping proxy, so nested data is copied twice.

### Safe implementation and acceptance

First specify which telemetry, freshness/availability, diagnostics, and manager
changes must notify. Measure snapshot construction time, allocations, listener
calls, and entity state writes for realistic charger, inverter, and battery
inventories. Retain acquisition timestamps in diagnostics while distinguishing
them from device-state equality if the publication contract permits it.

Eliminate the duplicate traversal with one recursive freeze of the original
mapping; prove deep immutability and detachment from mutable source containers.
Do not change `always_update` as an isolated optimization. Tests must cover
unchanged telemetry, changed telemetry, availability recovery/failure,
time-sensitive freshness, optimistic command changes, and manager-only current
power, feature-flag, and VPP transitions.

This is source-confirmed overhead, not a measured performance regression.

## T5/T7: Finish cohesive extraction without erasing compatibility

`transport_review` also owns main-report A17: the runtime state/protocol
migration described in [ADR 0001](adr/0001-runtime-state-ownership.md).
`state_models.py:532` still exposes grouped state through descriptors returning
`Any`; the descriptor installer at `:558` projects those fields dynamically.
Migrate one family at a time to runtime-owned state, an immutable snapshot, and
narrow coordinator service protocols for endpoint health, auth circuits, task
ownership, and publication. Preserve verified public coordinator adapters until
entity and diagnostics consumers have migrated. Add snapshot immutability,
manager-only publication, and compatibility tests before removing reflective
factories or descriptors. Coordinate entity-facing migrations with
`entity_review` and lifecycle/task ownership with `lifecycle_review`.

`api.py` still contains 8,521 lines. The extracted `api_client/transport.py`
contains only authentication requests (140 lines); main JSON/text handling,
rate guards, payload errors, auth shaping, BatteryConfig compatibility attempts,
MQTT packet handling, and numerous endpoint families remain in the facade.

Extract in small behavior-preserving changes:

1. JSON/text request policy, timing, and sanitized error construction.
2. Auth/cookie/header shaping and typed endpoint request context.
3. BatteryConfig compatibility attempts as a cohesive surface.
4. EVSE controls/scheduler and site dashboard/history surfaces.
5. MQTT websocket framing/packet parsing, separately from HTTP discovery.

Concrete redundancy includes the header-merge loop in `_text_response`
(`api.py:4996`) duplicating `_merge_request_headers` (`api.py:2728`), repeated
401 retry/response-error handling in `_json` and `_text_response`, and duplicated
authentication transport setup/status/session-limit checks. Share policy only
where semantics match: MFA permits empty/textual success while normal auth does
not; text responses also preserve expected statuses, redirect location, and
headers.

`api_client/site_surface.py` and `vpp_surface.py` take `client: Any` and access
private facade methods. Introduce a small structural protocol for only the
required session/request/header capabilities so extraction produces a checked
dependency boundary. Preserve existing exports and patch targets in `api.py`;
they are intentional compatibility seams, not dead code.

`refresh_plan.py:89` and related factories use string-based lookup and callbacks
returning `object`. `RefreshRunner.async_run_refresh_call` accepts a non-awaitable
result silently. New refresh contracts should return an explicit awaitable;
separate any intentional synchronous callback instead of silently accepting a
mistaken non-async implementation. Confirm the current production callback set
before removing compatibility fallbacks used by tests.

Acceptance: public facade imports remain stable; async injected-session use,
limiter reservation, timeout/cancellation, auth/error behavior, optional endpoint
semantics, and request metrics remain unchanged. Add boundary-oriented tests,
not merely one test per forwarding method. Correct the contradictory contributor
instruction in `docs/architecture.md` that says new endpoints start in `api.py`
while the cloud-client section directs new behavior into `api_client/`.

## T6: Consolidate inventory pagination with completeness as data

`api.py:2027` implements config-flow inverter pagination.
`inventory_runtime.py:3621` and `:3674` implement runtime pagination independently.
Both accept a server-reported total and keep extending the expected total from
later pages without an overall page/item/deadline bound. They differ in support
for wrapped `result.inverters` payloads and handling of later-page failures.
Runtime pagination also sits outside the first-page endpoint-failure/cache
handling block.

Assign to `entity_review` a reusable paginator/parser returning items plus
an explicit completeness result. Define maximum pages/items or a total operation
budget, detect repeated/no-progress pages, and preserve cancellation. Cache and
registry cleanup must distinguish complete discovery from a partial listing.
Respect the existing broad flow-UX exception policy only at the flow boundary.

Acceptance tests: root/wrapped payloads, multi-page success, repeated pages,
unreasonable/increasing totals, later-page failure, stale cached inventory,
and conservative entity retention until discovery is complete. This is an
identified reliability and duplication improvement; the review did not reproduce
a live Enphase pagination incident.

## Execution and validation order

1. `transport_review` implements T1 with a real poll-to-401 regression.
2. `transport_review` implements T2 with request-header assertions across families.
3. `transport_review` implements T3; root coordinates any shared coordinator
   edits with `lifecycle_review`.
4. `entity_review` handles T6, coordinating the paginator API and `api.py` edits
   with `transport_review`.
5. `transport_review` measures and implements T4, then pursues T5/T7 in reviewable
   extraction and runtime migration slices with the other existing owners.

All implementation validation must use the pinned Docker environment from
`CONTRIBUTING.md` and the repository-required gates, including 100% targeted
coverage for each touched Python module. The root reviewer owns the complete
baseline suite and final integration validation to avoid parallel expensive
full-suite runs. The three narrow reproductions above ran successfully in
`docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev python -c`;
they are evidence of current failures, not a claim of a fixed or fully tested
implementation. No runtime files, PR, branch, or release were changed for this
review.
