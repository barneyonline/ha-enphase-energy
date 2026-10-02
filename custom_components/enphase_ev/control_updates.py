"""Confirmed control values and independent command progress.

Only successful read endpoints may call observe. Command responses and optimistic
runtime caches are deliberately excluded from confirmation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps
from inspect import signature
import time
from typing import Any, ParamSpec, TypeVar, cast

from homeassistant.core import callback as ha_callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import async_call_later

from .const import DEFAULT_CHARGE_LEVEL_SETTING, DOMAIN

CONFIRMATION_WINDOW_S = 600.0
_P = ParamSpec("_P")
_R = TypeVar("_R")
_V = TypeVar("_V")
_CallbackT = TypeVar("_CallbackT", bound=Callable[..., object])
callback = cast(Callable[[_CallbackT], _CallbackT], ha_callback)
_active_command: ContextVar[tuple[object, object, str, str | None] | None] = ContextVar(
    "enphase_control_command", default=None
)


@dataclass
class ControlUpdate:
    group: str
    requested: dict[str, object]
    started: float
    status: str = "pending"
    submitting: bool = True
    observed: dict[str, object] | None = None
    cancel_timeout: Callable[[], None] | None = None
    read_token: object = field(default_factory=object)


class ControlUpdates:
    """Own bounded per-control progress without changing device availability."""

    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator
        self.updates: dict[tuple[str, str | None], ControlUpdate] = {}
        self.values: dict[tuple[str, str | None], dict[str, object]] = {}
        self.held_by: dict[tuple[str, str | None], ControlUpdate] = {}

    def begin(
        self,
        control: str,
        serial: str | None,
        requested: dict[str, object],
        confirmed: dict[str, object],
        *,
        group: str | None = None,
        supersede: bool = False,
    ) -> ControlUpdate:
        group_key = group or control
        for (_other_control, other_serial), update in self.updates.items():
            if other_serial != serial or update.group != group_key:
                continue
            if not supersede and (update.submitting or update.status == "pending"):
                raise ServiceValidationError(
                    "A control change is awaiting confirmation. Refresh its status before submitting another change.",
                    translation_domain=DOMAIN,
                    translation_key="control_change_pending",
                )
        key = (control, serial)
        previous = self.held_by.get(key) or self.updates.get(key)
        for other_key, older in self.updates.items():
            if (
                other_key != key
                and other_key[1] == serial
                and older.group == group_key
                and older.status != "confirmed"
            ):
                older.status = "cancelled"
                self._cancel_timeout(older)
        held = self.held_by.pop(key, None)
        if previous and previous.cancel_timeout:
            previous.cancel_timeout()
        if previous is None or (previous.status == "confirmed" and held is None):
            self.values[key] = deepcopy(confirmed)
        update = ControlUpdate(group_key, deepcopy(requested), time.monotonic())
        self.updates[key] = update

        @callback
        def expire(_now: object) -> None:
            if self.updates.get(key) is update and update.status == "pending":
                update.status = "unconfirmed"
                update.cancel_timeout = None
                self.publish()

        update.cancel_timeout = async_call_later(
            self.coordinator.hass, CONFIRMATION_WINDOW_S, expire
        )
        self.publish()
        return update

    def finish(
        self,
        key: tuple[str, str | None],
        update: ControlUpdate,
        *,
        failed: bool = False,
    ) -> None:
        if self.updates.get(key) is not update:
            return
        update.submitting = False
        if failed:
            update.status = "failed"
            self._cancel_timeout(update)
        elif update.observed is not None and self._matches(update):
            update.status = "confirmed"
            self._cancel_timeout(update)
        self.publish()

    @staticmethod
    def _matches(update: ControlUpdate) -> bool:
        from .control_values import matches_requested

        return matches_requested(update.requested, update.observed or {})

    @staticmethod
    def _cancel_timeout(update: ControlUpdate) -> None:
        if update.cancel_timeout:
            update.cancel_timeout()
            update.cancel_timeout = None

    def read_tokens(self) -> dict[tuple[str, str | None], object]:
        return {
            key: update.read_token
            for key, update in {**self.updates, **self.held_by}.items()
        }

    def observe(
        self,
        control: str,
        serial: str | None,
        values: dict[str, object],
        tokens: Mapping[tuple[str, str | None], object],
    ) -> None:
        key = (control, serial)
        update = self.updates.get(key)
        owner = self.held_by.get(key) or update
        if (owner.read_token if owner else None) is not tokens.get(key):
            return
        self.values.setdefault(key, {}).update(deepcopy(values))
        if (
            update is None
            or update is not owner
            or update.status in {"failed", "cancelled"}
        ):
            return
        update.observed = {**(update.observed or {}), **deepcopy(values)}
        if not update.submitting and self._matches(update):
            update.status = "confirmed"
            self._cancel_timeout(update)
            self.publish()

    def value(
        self, control: str, field: str, fallback: Any, serial: str | None = None
    ) -> Any:
        key = (control, serial)
        update = self.held_by.get(key) or self.updates.get(key)
        if update is None or (update.status == "confirmed" and key not in self.held_by):
            return fallback
        return self.values.get((control, serial), {}).get(field, fallback)

    def hold_related(
        self,
        control: str,
        serial: str | None,
        update: ControlUpdate,
        confirmed: dict[str, object],
    ) -> None:
        key = (control, serial)
        previous = self.held_by.get(key) or self.updates.get(key)
        if previous is None or (
            previous.status == "confirmed" and key not in self.held_by
        ):
            self.values[key] = deepcopy(confirmed)
        self.held_by[key] = update

    def set_requested(
        self, control: str, requested: dict[str, object], serial: str | None = None
    ) -> None:
        update = self.updates.get((control, serial))
        if update:
            update.requested = deepcopy(requested)
            self.invalidate_observations(control, serial)

    def invalidate_observations(self, control: str, serial: str | None = None) -> None:
        """Require reads started after the latest command acknowledgement."""
        update = self.updates.get((control, serial))
        if update:
            update.observed = None
            update.read_token = object()

    def pending(self, control: str, serial: str | None = None) -> bool:
        update = self.updates.get((control, serial))
        return update is not None and (
            update.submitting or update.status in {"pending", "unconfirmed"}
        )

    def attributes(self, serial: str | None = None) -> dict[str, object]:
        return {
            control: {
                "status": update.status,
                "requested": deepcopy(update.requested),
                "confirmed": deepcopy(self.values.get((control, serial), {})),
            }
            for (control, device), update in self.updates.items()
            if device == serial
        }

    def status(self, serial: str | None = None) -> str:
        statuses = {
            update.status
            for (_control, device), update in self.updates.items()
            if device == serial
        }
        for state in ("pending", "unconfirmed", "failed"):
            if state in statuses:
                return state
        return "idle"

    def publish(self) -> None:
        self.coordinator.publish_runtime_state_update("control_updates")

    def cancel_group(self, group: str, serial: str | None = None) -> None:
        for (_control, device), update in self.updates.items():
            if device == serial and update.group == group:
                update.status = "cancelled"
                update.submitting = False
                self._cancel_timeout(update)
        self.publish()

    def prune(self, serials: set[str]) -> None:
        for key in list(self.updates):
            if key[1] is not None and key[1] not in serials:
                self._cancel_timeout(self.updates.pop(key))
                self.values.pop(key, None)
        for key in list(self.held_by):
            if key[1] is not None and key[1] not in serials:
                self.held_by.pop(key)
        for key in list(self.values):
            if key[1] is not None and key[1] not in serials:
                self.values.pop(key)

    def cleanup(self) -> None:
        for update in self.updates.values():
            self._cancel_timeout(update)
        self.updates.clear()
        self.values.clear()
        self.held_by.clear()


def current_control_value(
    coord: Any, control: str, field: str, fallback: _V, serial: str | None = None
) -> _V:
    runtime = getattr(coord, "control_updates", None)
    return (
        cast(_V, runtime.value(control, field, fallback, serial))
        if runtime
        else fallback
    )


def tracked_control(
    control: str,
    *,
    arguments: tuple[str, ...],
    serial_argument: str | None = None,
    group: str | None = None,
    supersede: bool = False,
    fixed_enabled: bool | None = None,
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Track runtime commands at the boundary shared by entities and services."""

    def decorate(func: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
        sig = signature(func)

        @wraps(func)
        async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            from .control_values import (
                RELATED_CONTROLS,
                confirmed_control_values,
                requested_control_values,
            )

            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            host = args[0]
            coord = getattr(host, "coordinator", None) or getattr(
                host, "_coordinator", None
            )
            runtime = getattr(coord, "control_updates", None)
            serial = str(bound.arguments[serial_argument]) if serial_argument else None
            if (
                serial is not None
                and getattr(coord, "evse_status_available", True) is False
                and not (control == "charging" and fixed_enabled is False)
            ):
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="charger_status_unavailable",
                )
            group_key = group or control
            active = _active_command.get()
            task = asyncio.current_task()
            if (
                runtime is None
                or not getattr(coord, "runtime_active", True)
                or active == (runtime, task, group_key, serial)
            ):
                return await func(*args, **kwargs)
            requested = requested_control_values(
                control, {key: bound.arguments[key] for key in arguments}, coord
            )
            if fixed_enabled is not None:
                requested["enabled"] = fixed_enabled
            update = runtime.begin(
                control,
                serial,
                requested,
                confirmed_control_values(coord, control, serial),
                group=group,
                supersede=supersede,
            )
            for related in RELATED_CONTROLS.get(group_key, ()):
                runtime.hold_related(
                    related,
                    serial,
                    update,
                    confirmed_control_values(coord, related, serial),
                )
            token = _active_command.set((runtime, task, group_key, serial))
            try:
                result = await func(*args, **kwargs)
            except BaseException:
                runtime.finish((control, serial), update, failed=True)
                raise
            else:
                rejected = result is False or (
                    isinstance(result, dict) and result.get("status") == "not_ready"
                )
                runtime.finish((control, serial), update, failed=rejected)
                return result
            finally:
                _active_command.reset(token)

        return wrapped

    return decorate


