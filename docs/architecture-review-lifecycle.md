# Lifecycle, configuration, and service architecture review

Implementation and final validation: [completed change record](architecture-implementation-2026-09-05.md).

Review date: 2026-09-05. Baseline: `d359048513f726408d5dcc2a993c756818f4a4e0`.
Assigned owner: `lifecycle_review` subagent. This document preserves the original
audit and assigned implementation plan. The user subsequently authorized all
changes; implementation status is recorded below. Line references describe the
baseline.

## Assessment and evidence limits

The integration already uses typed config-entry runtime data, a shared main
coordinator, an independently owned optional weather coordinator, asynchronous
I/O, translated service validation, conservative registry ownership checks, and
an authoritative initial refresh. Those are sound foundations. However, the
runtime handoff conflicts with Home Assistant's actual unload callbacks. Service
registration also does not fully meet the current action-setup rule.

The Home Assistant implementation was inspected inside the repository's pinned
`ha-dev` Docker environment, which reported Home Assistant **2026.8.3**. The
inspection covered `DataUpdateCoordinator.__init__`, `async_shutdown`,
`ConfigEntry.__async_setup_with_context`, `_async_process_on_unload`, and
`aiohttp_client._async_register_clientsession_shutdown`. This is direct source
evidence of the callback interactions below. An end-to-end integration reload
reproduction was not run during this read-only review; that regression test is
the first implementation step. Current official guidance was also read:

- [Register service actions in async_setup](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/action-setup/).
- [Support config entry unloading](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/config-entry-unloading/).

## L1 — High priority: replace live coordinator handoff with safe state transfer

**Evidence.** `coordinator.py:1054-1061` passes the config entry to
`DataUpdateCoordinator`. Its constructor registers `async_shutdown` as an entry
unload callback. That callback sets `_shutdown_requested = True` and shuts down
the refresh debouncer. `__init__.py:1810-1818` also creates the stateless cookie
session with `auto_cleanup=True`; Home Assistant registers its `detach()` as an
entry unload callback when created in the config-entry setup context.

`__init__.py:2116-2127` preserves the entire runtime on a topology reload, then
`__init__.py:1787-1807` reuses the same coordinator, client, and managers. Home
Assistant executes its unload callbacks after the integration's unload returns.
The preserved objects consequently retain the shut-down coordinator and detached
session. There is no recreation or public restart step. The API cookie-session
selector at `api.py:4963-4974` yields the stored session without replacing a
detached one. This can break refresh scheduling and cookie-authenticated requests
after changing topology options such as weather or VPP.

**Test blind spot.** `test_init_module.py:824-887` calls the integration setup and
unload functions directly. It neither establishes the actual setup context nor
processes Home Assistant's entry unload callbacks. Mocking
`async_request_refresh` also hides the stopped debouncer. This explains why the
test proves object reuse without proving a working integration after reload.

**Safe design.** Preserve immutable published/discovery state where useful, while
creating a fresh Home Assistant coordinator, client sessions, and runtime managers
for every entry lifecycle. State transferred into the new instance must be scoped
to the same site and filtered by the new selected capabilities. Do not reset Home
Assistant's private shutdown or debouncer fields or remove its registered
callbacks. An initial corrective implementation may use the existing cold setup
path for topology reloads, explicitly documenting the temporary latency tradeoff;
a subsequent state-transfer change can restore immediate cached entity creation.

**Ordered implementation and acceptance tests.**

1. Reproduce through `hass.config_entries.async_setup` and `async_reload`, mocking
   cloud responses rather than lifecycle callbacks. Enable then disable a topology
   option and verify a later scheduled poll makes a real mocked API request.
2. Ensure a cookie-header-only API request succeeds after reload and the previous
   session is detached. Assert no old coordinator executes further requests.
3. Replace live-object handoff; test repeated reloads, setup failure after unload,
   user-disabled entries, and two independent sites.
4. If cached-state transfer is included, verify entities can use the last published
   state before optional warmup without publishing disabled VPP/profile features
   or transferring tasks, locks, pending commands, or session objects.
5. Verify unload failure leaves the current entry usable and does not release its
   resources. Run the existing cancellation timeout and active-refresh tests.

**Files owned.** `__init__.py`, `runtime_data.py`, and lifecycle tests in
`test_init_module.py`/`test_runtime_data.py`. Coordinate changes to `coordinator.py`
and any discovery snapshot interface with the coordinator audit owner. Keep API
transport changes with the API audit owner. Update architecture and changelog
documentation with the implemented behavior.

## L2 — Medium priority: make cold setup rollback explicit and complete

