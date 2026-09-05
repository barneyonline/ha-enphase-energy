# Architectural review and assigned change plan

Reviewed 5 September 2026 at commit `d3590485`. This document preserves the
original findings and baseline measurements. The subsequent implementation and
critical-review results are tracked in [the implementation record](architecture-implementation-2026-09-05.md).

## Assessment

The integration has strong Home Assistant foundations, but the review does **not** support an unqualified claim that it is fully reliable, highly maintainable, or optimally performant. Several important concurrency, authentication, lifecycle, and energy-statistics defects remain despite excellent measured statement coverage. Fix those before undertaking large structural changes.

This deliverable is an architectural analysis and assignment of changes. Production source has not been changed. Three subagents own the detailed work packages linked below. The findings distinguish reproduced failures, source-supported defects, and improvements requiring measurement; this is not a guarantee that every possible defect has been discovered.

## Scope and evidence

The review covered setup/unload/reload, configuration and services, coordinator/runtime ownership, HTTP authentication/retry behavior, refresh scheduling, entity platforms and inventory, snapshots, diagnostics, recorder attributes, tests, CI, and contributor documentation. Three independent reviews supplemented local source inspection, duplicate-function analysis, current official Home Assistant documentation, and Docker verification.

The integration contains 88 Python files and 84,360 physical lines, including blank lines and comments. Six modules account for 38,463 lines: coordinator (8,989), API facade (8,521), sensor platform (8,628), battery runtime (5,926), inventory runtime (4,340), and EVSE runtime (2,059). Size alone is not a defect; the mixed responsibilities and dependency crossings described below make changes harder to isolate.

### Foundations to preserve

- `EnphaseConfigEntry` and `EnphaseRuntimeData` provide typed entry-owned runtime access.
- Network operations use aiohttp and Home Assistant-provided session infrastructure. Existing isolated cookie-session ownership needs lifecycle correction, not replacement with blocking I/O.
- One main coordinator centralizes polling; the documented weather child has an independent cadence and runtime ownership.
- Refresh plans separate core readiness from optional services, with deadlines, backoff, stale-data handling, and scoped request metrics.
- Immutable current-power, feature-flag, and VPP snapshots are a good migration direction. Discovery persistence avoids unnecessary telemetry-driven serialization.
- Conservative topology reconciliation preserves user-customized entities during uncertain cloud discovery.
- Existing entity identity, translation, diagnostics redaction, remembered-password opt-in, and bounded structured error handling are valuable contracts.
- Ruff, strict mypy, integration import checks, quality-scale documentation, compatibility tests, and extensive behavioral fixtures provide a strong validation base.

