"""Tests for payload_debug helpers."""

from __future__ import annotations

from custom_components.enphase_ev.payload_debug import (
    debug_field_keys,
    debug_payload_shape,
    debug_render_summary,
    debug_sorted_keys,
)


def test_debug_sorted_keys_non_dict() -> None:
    assert debug_sorted_keys([]) == []


def test_debug_field_keys_non_list() -> None:
    assert debug_field_keys({}) == []


def test_debug_payload_shape_variants() -> None:
    assert debug_payload_shape(None)["kind"] == "none"
    shape = debug_payload_shape({"data": [{"a": 1}]})
    assert shape["kind"] == "dict"
    assert "data_length" in shape
    nested_dict = debug_payload_shape({"data": {"x": 1}})
    assert "data_keys" in nested_dict
    assert debug_payload_shape([{"b": 2}])["kind"] == "list"
    assert debug_payload_shape(42)["kind"] == "int"


def test_debug_render_summary_non_serializable_value() -> None:
    text = debug_render_summary({"k": object()})
    assert isinstance(text, str)
    assert len(text) > 0


def test_payload_shape_omits_empty_field_keys():
    from custom_components.enphase_ev.payload_debug import debug_payload_shape

    assert debug_payload_shape({"result": [{" ": "private"}, None]}) == {
        "kind": "dict",
        "keys": ["result"],
        "result_length": 2,
    }
