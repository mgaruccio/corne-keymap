#!/usr/bin/env python3
"""GTK4 editor, builder, and safe left-half flasher for the Corne keymap.

The firmware and this app share one JSON layout. BlueZ remains read-only; USB
flashing is enabled only for the verified left nRF52840 boot volume.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from scripts.corne_firmware import UF2Error, UF2SyncUnconfirmed, flash_uf2, parse_uf2
except ImportError:  # direct execution from the scripts directory
    from corne_firmware import UF2Error, UF2SyncUnconfirmed, flash_uf2, parse_uf2


SERVICE_UUID = "32dcce45-ae11-451c-8b48-7a3f083f6afe"
CHARACTERISTIC_UUID = "32dcce45-ae11-451c-8b48-7a3f083f6aff"
KEY_COUNT = 42
MASK_BYTES = 4
TRANSPARENT_BINDING = "&trans"
TRANSPARENT_TAP = "↳"


class LayoutError(ValueError):
    """Raised when the firmware's shared layout JSON is not usable."""


class LayerMaskError(ValueError):
    """Raised when a layer-state notification violates the wire contract."""


@dataclass(frozen=True)
class KeyRecord:
    """One physical key's firmware-facing labels."""

    tap: str
    hold: str
    repeat: str
    binding: str

    @classmethod
    def from_json(cls, value: Any, *, layer_index: int, key_index: int) -> "KeyRecord":
        if not isinstance(value, Mapping):
            raise LayoutError(
                f"layer {layer_index} key {key_index} must be an object"
            )
        fields: dict[str, str] = {}
        for field in ("tap", "hold", "repeat", "binding"):
            item = value.get(field)
            if not isinstance(item, str):
                raise LayoutError(
                    f"layer {layer_index} key {key_index} field {field!r} "
                    "must be a string"
                )
            fields[field] = item
        return cls(**fields)

    @property
    def is_transparent(self) -> bool:
        return self.binding == TRANSPARENT_BINDING


@dataclass(frozen=True)
class Layer:
    name: str
    keys: tuple[KeyRecord, ...]


@dataclass(frozen=True)
class Layout:
    service_uuid: str
    characteristic_uuid: str
    layers: tuple[Layer, ...]

    @property
    def layer_count(self) -> int:
        return len(self.layers)


def default_layout_path() -> Path:
    """Return the repository-relative location produced by the firmware task."""

    return Path(__file__).resolve().parent.parent / "configs" / "corne" / "layout.json"


def _normalise_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LayoutError(f"{field} must be a non-empty string")
    normalised = value.strip().lower()
    if normalised not in {SERVICE_UUID, CHARACTERISTIC_UUID}:
        raise LayoutError(f"{field} is not the Corne viewer UUID")
    return normalised


def parse_layout_data(data: Any) -> Layout:
    """Validate and parse the shared firmware/viewer layout object."""

    if not isinstance(data, Mapping):
        raise LayoutError("layout root must be an object")

    service_uuid = _normalise_uuid(data.get("service_uuid"), "service_uuid")
    characteristic_uuid = _normalise_uuid(
        data.get("characteristic_uuid"), "characteristic_uuid"
    )
    if service_uuid != SERVICE_UUID:
        raise LayoutError("service_uuid does not match the Corne service")
    if characteristic_uuid != CHARACTERISTIC_UUID:
        raise LayoutError("characteristic_uuid does not match the Corne characteristic")

    raw_layers = data.get("layers")
    if not isinstance(raw_layers, Sequence) or isinstance(raw_layers, (str, bytes)):
        raise LayoutError("layers must be an array")
    if not raw_layers:
        raise LayoutError("layers must contain at least one layer")
    if len(raw_layers) > 32:
        raise LayoutError("layers cannot exceed the 32 bits in the layer mask")

    layers: list[Layer] = []
    for layer_index, raw_layer in enumerate(raw_layers):
        if not isinstance(raw_layer, Mapping):
            raise LayoutError(f"layer {layer_index} must be an object")
        name = raw_layer.get("name")
        if not isinstance(name, str) or not name.strip():
            raise LayoutError(f"layer {layer_index} name must be a non-empty string")
        raw_keys = raw_layer.get("keys")
        if not isinstance(raw_keys, Sequence) or isinstance(raw_keys, (str, bytes)):
            raise LayoutError(f"layer {layer_index} keys must be an array")
        if len(raw_keys) != KEY_COUNT:
            raise LayoutError(
                f"layer {layer_index} must contain exactly {KEY_COUNT} keys "
                f"(got {len(raw_keys)})"
            )
        keys = tuple(
            KeyRecord.from_json(key, layer_index=layer_index, key_index=key_index)
            for key_index, key in enumerate(raw_keys)
        )
        layers.append(Layer(name=name.strip(), keys=keys))

    return Layout(
        service_uuid=service_uuid,
        characteristic_uuid=characteristic_uuid,
        layers=tuple(layers),
    )


