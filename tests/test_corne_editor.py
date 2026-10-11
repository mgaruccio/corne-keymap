#!/usr/bin/env python3
"""Targeted editor/UF2 checks. GTK cases run with xvfb-run; never touch hardware."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import corne_firmware as firmware

spec = importlib.util.spec_from_file_location("corne_editor_viewer", ROOT / "scripts/corne-keymap-viewer.py")
viewer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = viewer
spec.loader.exec_module(viewer)
CONFIG = ROOT / "configs/corne"

try:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, GLib, Gtk
    HAS_GTK = Gtk.init_check()
except (ImportError, ValueError):
    HAS_GTK = False


def block(*, target=0x1000, number=0, count=1, size=256, flags=0x2000,
          family=0xADA52840, payload=b"a"):
    raw = bytearray(512)
    struct.pack_into("<8I", raw, 0, firmware.UF2_MAGIC_START0, firmware.UF2_MAGIC_START1,
                     flags, target, size, number, count, family)
    raw[32:288] = payload * 256
    struct.pack_into("<I", raw, 508, firmware.UF2_MAGIC_END)
    return bytes(raw)


class TemporaryCase(unittest.TestCase):
    def setUp(self):
        cache = CONFIG / ".cache"
        cache.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="editor-test-", dir=cache)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)


class EditorDataTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.path = self.root / "layout.json"
        shutil.copy2(CONFIG / "layout.json", self.path)
        self.layout = viewer.load_layout(self.path)

    def test_compact_roundtrip_mode_and_unique_temp_safety(self):
        original = self.path.read_bytes()
        self.assertEqual(viewer.format_layout(self.layout), original)
        self.path.chmod(0o640)
        expected = viewer.FileSnapshot.read(self.path)
        victim = self.root / "untouched"
        victim.write_text("not a temporary file")
        self.path.with_suffix(".json.tmp").symlink_to(victim)
        collision = self.root / ".layout.json.already.tmp"
        collision.write_text("also untouched")
        edited = viewer.edit_layout_key(self.layout, 0, 11, tap="0", hold="", repeat="0", binding="&kp N0")
        result = viewer.save_layout(self.path, edited, expected=expected)
        self.assertEqual(viewer.load_layout(self.path), edited)
        self.assertEqual(result, viewer.FileSnapshot.read(self.path))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        before, after = original.splitlines(), self.path.read_bytes().splitlines()
        self.assertEqual(len(before), len(after))
        self.assertEqual(sum(a != b for a, b in zip(before, after)), 1)
        self.assertEqual(victim.read_text(), "not a temporary file")
        self.assertEqual(collision.read_text(), "also untouched")
        self.assertEqual(list(self.root.glob(".layout.json.*.tmp")), [collision])
        self.assertEqual(self.layout.layers[0].keys[11].binding, "&kp EQUAL")

    def test_apply_and_save_use_generator_validation(self):
        raw = viewer.edit_layout_key(self.layout, 1, 0, binding="&kp LC(LEFT)", tap="Left")
        self.assertEqual(raw.layers[1].keys[0].binding, "&kp LC(LEFT)")
        transparent = viewer.edit_layout_key(self.layout, 0, 11, tap="↳", hold="", repeat="", binding="&trans")
        viewer.keymap_generator().validate(viewer.layout_to_data(transparent))
        for fields in ({"binding": "&kp Q>; &bt BT_CLR"}, {"binding": "&kp Q\n"},
                       {"binding": "&trans"}, {"tap": 123}, {"extra": "x"}):
            with self.subTest(fields=fields), self.assertRaises(viewer.LayoutError):
                viewer.edit_layout_key(self.layout, 0, 11, **fields)
        two_layers = viewer.Layout(self.layout.service_uuid, self.layout.characteristic_uuid, self.layout.layers[:2])
        with self.assertRaisesRegex(viewer.LayoutError, "three layers"):
            viewer.save_layout(self.path, two_layers, expected=viewer.FileSnapshot.read(self.path))
        self.assertEqual(viewer.load_layout(self.path), self.layout)

    def test_external_conflict_and_symlink_are_not_overwritten(self):
        original = viewer.FileSnapshot.read(self.path)
        self.path.write_bytes(original.data + b"\n")
        with self.assertRaisesRegex(viewer.LayoutError, "changed externally"):
            viewer.save_layout(self.path, self.layout, expected=original)
        external = self.root / "external.json"
        external.write_bytes(original.data)
        self.path.unlink()
        self.path.symlink_to(external)
        with self.assertRaises(viewer.LayoutError):
            viewer.save_layout(self.path, self.layout, expected=original)
        self.assertEqual(external.read_bytes(), original.data)
        self.assertTrue(self.path.is_symlink())

    def test_conflict_during_save_cleans_temp_and_keeps_external_edit(self):
        original = viewer.FileSnapshot.read(self.path)
        edited = viewer.edit_layout_key(self.layout, 0, 11, binding="&kp N0")
        with patch.object(viewer.os, "fsync", side_effect=lambda _fd: self.path.write_text("external edit")):
            with self.assertRaisesRegex(viewer.LayoutError, "changed externally"):
                viewer.save_layout(self.path, edited, expected=original)
        self.assertEqual(self.path.read_text(), "external edit")
        self.assertEqual(list(self.root.glob(".layout.json.*.tmp")), [])

    def test_save_fsync_failure_does_not_replace_source(self):
        original = viewer.FileSnapshot.read(self.path)
        edited = viewer.edit_layout_key(self.layout, 0, 11, binding="&kp N0")
        with patch.object(viewer.os, "fsync", side_effect=OSError("sync failed")):
            with self.assertRaisesRegex(viewer.LayoutError, "sync failed"):
                viewer.save_layout(self.path, edited, expected=original)
        self.assertEqual(viewer.FileSnapshot.read(self.path), original)
        self.assertEqual(list(self.root.glob(".layout.json.*.tmp")), [])

    def test_edited_json_through_real_generator_command(self):
        config = self.root / "configs/corne"
        config.mkdir(parents=True)
        script = self.root / "scripts/generate-corne-keymap.py"
        script.parent.mkdir()
        shutil.copy2(ROOT / "scripts/generate-corne-keymap.py", script)
        path = config / "layout.json"
        shutil.copy2(self.path, path)
        edited = viewer.edit_layout_key(self.layout, 0, 11, tap="0", hold="", repeat="0", binding="&kp N0")
        viewer.save_layout(path, edited, expected=viewer.FileSnapshot.read(path))
        for args in ([], ["--check"]):
            result = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((config / "blecorne.keymap").read_text(),
                         viewer.keymap_generator().render_keymap(viewer.layout_to_data(edited)))
        self.assertIn("&kp N0", (config / "blecorne.keymap").read_text())

    def test_build_cache_cli(self):
        self.assertEqual(viewer.parse_args([]).build_cache, CONFIG / ".cache")
        self.assertEqual(viewer.parse_args(["--build-cache", str(self.root)]).build_cache, self.root)


class UF2Tests(unittest.TestCase):
    def test_mountinfo_device_filesystem_order_and_escaped_mountpoint(self):
        text = "1 2 8:17 / /media/Corne\\040Left rw - vfat /dev/sdb1 rw\n"
        with patch.object(Path, "read_text", return_value=text):
            self.assertEqual(list(firmware._mount_entries()), [(Path("/media/Corne Left"), "vfat", "/dev/sdb1")])

    def test_full_partition_not_previous_image_size(self):
        data = block(count=3) + block(number=1, count=3, target=0x40000) + block(number=2, count=3, target=0xD3F00)
        image = firmware.parse_uf2(data)
        self.assertEqual(image.block_count, 3)
        self.assertEqual(image.target_end, 0xD4000)
        self.assertEqual(image.raw, data)

    def test_magic_and_partial_images(self):
        for offset in (0, 4, 508):
            data = bytearray(block())
            struct.pack_into("<I", data, offset, 0)
            with self.subTest(offset=offset), self.assertRaises(firmware.UF2Error):
                firmware.parse_uf2(bytes(data))
        for data in (b"", block()[:-1], block() + b"x"):
            with self.assertRaises(firmware.UF2Error):
                firmware.parse_uf2(data)

    def test_flags_family_counts_and_sizes(self):
        cases = [{"flags": flag} for flag in (0, 1, 0x2001, 0x3000, 0x6000, 0xA000)]
        cases += [{"family": 0}, {"count": 0}, {"count": 2}, {"number": 1},
                  {"size": 0}, {"size": 477}, {"target": 0x1001}]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(firmware.UF2Error):
                firmware.parse_uf2(block(**kwargs))
        for data in (block(count=2) + block(number=0, count=2, target=0x1100),
                     block(count=2) + block(number=1, count=3, target=0x1100),
                     block(count=2) + block(number=1, count=2, target=0x1080)):
            with self.assertRaises(firmware.UF2Error):
                firmware.parse_uf2(data)

    def test_protected_nvs_bootloader_and_application_start(self):
        for target in (0, 0xD3FFC, 0xD4000, 0xF4000, 0x100000, 0xFFFFFF00):
            data = block(count=2) + block(number=1, count=2, target=target)
            with self.subTest(target=target), self.assertRaises(firmware.UF2Error):
                firmware.parse_uf2(data)
        with self.assertRaisesRegex(firmware.UF2Error, "begin"):
            firmware.parse_uf2(block(target=0x2000))


class BootVolumeTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.mount = self.root / "boot"
        self.mount.mkdir()
        self.info = self.mount / "INFO_UF2.TXT"
        self.info.write_text("UF2 Bootloader\nBoard-ID: NRF52840_KB\n")
        self.path = self.root / "left.uf2"
        self.path.write_bytes(block())
        self.destination = self.mount / firmware.UF2_FILENAME
        self.source = "/dev/corne-editor-test-no-hardware"
        self.identity = (Path("/sys/corne-editor-test"), "239a", "009f", firmware.expected_left_serial())
        self.device = SimpleNamespace(st_mode=stat.S_IFBLK | 0o600,
                                      st_rdev=self.mount.stat().st_dev, st_dev=self.mount.stat().st_dev + 999)
        real_stat = os.stat

        def device_stat(path, *args, **kwargs):
            return self.device if str(path) == self.source else real_stat(path, *args, **kwargs)

        for mocker in (patch.object(firmware, "_mount_entries", return_value=[(self.mount, "vfat", self.source)]),
                       patch.object(firmware, "_usb_identity_for_source", return_value=self.identity),
                       patch.object(firmware.os, "stat", side_effect=device_stat)):
            mocker.start()
            self.addCleanup(mocker.stop)

    def test_discovery_uses_rdev_not_device_node_filesystem(self):
        volume = firmware.find_left_boot_volume()
        self.assertEqual(volume.path, self.mount)
        volume.close()
        self.device.st_rdev += 1
        with self.assertRaises(firmware.UF2Error):
            firmware.find_left_boot_volume()

    def test_wrong_identity_board_and_nonblock_source(self):
        for index, value in ((1, "ffff"), (2, "0000"), (3, "RIGHT-HALF")):
            identity = list(self.identity)
            identity[index] = value
            with patch.object(firmware, "_usb_identity_for_source", return_value=tuple(identity)):
                with self.subTest(index=index), self.assertRaises(firmware.UF2Error):
                    firmware.find_left_boot_volume()
        self.info.write_text("Board-ID: WRONG\n")
        with self.assertRaises(firmware.UF2Error):
            firmware.find_left_boot_volume()
        self.info.write_text("Board-ID: NRF52840_KB\n")
        self.device.st_mode = stat.S_IFREG | 0o600
        with self.assertRaises(firmware.UF2Error):
            firmware.find_left_boot_volume()

    def test_symlink_volume_info_destination_and_source_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.mount, target_is_directory=True)
        self.assertIsNone(firmware._open_volume(alias, self.source, "vfat"))
        info = self.root / "info"
        self.info.rename(info)
        self.info.symlink_to(info)
        with self.assertRaises(firmware.UF2Error):
            firmware.find_left_boot_volume()
        self.info.unlink()
        info.rename(self.info)
        victim = self.root / "victim"
        victim.write_bytes(b"untouched")
        self.destination.symlink_to(victim)
        with self.assertRaises(OSError):
            firmware.flash_uf2(self.path)
        self.assertEqual(victim.read_bytes(), b"untouched")
        self.destination.unlink()
        source_alias = self.root / "source-alias"
        source_alias.symlink_to(self.path)
        with self.assertRaises(firmware.UF2Error):
            firmware.flash_uf2(source_alias)
        self.assertFalse(self.destination.exists())

    def test_revalidates_mount_identity_and_info_before_copy(self):
        wrong = (*self.identity[:3], "RIGHT-HALF")
        with patch.object(firmware, "_usb_identity_for_source", side_effect=[self.identity, wrong]):
            with self.assertRaisesRegex(firmware.UF2Error, "identity changed"):
                firmware.flash_uf2(self.path)
        with patch.object(firmware, "_mount_entries", side_effect=[[(self.mount, "vfat", self.source)], []]):
            with self.assertRaisesRegex(firmware.UF2Error, "mount changed"):
                firmware.flash_uf2(self.path)
        find = firmware.find_left_boot_volume

        def change_info(*args):
            volume = find(*args)
            self.info.write_text("Board-ID: WRONG\n")
            return volume

        with patch.object(firmware, "find_left_boot_volume", side_effect=change_info):
            with self.assertRaisesRegex(firmware.UF2Error, "identity changed"):
                firmware.flash_uf2(self.path)
        self.assertFalse(self.destination.exists())

    def test_replaced_mount_directory_is_rejected(self):
        find = firmware.find_left_boot_volume

        def replace_mount(*args):
            volume = find(*args)
            self.mount.rename(self.root / "old-boot")
            self.mount.mkdir()
            self.info.write_text("Board-ID: NRF52840_KB\n")
            return volume

        with patch.object(firmware, "find_left_boot_volume", side_effect=replace_mount):
            with self.assertRaisesRegex(firmware.UF2Error, "replaced"):
                firmware.flash_uf2(self.path)
        self.assertFalse(self.destination.exists())

    def test_source_change_before_validation_refused_and_after_uses_checked_bytes(self):
        expected = self.path.read_bytes()
        self.path.write_bytes(block(payload=b"b"))
        with self.assertRaisesRegex(firmware.UF2Error, "changed since"):
            firmware.flash_uf2(self.path, expected_data=expected)
        self.assertFalse(self.destination.exists())
        self.path.write_bytes(expected)
        find = firmware.find_left_boot_volume

        def change_source(*args):
            self.path.write_bytes(block(payload=b"b"))
            return find(*args)

        with patch.object(firmware, "find_left_boot_volume", side_effect=change_source):
            written = firmware.flash_uf2(self.path, expected_data=expected)
        self.assertEqual(written, len(expected))
        self.assertEqual(self.destination.read_bytes(), expected)

    def test_partial_writes_and_fsync_success_or_failure(self):
        real_write = os.write
        with patch.object(firmware.os, "write", side_effect=lambda fd, data: real_write(fd, data[:97])):
            with patch.object(firmware.os, "fsync", wraps=os.fsync) as sync:
                self.assertEqual(firmware.flash_uf2(self.path), 512)
                sync.assert_called_once()
        self.assertEqual(self.destination.read_bytes(), self.path.read_bytes())
        with patch.object(firmware.os, "fsync", side_effect=OSError("USB disappeared during fsync")):
            with self.assertRaises(firmware.UF2SyncUnconfirmed) as caught:
                firmware.flash_uf2(self.path)
            self.assertEqual(caught.exception.bytes_written, 512)
            self.assertIn("fsync", str(caught.exception))
        with patch.object(firmware.os, "write", side_effect=OSError("USB write failed")):
            with self.assertRaises(OSError) as caught:
                firmware.flash_uf2(self.path)
            self.assertNotIsInstance(caught.exception, firmware.UF2SyncUnconfirmed)
        with patch.object(firmware.os, "write", return_value=0):
            with self.assertRaisesRegex(firmware.UF2Error, "stopped accepting"):
                firmware.flash_uf2(self.path)


class QuietMonitor:
    def __init__(self, *_args, **_kwargs):
        pass

    def start(self):
        pass

    def close(self):
        pass


def widgets(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from widgets(child)
        child = child.get_next_sibling()


@unittest.skipUnless(HAS_GTK, "GTK4 display required; run with xvfb-run")
class GtkEditorTests(TemporaryCase):
    def setUp(self):
        super().setUp()
        self.config = self.root / "configs/corne"
        shutil.copytree(CONFIG, self.config, ignore=shutil.ignore_patterns(".cache", "tests"))
        self.path = self.config / "layout.json"
        self.cache = self.root / "warm-cache"
        self.cache.mkdir()
        self.output = self.cache / "blecorne_left-custom.uf2"
        self.app = viewer.create_application(viewer.load_layout(self.path), layout_path=self.path,
                                             build_cache=self.cache, monitor_factory=QuietMonitor)
        self.app.set_application_id(f"com.mike.CorneEditorTest{time.monotonic_ns()}")
        self.app.register()
        self.app.activate()
        self.addCleanup(self.close_app)

    def close_app(self):
        if self.app._editor is not None:
            self.app._editor.close()
        self.app.window.destroy()
        self.app.quit()

    def wait_until(self, predicate):
        if predicate():
            return
        loop = GLib.MainLoop()
        deadline = time.monotonic() + 4

        def check():
            if predicate() or time.monotonic() >= deadline:
                loop.quit()
                return False
            return True

        GLib.timeout_add(5, check)
        loop.run()
        self.assertTrue(predicate(), "GLib callback did not finish")

    def action(self, name, parameter=None):
        self.app.lookup_action(name).activate(parameter)

    def editor_fields(self):
        entries = [widget for widget in widgets(self.app._editor) if isinstance(widget, Gtk.Entry)]
        self.assertEqual(len(entries), 4)
        return dict(zip(("tap", "hold", "repeat", "binding"), entries))

    def editor_button(self, label):
        button = next(widget for widget in widgets(self.app._editor)
                      if isinstance(widget, Gtk.Button) and widget.get_label() == label)
        button.emit("clicked")

    def ready(self):
        self.output.write_bytes(block())
        self.app._last_uf2 = viewer.BuiltFirmware(self.output, viewer.FileSnapshot.read(self.output),
                                                viewer.build_inputs(self.path))
        self.app._set_operation_state("Ready")

    def process(self, code, callback):
        process = Gio.Subprocess.new([sys.executable, "-c", code],
                                     Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
        process.communicate_utf8_async(None, None, callback, process)

    def test_real_widgets_edit_immutable_target_despite_ble_and_save(self):
        self.ready()
        self.action("edit-key", GLib.Variant("(ii)", (0, 11)))
        editor = self.app._editor
        fields = self.editor_fields()
        for name, value in {"tap": "0", "hold": "", "repeat": "0", "binding": "&kp N0"}.items():
            fields[name].set_text(value)
        self.app._on_mask_changed(3)
        self.assertIs(self.app._editor, editor)
        self.assertEqual(self.app._displayed_layer, 0)
        self.assertFalse(self.app.lookup_action("save").get_enabled())
        self.editor_button("Apply")
        self.assertEqual(self.app.layout.layers[0].keys[11].binding, "&kp N0")
        self.assertEqual(self.app.layout.layers[1].keys[11].binding, "&trans")
        self.assertEqual(self.app._displayed_layer, 1)
        self.assertIsNone(self.app._last_uf2)
        self.action("save")
        self.assertEqual(viewer.load_layout(self.path).layers[0].keys[11].binding, "&kp N0")
        self.assertEqual(len(self.path.read_text().splitlines()), 147)

    def test_raw_transparent_editor_cancel_and_invalid_apply_do_not_mutate(self):
        before, source = self.app.layout, self.path.read_bytes()
        self.action("edit-key", GLib.Variant("(ii)", (1, 0)))
        fields = self.editor_fields()
        self.assertEqual([entry.get_text() for entry in fields.values()], ["↳", "", "", "&trans"])
        fields["binding"].set_text("&kp Q>; &bt BT_CLR")
        self.editor_button("Apply")
        self.assertIs(self.app.layout, before)
        self.assertIn("Edit rejected", self.app._operation_message)
        self.app._on_save_clicked(None)
        self.editor_button("Cancel")
        self.assertIsNone(self.app._editor)
        self.assertIs(self.app.layout, before)
        self.assertEqual(self.path.read_bytes(), source)

    def test_busy_guards_actions_direct_handlers_and_window_close(self):
        self.ready()
        before = self.path.read_bytes()
        self.app._set_operation_state("Building…", True)
        with patch.object(self.app, "quit") as quit_app, patch.object(self.app, "_start_subprocess") as start:
            for name in ("save", "build", "flash", "quit", "edit-key"):
                self.assertFalse(self.app.lookup_action(name).get_enabled())
            self.app._on_save_clicked(None)
            self.app._on_build_clicked(None)
            self.app._on_flash_clicked(None)
            self.app._request_quit(None)
            self.app._action_edit_key(None, GLib.Variant("(ii)", (0, 11)))
            self.assertTrue(self.app._on_close_request(self.app.window))
            self.assertIsNone(self.app._editor)
            quit_app.assert_not_called()
            start.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.app._set_operation_state("Done")

    def test_operation_status_survives_ble_callbacks(self):
        self.app._set_operation_state("Firmware build failed: exit 9")
        self.app._on_connection_changed(True)
        self.app._on_mask_changed(3)
        self.app._on_monitor_error("")
        self.app._on_connection_changed(False)
        self.assertEqual(self.app._operation_label.get_text(), "Firmware build failed: exit 9")

    def test_real_gio_nonzero_exit_never_adopts_stale_uf2(self):
        for callback in (self.app._on_generation_done, self.app._on_build_done):
            self.ready()
            self.app._set_operation_state("Running", True)
            self.process("import sys; print('intentional failure', file=sys.stderr); sys.exit(7)", callback)
            self.wait_until(lambda: not self.app._operation_running)
            self.assertIsNone(self.app._last_uf2)
            self.assertFalse(self.app.lookup_action("flash").get_enabled())
            self.assertIn("exit 7", self.app._operation_message)
            self.assertIn("intentional failure", self.app._operation_message)

    def test_communicate_true_is_not_exit_success(self):
        observed = []
        def finished(_source, result, process):
            communicated, _stdout, _stderr = process.communicate_utf8_finish(result)
            observed.extend([communicated, process.get_successful()])
        self.process("raise SystemExit(4)", finished)
        self.wait_until(lambda: len(observed) == 2)
        self.assertEqual(observed, [True, False])

    def test_subprocess_start_failure_clears_busy_and_artifact(self):
        self.ready()
        self.app._set_operation_state("Running", True)
        self.app._start_subprocess([str(self.root / "missing-command")], self.app._on_build_done)
        self.assertFalse(self.app._operation_running)
        self.assertIsNone(self.app._last_uf2)
        self.assertIn("failed to start", self.app._operation_message)

    def test_public_build_action_and_real_generator_callback_pass_cache(self):
        with patch.object(viewer, "default_layout_path", return_value=self.path):
            with patch.object(self.app, "_start_subprocess") as start:
                self.action("build")
                self.assertEqual(start.call_args.args[0],
                                 [sys.executable, str(ROOT / "scripts/generate-corne-keymap.py")])
        script = self.root / "scripts/generate-corne-keymap.py"
        script.parent.mkdir()
        shutil.copy2(ROOT / "scripts/generate-corne-keymap.py", script)
        self.output.write_bytes(block(payload=b"b"))  # Old image must be removed before building.
        with patch.object(self.app, "_start_subprocess") as start:
            process = Gio.Subprocess.new([sys.executable, str(script)],
                                         Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
            process.communicate_utf8_async(None, None, self.app._on_generation_done, process)
            self.wait_until(lambda: start.called or not self.app._operation_running)
            self.assertEqual(start.call_args.args[0], [str(ROOT / "scripts/build-corne-firmware.sh"),
                             "--cache", str(self.cache), "--output", str(self.output)])
            self.assertFalse(self.output.exists())
        self.process(f"from pathlib import Path; Path({str(self.output)!r}).write_bytes({block()!r})",
                     self.app._on_build_done)
        self.wait_until(lambda: not self.app._operation_running)
        self.assertTrue(self.app.lookup_action("flash").get_enabled())
        self.assertEqual(self.app._last_uf2.source.data, block())

    def test_generation_refuses_changed_source_or_mismatched_outputs(self):
        for path in (self.path, self.config / "blecorne.keymap"):
            self.app._building_inputs = viewer.build_inputs(self.path, generated=False)
            path.write_bytes(path.read_bytes() + b"\n")
            self.app._set_operation_state("Generating", True)
            with patch.object(self.app, "_start_subprocess") as start:
                self.process("pass", self.app._on_generation_done)
                self.wait_until(lambda: not self.app._operation_running)
                start.assert_not_called()
            self.assertIsNone(self.app._last_uf2)
            self.assertFalse(self.app.lookup_action("flash").get_enabled())
            self.assertIn("Cannot build", self.app._operation_message)

    def test_external_edit_during_build_or_before_flash_invalidates_artifact(self):
        self.app._building_inputs = viewer.build_inputs(self.path)
        generated = self.config / "blecorne.keymap"
        generated.write_bytes(generated.read_bytes() + b"\n")
        self.output.write_bytes(block())
        self.app._set_operation_state("Building", True)
        self.process("pass", self.app._on_build_done)
        self.wait_until(lambda: not self.app._operation_running)
        self.assertIsNone(self.app._last_uf2)
        self.assertIn("changed externally", self.app._operation_message)
        for path in (self.path, generated, self.config / "blecorne.conf", self.output):
            self.ready()
            path.write_bytes(path.read_bytes() + b"\n")
            with patch.object(viewer, "flash_uf2") as flash:
                self.action("flash")
                flash.assert_not_called()
            self.assertIsNone(self.app._last_uf2)
            self.assertFalse(self.app.lookup_action("flash").get_enabled())

    def test_flash_runs_off_main_thread_and_reports_fsync_failure_honestly(self):
        main_thread = threading.get_ident()
        for failure in (False, True, "sync unconfirmed"):
            self.ready()
            release = threading.Event()
            entered = threading.Event()
            observed = []

            def flash(path, **kwargs):
                observed.append((threading.get_ident(), path, kwargs["expected_data"]))
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("test write release timed out")
                if failure == "sync unconfirmed":
                    raise viewer.UF2SyncUnconfirmed(512, OSError("USB sync not acknowledged"))
                if failure:
                    raise OSError("USB disconnected during fsync")
                return 512

            with patch.object(viewer, "flash_uf2", side_effect=flash):
                self.action("flash")
                self.assertTrue(self.app._operation_running)
                self.wait_until(entered.is_set)
                self.assertNotEqual(observed[0][0], main_thread)
                self.assertEqual(observed[0][1:], (self.output, block()))
                self.assertTrue(self.app._on_close_request(self.app.window))
                self.assertFalse(self.app.lookup_action("save").get_enabled())
                self.app._on_monitor_error("")
                self.assertIn("Writing left UF2", self.app._operation_label.get_text())
                GLib.idle_add(lambda: (release.set(), False)[1])
                self.wait_until(lambda: not self.app._operation_running)
            if failure:
                self.assertIn("Completion is NOT confirmed", self.app._operation_message)
                if failure == "sync unconfirmed":
                    self.assertIn("data submitted (512 bytes)", self.app._operation_message)
                    self.assertNotIn("write/fsync complete", self.app._operation_message)
                    continue
                self.assertIn("disconnected during fsync", self.app._operation_message)
            else:
                self.assertIn("write/fsync complete", self.app._operation_message)
                self.assertIn("not verified", self.app._operation_message)


if __name__ == "__main__":
    unittest.main()