**Proven gap.** `__init__.py:2094-2098` invokes full runtime cleanup only for a
claimed preserved runtime. Cold setup assigns `entry.runtime_data` before the
initial refresh. Its local bootstrap exception handler cancels label/version and
startup power work, but does not call the broader coordinator cleanup. Exceptions
after bootstrap likewise bypass that cleanup. `coordinator.py:2267-2375` owns
additional cleanup for discovery-save timers, backoff timers, task families,
caches, and listeners.

**Qualification.** Home Assistant *does* process registered unload callbacks when
setup fails, including coordinator shutdown, entry-owned background tasks, and
automatic private-session detach. A claim that every cold setup failure leaks the
HTTP session would therefore be incorrect. The missing integration-owned rollback
is proven by control flow; the exact residual timers/tasks depend on the failure
phase and need a regression test. The directly assigned runtime also remains
visible until replaced or cleared by integration code.

**Implementation.** Generalize the existing cleanup routine to release any runtime
created by the failed setup attempt; run cleanup steps independently so one cleanup
error does not prevent the others. Clear only the runtime owned by that attempt,
preserve the original exception/cancellation, and leave Home Assistant callbacks
idempotent. Cover a failure before runtime creation separately.

**Acceptance tests.** Inject failures during initial refresh, registry/editor
initialization, and platform forwarding, plus external cancellation. Verify no
integration-owned timer/task remains, no schedule service starts after failure,
runtime routing cannot select the failed attempt, session release remains safe,
and the next retry succeeds. Test a cleanup step failing while later steps still
run. Use the actual Home Assistant lifecycle for at least one retry scenario.

**Files owned.** Same lifecycle files as L1. Implement after L1 establishes the
ownership model, rather than extending two competing cleanup designs.

## L3 — Medium priority: retain domain services and centralize loaded routing

**Evidence.** `async_setup` correctly registers services (`__init__.py:453-457`),
but `async_unload_entry` removes them when the last entry unloads
(`__init__.py:2139-2145`) and entry setup restores them (`:1772-1773`). Current
Home Assistant action-setup guidance explicitly requires service availability
independent of loaded entries so automations remain valid and calls can explain an
unavailable target. The current behavior does not satisfy that intent.

`services.py:155-161` and `:262-270` identify loaded runtimes solely from their
runtime-data type. `runtime_data.py:76-96` duplicates that policy. They do not
verify `ConfigEntryState.LOADED`, so a setup/unload transition with runtime data
still attached can be selected. This is particularly relevant to L2.

**Implementation.** Keep registration at domain setup for the domain lifetime;
remove entry-driven unregister/re-register behavior. Introduce one typed helper
for selecting loaded entry runtimes, with an explicit policy for site filtering
and duplicate sites. Use it in service routing. Preserve exact device/entry routing
precedence and existing translated no-target errors. Runtime helpers needed during
setup should remain distinct from service-only loaded-entry selection.

**Acceptance tests.** Services remain registered after the last entry unloads,
across reload, and when an entry fails setup. Calls to unavailable targets raise a
useful translated validation error. Setup-in-progress, unload-in-progress, disabled,
and failed entries are never command targets. Loaded entries remain selectable
when another site is unloading. Preserve ambiguous-serial rejection, explicit
config-entry routing, area/entity/device targets, and admin-only write behavior.

**Files owned.** `__init__.py`, `runtime_data.py`, `services.py`, corresponding
initialization/runtime/service tests, and relevant quality-scale documentation.
Perform after L1/L2 to avoid simultaneous edits to entry ownership.

## L4 — Lower priority: remove duplication and separate responsibilities

These are maintainability opportunities, not evidence of runtime defects.

| Change | Baseline evidence | Boundary and acceptance criteria |
| --- | --- | --- |
| Shared selection normalizers | `config_flow.py:996` / `:1285` duplicate serial normalization; `:1056` / `:1300` duplicate allowed type normalization; `__init__.py:231` / `config_flow.py:1315` duplicate unrestricted type normalization | Extract pure helpers with explicit allowed-type policy. Preserve accepted input forms, ordering, deduplication, aliases, and unknown-type behavior. |
| Registry migration separation | `__init__.py` is 2,146 lines; its central section is predominantly registry migrations/reconciliation | Move migrations and reconciliation into cohesive modules. Preserve conservative ownership/readiness checks, unique IDs, disabled state, custom names, and multi-entry devices. Avoid broad registry deletion changes. |
| Service routing/schema separation | `services.py` is 1,987 lines, largely inside one registration closure | Extract target routing and schemas first. Preserve service metadata, response mode, permissions, validation, and outbound command semantics. Do not combine this with new services. |
| Config/options flow separation | `config_flow.py` is 2,794 lines and contains grid control/profile operational flows and an Envoy history migration wizard | Extract independent helpers/controllers, retaining Home Assistant flow contracts and step IDs. Keep authentication secrets confined to the existing flow state. |
| Remove test-only production fallbacks gradually | Bootstrap/setup contains multiple `getattr` compatibility paths explicitly justified by lightweight downstream test coordinators (`__init__.py:1847-1855`, `:1883-1906`) | Prefer real coordinators with mocked boundaries or typed test doubles, then remove fallbacks only after confirming the documented compatibility contract. |

