"""Run with: python3 -m unittest discover -s configs/corne/tests -v"""

import copy
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "configs" / "corne"
SCRIPT = ROOT / "scripts" / "generate-corne-keymap.py"
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("corne_generator", SCRIPT)
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


def kp(keys):
    return ["&kp " + key for key in keys.split()]


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.layout = json.loads((CONFIG / "layout.json").read_text(encoding="utf-8"))

    def test_shared_contract(self):
        generator.validate(self.layout)
        self.assertEqual(self.layout["service_uuid"], "32dcce45-ae11-451c-8b48-7a3f083f6afe")
        self.assertEqual(self.layout["characteristic_uuid"], "32dcce45-ae11-451c-8b48-7a3f083f6aff")
        self.assertEqual([layer["name"] for layer in self.layout["layers"]],
                         ["Base", "Navigation", "Numbers / Utilities"])
        for layer in self.layout["layers"]:
            self.assertEqual(len(layer["keys"]), 42)
            for key in layer["keys"]:
                self.assertEqual(set(key), {"tap", "hold", "repeat", "binding"})

    def test_all_approved_bindings_in_physical_order(self):
        expected = [
            ["&esc_lt 2 ESC"] + kp("Q W E R T Y U I O P") + ["&outside_mod LEFT_ALT EQUAL"]
            + ["&outside_mod LEFT_GUI BSPC"] + kp("A S D F G H J K L SEMI")
            + ["&kp SQT", "&outside_mod LEFT_CONTROL GRAVE"]
            + kp("Z X C V B N M COMMA DOT FSLH") + ["&outside_mod RIGHT_CONTROL MINUS"]
            + ["&space_cadet LSHFT LPAR", "&thumb_lt 1 BSPC", "&kp TAB", "&kp RET",
               "&thumb_lt 1 SPACE", "&space_cadet RSHFT RPAR"],
            ["&trans"] + kp("N1 N2 N3 N4 N5 HOME PG_DN PG_UP END INS") + ["&trans"]
            + ["&trans"] + kp("N6 N7 N8 N9 N0 LEFT DOWN UP RIGHT") + ["&trans", "&trans"]
            + ["&trans"] + kp("LALT LBKT RBKT BSLH DEL LC(LEFT) LC(HOME) LC(END) LC(RIGHT) DEL")
            + ["&trans"] + ["&trans"] * 6,
            ["&trans"] + kp("F1 F2 F3 F4 F5 BSPC N7 N8 N9 FSLH EQUAL")
            + ["&trans"] + kp("F6 F7 F8 F9 F10 TAB N4 N5 N6 STAR") + ["&outside_mod LEFT_ALT PLUS"]
            + ["&trans"] + kp("F11 F12") + ["&bt BT_SEL 0", "&bt BT_NXT", "&studio_unlock"]
            + kp("N0 N1 N2 N3 DOT") + ["&trans"]
            + ["&trans"] * 4 + ["&kp N0", "&trans"],
        ]
        for layer, bindings in zip(self.layout["layers"], expected):
            with self.subTest(layer=layer["name"]):
                self.assertEqual(len(bindings), 42)
                self.assertEqual([key["binding"] for key in layer["keys"]], bindings)

    def test_repeat_and_navigation_labels(self):
        base, nav, numbers = [layer["keys"] for layer in self.layout["layers"]]
        self.assertEqual(base[12]["tap"], "Bksp")
        self.assertEqual(base[12]["hold"], "Super")
        self.assertEqual(base[12]["repeat"], "Bksp")  # Never forward Delete.
        for index in (0, 36, 41):
            self.assertEqual(base[index]["repeat"], "")
        self.assertEqual(base[37]["repeat"], "Bksp")
        self.assertEqual(base[40]["repeat"], "Space")
        self.assertEqual([nav[index]["tap"] for index in (6, 9, 31, 32)],
                         ["Line Home", "Line End", "Doc Home", "Doc End"])
        self.assertEqual((numbers[23]["tap"], numbers[23]["hold"]), ("+", "LAlt"))

    def test_generated_key_order_and_hold_tap_settings(self):
        text = generator.render_keymap(self.layout)
        layers = text.split("    keymap {", 1)[1]
        actual = re.findall(r"&[a-z][a-z0-9_]*(?: [A-Z0-9_()]+)*", layers)
        expected = [key["binding"] for layer in self.layout["layers"] for key in layer["keys"]]
        self.assertEqual(actual, expected)
        for name, flavor, quick, held in (
            ("outside_mod", "hold-preferred", 200, "kp"),
            ("space_cadet", "hold-preferred", 0, "kp"),
            ("thumb_lt", "balanced", 200, "mo"),
            ("esc_lt", "hold-preferred", 0, "mo"),
        ):
            with self.subTest(behavior=name):
                block = text.split(name + ":", 1)[1].split("};", 1)[0]
                self.assertIn(f'flavor = "{flavor}";', block)
                self.assertIn("tapping-term-ms = <200>;", block)
                self.assertIn(f"quick-tap-ms = <{quick}>;", block)
                self.assertIn(f"bindings = <&{held}>, <&kp>;", block)

    def test_validation_rejects_bad_counts_types_and_bindings(self):
        bad = copy.deepcopy(self.layout)
        bad["layers"][0]["keys"].pop()
        with self.assertRaises(ValueError):
            generator.validate(bad)
        for field, value in (("tap", 123), ("binding", "&kp Q>; &bt BT_CLR")):
            bad = copy.deepcopy(self.layout)
            bad["layers"][0]["keys"][0][field] = value
            with self.assertRaises(ValueError):
                generator.validate(bad)
        bad = copy.deepcopy(self.layout)
        bad["layers"][1]["keys"][0]["repeat"] = "repeat"
        with self.assertRaises(ValueError):
            generator.validate(bad)

    def test_public_check_command(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--check"], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_generation_and_staleness_for_both_outputs(self):
        cache = CONFIG / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="generator-test-", dir=cache) as directory:
            root = Path(directory)
            config = root / "configs" / "corne"
            config.mkdir(parents=True)
            (config / "layout.json").write_text(json.dumps(self.layout), encoding="utf-8")
            with patch.object(generator, "ROOT", root), patch.object(generator, "CONFIG", config):
                with patch.object(sys, "argv", [str(SCRIPT)]), patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(generator.main(), 0)
                for relative in ("blecorne.keymap", "telemetry/include/corne_layout.h"):
                    path = config / relative
                    original = path.read_text(encoding="utf-8")
                    with patch.object(sys, "argv", [str(SCRIPT), "--check"]):
                        self.assertEqual(generator.main(), 0)
                        path.write_text(original + "/* stale */\n", encoding="utf-8")
                        with patch("sys.stderr", new_callable=io.StringIO) as errors:
                            self.assertEqual(generator.main(), 1)
                            self.assertIn(relative, errors.getvalue())
                    path.write_text(original, encoding="utf-8")


class ProtocolTests(unittest.TestCase):
    def test_production_c_callbacks(self):
        cache = CONFIG / ".cache"
        cache.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="protocol-test-", dir=cache) as directory:
            temp = Path(directory)
            for header in (
                "zephyr/kernel.h", "zephyr/bluetooth/conn.h", "zephyr/bluetooth/gatt.h",
                "zephyr/sys/byteorder.h", "zmk/ble.h", "zmk/event_manager.h",
                "zmk/events/layer_state_changed.h", "zmk/keymap.h",
            ):
                path = temp / header
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('#include "mock_zmk.h"\n', encoding="utf-8")
            binary = temp / "protocol-test"
            result = subprocess.run([
                "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-I" + str(temp), "-I" + str(CONFIG / "tests"),
                "-I" + str(CONFIG / "telemetry" / "include"),
                str(CONFIG / "tests" / "test_layer_telemetry.c"), "-o", str(binary),
            ], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
