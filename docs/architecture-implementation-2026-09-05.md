# Architecture implementation and critical review

This records the implementation of the [25-item architectural review](architecture-review-2026-09-05.md). The original review is a historical baseline; this document and [the contributor architecture map](architecture.md) describe the resulting changes.

## Change and evidence ledger

| Findings | Implemented change | Behavioral evidence |
| --- | --- | --- |
| A01–A02 | Separate authentication synchronization from polling; rebuild every endpoint's authentication headers after credential rotation. | `test_transport_publication_contracts.py` exercises a real status request returning 401 during the poll lock, successful login, fresh headers, concurrent waiters, and cancellation. Existing API tests retain endpoint-specific retry contracts. |
| A03 | Merge startup enrichment into current charger state after awaits; preserve concurrent commands, polls, and removals. | Interleaved warmup and publication tests in `test_transport_publication_contracts.py`. |
| A04 | Transfer detached discovery and charger data across reload; construct new coordinator, session, managers, and tasks. | `test_entry_lifecycle.py` uses Home Assistant's actual setup/reload callbacks and checks old-session shutdown, immediate cached data, and continued polling. `test_reload_snapshot.py` covers selection and site boundaries. |
| A05 | Daily heat-pump totals expose timezone-aware source-day reset metadata and reject unavailable reset metadata. | `test_entity_architecture.py` invokes the recorder statistics compiler for all four daily sensors across midnight and corrections; reset tests cover source dates and timezones. |
| A06 | Reject nonfinite inverter lifetime samples and recover poisoned last-good caches. | Runtime and entity regressions in `test_entity_architecture.py` and inventory tests. |
| A07–A08 | Roll back failed/cancelled setup, keep domain actions registered after unload, and require loaded runtime data for action routing. | Actual failed-setup/retry/cancellation tests in `test_entry_lifecycle.py`; service and routing tests cover unloaded and explicit targets. |
| A09–A10 | Retain inverter cache sources, remove redundant copies, and exclude volatile diagnostic attributes from recorder serialization. | `test_entity_architecture.py` covers identity invalidation, defensive copies, and recorder serialization. |
| A11 | Exclude charger acquisition time from equality; include auth, controls, health, and semantic battery/heat-pump/inventory/site-energy/tariff/event state. Reuse unchanged immutable family snapshots. | `test_feature_snapshot.py` verifies actual coordinator listeners publish family-only changes, suppress unchanged polls, and detect nested mutation. Transport publication tests cover command/auth/health changes. |
| A12 | Add regressions through framework, HTTP, concurrent publication, and recorder boundaries. | The tests above supplement existing parser/unit coverage instead of bypassing the failing lifecycle paths. |
| A13 | Pin default HA 2026.9.0 and compatible plugin/tool dependencies; resolve independent transitive constraints for default, HA 2026.8.3, and minimum HA 2026.6.0 lanes. | Version-policy tests, checked-in requirements/constraints, CI matrix, and Docker validation described below. |
| A14 | Remove blanket exclusions from reachable InventoryView logic; replace dynamic payload aliases with typed containers and add narrow runtime/client protocols. | Inventory behavior/edge tests, targeted coverage, and strict package mypy. Existing compatibility projections remain where callers still require them. |
| A15 | Redact expected weather failures and handle an endpoint becoming unsupported after discovery. | Weather regressions in `test_entity_architecture.py`. |
| A16 | Bound instantaneous site/family freshness and schedule entity-owned expiry callbacks; retain cumulative measurements. | Actual HA state expiry without another poll, optional-family expiry during healthy core polling, recovery/removal cancellation, and historical-total tests. |
| A17 | Move auth and EVSE state to runtime ownership; inject battery, heat-pump, and inventory state explicitly; publish detached snapshots while preserving existing facade access. | `test_evse_state.py`, feature/transport publication tests, and existing coordinator compatibility tests. |
| A18 | Extract request/header, BatteryConfig, EVSE/scheduler, dashboard/history, Activation, MQTT, error, and shared parsing surfaces from the API facade. | Existing HTTP/parser suites plus cross-boundary authentication tests; public facade methods and error exports remain compatible. |
| A19 | Separate registry migrations/reconciliation, options, config-selection/discovery policies, and service routing. | Existing configuration, registry, action, and lifecycle suites exercise the extracted modules. |
| A20 | Extract gateway, inverter, site-energy, and tariff sensor models; consolidate presentation and capability checks. | Existing entity/platform and tariff suites plus architecture regressions. |
| A21 | Share identical scalar, optional-sum, schedule formatting/day, inventory grouping, and redaction helpers; remove dead capability helpers. | `test_scalar_helpers.py`, schedule editor parity, helper, and action tests retain distinct vocabularies and family defaults. |
| A22 | Remove the second recursive charger freeze and repeated defensive inventory copies. Add a reproducible synthetic construction benchmark. | Snapshot immutability tests and `scripts/benchmark_snapshots.py`; measured scope and limits below. |
| A23 | Correct setup/reload, module ownership, endpoint contribution guidance, and minimum-version documentation; link quality claims to behavioral tests. | Architecture/contributor documentation and the repository quality-scale validator. |
| A24 | Share bounded inverter pagination with explicit completeness, repeated-page detection, and conservative retention after partial/malformed discovery. | Pagination/inventory tests assert incomplete results cannot authorize removal. |
| A25 | Restore native power values with display-unit conversion only for legacy state records. | Power restoration regressions cover W/kW display overrides and invalid restored readings. |