These patterns align with the official [runtime-data rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/runtime-data/), [data-fetching guidance](https://developers.home-assistant.io/docs/integration_fetching_data/), and [strict typing guidance](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/strict-typing/). Passing a local quality-scale validator verifies the repository's claims and references; it is not independent Home Assistant certification.

## Prioritized findings and ownership

P1 means a correctness/reliability fix to schedule first. P2 means a material reliability, performance, or validation improvement. P3 means maintainability work after regression protection. IDs are stable within this report; detailed subagent documents may use their own local numbering.

| ID | Priority | Finding and consequence | Assigned subagent |
| --- | --- | --- | --- |
| A01 | P1 | Poll-triggered token renewal reacquires the polling lock and stalls before login. | `transport_review` |
| A02 | P1 | Retried requests can overlay refreshed credentials with eagerly captured old authentication headers. | `transport_review` |
| A03 | P1 | Warmup repeatedly republishes a pre-await charger snapshot, overwriting newer poll or command state. | `transport_review` |
| A04 | P1 | Runtime handoff reuses objects that Home Assistant shuts down/detaches during the actual unload lifecycle. | `lifecycle_review` |
| A05 | P1 | Four daily heat-pump energy sensors lack reset metadata and can report incorrect statistics across midnight. | `entity_review` |
| A06 | P1 | Nonfinite inverter energy can poison the lifetime clamp until its cache is cleared. | `entity_review` |
| A07 | P2 | Failed setup needs explicit rollback of integration-owned tasks/runtime state beyond HA's own cleanup. | `lifecycle_review` |
| A08 | P2 | Services are removed on last-entry unload, and service runtime selection does not consistently require a loaded entry. | `lifecycle_review` |
| A09 | P2 | Inverter snapshot caching uses integer object IDs without retaining source identities. | `entity_review` |
| A10 | P2 | Frequently changing diagnostic attributes and repeated daily details remain recorder inputs. | `entity_review` |
| A11 | P2 | A fresh fetch timestamp participates in aggregate equality, defeating unchanged-payload callback suppression. | `transport_review` |
| A12 | P2 | Real lifecycle, concurrent authentication, publication ordering, and recorder reset contracts need stronger tests. | All three, within owned changes |
| A13 | P2 | Default Docker dependencies are not reproducibly pinned; advertised minimum and contributor guidance disagree. | `lifecycle_review` |
| A14 | P2 | Reachable inventory logic is excluded from coverage; strict typing still permits broad dynamic runtime access. | `entity_review` for coverage; `transport_review` for runtime typing |
| A15 | P2 | Weather errors need consistent redaction and expected handling when an available endpoint becomes unsupported. | `entity_review` |
| A16 | P2/design | Live measurement freshness needs an explicit family policy rather than availability after any historical success. | `entity_review`, with runtime owner review |
| A17 | P3 | Finish incremental runtime state ownership and remove obsolete reflective compatibility paths. | `transport_review` |
| A18 | P3 | Continue API surface extraction without changing public compatibility/error contracts. | `transport_review` |
| A19 | P3 | Separate registry migrations, configuration policies, and service routing from lifecycle entry points. | `lifecycle_review` |
| A20 | P3 | Extract remaining sensor feature models and consolidate duplicated capability/presentation helpers. | `entity_review` |
| A21 | P3 | Consolidate demonstrably identical scalar, inventory, schedule-editor, and redaction helpers. | Split by ownership below |
| A22 | P3/measure | Remove redundant snapshot/inventory copies and establish realistic runtime performance measurements. | `transport_review` and `entity_review` |
| A23 | P3 | Correct contradictory architecture instructions and tie quality-scale claims to behavioral evidence. | `lifecycle_review` |
| A24 | P2 | Duplicate inverter pagination lacks an overall bound and a shared explicit completeness contract. | `entity_review`, coordinating API changes with `transport_review` |
| A25 | P3/verify | Audit restored power values for native-versus-display unit consistency. | `entity_review` |

### Authentication and publication: A01–A03

`coordinator.py:4091` holds `_refresh_lock` while polling. A 401 calls `_handle_client_unauthorized()` (`:6224`), which reaches `AuthRefreshRuntime.attempt_auto_refresh()` and reacquires that same non-reentrant lock (`auth_refresh_runtime.py:80`). A Docker reproduction held the polling lock and invoked the real runtime; it timed out with zero login calls. Use a dedicated authentication synchronization boundary or atomic task coalescing. Preserve shared login tasks, cooldowns, cancellation shielding, and manual retry semantics.

Separately, `api.py:5118` passes an already-built `_today_headers()` dictionary into `_json`. On retry, `_json` refreshes base headers but merges the old overrides back in (`:4695–4704`). A real `status()` reproduction observed old-token headers on both attempts after successful credential replacement. Audit all auth-dependent header factories and any associated body/query identity. A lock fix alone will not fix this retry defect.

`refresh_runner.py:384–415` snapshots charger data before awaiting startup power, then publishes that snapshot after multiple awaits. An interleaved update to `charging=True` was overwritten by five stale `False` publications in a reproduction. Publish from current state and merge only the stage-owned enriched fields using explicit conflict rules. Do not serialize all optional network work under the core polling lock.

### Entry lifecycle and actions: A04, A07–A08

`__init__.py:2120` preserves runtime objects across topology reloads. However, the coordinator is registered with the config entry (`coordinator.py:1054`), so Home Assistant unload callbacks shut it down; an auto-cleanup private session is detached as well. Reusing those objects after the callbacks creates a stopped coordinator and unusable session. Existing direct calls to integration setup/unload bypass these framework callbacks. The lifecycle agent verified the installed HA implementation; the assigned regression must exercise the real config-entry reload path.

Prefer preserving immutable discovery/published data and constructing fresh lifecycle-owned objects. Do not reset private Home Assistant shutdown/debouncer fields. Preserve fast entity recreation as an explicit acceptance criterion.

Cold setup cleanup must account for partially created runtime managers and tasks. Home Assistant already performs its own unload callbacks on failure, so this finding must not be described as proof that its shared session is leaked. Test failures before/after first refresh and platform forwarding, cancellations, and retries, then make rollback idempotent.

Domain services should remain registered after an entry unloads; handlers should reject unavailable entries with an appropriate validation error. `runtime_data.py:76` and service-specific resolvers check runtime shape without consistently checking loaded state. Separate platform-setup runtime access from service-action runtime selection so a loaded-state guard does not break legitimate setup. This follows the official [action-setup rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/action-setup/).

### Energy correctness and entities: A05–A06, A09–A10, A15–A16

`sensor_heatpump.py:768` declares resetting daily counters as `TOTAL` without `last_reset`. Preserve `TOTAL` with a timezone-aware reset derived from the source day if within-day corrections are supported; use `TOTAL_INCREASING` only with a demonstrated monotonic contract. Test recorder sums across midnight, DST, corrections, and stale prior-day readings. The [sensor documentation](https://developers.home-assistant.io/docs/core/entity/sensor/) explains these distinct accumulation rules.

`inventory_runtime.py:3880` and `sensor.py:3665` accept NaN/infinity from numeric conversion. Positive infinity passes the nonnegative check and can prevent recovery from later valid readings until the cache is cleared. Reject nonfinite values before they enter published state or last-good caches, and prove recovery after a malformed cache/cloud response.

The inverter cache (`sensor.py:3635`) stores integer IDs of source maps. Use retained source references or explicit revisions, as the battery model already does. Test deterministic invalidation rather than relying on allocator reuse.

Inverter pagination is repeated in `api.py:2027` and `inventory_runtime.py:3621,3674`, with differing handling of wrapped payloads and later-page failures. Add a shared bounded paginator with explicit complete/partial results, repeated-page detection, and cancellation. Keep partial discovery from authorizing entity deletion. This is a source-supported improvement; no live pagination failure was reproduced. Separately, verify power sensor restoration with user-selected display units; this is an investigation assignment, not a confirmed unit-conversion defect.

Apply targeted recorder exclusions to per-poll timestamps, inverter sampled-at maps, and repeated heat-pump details. Keep useful live attributes; verify exclusions through recorder behavior and measure the effect before claiming a storage reduction.

Weather wraps raw exception text in `UpdateFailed` (`weather.py:182,189`), bypassing the integration's normal redaction boundary. Normalize unsupported-after-discovery behavior too. For general site sensors, `_SiteBaseEntity.available` preserves availability after any historical success; define separate freshness contracts for instantaneous telemetry, cumulative totals, and diagnostics before changing this policy.

### Publication performance and state ownership: A11, A14, A17, A22

`coordinator.py:4952` inserts the current `fetched_at_utc` into each charger payload. `IntegrationSnapshot.chargers` compares the complete recursively frozen payload, so identical cloud telemetry fetched later still differs. Separate observation metadata from semantic equality while explicitly publishing health and runtime-only changes. Simply setting `always_update=False` is insufficient, and removing metadata without adding complete family publication contracts can hide real transitions. HA recommends suppression only where [data comparison correctly represents changes](https://developers.home-assistant.io/docs/integration_fetching_data/).

`integration_snapshot.freeze_charger_data()` freezes each charger mapping and then recursively freezes the enclosing mapping, walking nested payloads twice. Remove the redundant traversal with immutability/detachment tests and profile representative sites. Likewise, inventory/entity helpers copy already-copied member dictionaries. These are source-supported inefficiencies, not measured end-user latency claims.

ADR 0001 already describes the appropriate migration: entity → coordinator public API → feature runtime → async client, with immutable snapshots. Implementation still uses hundreds of projected fields (`state_models.py:532–570`), reflective factories, `__getattr__ -> Any`, and direct manager access to coordinator private state. Migrate one family at a time behind narrow typed protocols. Keep compatibility adapters only for verified consumers, and avoid a wholesale rewrite.

## Concrete redundancy inventory: A20–A21

AST comparison found identical function bodies across files. The following are cleanup candidates; identical bodies are evidence of duplication, not permission to erase intentionally different public contracts.

| Family | Existing duplicates | Owner / safe destination |
| --- | --- | --- |
| Broad optional booleans | `coordinator.py:405`, `sensor.py:3713`, `parsing_helpers.py:36` | Transport owns parser contract; entity agent migrates its consumers afterward |
| Narrow booleans | `coordinator.py:2564`, sensor helpers, `diagnostics.py:242`, `discovery_snapshot.py:191` | Shared named policy; keep separate from broader enabled/yes vocabulary |
| HEMS grouping | `inventory_runtime.py:1145`, `config_flow.py:267` | Lifecycle agent, pure inventory parser |
| Inventory summaries | `inventory_runtime.py:2625`, `system_dashboard_helpers.py:19` | Entity agent, existing dashboard helper |
| Optional summation | `coordinator.py:3083`, `heatpump_runtime.py:870` | Transport agent, domain normalization helper |
| Identifier truncation | `api.py:4381`, `log_redaction.py:34` | Transport agent, existing redaction boundary |
| Charging / safe-limit checks | `sensor.py:2751,2762`, `number.py:418,429` | Entity agent, existing capability/model boundary |
| Type normalization | `__init__.py:231`, `config_flow.py:1315`, duplicate flow methods | Lifecycle agent; preserve onboarding vs options fallback differences |
| Heat-pump status ordering | `sensor_heatpump.py:90`, `inventory_runtime.py:1317` | Entity agent, shared normalization |
| Schedule formatting | `evse_schedule_editor.py:60,74`, `battery_schedule_editor.py:64,78` | Lifecycle agent, narrow pure formatting/day helper; keep family write schemas separate |
| Battery write capability | `number.py:54`, `switch.py:199`, unused `select.py:145` helper | Entity agent, coordinator capability API; remove helper-only tests with dead helper |

## Test and tooling improvements: A12–A14, A23

The successful baseline is valuable evidence, but all three reproduced transport failures escaped it. Add tests at integration boundaries: real HA entry lifecycle, HTTP → coordinator → reauth, concurrent warmup/poll/control publication, and recorder daily accumulation. Prefer externally observable state and outbound requests over mock-internal assertions.

`inventory_view.py` excludes reachable production methods from coverage, including entity gating and device information. Incrementally remove those blanket exclusions and test their behavior. Audit similar exclusions by reason; retain legitimate type-only/unreachable compatibility exclusions. Strict mypy currently passes, but `Any`, dynamic descriptors, ignored HA base typing, and skipped imports limit the proof it provides.

The documented “pinned” default Docker file uses `homeassistant>=2026.6.0` and several unpinned tools; the existing image actually runs HA 2026.8.3. CI's named forward lane still overlays 2026.8.0b4. Make supported-version lanes explicit and reproducible, including the current stable target, with compatible test-plugin locks. `hacs.json` and README advertise 2026.6.0 while AGENTS.md mentions 2026.3.0. Reconcile that policy without silently expanding support.

`docs/architecture.md` directs new HTTP behavior into `api_client/` in the cloud section but later says to start new endpoints in `api.py`. Fix the contradictory guidance after agreeing extraction boundaries. Update quality-scale references to the new behavioral tests; a successful reference validator must not be presented as a reliability certificate.

## Assigned execution sequence

1. **Transport subagent:** A01 and A02 as separate focused fixes, then A03. Own `auth_refresh_runtime.py`, `api.py`, `api_client/`, and `refresh_runner.py`; coordinate any `coordinator.py` edit with root.
2. **Lifecycle subagent:** A04 with real reload regression, then A07/A08. Own `__init__.py`, runtime lifecycle access, services/config flow and their tests. Root arbitrates coordinator changes shared with transport.
3. **Entity subagent:** A05/A06/A09, then A10/A15. Own sensor/platform and inventory changes/tests. Notify lifecycle before shared inventory normalization changes.
4. **After correctness gates pass:** transport handles equality/runtime/API work; lifecycle handles reproducible tooling, configuration/service/migration decomposition and shared schedule helpers; entity handles coverage, capability cleanup, freshness design and sensor extractions. Serialize shared parser and coordinator edits.
5. **Root integration review:** review compatibility and diffs, run the prescribed Docker gates, require targeted coverage for changed Python modules, update user-facing documentation/changelog where behavior changes, and reconcile each finding with evidence. Each logical fix should remain independently reviewable.

The three assigned plans are [lifecycle](architecture-review-lifecycle.md), [transport and concurrency](architecture-review-transport.md), and [entities and state](architecture-review-entities.md). This was the original assignment sequence; the implementation record documents
the work completed after authorization.

## Validation performed

All checks below used the repository's Docker `ha-dev` service, with HA **2026.8.3** from the available image:

```bash
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc 'ruff check .'
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc 'python scripts/validate_quality_scale.py'
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc 'python scripts/importtime_profile.py --strict-integration-warnings --output /tmp/architecture-importtime.log'
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc 'mypy --strict --ignore-missing-imports --follow-imports=skip custom_components/enphase_ev'
```

Those four checks were executed sequentially inside one container command and passed. Mypy reported 88 source files. The baseline test/coverage command executed was:

```bash
docker compose -f devtools/docker/docker-compose.yml run --rm ha-dev bash -lc 'COVERAGE_FILE=/tmp/architecture-baseline.coverage python -m coverage run -m pytest -q tests/components/enphase_ev tests/compatibility > /tmp/architecture-baseline-tests.log 2>&1; result=$?; tail -50 /tmp/architecture-baseline-tests.log; if [ "$result" -eq 0 ]; then COVERAGE_FILE=/tmp/architecture-baseline.coverage python -m coverage report --include="custom_components/enphase_ev/*" --fail-under=95; fi; exit "$result"'
```

Result: **3,959 tests passed in 29.85 seconds**. Coverage counted **44,647 statements, two missed**, with the misses in `grid_profile_runtime.py`; the 95% gate passed. This was statement coverage with configured exclusions, not exhaustive branch or lifecycle coverage. A prior `pytest --cov` invocation could not run because plugin autoload is disabled; the successful run used `python -m coverage` instead.

The transport agent additionally ran isolated Docker reproductions for A01–A03. The lifecycle agent inspected the installed HA unload/session implementation. No live Enphase control action, deployment, commit, push, or PR was performed. No sustained live-site latency, CPU, memory, or recorder growth benchmark was performed, so performance recommendations remain bounded by static evidence and require measurement.
