"""Public endpoint-service contracts and the bounded legacy host adapter."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.enphase_ev.runtime_health import (
    RuntimeAuthServices,
    RuntimeHealthServices,
)


@pytest.mark.parametrize("legacy", [False, True])
def test_health_services_route_all_outcomes_without_changing_arguments(legacy):
    should_run = Mock(return_value=True)
    success = Mock()
    failure = Mock(return_value=False)
    prefix = "_" if legacy else ""
    host = SimpleNamespace(
        **{
            f"{prefix}endpoint_family_should_run": should_run,
            f"{prefix}note_endpoint_family_success": success,
            f"{prefix}note_endpoint_family_failure": failure,
        }
    )
    services = RuntimeHealthServices(host)
    assert services.endpoint_family_should_run("battery_status", force=True)
    should_run.assert_called_once_with("battery_status", force=True)
    services.note_endpoint_family_success("battery_status", success_ttl_s=30)
    success.assert_called_once_with("battery_status", success_ttl_s=30)
    error = ValueError("invalid normalized payload")
    assert not services.note_endpoint_family_failure("battery_status", error)
    failure.assert_called_once_with("battery_status", error)


def test_public_health_contract_takes_precedence_over_legacy_compatibility():
    public = Mock(return_value=True)
    private = Mock(side_effect=AssertionError("private host method used"))
    services = RuntimeHealthServices(
        SimpleNamespace(
            endpoint_family_should_run=public,
            _endpoint_family_should_run=private,
        )
    )
    assert services.endpoint_family_should_run("hems_inventory")
    private.assert_not_called()


@pytest.mark.parametrize("legacy", [False, True])
def test_hems_auth_services_preserve_shared_circuit_semantics(legacy):
    skip = Mock(return_value=False)
    failure = Mock(return_value=True)
    success = Mock()
    prefix = "_" if legacy else ""
    services = RuntimeAuthServices(
        SimpleNamespace(
            **{
                f"{prefix}skip_hems_polling_due_to_auth_circuit": skip,
                f"{prefix}note_hems_auth_failure": failure,
                f"{prefix}note_hems_auth_success": success,
            }
        )
    )
    assert not services.skip_hems_polling_due_to_auth_circuit(endpoint="hems_devices")
    skip.assert_called_once_with(endpoint="hems_devices")
    error = ValueError("rejected credentials")
    assert services.note_hems_auth_failure(error, endpoint="hems_devices")
    failure.assert_called_once_with(error, endpoint="hems_devices")
    services.note_hems_auth_success(endpoint="hems_devices")
    success.assert_called_once_with(endpoint="hems_devices")
