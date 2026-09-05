# Entity and state architecture review

Review date: 2026-09-05. Baseline: `d359048513f726408d5dcc2a993c756818f4a4e0`.
Owner: `entity_review` subagent. Status: assigned changes implemented and undergoing
combined repository validation. Finding locations below refer to the baseline;
implementation files and verification contracts are listed at the end.

## Assessment and scope

The entity architecture has useful foundations: coordinator-backed state,
stable unique IDs, inventory-aware discovery, conservative registry cleanup,
typed feature models, and explicit recorder exclusions for many diagnostic
attributes. Battery and heat-pump feature modules demonstrate a viable approach
to reducing the remaining sensor platform's scope. Weather is deliberately a
separate child coordinator with explicit config-entry lifecycle ownership.

This review covered entity bases, sensor feature modules, presentation helpers,
inventory views, number/select/switch capability decisions, calendars, weather,
and the diagnostics assembly boundary. The baseline review used static analysis and current
Home Assistant developer documentation; implementation now includes executable
Home Assistant state and recorder boundary tests. It does not establish live cloud
reliability or measured resource consumption. The parent task owns repository
validation evidence and the aggregate architectural assessment.

## Confirmed correctness findings

### E1: Daily heat-pump energy lacks reset metadata — P1

`sensor_heatpump.py:767-883` exposes daily total, grid, solar, and battery energy
as `SensorStateClass.TOTAL` without `last_reset`. The source carries `day_key` and
`timezone`; `heatpump_runtime.py:1473-1510` explicitly handles changed days and
negative daily deltas. Consequently a daily counter's midnight decrease is
interpreted as negative consumption by long-term statistics.

Home Assistant's [sensor documentation](https://developers.home-assistant.io/docs/core/entity/sensor/)
distinguishes net totals from resetting counters. Preserve `TOTAL` and derive
`last_reset` from the source day's midnight in its source timezone, converted to
a timezone-aware datetime. This preserves same-day downward corrections.
`TOTAL_INCREASING` is an alternative only after proving source values are
monotonic within each cycle; otherwise a correction can be mistaken for a reset.

The reset marker must follow the published snapshot, not wall-clock midnight:
stale yesterday data must keep yesterday's marker until today's data arrives.
Define a safe missing/invalid date policy before coding; do not pair a guessed
new-day reset with a previous-day value. Date metadata normalization belongs at
the feature-model boundary. Keep the entity ID, unit, and state class stable.

Acceptance criteria:

- Exercise all four daily entities through two local dates and compile recorder
  statistics; a midnight rollover must not subtract the prior day's consumption.
- A same-day correction keeps the reset marker and adjusts that day's total.
- Cover daylight-saving transitions, non-UTC source timezones, a restart, stale
  previous-day data, and missing/invalid day and timezone metadata.
- Verify unavailable values cannot publish a contradictory reset and value pair.
- Extend `test_sensor_additional_coverage.py` around its existing daily sensor
  tests (`:5834`, class assertion at `:5915`); the existing class assertion alone
  does not establish correct statistics behavior.
- Document that the change prevents future bad sums; it does not automatically
  rewrite statistics already stored in users' databases.

### E2: Nonfinite inverter energy poisons the last-good cache — P1

`inventory_runtime.py:3880-3895` converts cloud production and cached production
using `float()` but rejects only missing or negative values. `sensor.py:3665-3679`
repeats this incomplete validation and caches the resulting native value.
Positive infinity passes both checks and then dominates future finite readings
under the nondecreasing-value clamp. NaN also reaches publication.

Require finite, nonnegative values at normalization and at the entity boundary.
Validate the old cache as well as the incoming value so an already poisoned
cache can recover. Preserve a previous finite value when the latest sample is
invalid; publish unknown when neither is valid. Preserve legitimate monotonic
handling and restore-unit conversion. Do not introduce speculative physical
energy caps.

Acceptance criteria:

- Cover numbers and strings representing NaN, positive infinity, negative
  infinity, negative energy, malformed values, and missing values.
- Verify a valid sample after invalid input advances normally, including when
  the previous runtime or entity cache is already nonfinite.
- Test the actual inventory-normalization-to-lifetime-entity path, not only a
  standalone numeric helper; preserve ordinary finite decreases and restoration.

### E3: Inverter cache uses reusable integer object identities — P2

`sensor.py:3635-3655` caches an inverter snapshot using
`(id(coordinator.data), id(coordinator._inverter_data))` without retaining those
source objects. Once sources are released, Python may reuse their IDs for new
maps. A cache hit can then retain a missing or stale snapshot.

