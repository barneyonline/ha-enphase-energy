"""MQTT websocket framing and decoding for signed Enphase live telemetry.

The facade supplies the injected session and site identity; this surface never
creates or closes an HTTP session.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Callable, Iterable, cast
from urllib.parse import urlencode

import aiohttp

from ..const import BASE_URL

_LIVE_STATUS_GRID_RELAY_ENUM = {
    0: "OPER_RELAY_UNKNOWN",
    1: "OPER_RELAY_OPEN",
    2: "OPER_RELAY_CLOSED",
    3: "OPER_RELAY_OFFGRID_AC_GRID_PRESENT",
    4: "OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD",
    5: "OPER_RELAY_WAITING_TO_INITIALIZE_ON_GRID",
}


class MqttStreamSurface:
    """Cohesive live-stream transport inherited by the compatibility facade."""

    _site: str
    _s: aiohttp.ClientSession

    @staticmethod
    def _coerce_text(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _site_livestream_mqtt_username(
        self, authorizer: dict[str, object]
    ) -> str | None:
        authorizer_name = self._coerce_text(authorizer.get("aws_authorizer"))
        token_key = self._coerce_text(authorizer.get("aws_token_key"))
        token_value = self._coerce_text(authorizer.get("aws_token_value"))
        digest = self._coerce_text(authorizer.get("aws_digest"))
        if (
            authorizer_name is None
            or token_key is None
            or token_value is None
            or digest is None
        ):
            return None
        return "?" + urlencode(
            {
                "x-amz-customauthorizer-name": authorizer_name,
                token_key: token_value,
                "site-id": str(self._site),
                "x-amz-customauthorizer-signature": digest,
                "env": "production",
            }
        )

    async def _read_mqtt_websocket_payload(
        self,
        endpoint: str,
        topic: str,
        username: str,
        *,
        timeout_s: float,
    ) -> bytes:
        ws_url = f"wss://{endpoint}/mqtt"
        client_id = f"ha-enphase-ev-{uuid.uuid4().hex[:20]}"
        deadline = asyncio.get_running_loop().time() + timeout_s
        ws_connect = cast(Callable[..., Any], self._s.ws_connect)
        async with ws_connect(
            ws_url,
            protocols=("mqtt",),
            headers={"Origin": BASE_URL},
            timeout=self._mqtt_remaining_timeout(deadline),
        ) as ws:
            await ws.send_bytes(self._mqtt_connect_packet(client_id, username))
            _, connack_payload = await self._wait_for_mqtt_packet(
                ws, {0x20}, deadline=deadline
            )
            self._validate_mqtt_connack(connack_payload)
            await ws.send_bytes(self._mqtt_subscribe_packet(topic))
            _, suback_payload = await self._wait_for_mqtt_packet(
                ws, {0x90}, deadline=deadline
            )
            self._validate_mqtt_suback(suback_payload)
            packet_type, payload = await self._wait_for_mqtt_packet(
                ws, {0x30}, deadline=deadline
            )
            return self._mqtt_publish_payload(packet_type, payload)

    @staticmethod
    def _mqtt_remaining_timeout(deadline: float) -> float:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return remaining

    @staticmethod
    def _mqtt_string(value: str) -> bytes:
        data = value.encode()
        return len(data).to_bytes(2, "big") + data

    @staticmethod
    def _mqtt_remaining_length(length: int) -> bytes:
        encoded = bytearray()
        while True:
            digit = length % 128
            length //= 128
            if length > 0:
                digit |= 0x80
            encoded.append(digit)
            if length == 0:
                return bytes(encoded)

    @classmethod
    def _mqtt_packet(cls, packet_type: int, payload: bytes) -> bytes:
        return bytes([packet_type]) + cls._mqtt_remaining_length(len(payload)) + payload

    @classmethod
    def _mqtt_connect_packet(cls, client_id: str, username: str) -> bytes:
        variable_header = (
            cls._mqtt_string("MQTT")
            + b"\x04"  # MQTT 3.1.1
            + b"\x82"  # username + clean session
            + (30).to_bytes(2, "big")
        )
        payload = cls._mqtt_string(client_id) + cls._mqtt_string(username)
        return cls._mqtt_packet(0x10, variable_header + payload)

    @classmethod
    def _mqtt_subscribe_packet(cls, topic: str) -> bytes:
        payload = (1).to_bytes(2, "big") + cls._mqtt_string(topic) + b"\x00"
        return cls._mqtt_packet(0x82, payload)

    @staticmethod
    def _validate_mqtt_connack(payload: bytes) -> None:
        if len(payload) < 2:
            raise aiohttp.ClientConnectionError("MQTT CONNACK payload was incomplete")
        return_code = payload[1]
        if return_code:
            raise aiohttp.ClientConnectionError(
                f"MQTT CONNECT was rejected with return code {return_code}"
            )

    @staticmethod
    def _validate_mqtt_suback(payload: bytes) -> None:
        if len(payload) < 3:
            raise aiohttp.ClientConnectionError("MQTT SUBACK payload was incomplete")
        granted_qos = payload[2:]
        if not granted_qos or any(qos == 0x80 for qos in granted_qos):
            raise aiohttp.ClientConnectionError("MQTT subscription was rejected")

    @classmethod
    def _mqtt_packets(cls, data: bytes) -> Iterable[tuple[int, bytes]]:
        offset = 0
        data_len = len(data)
        while offset + 2 <= data_len:
            packet_type = data[offset]
            offset += 1
            multiplier = 1
            remaining = 0
            while offset < data_len:
                digit = data[offset]
                offset += 1
                remaining += (digit & 127) * multiplier
                if (digit & 128) == 0:
                    break
                multiplier *= 128
            end = offset + remaining
            if end > data_len:
                return
            yield packet_type, data[offset:end]
            offset = end

    @classmethod
    async def _wait_for_mqtt_packet(
        cls,
        ws: aiohttp.ClientWebSocketResponse,
        packet_prefixes: set[int],
        *,
        deadline: float,
    ) -> tuple[int, bytes]:
        while True:
            msg = await ws.receive(timeout=cls._mqtt_remaining_timeout(deadline))
            if msg.type == aiohttp.WSMsgType.BINARY:
                for packet_type, payload in cls._mqtt_packets(msg.data):
                    if packet_type & 0xF0 in packet_prefixes:
                        return packet_type, payload
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.ERROR,
            ):
                raise aiohttp.ClientConnectionError("MQTT WebSocket closed")

    @staticmethod
    def _mqtt_publish_payload(packet_type: int, payload: bytes) -> bytes:
        if len(payload) < 2:
            return b""
        topic_len = int.from_bytes(payload[:2], "big")
        offset = 2 + topic_len
        qos = (packet_type & 0x06) >> 1
        if qos:
            offset += 2
        return payload[offset:]

    @staticmethod
    def _decode_json_mqtt_payload(payload: bytes) -> object | None:
        text = payload.decode("utf-8", "ignore").strip(" \t\r\n\0")
        if not text:
            return None
        try:
            return cast(object, json.loads(text))
        except ValueError:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end <= start:
                return None
            try:
                return cast(object, json.loads(text[start : end + 1]))
            except ValueError:
                return None

    @classmethod
    def _decode_site_livestream_payload(
        cls, payload: bytes
    ) -> dict[str, object] | None:
        decoded = cls._decode_json_mqtt_payload(payload)
        if isinstance(decoded, dict):
            return decoded
        grid_relay = cls._decode_live_status_grid_relay(payload)
        if grid_relay is None:
            return None
        return {"meters": {"gridRelay": grid_relay}}

    @staticmethod
    def _protobuf_varint(data: bytes, offset: int) -> tuple[int, int] | None:
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                return None
            byte = data[offset]
            offset += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value, offset
        return None

    @classmethod
    def _protobuf_field(
        cls,
        data: bytes,
        field_number: int,
        wire_type: int,
    ) -> int | bytes | None:
        offset = 0
        field_value: int | bytes
        while offset < len(data):
            key_result = cls._protobuf_varint(data, offset)
            if key_result is None:
                return None
            key, offset = key_result
            current_field = key >> 3
            current_wire = key & 0x07
            if current_field == 0:
                return None

            if current_wire == 0:
                value_result = cls._protobuf_varint(data, offset)
                if value_result is None:
                    return None
                field_value, offset = value_result
            elif current_wire == 1:
                end = offset + 8
                if end > len(data):
                    return None
                field_value = data[offset:end]
                offset = end
            elif current_wire == 2:
                length_result = cls._protobuf_varint(data, offset)
                if length_result is None:
                    return None
                length, offset = length_result
                end = offset + length
                if end > len(data):
                    return None
                field_value = data[offset:end]
                offset = end
            elif current_wire == 5:
                end = offset + 4
                if end > len(data):
                    return None
                field_value = data[offset:end]
                offset = end
            else:
                return None

            if current_field == field_number and current_wire == wire_type:
                return field_value
        return None

    @classmethod
    def _decode_live_status_grid_relay(cls, payload: bytes) -> str | None:
        # DataMsg.meters is field 3; MeterSummaryData.grid_relay is field 5.
        meters = cls._protobuf_field(payload, 3, 2)
        if not isinstance(meters, bytes):
            return None
        grid_relay = cls._protobuf_field(meters, 5, 0)
        if not isinstance(grid_relay, int):
            return None
        return _LIVE_STATUS_GRID_RELAY_ENUM.get(grid_relay)