def load_layout(path: str | Path) -> Layout:
    """Load and validate a layout JSON file."""

    layout_path = Path(path).expanduser()
    try:
        data = json.loads(layout_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LayoutError(f"cannot read layout {layout_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LayoutError(f"invalid JSON in layout {layout_path}: {exc}") from exc
    return parse_layout_data(data)


def layout_to_data(layout: Layout) -> dict[str, Any]:
    """Return the editable JSON representation without leaking dataclasses."""
    return {
        "service_uuid": layout.service_uuid,
        "characteristic_uuid": layout.characteristic_uuid,
        "layers": [
            {"name": layer.name, "keys": [
                {"tap": key.tap, "hold": key.hold, "repeat": key.repeat, "binding": key.binding}
                for key in layer.keys
            ]}
            for layer in layout.layers
        ],
    }


@cache
def keymap_generator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "corne_generator", Path(__file__).with_name("generate-corne-keymap.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_editable_layout(layout: Layout) -> dict[str, Any]:
    data = layout_to_data(layout)
    try:
        keymap_generator().validate(data)
    except (ValueError, KeyError, TypeError) as exc:
        raise LayoutError(str(exc)) from exc
    return data


@dataclass(frozen=True)
class FileSnapshot:
    """In-memory content/identity check; never a persistent build manifest."""
    data: bytes
    identity: tuple[int, ...]

    @classmethod
    def read(cls, path: Path) -> "FileSnapshot":
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise LayoutError(f"not a regular file: {path}")
                data = stream.read()
                after = os.fstat(stream.fileno())
            def identity(info: Any) -> tuple[int, ...]:
                return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                        info.st_mtime_ns, info.st_ctime_ns)
            if identity(before) != identity(after):
                raise LayoutError(f"file changed while reading: {path}")
            return cls(data, identity(after))
        except OSError as exc:
            raise LayoutError(f"cannot read {path}: {exc}") from exc

    def require_current(self, path: Path) -> None:
        if self != self.read(path):
            raise LayoutError(f"{path.name} changed externally; reload/rebuild before continuing")


def format_layout(layout: Layout) -> bytes:
    """Keep the shared JSON's one-record-per-line formatting."""
    data = validate_editable_layout(layout)
    encode = lambda value: json.dumps(value, ensure_ascii=False)
    lines = ["{", f'  "service_uuid": {encode(data["service_uuid"])},',
             f'  "characteristic_uuid": {encode(data["characteristic_uuid"])},', '  "layers": [']
    for index, layer in enumerate(data["layers"]):
        lines.extend(["    {", f'      "name": {encode(layer["name"])},', '      "keys": ['])
        lines.extend("        " + encode(key) + ("," if key_index < KEY_COUNT - 1 else "")
                     for key_index, key in enumerate(layer["keys"]))
        lines.extend(['      ]', '    }' + ("," if index < len(data["layers"]) - 1 else "")])
    return ("\n".join(lines + ["  ]", "}"]) + "\n").encode("utf-8")


def save_layout(path: str | Path, layout: Layout, *, expected: FileSnapshot) -> FileSnapshot:
    """Validate and atomically replace only the unchanged source, preserving mode."""
    data = format_layout(layout)
    target = Path(path).expanduser()
    expected.require_current(target)
    if data == expected.data:
        return expected
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), stat.S_IMODE(expected.identity[2]))
            stream.flush()
            os.fsync(stream.fileno())
        expected.require_current(target)
        os.replace(temporary, target)
        return FileSnapshot.read(target)
    except OSError as exc:
        raise LayoutError(f"cannot save layout {target}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_inputs(layout_path: Path, *, generated: bool = True) -> dict[Path, FileSnapshot]:
    config = layout_path.parent
    paths = [layout_path, config / "blecorne.conf", config / "west.yml",
             config / "telemetry/Kconfig", config / "telemetry/CMakeLists.txt",
             config / "telemetry/zephyr/module.yml", config / "telemetry/src/layer_telemetry.c",
             Path(__file__).with_name("generate-corne-keymap.py"),
             Path(__file__).with_name("build-corne-firmware.sh")]
    if generated:
        paths.extend([config / "blecorne.keymap", config / "telemetry/include/corne_layout.h"])
    return {path: FileSnapshot.read(path) for path in paths}


def require_current_inputs(inputs: dict[Path, FileSnapshot]) -> None:
    for path, snapshot in inputs.items():
        snapshot.require_current(path)


@dataclass(frozen=True)
class BuiltFirmware:
    path: Path
    source: FileSnapshot
    inputs: dict[Path, FileSnapshot]

    def require_current(self) -> None:
        require_current_inputs(self.inputs)
        self.source.require_current(self.path)


def edit_layout_key(layout: Layout, layer_index: int, key_index: int, **fields: str) -> Layout:
    """Return a validated layout with one key edited; never mutates the old layout."""
    if not 0 <= layer_index < layout.layer_count or not 0 <= key_index < KEY_COUNT:
        raise LayoutError("key position is out of range")
    allowed = {"tap", "hold", "repeat", "binding"}
    if set(fields) - allowed or any(not isinstance(value, str) for value in fields.values()):
        raise LayoutError("key fields must be strings named tap, hold, repeat, or binding")
    data = layout_to_data(layout)
    data["layers"][layer_index]["keys"][key_index].update(fields)
    edited = parse_layout_data(data)
    validate_editable_layout(edited)
    return edited


def _payload_bytes(payload: Any) -> bytes:
    """Convert a DBus byte-array representation into bytes without truncation."""

    if hasattr(payload, "unpack"):
        payload = payload.unpack()
    if isinstance(payload, tuple) and len(payload) == 1:
        payload = payload[0]
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, (bytearray, memoryview)):
        return bytes(payload)
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        try:
            values = list(payload)
            if all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= 255
                for value in values
            ):
                return bytes(values)
        except (TypeError, ValueError):
            pass
    raise LayerMaskError("layer notification is not a byte array")


def parse_layer_mask(payload: Any, layer_count: int) -> int:
    """Validate a 4-byte little-endian layer mask and return its integer value."""

    if not isinstance(layer_count, int) or isinstance(layer_count, bool):
        raise LayerMaskError("layer count must be an integer")
    if layer_count < 1 or layer_count > 32:
        raise LayerMaskError("layer count must be between 1 and 32")

    raw = _payload_bytes(payload)
    if len(raw) != MASK_BYTES:
        raise LayerMaskError(
            f"layer notification must be exactly {MASK_BYTES} bytes (got {len(raw)})"
        )
    mask = int.from_bytes(raw, byteorder="little", signed=False)
    allowed_bits = (1 << layer_count) - 1
    if not (mask & 0x01):
        raise LayerMaskError("layer notification must include the default layer bit")
    if mask & ~allowed_bits:
        raise LayerMaskError("layer notification contains a bit outside the layout")
    return mask


def highest_active_layer(mask: int, layer_count: int) -> int:
    """Return the highest configured active bit after validating the mask."""

    if not isinstance(mask, int) or isinstance(mask, bool):
        raise LayerMaskError("layer mask must be an integer")
    if mask < 0 or mask >= (1 << 32):
        raise LayerMaskError("layer mask must fit in an unsigned 32-bit value")
    validated = parse_layer_mask(mask.to_bytes(MASK_BYTES, "little"), layer_count)
    return validated.bit_length() - 1


def matrix_key_index(half: int, row: int, column: int) -> int:
    """Map a tile to the shared left-six/right-six interleaved row order."""
    if half not in (0, 1) or not 0 <= row < 3 or not 0 <= column < 6:
        raise IndexError("matrix position out of range")
    return row * 12 + half * 6 + column


@dataclass(frozen=True)
class KeyGeometry:
    x: float
    y: float
    width: float = 1.0
    height: float = 1.0
    rotation: float = 0.0
    origin_x: float = 0.0
    origin_y: float = 0.0


def physical_key_geometry(index: int) -> KeyGeometry:
    """ZMK foostan/corne/6column.dtsi geometry, in key units and degrees."""
    if not 0 <= index < KEY_COUNT:
        raise IndexError("physical key index out of range")
    if index < 36:
        row, column = divmod(index, 12)
        stagger = (0.37, 0.37, 0.12, 0.0, 0.12, 0.24)
        if column < 6:
            return KeyGeometry(float(column), row + stagger[column])
        return KeyGeometry(float(column + 2), row + stagger[11 - column])
    return (
        KeyGeometry(3.5, 3.12),
        KeyGeometry(4.5, 3.12, rotation=12, origin_x=4.5, origin_y=4.12),
        KeyGeometry(5.48, 2.83, height=1.5, rotation=24, origin_x=5.48, origin_y=4.33),
        KeyGeometry(7.52, 2.83, height=1.5, rotation=-24, origin_x=8.52, origin_y=4.33),
        KeyGeometry(8.5, 3.12, rotation=-12, origin_x=9.5, origin_y=4.12),
        KeyGeometry(9.5, 3.12),
    )[index - 36]