The same failure class is already addressed in `BatterySensorModel.snapshot`,
`sensor_battery.py:90-116`, whose comment explains why it retains source objects.
Use source-reference identity checks for the inverter model too, or a published
revision with a clear invalidation contract. Prefer the established battery
approach for the smallest correction. Do not change publication or polling.

Acceptance criteria:

- Empty startup maps becoming populated invalidate a cached missing snapshot.
- Replacing either source invalidates; unchanged sources reuse the cached result.
- Cover missing snapshots, removal, successive replacements, and one getter call
  per source version across availability/value/attribute reads.
- Tests must not depend on probabilistic allocator reuse; verify the source
  ownership contract deterministically.

## Performance and maintainability findings

### E4: Recorder exclusions miss duplicated changing metadata — P2

`sensor_heatpump.py:798-831` attaches detail lists, source timestamps, and all
sibling daily energy counters to each daily sensor. Its inherited exclusions
cover cloud timestamps but omit `sampled_at_utc`, endpoint timestamps, and
`details`. `sensor.py:3529-3560` also attaches inverter `sampled_at` maps without
recorder exclusions. These fields can create additional distinct attribute
records even when their historical values are not useful.

Add narrowly chosen `_unrecorded_attributes`, preserving live state attributes
for dashboards and automations. Retain historical measurement fields where that
history is intentionally useful. Do not remove attributes as a cleanup shortcut.
The split heat-pump sensors and inverter telemetry are disabled by default, so
the full multiplication applies only when users enable them. No numerical
database-saving estimate was measured by this audit.

Acceptance: verify live attributes are unchanged, recorder omits the designated
metadata, value/statistics recording continues, and changing only excluded
metadata does not create new persisted attribute payloads.

### E5: Reachable inventory behavior has blanket coverage exclusions — P2

`inventory_view.py` contains 15 `pragma: no cover` occurrences, including complete
production methods: `has_type:91`, `has_type_for_entities:104`, `type_bucket:138`,
and `type_device_info:775`. These are important discovery/identity boundaries.
An aggregate coverage percentage does not demonstrate their behavior.

Remove whole-method exclusions incrementally after adding meaningful tests for
selected/unselected categories, startup uncertainty, confirmed absence, fallback
battery/heat-pump discovery, malformed inventory, defensive snapshot copying,
and stable device identifiers. Keep justified typing-only exclusions. Coordinate
this work with the repository-wide test architecture owner.

### E6: Shared capability logic and presentation parsers are duplicated — P3

Candidate removals/consolidations verified in the baseline:

| Existing copies | Proposed owner and constraint |
| --- | --- |
| `select.py:145` `_battery_write_access_confirmed` | Remove: no production caller was found; only tests exercise it. Remove its implementation-only tests while retaining behavior tests. |
| `number.py:54`, `switch.py:199` battery permission confirmation; repeated explicit-denial helpers | Coordinator/runtime capability boundary, already represented by `coordinator.py:6780`. Preserve unknown versus explicit denial; do not infer production requirements from permissive test doubles. |
| Battery schedule API method capability checks in number/select/time and related platform setup | One capability helper, while preserving each platform's additional inventory and option gates. |
| `sensor.py:3713` `_gateway_optional_bool`, `parsing_helpers.py:36` `coerce_optional_bool` | Use the existing parsing helper; these vocabularies and behavior match. |
| `sensor.py:2751,2762` and `number.py:418,429` `_safe_limit_active`/`_charging_active` | Shared EVSE semantic helpers, retaining distinct names and their distinct accepted vocabularies. |
| `sensor_heatpump.py:90` and `inventory_runtime.py:1317` `_heatpump_worst_status_text` | A pure heat-pump status helper usable by runtime and presentation without a platform import. Preserve severity ordering and displayed labels. |

Keep shared helpers below platforms in the dependency graph. Retain compatibility
aliases only where existing imports require them. Do not combine boolean parsers
merely because they return booleans: protocol-specific meanings can differ.
Verify permissions through entity availability and outbound write behavior, plus
table-driven parser cases and heat-pump severity precedence. Remove duplicate
helper-internal tests when equivalent behavior coverage already exists.

### E7: Sensor platform still combines unrelated responsibilities — P3

The baseline `sensor.py` has 8,628 lines: discovery, registry migration, inverter
entities, gateway parsing, tariff models, and complex cumulative-energy-to-power
estimation. `sensor_battery.py` and `sensor_heatpump.py` establish a suitable
feature-module pattern.

Extract inverter, gateway, tariff, and site-energy families as separate bounded
changes. Keep platform setup and discovery orchestration in `sensor.py`. Move
power estimation state transitions into a dedicated testable model, with explicit
restore data. Preserve entity imports via compatibility aliases as required.
Avoid circular imports or introducing cloud-client access into entities.

