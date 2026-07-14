# ADR 0001: Runtime State Ownership And Publication

- Status: Accepted
- Date: 2026-07-14

## Context

The integration historically grouped hundreds of coordinator fields in mutable
dataclasses and projected them back onto `EnphaseCoordinator` with descriptors.
Runtime managers then read and wrote those projected private fields. This kept
entity compatibility but obscured ownership, made update significance depend on
unchanged charger dictionaries, and coupled managers to coordinator internals.

## Decision

One `EnphaseCoordinator` remains the Home Assistant update and lifecycle
boundary for a config entry. Feature runtimes own their endpoint-family caches,
parsing state, stale-state decisions, and immutable public snapshots.

Dependencies flow in this direction:

```text
entity platform -> coordinator public API -> feature runtime -> cloud client
                                      \-> immutable integration snapshot
```

Feature runtimes may use narrow coordinator services such as endpoint-health,
authentication-circuit, task-tracking, and publication APIs. They must not add
new projected private state to the coordinator. Existing projected state is
migrated incrementally because entities, diagnostics, and tests have long-lived
compatibility contracts.

The coordinator publishes an immutable `IntegrationSnapshot` containing
normalized charger data, manager snapshots, and explicit manager revisions.
`CoordinatorData` preserves the historical dictionary interface while using the
aggregate snapshot for equality. A manager with state not otherwise represented
in the aggregate calls `publish_runtime_state_update(source)`; managers do not
call Home Assistant listeners directly.

Coordinator-owned one-time preparation runs in `_async_setup`. Config-entry
setup calls public bootstrap, warmup, cancellation, timing, and milestone APIs;
it does not manipulate coordinator lifecycle flags.

## Incremental Migration Rules

1. New feature state lives on its runtime and is exposed through a frozen,
   read-only snapshot.
2. Compatibility coordinator properties may delegate to a runtime during a
   migration, but dynamic `StateBackedAttribute` projection is not added.
3. Entity properties perform lookups and presentation only; payload parsing and
   derived domain values belong to runtimes or domain helpers.
4. Runtime-to-coordinator calls use public methods or a narrow typed protocol.
5. Each migrated family adds contract tests for snapshot immutability,
   publication, and preserved coordinator compatibility.
6. Compatibility properties and reflective runtime factories are removed once
   no supported entity, diagnostic path, or test contract depends on them.

The current-power and EVSE feature-flag families are the first migrated state
owners. Grid-profile state uses explicit runtime publication revisions. Other
families follow the same pattern without requiring a big-bang rewrite.

## Consequences

- Home Assistant listeners observe manager-only transitions even when charger
  payloads are unchanged.
- Ownership and update significance are explicit and testable.
- Existing entity IDs, coordinator data access, and diagnostics remain stable.
- Temporary compatibility properties add some duplication during migration.
- The aggregate snapshot must be extended, or a manager revision published,
  whenever a new observable runtime state family is added.