## Additional findings fixed during implementation review

- Queued EVSE lookup coroutines are closed when cancellation occurs before they acquire concurrency capacity; shared Future results remain supported.
- Site energy, tariff, and system-event changes also participate in equality on charger sites. Nested dataclass containers are detached so in-place updates cannot alter a previous snapshot. Grid-profile changes retain their explicit runtime publication.
- Battery schedule inventory participates in publication equality because schedule editor entities read it directly; excluding every raw-looking payload would hide these changes.
- Inventory firmware lookup no longer uses identity comparisons between defensive copies; a controller firmware fallback remains available when gateway firmware is absent.
- Explicit invalid/unloaded config-entry action targets cannot fall through to a different site's runtime.
- Stale measurement expiry runs through a timer because repeated coordinator failures may not trigger another listener callback. Missing family-success metadata gets a bounded initial grace period instead of extending on unrelated successful polls. Expiry callbacks reschedule when unchanged successful polls move the source deadline without notifying listeners.
- Queued internal config-entry data writes retain field-level deltas, so they cannot consume a simultaneous user options/topology change.
- Daily resetting totals without valid source-day metadata are unavailable rather than publishing values that recorder cannot accumulate correctly.

## Performance measurement

`scripts/benchmark_snapshots.py` reproduces the baseline double traversal and compares it with the current single traversal for deterministic nested payloads containing 1, 8, and 32 chargers. Five batches report median construction time and peak Python allocation; [the JSON output](architecture-benchmark-results.json) includes Python version and iteration counts.

This is a construction microbenchmark. It does not measure a live site's cloud latency, complete coordinator CPU consumption, or recorder database growth. The family-content comparison performs a read-only traversal to detect in-place changes; unchanged families avoid a second allocation. No whole-integration performance percentage is inferred from the charger benchmark.

## Validation

The frozen repository passed **4,259 tests** on Home Assistant **2026.9.0**. The minimum **2026.6.0** lane passed **206** targeted compatibility/lifecycle/action tests, and the previous stable **2026.8.3** lane passed **26** compatibility/lifecycle tests. All three environments passed `python -m pip check`.

The clean coverage run passed the same **4,259 tests** and reported **45,576 statements, zero missed: 100%** across all integration modules, exceeding the requirement to cover touched modules. Ruff, Black, strict mypy across 119 modules, all tracked/new-file pre-commit hooks, both quality-scale checks, the strict import-time check, and all 54 service translation tests passed. It measures statement coverage with the repository's remaining configured exclusions; this is not a claim that every possible branch or cloud failure has been simulated. No live Enphase control action, deployment, commit, push, or PR is part of this implementation.

### Docker environment

The standard Docker build ran out of space in the local Docker VM, first while installing Python packages and then while rebuilding apt dependencies. Unused BuildKit cache was reclaimed; unrelated images, containers, and volumes were preserved. Validation used isolated images based on the existing `docker-ha-dev:latest` image, with the finalized requirement/constraint pairs installed and dependency consistency verified. The minimum overlay removes inherited mypy, which is not installed by the repository's normal minimum image.

The temporary build recipes are `/tmp/enphase-architecture-validation.Dockerfile`, `/tmp/enphase-architecture-minimum.Dockerfile`, and `/tmp/enphase-architecture-previous.Dockerfile`. For example:

```bash
BUILDX_CONFIG=/tmp/enphase-architecture-buildx docker build -f /tmp/enphase-architecture-validation.Dockerfile -t enphase-architecture-ha-dev:2026.9 devtools/docker
```