Acceptance: preserve unique IDs, translation keys, device assignments, restore
formats and units, stale-state behavior, topology discovery, command behavior,
and event-loop import safety. Run behavior tests for each moved family and the
full import-time check. Do correctness changes before moves so diffs remain
reviewable and regressions remain attributable.

## Follow-ups requiring policy decisions or profiling

- **Availability:** `sensor_base.py:48-55` considers site sensors available after
  any historical core success even during later coordinator failure. For
  example, aggregate battery charge (`sensor.py:7673-7690`) uses that policy and
  returns no explanatory attributes. Define family-specific freshness contracts
  for live measurements, cumulative totals, and diagnostics before changing
  behavior. This is a policy gap, not proof all preserved data is incorrect.
- **Inventory metadata cost:** `InventoryView.type_device_info:775-811` invokes
  several summaries. `_type_bucket_members:203-210` copies members already copied
  by `type_bucket`. Profile large inventories, then consider topology-revision
  metadata caching or eliminating duplicate copying. No measured bottleneck is
  claimed here.
- **Weather error handling:** `weather.py:182,189` wraps raw exception strings in
  `UpdateFailed`; redact before the Home Assistant logging boundary, as later
  diagnostic redaction cannot protect logs already emitted. Handle unsupported
  responses after discovery as expected failures. Consider `always_update=False`
  for the immutable weather payload. Cover available-to-404 transitions,
  repeated identical payloads, sanitized error output, and discovery health.
- **Restore contracts:** complex power sensors use custom `RestoreEntity`
  handling. Preserve native-unit extra data during extraction and verify a user
  display-unit override cannot be restored as if it were native watts.

## Assigned work and implementation order

The `entity_review` subagent owns this implementation-ready plan. Assignments
below are queued packages, not claims that source changes have been executed.

| Order | Package | Files owned | Dependencies |
| --- | --- | --- | --- |
| 1 | E1 daily reset semantics | `sensor_heatpump.py`, relevant sensor/recorder tests; narrowly scoped heat-pump model normalization if needed | Decide invalid metadata fallback; coordinate any runtime file edit with runtime owner. |
| 2 | E2/E3 inverter numerical and cache correctness | Inverter sections of `sensor.py`, production normalization in `inventory_runtime.py`, relevant tests | Reserve runtime file edit window; preserve existing finite monotonic contract. |
| 3 | E4 recorder metadata | Daily heat-pump and inverter entity classes and recorder tests | Follow correctness tests so state contracts are settled. |
| 4 | E6 redundant helpers | `parsing_helpers.py`, semantic helper modules, narrow sensor/number/select/switch/runtime call sites | Coordinate shared helper and runtime ownership; no parallel edits to the same file. |
| 5 | E5 inventory coverage | `inventory_view.py`, inventory/entity tests | Test architecture owner reviews exclusions and acceptance coverage. |
| 6 | E7 feature decomposition | New sensor feature/model modules, `sensor.py`, tests, architecture docs | Only after earlier packages; one family per change. |
| Follow-up | E8/A24 bounded inverter pagination | Inventory runtime/parser and completeness tests | Entity agent owns inventory changes; transport agent coordinates edits in `api.py` after agreeing the shared result contract. |
| Follow-up | Freshness, metadata profiling, weather, restore review | Feature-specific files reserved when a concrete design is approved | Gather evidence/define contracts before behavior changes. |

The parent task owns changelog aggregation, repository-wide documentation links,
and coordination with lifecycle/transport reviewers. No subagent should
independently edit those shared files or publish a PR for these queued packages.

### E8 / A24: Bounded inverter pagination and explicit completeness — P2

This assigned follow-up corresponds to transport finding T6 and aggregate finding
A24. The baseline has separate pagination implementations in `api.py:2027`
(config-flow discovery) and `inventory_runtime.py:3621,3674` (runtime inventory).
Both extend the expected total from server responses without a local overall
page/item bound. The config-flow implementation accepts nested
`result.inverters`, while the runtime implementation expects root `inverters`.
Later runtime page requests sit outside the first-page failure/cache handling
block. Repeated rows can advance offsets without representing discovery progress.
These are source-supported reliability and duplication concerns; this audit did
not reproduce a live Enphase pagination failure.

The entity agent owns inventory behavior and tests. The transport agent owns API
edit coordination; agree the shared parser/paginator interface before editing
either implementation. Keep the reusable implementation below platform and
runtime modules, with an injected async page fetcher. Return items plus an
explicit complete/partial result and a bounded reason for incompleteness.
Normalize root and wrapped response shapes consistently. Define a finite page or
item budget, detect repeated/no-progress pages, and retain cancellation behavior.
Avoid suppressing authentication failures inside the reusable paginator; apply
the existing best-effort config-flow policy only at that boundary.

