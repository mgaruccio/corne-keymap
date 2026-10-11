# Corne keymap editor

GTK editor, pinned ZMK builder, and left-half UF2 flasher for a 42-key Boardsource BLE Corne. The editable source of truth is [`configs/corne/layout.json`](configs/corne/layout.json). This repository includes the author's three-layer keymap as that file.

This is not ZMK Studio live editing. It does not pair, unpair, clear bonds, or reset keyboard settings. It flashes only the left half whose bootloader USB serial matches [`configs/corne/left-serial`](configs/corne/left-serial).

ZMK's own user-config model is documented at [zmk.dev customization](https://github.com/zmkfirmware/zmk/blob/main/docs/docs/customization.md) and [pinned revisions](https://zmk.dev/blog/2025/06/20/pinned-zmk). This repo pins the same revisions in `configs/corne/west.yml` and builds them locally with the official ZMK Docker image.

## Requirements

- Python 3
- GTK 4 and PyGObject, for the editor
- BlueZ, only for the read-only layer-follow view
- Docker, only when building a UF2
- A Boardsource nRF52840 left half (`239a:009f` in the bootloader, board ID `NRF52840_KB`)

## Use

Set the left half's bootloader serial before the first flash on a different keyboard:

```sh
lsusb -d 239a:009f -v | sed -n 's/.*iSerial.*/&/p'
# write that serial into configs/corne/left-serial, or:
export CORNE_LEFT_SERIAL=YOUR_LEFT_SERIAL
```

Edit and build:

```sh
python3 scripts/corne-keymap-viewer.py
```

Save JSON, then Build UF2. Double-tap reset on the **left** half only, mount its `BS_KBD` volume if the desktop does not, and use Flash left half. The writer checks the USB serial, vendor/product, board ID, and that the mounted directory is that block device. It will not guess `/dev/sda`.

A successful flash often reboots the bootloader before Linux finishes `fsync`. The editor then says the bytes were submitted and USB sync is unconfirmed. That is not a confirmed failure and not a confirmed success. Check that the left half returns as a normal BLE Corne and that the edited key types the new character before flashing again. Do not clear bonds or use a settings reset to recover from that message.

The right half is not flashed by this tool.

## Build pins

`scripts/build-corne-firmware.sh` uses:

- Docker image `zmkfirmware/zmk-build-arm@sha256:edb1c953438c6f720ddb79c3762f3972013b7fbbaf4fff3592fc869983e7afc5`
- ZMK `5b51501fead672c41b5cfb396f3dafe0894bf4e9`
- Zephyr `10ba6d0cb38bc3d258775d27982f707599320085`
- Boardsource wireless Corne config `2158104902e164c317e35f53dfa0ca1e36e1a751`

The first build downloads those trees into `configs/corne/.cache`, which is gitignored. Later builds reuse that cache but still run a pristine `west build`.

## Checks

```sh
python3 scripts/generate-corne-keymap.py --check
python3 -m unittest discover -s tests -p 'test_corne_*.py' -v
python3 -m unittest discover -s configs/corne/tests -p test_firmware.py -v
```

GTK cases need a display. `xvfb-run -a` is enough; they do not touch hardware.