The corresponding Compose overrides are `/tmp/enphase-architecture-compose.yml`, `/tmp/enphase-architecture-minimum-compose.yml`, and `/tmp/enphase-architecture-previous-compose.yml`. Each changes only `services.ha-dev.image` to its `enphase-architecture-ha-dev:2026.9`, `:2026.6`, or `:2026.8` image. On a machine with sufficient Docker capacity, the ordinary repository build commands in `CONTRIBUTING.md` create the same pinned Python dependency stacks without these temporary overlays.

### Commands

The full suite and standard checks ran in the current pinned environment:

```bash
docker compose -f devtools/docker/docker-compose.yml -f /tmp/enphase-architecture-compose.yml run --rm ha-dev bash -lc 'pytest -q'
docker compose -f devtools/docker/docker-compose.yml -f /tmp/enphase-architecture-compose.yml run --rm ha-dev bash -lc 'ruff check .'
docker compose -f devtools/docker/docker-compose.yml -f /tmp/enphase-architecture-compose.yml run --rm ha-dev bash -lc 'black custom_components/enphase_ev tests/components/enphase_ev'
docker compose -f devtools/docker/docker-compose.yml -f /tmp/enphase-architecture-compose.yml run --rm ha-dev bash -lc 'mypy --strict --ignore-missing-imports --follow-imports=skip custom_components/enphase_ev'
docker compose -f devtools/docker/docker-compose.yml -f /tmp/enphase-architecture-compose.yml run --rm ha-dev bash -lc 'python scripts/validate_quality_scale.py'
docker compose -f devtools/docker/docker-compose.yml -f /tmp/enphase-architecture-compose.yml run --rm ha-dev bash -lc 'python scripts/validate_quality_scale.py --validate-remote-brands'
docker compose -f devtools/docker/docker-compose.yml -f /tmp/enphase-architecture-compose.yml run --rm ha-dev bash -lc 'python scripts/importtime_profile.py --strict-integration-warnings --output /tmp/importtime-enphase-ev.log'
docker compose -f devtools/docker/docker-compose.yml -f /tmp/enphase-architecture-compose.yml run --rm ha-dev bash -lc 'pytest -q tests/components/enphase_ev/test_service_translations.py'
```

Coverage was collected with `COVERAGE_FILE=/tmp/architecture.coverage python -m coverage run -m pytest -q` and checked in the same container with `COVERAGE_FILE=/tmp/architecture.coverage python -m coverage report -m --include="custom_components/enphase_ev/*" --fail-under=100`.

Pre-commit used `python -m pre_commit run --all-files` in the same HA 2026.9 Compose environment, with the worktree's common Git metadata mounted read-only at its host path and `PRE_COMMIT_HOME=/tmp/pre-commit`. A second `pre_commit run --files ...` covered every newly created file returned by `git ls-files --others --exclude-standard`, since `--all-files` only includes tracked files. A test fixture was reshaped so both the development and hook versions of Black agree.

The minimum lane command was:

```bash
docker compose -f devtools/docker/docker-compose.yml -f /tmp/enphase-architecture-minimum-compose.yml run --rm ha-dev bash -lc 'python -c "from homeassistant.const import __version__; print(__version__)" && python -m pip check && pytest -q tests/components/enphase_ev/test_manifest.py tests/components/enphase_ev/test_device_action.py tests/components/enphase_ev/test_device_trigger.py tests/components/enphase_ev/test_schedule_sync.py tests/components/enphase_ev/test_entry_lifecycle.py tests/components/enphase_ev/test_reload_snapshot.py tests/components/enphase_ev/test_services.py'
```

The previous stable lane command was:

```bash
docker compose -f devtools/docker/docker-compose.yml -f /tmp/enphase-architecture-previous-compose.yml run --rm ha-dev bash -lc 'python -c "from homeassistant.const import __version__; print(__version__)" && python -m pip check && pytest -q --asyncio-mode=auto tests/compatibility/test_ha_2026_8_device_registry.py tests/components/enphase_ev/test_entry_lifecycle.py tests/components/enphase_ev/test_reload_snapshot.py tests/components/enphase_ev/test_init_module.py::test_async_setup_entry_updates_existing_device tests/components/enphase_ev/test_init_module.py::test_remove_legacy_site_device_preserves_real_devices_with_site_identifier tests/components/enphase_ev/test_init_module.py::test_remove_legacy_site_device_removes_empty_device_without_gateway tests/components/enphase_ev/test_services.py::test_services_route_evse_targets_to_owning_entry_with_site_only_entry'
```

The synthetic benchmark used `PYTHONPATH=. python scripts/benchmark_snapshots.py --iterations 500` inside the original HA 2026.8.3 `ha-dev` image; its Python 3.14.6 runtime is recorded in the output. It reports lower construction time and peak traced allocation in all three scenarios. These results apply only to the measured construction operation.