def control_readback(
    family: str,
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Observe only refreshes that recorded a new endpoint success."""

    def decorate(func: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
        sig = signature(func)

        @wraps(func)
        async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            from .control_values import (
                EVSE_CACHES,
                confirmed_control_values,
                observe_control_family,
            )

            host = args[0]
            coord = getattr(host, "coordinator", None) or getattr(
                host, "_coordinator", None
            )
            runtime = getattr(coord, "control_updates", None)
            health = getattr(coord, "_endpoint_family_health", {}).get(family)
            stamp = getattr(health, "request_count", 0)
            tokens = runtime.read_tokens() if runtime else {}
            evse_control = (
                family.removeprefix("evse:") if family.startswith("evse:") else None
            )
            serial = (
                str(sig.bind(*args, **kwargs).arguments["sn"]) if evse_control else None
            )
            cache = getattr(
                getattr(coord, "evse_state", None),
                EVSE_CACHES.get(evse_control or "", ""),
                {},
            )
            before = cache.get(serial)
            result = await func(*args, **kwargs)
            health = getattr(coord, "_endpoint_family_health", {}).get(family)
            if (
                runtime
                and evse_control
                and cache.get(serial) is not before
                and (
                    evse_control != "default_charge_level"
                    or (
                        isinstance(result, dict)
                        and DEFAULT_CHARGE_LEVEL_SETTING in result
                    )
                )
            ):
                runtime.observe(
                    evse_control,
                    serial,
                    confirmed_control_values(coord, evse_control, serial),
                    tokens,
                )
            if (
                runtime
                and not evse_control
                and getattr(health, "request_count", 0) > stamp
                and getattr(health, "consecutive_failures", 0) == 0
            ):
                observe_control_family(coord, family, tokens)
            return result

        return wrapped

    return decorate