DARK_STYLE = """
window.corne-viewer { background: #141a24; color: #e7edf7; }
window.corne-viewer entry { background: #252f40; color: #e7edf7; border-color: #4f607c; }
window.corne-viewer entry text { color: #e7edf7; }
window.corne-viewer label { color: #e7edf7; }
window.corne-viewer button { background-image: none; background-color: #252f40;
    color: #e7edf7; border: 1px solid #45546b; box-shadow: none; }
window.corne-viewer button:hover { background-color: #35465f; }
window.corne-viewer button:focus-visible { outline: 2px solid #76b9ff; }
window.corne-viewer .corne-key { padding: 3px; border-radius: 8px; }
window.corne-viewer .corne-tap { font-size: 16px; font-weight: bold; color: #f2f6fc; }
window.corne-viewer .corne-meta { font-size: 11px; color: #b6c5db; }
window.corne-viewer .dim-label { color: #a9b9d0; }
window.corne-viewer checkbutton { color: #e7edf7; }
window.corne-viewer checkbutton check { background: #252f40; border-color: #60738e; }
window.corne-viewer checkbutton check:checked { background: #337bc5; color: white; }
window.corne-viewer popover contents { background: #202b3c; color: #e7edf7;
    border: 1px solid #60738e; }
window.corne-viewer popover label { color: #e7edf7; }
"""


def resolve_key_record(layout: Layout, layer_index: int, key_index: int) -> KeyRecord:
    """Resolve transparent records to the nearest lower-layer key for preview."""

    if not 0 <= layer_index < layout.layer_count:
        raise IndexError(f"layer index out of range: {layer_index}")
    if not 0 <= key_index < KEY_COUNT:
        raise IndexError(f"key index out of range: {key_index}")

    record = layout.layers[layer_index].keys[key_index]
    if not record.is_transparent:
        return record
    for lower_index in range(layer_index - 1, -1, -1):
        lower_record = layout.layers[lower_index].keys[key_index]
        if not lower_record.is_transparent:
            return lower_record
    return record


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only GTK4 BlueZ monitor with Corne keymap editor, builder, and left-half flasher."
    )
    parser.add_argument(
        "--layout",
        type=Path,
        default=default_layout_path(),
        help="shared firmware layout JSON (default: repository configs/corne/layout.json)",
    )
    parser.add_argument(
        "--flash-volume", type=Path, default=None,
        help="optional mounted volume path; it is still checked against the exact left USB identity",
    )
    parser.add_argument(
        "--build-cache", type=Path, default=default_layout_path().parent / ".cache",
        help="reuse the existing pinned builder cache (default: configs/corne/.cache)",
    )
    return parser.parse_args(argv)