Only complete authoritative discovery should permit removals based on absence.
Partial results may enrich existing devices conservatively but must not replace
the last complete inventory as if it were authoritative. Preserve cached complete
inventory on later-page failures and integrate endpoint health/backoff handling
across the whole operation. Do not make a partial discovery look like a complete
empty site. Preserve deterministic ordering and deduplicate by the established
inventory identity rather than by a display name.

Acceptance criteria:

- Root and nested payloads produce the same normalized multi-page inventory.
- Ordinary pagination completes within the budget without extra requests.
- Repeated pages, duplicate-only pages, empty pages before the advertised total,
  malformed rows/totals, and unreasonable or increasing totals terminate safely.
- A second-page failure preserves the last complete cache and records endpoint
  failure consistently; a complete later retry clears partial state correctly.
- Cancellation propagates promptly and does not publish a partial authoritative
  inventory or leave tasks running.
- Entity discovery/registry tests prove partial listings do not authorize device
  or entity deletion, while complete authoritative absence still can.
- Config-flow tests preserve its user-facing best-effort behavior, independently
  of the runtime's failure/backoff policy.

## Implementation and critical-review results

The assigned runtime changes are applied in this checkout:

- A05: all four daily heat-pump energy entities retain `TOTAL`, publish source-day
  timezone-aware `last_reset`, and withhold invalid-date or nonfinite samples.
  The real Home Assistant statistics compiler verifies rollover, same-day
  correction, and source-timezone DST handling.
- A06/A09: inverter energy rejects nonfinite cloud and cached values and recovers
  on the next finite sample. Lifetime lookup caches retain source objects and use
  identity comparison, preventing allocator-ID reuse from returning stale data.
- A10: heat-pump detail arrays, redundant daily readings, and sampling metadata,
  plus inverter sampling metadata, remain in live attributes and are excluded
  from recorder. Tests register entities with a real `EntityComponent` and check
  actual recorder attribute serialization across metadata-only changes.
- A14/A22: inventory gating and identity helpers are covered without whole-method
  coverage exclusions. Inventory members are detached once at the public
  `type_bucket` boundary, preserving public overrides and mutation isolation.
  Critical review also fixed a gateway firmware fallback that incorrectly used
  identity comparison on defensive copies.
- A15: weather unsupported responses use an expected coordinator failure class;
  exception text is redacted before reaching Home Assistant logs. Unchanged
  normalized weather results suppress redundant publication.
- A16: instantaneous site power, battery state of charge, and stored energy expire
  under explicit family policies. Core outages use 15 minutes; optional battery
  and heat-pump readings use their source success time and 30 minutes. Cumulative
  energy and diagnostics retain their existing cached policy. A listener-owned
  timer publishes expiry even without another poll, follows source timestamps
  advanced by unchanged polls, and cancels on entity removal. Missing source
  success uses one initial observation rather than unrelated successful core
  polls. Actual HA states cover expiry, recovery, and silent source advancement.
- A17: inventory and heat-pump runtimes accept explicit state injection while
  preserving legacy coordinator adapters. Production descriptor writes update
  the shared state once. Root owns coordinator construction and state aliases.
- A20/A21: sensor orchestration now imports `sensor_gateway.py`,
  `sensor_inverter.py`, `sensor_site_energy.py`, `sensor_tariff.py`, and pure
  `sensor_common.py`. Existing imports are preserved through explicit reexports.
  Shared scalar, EVSE flag, role-access, schedule capability, heat-pump status,
  optional sum, HEMS inventory, and model-summary helpers remove exact semantic
  duplicates. Distinct boolean vocabularies remain distinct.
- A24: `inverter_inventory.py` supplies one bounded paginator to runtime and API
  discovery, with root/wrapped normalization, metadata preservation, deduplication,
  no-progress detection, cancellation propagation, and explicit completeness.
  The runtime retains the last complete cache when any page fails or required
  serial identities are missing; only complete results authorize removal.
- A25: restored power favors native extra data and converts legacy display-state
  units to watts, preventing user-selected kW display values from contaminating
  native power estimates.

Validation uses the pinned `ha-dev` Docker environment. New regression coverage
is in `test_entity_architecture.py` and `test_inventory_view_contracts.py`, alongside
updated feature tests. Existing assertions were retained except where the new
explicit complete-inventory policy requires preserving a prior cache instead of
publishing partial discovery. Feature extraction tests target the module owning
the implementation; constructor-bypassing legacy fixtures initialize coordinator
health state explicitly. The parent report records final combined test, coverage,
formatting, typing, and repository gate results. These tests establish deterministic
local contracts, not live Enphase availability or measured production performance.
