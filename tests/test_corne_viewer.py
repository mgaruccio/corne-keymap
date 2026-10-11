#!/usr/bin/env python3
"""Unit coverage for the pure Corne keymap viewer contract."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "corne-keymap-viewer.py"
SPEC = importlib.util.spec_from_file_location("corne_keymap_viewer", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
viewer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = viewer
SPEC.loader.exec_module(viewer)


def record(label: str, *, binding: str | None = None) -> dict[str, str]:
    return {
        "tap": label,
        "hold": f"hold-{label}",
        "repeat": f"repeat-{label}",
        "binding": binding if binding is not None else label,
    }


def fixture_data() -> dict[str, object]:
    base = [record(f"base-{index}") for index in range(viewer.KEY_COUNT)]
    navigation = [record(f"nav-{index}") for index in range(viewer.KEY_COUNT)]
    numbers = [record(f"num-{index}") for index in range(viewer.KEY_COUNT)]
    navigation[3] = {
        "tap": viewer.TRANSPARENT_TAP,
        "hold": "",
        "repeat": "",
        "binding": viewer.TRANSPARENT_BINDING,
    }
    numbers[3] = {
        "tap": viewer.TRANSPARENT_TAP,
        "hold": "",
        "repeat": "",
        "binding": viewer.TRANSPARENT_BINDING,
    }
    return {
        "service_uuid": viewer.SERVICE_UUID,
        "characteristic_uuid": viewer.CHARACTERISTIC_UUID,
        "layers": [
            {"name": "Base", "keys": base},
            {"name": "Navigation", "keys": navigation},
            {"name": "Numbers / Utilities", "keys": numbers},
        ],
    }


class LayoutContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.layout = viewer.parse_layout_data(fixture_data())

    def test_canonical_shape_has_three_layers_and_42_keys(self) -> None:
        self.assertEqual(self.layout.layer_count, 3)
        self.assertEqual(tuple(layer.name for layer in self.layout.layers), ("Base", "Navigation", "Numbers / Utilities"))
        self.assertTrue(all(len(layer.keys) == viewer.KEY_COUNT for layer in self.layout.layers))

    def test_physical_halves_follow_interleaved_firmware_rows(self) -> None:
        expected = (
            ((0, 1, 2, 3, 4, 5), (12, 13, 14, 15, 16, 17), (24, 25, 26, 27, 28, 29)),
            ((6, 7, 8, 9, 10, 11), (18, 19, 20, 21, 22, 23), (30, 31, 32, 33, 34, 35)),
        )
        positions = []
        for half in range(2):
            for row in range(3):
                actual = tuple(viewer.matrix_key_index(half, row, column) for column in range(6))
                self.assertEqual(actual, expected[half][row])
                positions.extend(actual)
        self.assertEqual(sorted(positions), list(range(36)))
        with self.assertRaises(IndexError):
            viewer.matrix_key_index(2, 0, 0)

    def test_corne_geometry_is_staggered_and_mirrored(self) -> None:
        left_stagger = (0.37, 0.37, 0.12, 0.0, 0.12, 0.24)
        for row in range(3):
            for column in range(6):
                left = viewer.physical_key_geometry(viewer.matrix_key_index(0, row, column))
                right = viewer.physical_key_geometry(viewer.matrix_key_index(1, row, 5 - column))
                self.assertAlmostEqual(left.y, row + left_stagger[column])
                self.assertAlmostEqual(left.x + left.width + right.x, 14.0)
                self.assertEqual(left.y, right.y)
        thumbs = [viewer.physical_key_geometry(index) for index in range(36, 42)]
        self.assertEqual(tuple(key.rotation for key in thumbs), (0, 12, 24, -24, -12, 0))
        self.assertEqual(thumbs[2].height, 1.5)
        self.assertEqual(thumbs[3].height, 1.5)
        for left, right in zip(thumbs[:3], reversed(thumbs[3:])):
            self.assertAlmostEqual(left.x + left.width + right.x, 14.0)
            self.assertEqual(left.y, right.y)
            self.assertEqual(left.rotation, -right.rotation)
        with self.assertRaises(IndexError):
            viewer.physical_key_geometry(42)

    def test_base_punctuation_legends_show_qwerty_shift_pairs(self) -> None:
        layout = viewer.load_layout(ROOT / "configs/corne/layout.json")
        expected = {11: "= +", 22: "; :", 23: "' \"", 24: "` ~",
                    32: ", <", 33: ". >", 34: "/ ?", 35: "- _"}
        for index, legend in expected.items():
            key = layout.layers[0].keys[index]
            with self.subTest(index=index):
                before = (key.tap, key.hold, key.repeat, key.binding)
                self.assertEqual(viewer.tap_legend(key, 0), legend)
                for layer_index in (1, 2):
                    self.assertEqual(viewer.tap_legend(key, layer_index), key.tap)
                self.assertEqual((key.tap, key.hold, key.repeat, key.binding), before)
        for index in (0, 1, 36, 41):
            key = layout.layers[0].keys[index]
            self.assertEqual(viewer.tap_legend(key, 0), key.tap)

    def test_transparent_records_resolve_to_nearest_lower_layer(self) -> None:
        self.assertEqual(
            viewer.resolve_key_record(self.layout, 1, 3).tap,
            "base-3",
        )
        self.assertEqual(
            viewer.resolve_key_record(self.layout, 2, 3).tap,
            "base-3",
        )
        self.assertEqual(
            viewer.resolve_key_record(self.layout, 2, 4).tap,
            "num-4",
        )

    def test_exact_four_byte_little_endian_masks(self) -> None:
        self.assertEqual(viewer.parse_layer_mask(bytes((0x05, 0, 0, 0)), 3), 0x05)
        self.assertEqual(viewer.highest_active_layer(0x05, 3), 2)
        self.assertEqual(viewer.highest_active_layer(0x03, 3), 1)

    def test_invalid_masks_are_rejected(self) -> None:
        invalid_payloads = (
            b"",
            b"\x01",
            b"\x01\x00\x00\x00\x00",
            b"\x00\x00\x00\x00",
            b"\x09\x00\x00\x00",
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(viewer.LayerMaskError):
                    viewer.parse_layer_mask(payload, 3)
        with self.assertRaises(viewer.LayerMaskError):
            viewer.highest_active_layer(0x09, 3)

    def test_layout_rejects_wrong_key_count(self) -> None:
        data = fixture_data()
        data["layers"][0]["keys"] = data["layers"][0]["keys"][:-1]  # type: ignore[index]
        with self.assertRaises(viewer.LayoutError):
            viewer.parse_layout_data(data)


class CommandLineTests(unittest.TestCase):
    def test_help_is_available_without_gtk_startup(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--layout", result.stdout)
        self.assertIn("Read-only GTK4", result.stdout)

    def test_fixture_can_be_loaded_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "layout.json"
            path.write_text(json.dumps(fixture_data()), encoding="utf-8")
            layout = viewer.load_layout(path)
            self.assertEqual(layout.layers[2].keys[3].binding, viewer.TRANSPARENT_BINDING)


class _CharacteristicProxy:
    def __init__(self):
        self.calls = []
        self.finished = []

    def call(self, *args):
        self.calls.append(args)

    def call_finish(self, result):
        self.finished.append(result)
        if isinstance(result, Exception):
            raise result
        return result


class MonitorCallbackTests(unittest.TestCase):
    """Transport fakes with real Gio/GLib types; not live keyboard evidence."""

    def setUp(self) -> None:
        self.masks = []
        self.errors = []
        try:
            self.monitor = viewer.BlueZMonitor(
                viewer.parse_layout_data(fixture_data()),
                on_mask_changed=self.masks.append,
                on_error=self.errors.append,
            )
        except RuntimeError as exc:
            self.skipTest(str(exc))
        self.proxy = _CharacteristicProxy()
        self.path = "/custom_layer_characteristic"
        self.monitor._characteristic_proxy = self.proxy
        self.monitor._characteristic_path = self.path

    def read_token(self):
        return (self.monitor._generation, self.path, self.proxy, self.monitor._value_serial)

    def test_initial_read_uses_real_glib_variant(self) -> None:
        self.monitor._read_initial_value(self.monitor._generation, self.path, self.proxy)
        self.assertEqual(self.errors, [])
        self.assertEqual(len(self.proxy.calls), 1)
        method, arguments, *_ = self.proxy.calls[0]
        self.assertEqual(method, "ReadValue")
        self.assertIsInstance(arguments, self.monitor._GLib.Variant)
        self.assertEqual(arguments.get_type_string(), "(a{sv})")
        self.assertEqual(arguments.unpack(), ({},))

    def test_stale_notify_success_is_finished_and_abandoned_session_stopped(self) -> None:
        token = (self.monitor._generation, self.path, self.proxy)
        self.monitor._start_notify_pending = True
        self.monitor._detach()
        self.assertFalse(self.monitor._start_notify_pending)
        self.monitor._on_start_notify_done(self.proxy, (), token)
        self.assertEqual(self.proxy.finished, [()])
        self.assertEqual([call[0] for call in self.proxy.calls], ["StopNotify"])
        self.assertFalse(self.monitor._notify_started)

    def test_stale_notify_does_not_cancel_new_same_path_session(self) -> None:
        token = (self.monitor._generation, self.path, self.proxy)
        old_proxy = self.proxy
        self.monitor._generation += 1
        self.monitor._characteristic_proxy = _CharacteristicProxy()
        for started, pending in ((True, False), (False, True)):
            with self.subTest(started=started, pending=pending):
                self.monitor._notify_started = started
                self.monitor._start_notify_pending = pending
                self.monitor._on_start_notify_done(old_proxy, (), token)
                self.assertEqual(old_proxy.calls, [])
                self.assertEqual(self.monitor._notify_started, started)
                self.assertEqual(self.monitor._start_notify_pending, pending)
        self.assertEqual(old_proxy.finished, [(), ()])

    def test_invalid_notification_does_not_suppress_valid_initial_read(self) -> None:
        token = self.read_token()
        self.monitor._consume_value(b"\x01", source="notification")
        self.assertEqual(self.monitor._value_serial, token[3])
        self.monitor._on_read_value_done(self.proxy, (b"\x05\x00\x00\x00",), token)
        self.assertEqual(self.masks, [5])
        self.assertEqual(self.monitor.last_mask, 5)
        self.assertEqual(self.errors[-1], "")

    def test_valid_notification_wins_over_older_initial_read(self) -> None:
        token = self.read_token()
        self.monitor._consume_value(b"\x03\x00\x00\x00", source="notification")
        self.monitor._on_read_value_done(self.proxy, (b"\x01\x00\x00\x00",), token)
        self.assertEqual(self.masks, [3])
        self.assertEqual(self.monitor.last_mask, 3)
        self.assertEqual(len(self.proxy.finished), 1)

    def test_stale_read_is_finished_without_changing_preview(self) -> None:
        token = self.read_token()
        self.monitor._generation += 1
        self.monitor._on_read_value_done(self.proxy, (b"\x05\x00\x00\x00",), token)
        self.assertEqual(len(self.proxy.finished), 1)
        self.assertEqual(self.masks, [])
        self.assertIsNone(self.monitor.last_mask)


if __name__ == "__main__":
    unittest.main()