class BlueZMonitor:
    """Observe the already-connected Corne device through BlueZ GATT.

    This class deliberately does not pair, trust, connect, disconnect, or write.
    Native bluetoothd owns the keyboard connection; this monitor only follows
    object-manager/property changes and subscribes to the custom characteristic.
    """

    def __init__(
        self,
        layout: Layout,
        *,
        on_connection_changed: Callable[[bool], None] | None = None,
        on_mask_changed: Callable[[int], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        bus: Any | None = None,
    ) -> None:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            gi.require_version("GLib", "2.0")
            from gi.repository import Gio, GLib
        except (ImportError, ValueError) as exc:  # pragma: no cover - host dependency
            raise RuntimeError("PyGObject Gio/GLib is required for BlueZ monitoring") from exc

        self._Gio = Gio
        self._GLib = GLib
        self.layout = layout
        self.on_connection_changed = on_connection_changed
        self.on_mask_changed = on_mask_changed
        self.on_error = on_error
        self._bus = bus
        self._started = False
        self._closing = False
        self._connected = False
        self._device_path: str | None = None
        self._service_path: str | None = None
        self._characteristic_path: str | None = None
        self._characteristic_proxy: Any | None = None
        self._value_signal_id: int | None = None
        self._notify_started = False
        self._start_notify_pending = False
        self._generation = 0
        self._value_serial = 0
        self._last_mask: int | None = None
        self._discovery_source: int | None = None
        self._discovery_retries = 0
        self._subscriptions: list[int] = []

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_mask(self) -> int | None:
        return self._last_mask

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._closing = False
        try:
            if self._bus is None:
                self._bus = self._Gio.bus_get_sync(self._Gio.BusType.SYSTEM, None)
            self._subscribe_to_bluez()
        except Exception as exc:  # noqa: BLE001 - a viewer remains useful offline
            self._report_error(f"BlueZ unavailable: {exc}")
            self._set_connected(False)
            return
        self._queue_discovery()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._started = False
        if self._discovery_source is not None:
            try:
                self._GLib.source_remove(self._discovery_source)
            except (TypeError, ValueError):
                pass
            self._discovery_source = None

        old_proxy = self._characteristic_proxy
        old_value_signal_id = self._value_signal_id
        should_stop_notify = self._notify_started
        self._generation += 1
        self._characteristic_proxy = None
        self._value_signal_id = None
        self._notify_started = False
        self._start_notify_pending = False
        self._device_path = None
        self._service_path = None
        self._characteristic_path = None
        if old_proxy is not None and old_value_signal_id is not None:
            try:
                old_proxy.disconnect(old_value_signal_id)
            except (TypeError, ValueError):
                pass
        if old_proxy is not None and should_stop_notify:
            self._stop_notify(old_proxy)

        if self._bus is not None:
            for subscription_id in self._subscriptions:
                try:
                    self._bus.signal_unsubscribe(subscription_id)
                except (TypeError, ValueError):
                    pass
        self._subscriptions.clear()
        self._set_connected(False)

    def _subscribe_to_bluez(self) -> None:
        # NameOwnerChanged lets a BlueZ restart invalidate all old proxies and
        # causes one bounded rediscovery when the owner returns.
        self._subscriptions.append(
            self._bus.signal_subscribe(
                None,
                "org.freedesktop.DBus",
                "NameOwnerChanged",
                "/org/freedesktop/DBus",
                "org.bluez",
                self._Gio.DBusSignalFlags.NONE,
                self._on_name_owner_changed,
                None,
            )
        )
        self._subscriptions.append(
            self._bus.signal_subscribe(
                "org.bluez",
                "org.freedesktop.DBus.ObjectManager",
                "InterfacesAdded",
                "/",
                None,
                self._Gio.DBusSignalFlags.NONE,
                self._on_interfaces_changed,
                None,
            )
        )
        self._subscriptions.append(
            self._bus.signal_subscribe(
                "org.bluez",
                "org.freedesktop.DBus.ObjectManager",
                "InterfacesRemoved",
                "/",
                None,
                self._Gio.DBusSignalFlags.NONE,
                self._on_interfaces_changed,
                None,
            )
        )
        self._subscriptions.append(
            self._bus.signal_subscribe(
                "org.bluez",
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                None,
                None,
                self._Gio.DBusSignalFlags.NONE,
                self._on_properties_changed,
                None,
            )
        )

    def _queue_discovery(self, *, delay_ms: int = 0, retry: bool = False) -> None:
        if self._closing or self._bus is None or self._discovery_source is not None:
            return
        if retry:
            if self._discovery_retries >= 3:
                return
            self._discovery_retries += 1
        if delay_ms:
            self._discovery_source = self._GLib.timeout_add(
                delay_ms, self._run_queued_discovery
            )
        else:
            self._discovery_source = self._GLib.idle_add(self._run_queued_discovery)

    def _run_queued_discovery(self) -> bool:
        self._discovery_source = None
        if not self._closing:
            self._discover()
        return False

    def _on_name_owner_changed(
        self,
        _connection: Any,
        _sender_name: str,
        _object_path: str,
        _interface_name: str,
        _signal_name: str,
        parameters: Any,
        _user_data: Any,
    ) -> None:
        try:
            name, _old_owner, new_owner = parameters.unpack()
        except (AttributeError, TypeError, ValueError):
            return
        if name != "org.bluez" or self._closing:
            return
        self._detach()
        if new_owner:
            self._discovery_retries = 0
            self._queue_discovery()

    def _on_interfaces_changed(
        self,
        _connection: Any,
        _sender_name: str,
        _object_path: str,
        _interface_name: str,
        _signal_name: str,
        _parameters: Any,
        _user_data: Any,
    ) -> None:
        self._queue_discovery()

    def _on_properties_changed(
        self,
        _connection: Any,
        _sender_name: str,
        object_path: str,
        _interface_name: str,
        _signal_name: str,
        parameters: Any,
        _user_data: Any,
    ) -> None:
        if self._closing:
            return
        try:
            changed_interface, changed, _invalidated = parameters.unpack()
        except (AttributeError, TypeError, ValueError):
            return
        changed = _unpack_mapping(changed)

        # The characteristic proxy listener handles Value changes.  Keeping
        # the connection-level PropertiesChanged subscription for discovery
        # avoids consuming each notification twice when Gio forwards it through
        # both paths.
        if (
            object_path == self._characteristic_path
            and changed_interface == "org.bluez.GattCharacteristic1"
            and set(changed) <= {"Value"}
        ):
            return
        self._queue_discovery()

    def _discover(self) -> None:
        if self._closing or self._bus is None:
            return
        try:
            reply = self._bus.call_sync(
                "org.bluez",
                "/",
                "org.freedesktop.DBus.ObjectManager",
                "GetManagedObjects",
                None,
                None,
                self._Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            objects = reply.unpack() if hasattr(reply, "unpack") else reply
            if isinstance(objects, tuple) and len(objects) == 1:
                objects = objects[0]
            candidate = self._find_candidate(objects)
        except Exception as exc:  # noqa: BLE001 - transient BlueZ failures are expected
            self._report_error(f"BlueZ discovery failed: {exc}")
            self._queue_discovery(delay_ms=250, retry=True)
            return

        if candidate is None:
            self._detach()
            return
        if self._same_candidate(candidate):
            return
        self._attach(candidate)

    def _find_candidate(self, objects: Any) -> dict[str, str] | None:
        if not isinstance(objects, Mapping):
            raise RuntimeError("GetManagedObjects returned an invalid object map")

        devices: dict[str, Mapping[str, Any]] = {}
        services: dict[str, Mapping[str, Any]] = {}
        characteristics: dict[str, Mapping[str, Any]] = {}
        for path, interfaces in objects.items():
            if not isinstance(path, str) or not isinstance(interfaces, Mapping):
                continue
            if "org.bluez.Device1" in interfaces:
                devices[path] = _unpack_mapping(interfaces["org.bluez.Device1"])
            if "org.bluez.GattService1" in interfaces:
                services[path] = _unpack_mapping(interfaces["org.bluez.GattService1"])
            if "org.bluez.GattCharacteristic1" in interfaces:
                characteristics[path] = _unpack_mapping(
                    interfaces["org.bluez.GattCharacteristic1"]
                )

        for device_path, device in sorted(devices.items()):
            if not device.get("Connected") or not device.get("ServicesResolved"):
                continue
            service_paths = {
                path
                for path, service in services.items()
                if service.get("Device") == device_path
                and str(service.get("UUID", "")).lower() == self.layout.service_uuid
            }
            for characteristic_path, characteristic in sorted(characteristics.items()):
                service_path = characteristic.get("Service")
                if service_path in service_paths and str(
                    characteristic.get("UUID", "")
                ).lower() == self.layout.characteristic_uuid:
                    return {
                        "device_path": device_path,
                        "service_path": str(service_path),
                        "characteristic_path": characteristic_path,
                    }
        return None

    def _same_candidate(self, candidate: Mapping[str, str]) -> bool:
        return (
            self._device_path == candidate.get("device_path")
            and self._service_path == candidate.get("service_path")
            and self._characteristic_path == candidate.get("characteristic_path")
            and self._characteristic_proxy is not None
            and (self._notify_started or self._start_notify_pending)
        )

    def _attach(self, candidate: Mapping[str, str]) -> None:
        self._detach()
        self._device_path = candidate["device_path"]
        self._service_path = candidate["service_path"]
        self._characteristic_path = candidate["characteristic_path"]
        self._generation += 1
        generation = self._generation
        try:
            proxy = self._Gio.DBusProxy.new_sync(
                self._bus,
                self._Gio.DBusProxyFlags.NONE,
                None,
                "org.bluez",
                self._characteristic_path,
                "org.bluez.GattCharacteristic1",
                None,
            )
            # Register this before StartNotify so the first notification cannot
            # race with the initial ReadValue.
            self._value_signal_id = proxy.connect(
                "g-properties-changed", self._on_proxy_properties_changed
            )
            self._characteristic_proxy = proxy
        except Exception as exc:  # noqa: BLE001 - BlueZ can restart mid-discovery
            self._report_error(f"cannot observe Corne characteristic: {exc}")
            self._queue_discovery(delay_ms=250, retry=True)
            return

        self._set_connected(True)
        self._start_notify_pending = True
        try:
            proxy.call(
                "StartNotify",
                None,
                self._Gio.DBusCallFlags.NONE,
                5000,
                None,
                self._on_start_notify_done,
                (generation, self._characteristic_path, proxy),
            )
        except Exception as exc:  # noqa: BLE001 - transient disconnect
            self._start_notify_pending = False
            self._report_error(f"cannot start Corne notifications: {exc}")
            self._queue_discovery(delay_ms=250, retry=True)

    def _on_proxy_properties_changed(
        self, proxy: Any, changed: Any, _invalidated: Any
    ) -> None:
        if proxy is not self._characteristic_proxy or self._closing:
            return
        changed = _unpack_mapping(changed)
        if "Value" in changed:
            self._consume_value(changed["Value"], source="notification")

    def _on_start_notify_done(self, proxy: Any, result: Any, token: Any) -> None:
        generation, characteristic_path, expected_proxy = token
        try:
            proxy.call_finish(result)
        except Exception as exc:  # noqa: BLE001 - transient disconnect
            if self._is_current(generation, characteristic_path, expected_proxy):
                self._start_notify_pending = False
                self._report_error(f"Corne notifications unavailable: {exc}")
                self._queue_discovery(delay_ms=250, retry=True)
            return
        if not self._is_current(generation, characteristic_path, expected_proxy):
            # BlueZ shares sessions per D-Bus client/path. Do not cancel a newer
            # attachment that is already using (or starting) that same session.
            session_reused = (
                self._characteristic_path == characteristic_path
                and self._characteristic_proxy is not None
                and (self._notify_started or self._start_notify_pending)
            )
            if not session_reused:
                self._stop_notify(proxy)
            return
        self._start_notify_pending = False
        self._discovery_retries = 0
        self._notify_started = True
        self._read_initial_value(generation, characteristic_path, proxy)

    def _read_initial_value(self, generation: int, characteristic_path: str, proxy: Any) -> None:
        if not self._is_current(generation, characteristic_path, proxy):
            return
        serial_at_request = self._value_serial
        try:
            proxy.call(
                "ReadValue",
                self._GLib.Variant("(a{sv})", ({},)),
                self._Gio.DBusCallFlags.NONE,
                5000,
                None,
                self._on_read_value_done,
                (generation, characteristic_path, proxy, serial_at_request),
            )
        except Exception as exc:  # noqa: BLE001 - transient disconnect
            self._report_error(f"initial Corne layer read failed: {exc}")

    def _on_read_value_done(self, proxy: Any, result: Any, token: Any) -> None:
        generation, characteristic_path, expected_proxy, serial_at_request = token
        try:
            reply = proxy.call_finish(result)
        except Exception as exc:  # noqa: BLE001 - transient disconnect
            if self._is_current(generation, characteristic_path, expected_proxy):
                self._report_error(f"initial Corne layer read failed: {exc}")
            return
        if not self._is_current(generation, characteristic_path, expected_proxy):
            return
        # A valid notification after ReadValue was issued is newer than this
        # snapshot. Never let the asynchronous read roll the UI backwards.
        if serial_at_request != self._value_serial:
            return
        payload = reply.unpack() if hasattr(reply, "unpack") else reply
        self._consume_value(payload, source="initial read")

    def _is_current(self, generation: int, characteristic_path: str, proxy: Any) -> bool:
        return (
            not self._closing
            and generation == self._generation
            and characteristic_path == self._characteristic_path
            and proxy is self._characteristic_proxy
        )

    def _consume_value(self, payload: Any, *, source: str) -> None:
        try:
            mask = parse_layer_mask(payload, self.layout.layer_count)
        except LayerMaskError as exc:
            # Invalid data is not a transport disconnect.  Keep the real
            # connection state and leave the last valid preview in place.
            self._report_error(f"invalid {source} layer mask: {exc}")
            return
        self._value_serial += 1
        self._last_mask = mask
        if self.on_mask_changed is not None:
            self.on_mask_changed(mask)
        self._report_error("")

    def _stop_notify(self, proxy: Any) -> None:
        try:
            proxy.call(
                "StopNotify",
                None,
                self._Gio.DBusCallFlags.NONE,
                3000,
                None,
                self._on_stop_notify_done,
                None,
            )
        except Exception:
            # BlueZ may already have disappeared.  The local subscription is
            # nevertheless released and no reconnect is attempted here.
            pass

    def _on_stop_notify_done(self, proxy: Any, result: Any, _user_data: Any) -> None:
        try:
            proxy.call_finish(result)
        except Exception:
            pass

    def _detach(self) -> None:
        old_proxy = self._characteristic_proxy
        old_value_signal_id = self._value_signal_id
        should_stop_notify = self._notify_started
        was_connected = self._connected
        self._generation += 1
        self._characteristic_proxy = None
        self._value_signal_id = None
        self._notify_started = False
        self._start_notify_pending = False
        self._device_path = None
        self._service_path = None
        self._characteristic_path = None
        if old_proxy is not None and old_value_signal_id is not None:
            try:
                old_proxy.disconnect(old_value_signal_id)
            except (TypeError, ValueError):
                pass
        if old_proxy is not None and should_stop_notify:
            self._stop_notify(old_proxy)
        if was_connected:
            self._set_connected(False)

    def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        if self.on_connection_changed is not None:
            self.on_connection_changed(connected)

    def _report_error(self, message: str) -> None:
        if self.on_error is not None:
            self.on_error(message)


def _unpack_mapping(value: Any) -> Any:
    if hasattr(value, "unpack"):
        value = value.unpack()
    if isinstance(value, Mapping):
        return {
            key: item.unpack() if hasattr(item, "unpack") else item
            for key, item in value.items()
        }
    return value


def create_application(layout: Layout, *, monitor_factory: Callable[..., Any] = BlueZMonitor,
                       layout_path: Path | None = None, flash_volume: Path | None = None,
                       build_cache: Path | None = None) -> Any:
    """Build the GTK application while keeping Gtk imports out of unit-test import time."""

    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        gi.require_version("Graphene", "1.0")
        gi.require_version("Gsk", "4.0")
        from gi.repository import Gio, GLib, Graphene, Gsk, Gtk
    except (ImportError, ValueError) as exc:  # pragma: no cover - host dependency
        raise RuntimeError("PyGObject Gtk4/Gio/GLib is required for the viewer") from exc

    class CorneViewerApplication(Gtk.Application):
        def __init__(self) -> None:
            super().__init__(
                application_id="com.mike.CorneKeymapViewer",
                flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
            )
            self.layout = layout
            self.layout_path = (layout_path or default_layout_path()).expanduser().absolute()
            self.build_cache = (build_cache or default_layout_path().parent / ".cache").expanduser().absolute()
            self.flash_volume = flash_volume
            self._source = FileSnapshot.read(self.layout_path)
            if parse_layout_data(json.loads(self._source.data)) != layout:
                raise LayoutError("layout changed while opening; reopen the editor")
            self.window: Any | None = None
            self.monitor: Any | None = None
            self._follow_keyboard = True
            self._connected = False
            self._last_mask: int | None = None
            self._displayed_layer = 0
            self._monitor_error = ""
            self._follow_guard = False
            self._editor: Any | None = None
            self._status_label: Any | None = None
            self._active_label: Any | None = None
            self._operation_label: Any | None = None
            self._key_area: Any | None = None
            self._follow_button: Any | None = None
            self._operation_running = False
            self._operation_message = "Edit a key, save JSON, then build and flash the left half."
            self._last_uf2: BuiltFirmware | None = None
            self._building_inputs: dict[Path, FileSnapshot] = {}

        def do_startup(self) -> None:
            Gtk.Application.do_startup(self)
            show_layer = Gio.SimpleAction.new("show-layer", GLib.VariantType("s"))
            show_layer.connect("activate", self._action_show_layer)
            self.add_action(show_layer)

            follow = Gio.SimpleAction.new("follow", GLib.VariantType("b"))
            follow.connect("activate", self._action_follow)
            self.add_action(follow)

            for name, handler in (("save", self._on_save_clicked),
                                  ("build", self._on_build_clicked),
                                  ("flash", self._on_flash_clicked),
                                  ("quit", self._request_quit)):
                action = Gio.SimpleAction.new(name, None)
                action.connect("activate", lambda action, _parameter, handler=handler: handler(action))
                self.add_action(action)
            edit = Gio.SimpleAction.new("edit-key", GLib.VariantType("(ii)"))
            edit.connect("activate", self._action_edit_key)
            self.add_action(edit)
            self._update_actions()

        def do_activate(self) -> None:
            if self.window is None:
                self._build_window()
                self.monitor = monitor_factory(
                    self.layout,
                    on_connection_changed=self._on_connection_changed,
                    on_mask_changed=self._on_mask_changed,
                    on_error=self._on_monitor_error,
                )
                self.monitor.start()
            self.window.present()

        def do_shutdown(self) -> None:
            if self.monitor is not None:
                self.monitor.close()
                self.monitor = None
            if self._editor is not None:
                self._editor.destroy()
                self._editor = None
            Gtk.Application.do_shutdown(self)

        def _build_window(self) -> None:
            self.window = Gtk.ApplicationWindow(application=self)
            self.window.set_title("Corne keymap viewer")
            self.window.add_css_class("corne-viewer")
            style = Gtk.CssProvider()
            style.set_property("prefers-color-scheme", Gtk.InterfaceColorScheme.DARK)
            style.load_from_string(DARK_STYLE)
            Gtk.StyleContext.add_provider_for_display(
                self.window.get_display(), style, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            self.window.set_default_size(1080, 550)
            self.window.set_size_request(820, 460)
            self.window.connect("close-request", self._on_close_request)

            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            root.set_margin_top(12)
            root.set_margin_bottom(12)
            root.set_margin_start(14)
            root.set_margin_end(14)
            self.window.set_child(root)

            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            header.set_hexpand(True)
            root.append(header)
            title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            title_box.set_hexpand(True)
            header.append(title_box)
            title = Gtk.Label(label="Corne keymap editor")
            title.set_xalign(0)
            title.add_css_class("title-2")
            title_box.append(title)
            subtitle = Gtk.Label(label="Tap · hold · ↻ quick-tap repeat")
            subtitle.set_xalign(0)
            subtitle.add_css_class("dim-label")
            title_box.append(subtitle)

            self._status_label = Gtk.Label()
            self._status_label.set_xalign(1)
            self._status_label.set_valign(Gtk.Align.CENTER)
            header.append(self._status_label)
            self._active_label = Gtk.Label()
            self._active_label.set_xalign(1)
            self._active_label.set_valign(Gtk.Align.CENTER)
            self._active_label.add_css_class("dim-label")
            header.append(self._active_label)

            controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            controls.set_hexpand(True)
            root.append(controls)
            manual_label = Gtk.Label(label="Preview layer:")
            manual_label.add_css_class("dim-label")
            controls.append(manual_label)
            for index, layer in enumerate(self.layout.layers):
                button = Gtk.Button(label=layer.name)
                button.set_action_name("app.show-layer")
                button.set_action_target_value(GLib.Variant("s", str(index)))
                button.set_tooltip_text(f"Show {layer.name} without reading keyboard input")
                controls.append(button)
            spacer = Gtk.Box()
            spacer.set_hexpand(True)
            controls.append(spacer)
            self._follow_button = Gtk.CheckButton(label="Follow keyboard")
            self._follow_button.set_active(True)
            self._follow_button.set_tooltip_text(
                "Automatically show the highest active layer from BlueZ notifications"
            )
            self._follow_button.connect("toggled", self._on_follow_toggled)
            controls.append(self._follow_button)

            operations = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            root.append(operations)
            for label, name in (("Save JSON", "save"), ("Build UF2", "build"),
                                ("Flash left half", "flash")):
                button = Gtk.Button(label=label)
                button.set_action_name(f"app.{name}")
                operations.append(button)
            self._operation_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
            root.append(self._operation_label)
            warning = Gtk.Label(
                label="Saved Studio overrides may mask the compiled keymap. No settings or bonds are reset.",
                xalign=0, wrap=True,
            )
            warning.add_css_class("dim-label")
            root.append(warning)
            self.window.connect("notify::is-active", self._on_window_active)

            self._key_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            self._key_area.set_vexpand(True)
            self._key_area.set_hexpand(True)
            root.append(self._key_area)
            self._refresh_view()
            self._refresh_status()

        def _update_actions(self) -> None:
            idle = not self._operation_running and self._editor is None
            for name in ("save", "build", "edit-key", "show-layer", "follow", "quit", "flash"):
                action = self.lookup_action(name)
                if action is not None:
                    action.set_enabled(idle and (name != "flash" or self._last_uf2 is not None))
            if self._follow_button is not None:
                self._follow_button.set_sensitive(idle)

        def _set_operation_state(self, message: str, running: bool = False) -> None:
            self._operation_running = running
            self._operation_message = message
            self._update_actions()
            self._refresh_status()

        def _on_save_clicked(self, _button: Any) -> None:
            if self._operation_running or self._editor is not None:
                return
            self._last_uf2 = None
            try:
                self._source = save_layout(self.layout_path, self.layout, expected=self._source)
            except LayoutError as exc:
                self._set_operation_state(f"Save failed: {exc}")
                return
            self._set_operation_state(f"Saved {self.layout_path.name}; build again before flashing")

        def _start_subprocess(self, argv: list[str], callback: Callable[..., Any]) -> None:
            try:
                process = Gio.Subprocess.new(
                    argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
                )
                process.communicate_utf8_async(None, None, callback, process)
            except Exception as exc:
                self._last_uf2 = None
                self._set_operation_state(f"Command failed to start: {exc}")

        def _on_build_clicked(self, _button: Any) -> None:
            if self._operation_running or self._editor is not None:
                return
            self._last_uf2 = None
            if self.layout_path != default_layout_path().absolute():
                self._set_operation_state("Build requires the repository Corne layout.json")
                return
            try:
                self._source = save_layout(self.layout_path, self.layout, expected=self._source)
                self._building_inputs = build_inputs(self.layout_path, generated=False)
            except LayoutError as exc:
                self._set_operation_state(f"Cannot build: {exc}")
                return
            self._set_operation_state("Generating keymap…", True)
            generator = str(Path(__file__).resolve().parent / "generate-corne-keymap.py")
            self._start_subprocess([sys.executable, generator], self._on_generation_done)

        def _finish_process(self, result: Any, process: Any) -> tuple[bool, str, str]:
            try:
                communicated, stdout, stderr = process.communicate_utf8_finish(result)
                ok = bool(communicated) and process.get_successful()
                if not ok:
                    status = (f"exit {process.get_exit_status()}" if process.get_if_exited()
                              else "terminated by signal")
                    stderr = f"{status}: {stderr or stdout or 'command failed'}"
                return ok, stdout or "", stderr or ""
            except Exception as exc:
                return False, "", str(exc)

        def _on_generation_done(self, _source: Any, result: Any, process: Any) -> None:
            ok, _stdout, stderr = self._finish_process(result, process)
            if not ok:
                self._last_uf2 = None
                self._set_operation_state(f"Keymap generation failed: {stderr.strip()[-500:]}")
                return
            output = self.build_cache / "blecorne_left-custom.uf2"
            try:
                require_current_inputs(self._building_inputs)
                inputs = build_inputs(self.layout_path)
                data = validate_editable_layout(self.layout)
                config = self.layout_path.parent
                expected = {
                    config / "blecorne.keymap": keymap_generator().render_keymap(data),
                    config / "telemetry/include/corne_layout.h": keymap_generator().render_header(data),
                }
                if any(inputs[path].data != text.encode("utf-8") for path, text in expected.items()):
                    raise LayoutError("generated outputs do not match the saved layout")
                require_current_inputs(self._building_inputs)
                self._building_inputs = inputs
                # A successful callback must never adopt an older output left by a failed build.
                output.unlink(missing_ok=True)
            except (LayoutError, OSError) as exc:
                self._set_operation_state(f"Cannot build: {exc}")
                return
            builder = str(Path(__file__).resolve().parent / "build-corne-firmware.sh")
            self._set_operation_state("Building pinned ZMK firmware…", True)
            self._start_subprocess([builder, "--cache", str(self.build_cache), "--output", str(output)],
                                   self._on_build_done)

        def _on_build_done(self, _source: Any, result: Any, process: Any) -> None:
            self._last_uf2 = None
            ok, _stdout, stderr = self._finish_process(result, process)
            if not ok:
                self._set_operation_state(f"Firmware build failed: {stderr.strip()[-500:]}")
                return
            output = self.build_cache / "blecorne_left-custom.uf2"
            try:
                require_current_inputs(self._building_inputs)
                source = FileSnapshot.read(output)
                image = parse_uf2(source.data)
            except (LayoutError, UF2Error) as exc:
                self._set_operation_state(f"Refusing stale/unsafe UF2: {exc}")
                return
            self._last_uf2 = BuiltFirmware(output, source, self._building_inputs)
            self._set_operation_state(
                f"UF2 ready ({image.block_count} blocks). Double-reset the LEFT half, mount its USB volume, then Flash."
            )

        def _check_build(self) -> bool:
            if self._last_uf2 is None:
                return False
            try:
                self._last_uf2.require_current()
            except LayoutError as exc:
                self._last_uf2 = None
                self._set_operation_state(f"Rebuild required: {exc}")
                return False
            return True

        def _on_window_active(self, window: Any, _property: Any) -> None:
            if window.is_active() and not self._operation_running and self._editor is None:
                self._check_build()

        def _on_flash_clicked(self, _button: Any) -> None:
            if self._operation_running or self._editor is not None or not self._check_build():
                return
            firmware = self._last_uf2
            self._set_operation_state("Writing left UF2 and syncing… Do not unplug or quit.", True)

            def write_firmware() -> None:
                try:
                    firmware.require_current()
                    written = flash_uf2(firmware.path, volume_path=self.flash_volume,
                                        expected_data=firmware.source.data)
                    message = (f"UF2 write/fsync complete ({written} bytes). "
                               "Reboot, BLE reconnection and key behavior are not verified.")
                except UF2SyncUnconfirmed as exc:
                    message = (f"UF2 data submitted ({exc.bytes_written} bytes); USB sync is unconfirmed. "
                               "Verify reboot and typing before retrying. Completion is NOT confirmed.")
                except Exception as exc:
                    message = f"UF2 write/fsync refused or failed: {exc}. Completion is NOT confirmed."
                GLib.idle_add(self._on_flash_done, message)

            try:
                threading.Thread(target=write_firmware, name="corne-uf2-write", daemon=True).start()
            except RuntimeError as exc:
                self._set_operation_state(f"Cannot start UF2 write: {exc}")

        def _on_flash_done(self, message: str) -> bool:
            self._set_operation_state(message)
            return False

        def _request_quit(self, _action: Any) -> None:
            if not self._operation_running and self._editor is None:
                self.quit()

        def _on_close_request(self, _window: Any) -> bool:
            if self._operation_running or self._editor is not None:
                return True
            if self.monitor is not None:
                self.monitor.close()
            return False

        def _action_show_layer(self, _action: Any, parameter: Any) -> None:
            if self._operation_running or self._editor is not None:
                return
            value = parameter.unpack() if hasattr(parameter, "unpack") else parameter
            index = self._layer_index_from_target(str(value))
            if index is None:
                self._on_monitor_error(f"unknown layer action target: {value}")
                return
            self._set_follow(False)
            self._displayed_layer = index
            self._refresh_view()
            self._refresh_status()

        def _action_follow(self, _action: Any, parameter: Any) -> None:
            if self._operation_running or self._editor is not None:
                return
            value = parameter.unpack() if hasattr(parameter, "unpack") else parameter
            self._set_follow(bool(value))

        def _layer_index_from_target(self, target: str) -> int | None:
            target = target.strip().lower()
            if target.isdecimal():
                index = int(target)
                return index if 0 <= index < self.layout.layer_count else None
            for index, layer in enumerate(self.layout.layers):
                name = layer.name.lower()
                if target == name or target == name.split("/")[0].strip():
                    return index
                if target and target == name[0]:
                    return index
            return None

        def _on_follow_toggled(self, button: Any) -> None:
            if self._follow_guard:
                return
            action = self.lookup_action("follow")
            action.activate(GLib.Variant("b", button.get_active()))

        def _set_follow(self, follow: bool) -> None:
            self._follow_keyboard = follow
            if self._follow_button is not None and self._follow_button.get_active() != follow:
                self._follow_guard = True
                self._follow_button.set_active(follow)
                self._follow_guard = False
            if follow and self._last_mask is not None:
                try:
                    self._displayed_layer = highest_active_layer(
                        self._last_mask, self.layout.layer_count
                    )
                except LayerMaskError:
                    pass
            self._refresh_view()
            self._refresh_status()

        def _on_connection_changed(self, connected: bool) -> None:
            self._connected = connected
            if not connected:
                self._last_mask = None
            self._refresh_status()

        def _on_mask_changed(self, mask: int) -> None:
            self._last_mask = mask
            if self._editor is not None:
                self._refresh_status()
                return
            if self._follow_keyboard:
                try:
                    self._displayed_layer = highest_active_layer(
                        mask, self.layout.layer_count
                    )
                except LayerMaskError:
                    return
            self._refresh_view()
            self._refresh_status()

        def _on_monitor_error(self, message: str) -> None:
            self._monitor_error = message
            self._refresh_status()

        def _refresh_status(self) -> None:
            if self._operation_label is not None:
                self._operation_label.set_text(self._operation_message)
            if self._status_label is None or self._active_label is None:
                return
            if self._connected:
                status = "Connected"
                if self._monitor_error:
                    status += f" · {self._monitor_error}"
            else:
                status = "Disconnected"
                if self._monitor_error:
                    status += f" · {self._monitor_error}"
            self._status_label.set_text(status)
            live = self._connected and self._last_mask is not None and self._follow_keyboard
            if live:
                mode = "following keyboard"
            elif self._follow_keyboard:
                mode = "waiting for keyboard"
            else:
                mode = "manual preview"
            layer_name = self.layout.layers[self._displayed_layer].name
            caption = "Active layer" if live else "Preview"
            self._active_label.set_text(f"{caption}: {layer_name} ({mode})")

        def _refresh_view(self) -> None:
            if self._key_area is None or self._editor is not None:
                return
            child = self._key_area.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                self._key_area.remove(child)
                child = next_child

            layer = self.layout.layers[self._displayed_layer]
            unit, gap, padding = 72, 6, 12
            canvas = Gtk.Fixed()
            canvas.set_size_request(14 * unit + 2 * padding, 5 * unit + 2 * padding)
            canvas.set_halign(Gtk.Align.CENTER)
            canvas.set_valign(Gtk.Align.CENTER)
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            scroller.set_hexpand(True)
            scroller.set_vexpand(True)
            scroller.set_child(canvas)
            self._key_area.append(scroller)
            positions = [matrix_key_index(half, row, column)
                         for row in range(3) for half in range(2) for column in range(6)]
            for index in positions + list(range(36, KEY_COUNT)):
                geometry = physical_key_geometry(index)
                button = self._build_key_tile(layer, index)
                button.set_size_request(int(geometry.width * unit - gap),
                                        int(geometry.height * unit - gap))
                canvas.put(button, 0, 0)
                origin = Graphene.Point().init(padding + geometry.origin_x * unit,
                                               padding + geometry.origin_y * unit)
                offset = Graphene.Point().init((geometry.x - geometry.origin_x) * unit + gap / 2,
                                               (geometry.y - geometry.origin_y) * unit + gap / 2)
                transform = Gsk.Transform.new().translate(origin).rotate(geometry.rotation).translate(offset)
                canvas.set_child_transform(button, transform)

        def _build_key_tile(self, layer: Layer, key_index: int) -> Any:
            raw_record = layer.keys[key_index]
            record = resolve_key_record(self.layout, self._displayed_layer, key_index)
            button = Gtk.Button()
            button.add_css_class("corne-key")
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            content.set_halign(Gtk.Align.CENTER)
            content.set_valign(Gtk.Align.CENTER)
            tap = Gtk.Label(label=tap_legend(record, self._displayed_layer))
            tap.add_css_class("corne-tap")
            tap.set_wrap(True)
            tap.set_justify(Gtk.Justification.CENTER)
            content.append(tap)
            if record.hold:
                hold = Gtk.Label(label=record.hold)
                hold.add_css_class("corne-meta")
                content.append(hold)
            if record.hold and record.repeat:
                repeat = Gtk.Label(label=f"↻ {record.repeat}")
                repeat.add_css_class("corne-meta")
                content.append(repeat)
            button.set_child(content)
            button.set_tooltip_text(
                "Quick tap: "
                + _display_value(record.tap)
                + "\nHold: "
                + _display_value(record.hold)
                + "\nRepeat: "
                + _display_value(record.repeat)
            )
            button.set_action_name("app.edit-key")
            button.set_action_target_value(GLib.Variant("(ii)", (self._displayed_layer, key_index)))
            return button

        def _action_edit_key(self, _action: Any, parameter: Any) -> None:
            if self._operation_running or self._editor is not None:
                return
            layer_index, key_index = parameter.unpack()
            if not 0 <= layer_index < self.layout.layer_count or not 0 <= key_index < KEY_COUNT:
                self._set_operation_state("Edit rejected: key position is out of range")
                return
            # Capture the immutable target now, not whichever layer BLE reports on Apply.
            raw_record = self.layout.layers[layer_index].keys[key_index]
            resolved_record = resolve_key_record(self.layout, layer_index, key_index)
            editor = Gtk.Window(application=self, transient_for=self.window, modal=True)
            editor.set_title(f"{self.layout.layers[layer_index].name} · Key {key_index + 1}")
            editor.add_css_class("corne-viewer")
            editor.set_default_size(460, 300)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            for side in ("top", "bottom", "start", "end"):
                getattr(box, f"set_margin_{side}")(12)
            editor.set_child(box)
            entries: dict[str, Any] = {}
            for field_name, label, value in (
                ("tap", "Quick tap", raw_record.tap),
                ("hold", "Hold", raw_record.hold),
                ("repeat", "Repeat", raw_record.repeat),
                ("binding", "Exact binding", raw_record.binding),
            ):
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                caption = Gtk.Label(label=label, xalign=0, width_chars=12)
                row.append(caption)
                entry = Gtk.Entry(text=value, hexpand=True)
                caption.set_mnemonic_widget(entry)
                entries[field_name] = entry
                row.append(entry)
                box.append(row)
            if raw_record.is_transparent and raw_record != resolved_record:
                box.append(Gtk.Label(label="Preview inherits from a lower layer; editing the raw &trans record.",
                                     xalign=0, wrap=True))
            error = Gtk.Label(xalign=0, wrap=True)
            box.append(error)
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            apply_button = Gtk.Button(label="Apply")
            cancel_button = Gtk.Button(label="Cancel")
            actions.append(apply_button)
            actions.append(cancel_button)
            box.append(actions)

            def close_editor(*_args: Any) -> bool:
                if self._operation_running:
                    return True
                self._editor = None
                editor.destroy()
                self._update_actions()
                self._set_follow(self._follow_keyboard)
                return True

            def apply_edit(*_args: Any) -> None:
                if self._operation_running:
                    return
                try:
                    edited = edit_layout_key(
                        self.layout, layer_index, key_index,
                        **{name: entry.get_text() for name, entry in entries.items()},
                    )
                except LayoutError as exc:
                    error.set_text(f"Edit rejected: {exc}")
                    self._set_operation_state(f"Edit rejected: {exc}")
                    return
                self.layout = edited
                self._last_uf2 = None
                self._set_operation_state("Edited in memory; save JSON to keep it")
                close_editor()

            apply_button.connect("clicked", apply_edit)
            cancel_button.connect("clicked", close_editor)
            editor.connect("close-request", close_editor)
            self._editor = editor
            self._update_actions()
            editor.present()

    return CorneViewerApplication()


def _display_value(value: str) -> str:
    return value if value else "—"


def tap_legend(record: KeyRecord, layer_index: int) -> str:
    """Show US-QWERTY punctuation pairs on Base without changing bindings."""
    shifted = {"=": "+", "-": "_", ";": ":", "'": '"',
               "`": "~", ",": "<", ".": ">", "/": "?"}
    if layer_index == 0 and record.tap in shifted:
        return f"{record.tap} {shifted[record.tap]}"
    return _display_value(record.tap)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        layout = load_layout(args.layout)
        application = create_application(layout, layout_path=args.layout, flash_volume=args.flash_volume,
                                         build_cache=args.build_cache)
    except (LayoutError, ValueError) as exc:
        print(f"corne-keymap-viewer: {exc}", file=sys.stderr)
        return 2

    # argparse owns --layout/--help.  Gtk only needs the program name here so
    # it does not try to parse the viewer's already-consumed command line.
    program_name = sys.argv[0] if argv is None else "corne-keymap-viewer"
    return int(application.run([program_name]))


if __name__ == "__main__":
    raise SystemExit(main())