The two `_legacy_selected_type_keys` methods are **not** identical: onboarding
uses discovered inventory while options uses legacy defaults. Do not blindly merge
them while deduplicating normalization.

Implement L4 as small independent changes after correctness work. Maintain 100%
targeted coverage for changed Python modules and run all repository-required
Docker checks before publishing implementation changes. Coverage of extracted
helpers should test input/behavior contracts; lifecycle tests must additionally
exercise Home Assistant's real callback machinery.

## Additional assigned follow-ups from the consolidated review

The stable IDs below refer to `architecture-review-2026-09-05.md`. These are
assigned plans, not implemented changes, and follow the lifecycle correctness work.

- **A13 — Reproducible validation and version policy.** Own
  `devtools/docker/Dockerfile`, the development/compatibility requirements and
  Compose lanes, and the corresponding CI jobs. Pin coherent Home Assistant and
  test-plugin/tool dependencies; expose minimum-supported and current-stable lanes
  with explicit runtime-version assertions. Preserve the existing minimum-version
  job rather than replacing it with only the default image. Reconcile README,
  `hacs.json`, contributor guidance and `AGENTS.md`: the advertised minimum is
  2026.6.0, while `AGENTS.md:44` says 2026.3.0. Confirm the intended minimum instead
  of silently expanding supported versions. Acceptance requires clean image
  rebuilds reporting the intended dependency versions and the relevant integration,
  compatibility, import and type checks passing in each lane. Where the plugin
  requires an HA overlay, document why it is compatible and test that combination.
- **A21 — Shared schedule and HEMS normalization.** Own a narrow pure helper for
  identical `_time_to_text` and `_normalize_days` implementations in the EVSE and
  battery schedule editors, plus the identical HEMS grouping implementation in
  `config_flow.py:267` and `inventory_runtime.py:1145`. Coordinate inventory edits
  with the entity owner. Preserve current formatting, day ordering, malformed input
  handling, aliases and payload-wrapper precedence with parity tests. Keep family
  write schemas and schedule defaults separate: EVSE `default_day_flags` returns
  false values, whereas battery defaults return true values. This intentional
  difference must survive extraction. Do not broaden this cleanup into schedule
  validation behavior changes without a separate finding.
- **A23 — Architecture and quality evidence.** Own corrections to
  `docs/architecture.md` and quality-scale evidence references. The cloud-client
  section directs new behavior into `api_client/`, but the later adding-behavior
  map directs new endpoints into `api.py`; agree the public facade/internal surface
  boundary with the transport owner, then make both sections consistent. Replace
  broad quality claims with links to the new behavioral tests and state their
  limits. Acceptance requires valid references, matching documented ownership, and
  explicit distinction between a passing repository validator and independent
  Home Assistant certification. Do not claim proposed tests already exist.

The consolidated report accurately represents L1–L4. Its fast entity recreation
requirement should remain a final L1 acceptance criterion. A cold-reload fallback,
if chosen as an interim correction, needs an explicit documented performance
tradeoff before it can be treated as the completed design.


## Implemented follow-up (2026-09-05)

L1–L4 and A13/A21/A23 now have source changes and behavioral regression tests.
Setup always creates fresh lifecycle objects; `ReloadSnapshot` transfers detached
read state for immediate cached entity recreation. Failed setup releases its own
runtime, including external cancellation after platform forwarding. Domain services
remain registered and a single router admits only enabled, loaded entries.
Registry synchronization/migrations, options flow, service routing, shared config
selection, HEMS inventory parsing, and schedule formatting now have separate
modules with compatibility exports for existing integration imports.

A further review found that internal config persistence could mask a user option
change queued before Home Assistant dispatched its listener. Runtime data now
records precise internal field deltas. The listener advances only those applied
data fields and independently evaluates the current options and remaining data
changes. Actual Home Assistant lifecycle tests cover overlapping topology and
credential edits, internal-only persistence, failed persistence, cached reload,
continued scheduled polling, failed setup retry, and cancellation cleanup.

Docker/CI requirement lanes are pinned to Home Assistant 2026.9.0, 2026.8.3, and
minimum 2026.6.0 with explicit transitive constraints and matching test plugins.
`CONTRIBUTING.md` describes these lanes, and `quality_scale.yaml` links concrete
lifecycle/action regression evidence. Final validation and any environment limits
are reported in the main architectural review.
