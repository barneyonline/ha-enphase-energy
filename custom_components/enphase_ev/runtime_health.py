"""Narrow endpoint-health services shared by feature runtimes.

The legacy adapter is confined to this boundary while downstream lightweight
hosts migrate to the public coordinator API. Feature code never chooses private
coordinator methods itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, cast


class RuntimeHealthHost(Protocol):
    """Endpoint admission and outcome contract required by feature runtimes."""

    def endpoint_family_should_run(
        self, family: str, *, force: bool = False
    ) -> bool: ...

    def note_endpoint_family_success(
        self, family: str, *, success_ttl_s: float | None = None
    ) -> None: ...

    def note_endpoint_family_failure(self, family: str, err: Exception) -> bool: ...


class _LegacyRuntimeServices:
    """Confine compatibility method resolution to the host boundary."""

    def __init__(self, host: object) -> None:
        self._host = host

    def _resolve(self, name: str) -> Callable[..., Any]:
        public = getattr(self._host, name, None)
        if callable(public):
            return cast(Callable[..., Any], public)
        return cast(Callable[..., Any], getattr(self._host, f"_{name}"))


class RuntimeHealthServices(_LegacyRuntimeServices):
    """Use public endpoint services with a compatibility adapter for older hosts."""

    def __init__(self, host: RuntimeHealthHost) -> None:
        super().__init__(host)

    def endpoint_family_should_run(self, family: str, *, force: bool = False) -> bool:
        return bool(self._resolve("endpoint_family_should_run")(family, force=force))

    def note_endpoint_family_success(
        self, family: str, *, success_ttl_s: float | None = None
    ) -> None:
        self._resolve("note_endpoint_family_success")(
            family, success_ttl_s=success_ttl_s
        )

    def note_endpoint_family_failure(self, family: str, err: Exception) -> bool:
        return bool(self._resolve("note_endpoint_family_failure")(family, err))


class HemsAuthHost(Protocol):
    """Shared authentication circuit contract required by HEMS runtimes."""

    def skip_hems_polling_due_to_auth_circuit(self, *, endpoint: str) -> bool: ...

    def note_hems_auth_failure(self, err: Exception, *, endpoint: str) -> bool: ...

    def note_hems_auth_success(self, *, endpoint: str | None = None) -> None: ...


class RuntimeAuthServices(_LegacyRuntimeServices):
    """Use public auth circuit services with the same bounded legacy adapter."""

    def __init__(self, host: HemsAuthHost) -> None:
        super().__init__(host)

    def skip_hems_polling_due_to_auth_circuit(self, *, endpoint: str) -> bool:
        return bool(
            self._resolve("skip_hems_polling_due_to_auth_circuit")(endpoint=endpoint)
        )

    def note_hems_auth_failure(self, err: Exception, *, endpoint: str) -> bool:
        return bool(self._resolve("note_hems_auth_failure")(err, endpoint=endpoint))

    def note_hems_auth_success(self, *, endpoint: str | None = None) -> None:
        self._resolve("note_hems_auth_success")(endpoint=endpoint)
