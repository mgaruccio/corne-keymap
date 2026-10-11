#!/usr/bin/env python3
"""Safe, bounded UF2 validation and left-Corne boot-volume flashing helpers."""
from __future__ import annotations

import os
import re
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

UF2_BLOCK_SIZE = 512
UF2_MAGIC_START0 = 0x0A324655
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_FLAG_NOFLASH = 0x00000001
UF2_FLAG_FAMILY_ID_PRESENT = 0x00002000
NRF52840_FAMILY_ID = 0xADA52840
APP_START = 0x1000
APP_END_EXCLUSIVE = 0xD4000  # Pinned DTS code_partition: reg = <0x1000 0xd3000>.
NVS_STORAGE_START = 0xD4000
BOOTLOADER_START = 0xF4000
UF2_PAYLOAD_MAX = 476
UF2_FILENAME = "CORNE_LEFT.UF2"
_LEFT_SERIAL_FILE = Path(__file__).resolve().parents[1] / "configs" / "corne" / "left-serial"


def _serial_from_file() -> str:
    try:
        text = _LEFT_SERIAL_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            return line
    return ""


LEFT_USB_SERIAL = _serial_from_file()
BOOT_USB_VENDOR = "239a"
BOOT_USB_PRODUCT = "009f"
BOOT_BOARD_ID = "NRF52840_KB"


class UF2Error(ValueError):
    """Raised when an image is not safe for the Corne left application region."""


class UF2SyncUnconfirmed(OSError):
    """All bytes reached write(), but filesystem sync was not acknowledged."""

    def __init__(self, bytes_written: int, cause: OSError) -> None:
        self.bytes_written = bytes_written
        super().__init__(f"all {bytes_written} UF2 bytes submitted; sync unconfirmed: {cause}")

def expected_left_serial() -> str:
    """Left-half bootloader serial. CORNE_LEFT_SERIAL overrides configs/corne/left-serial."""
    serial = os.environ.get("CORNE_LEFT_SERIAL", "").strip() or LEFT_USB_SERIAL
    if not serial:
        raise UF2Error(
            "set the left bootloader USB serial in configs/corne/left-serial or CORNE_LEFT_SERIAL"
        )
    return serial


@dataclass(frozen=True)
class UF2Block:
    raw: bytes
    flags: int
    target_address: int
    payload_size: int
    block_number: int
    block_count: int
    family_id: int

    @property
    def target_end(self) -> int:
        return self.target_address + self.payload_size


@dataclass(frozen=True)
class UF2Image:
    blocks: tuple[UF2Block, ...]
    raw: bytes

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def target_end(self) -> int:
        return max(block.target_end for block in self.blocks)


def _parse_block(raw: bytes, index: int) -> UF2Block:
    if len(raw) != UF2_BLOCK_SIZE:
        raise UF2Error(f"block {index} is not {UF2_BLOCK_SIZE} bytes")
    magic0, magic1, flags, target, payload_size, number, count, family = struct.unpack_from("<8I", raw)
    end_magic = struct.unpack_from("<I", raw, UF2_BLOCK_SIZE - 4)[0]
    if magic0 != UF2_MAGIC_START0 or magic1 != UF2_MAGIC_START1 or end_magic != UF2_MAGIC_END:
        raise UF2Error(f"block {index} has invalid UF2 magic")
    if flags != UF2_FLAG_FAMILY_ID_PRESENT:
        raise UF2Error(f"block {index} has unsupported UF2 flags 0x{flags:x}")
    if family != NRF52840_FAMILY_ID:
        raise UF2Error(f"block {index} targets family 0x{family:08x}, not nRF52840")
    if payload_size <= 0 or payload_size > UF2_PAYLOAD_MAX:
        raise UF2Error(f"block {index} has invalid payload size {payload_size}")
    if target % 4:
        raise UF2Error(f"block {index} target address is not aligned")
    if number >= count or count == 0:
        raise UF2Error(f"block {index} has invalid block number/count")
    block = UF2Block(raw, flags, target, payload_size, number, count, family)
    if target < APP_START or block.target_end > APP_END_EXCLUSIVE:
        raise UF2Error(
            f"block {index} targets 0x{target:x}-0x{block.target_end:x}; "
            f"allowed application range is 0x{APP_START:x}-0x{APP_END_EXCLUSIVE:x}"
        )
    if target < NVS_STORAGE_START < block.target_end or target < BOOTLOADER_START < block.target_end:
        raise UF2Error(f"block {index} intersects protected storage")
    return block


def parse_uf2(data: bytes) -> UF2Image:
    """Validate a complete UF2 and return its checked blocks.

    The accepted image is deliberately narrower than the general UF2 format:
    every block must be a real nRF52840 family block in this keyboard's
    application range, with no overlaps, duplicates, or hidden blocks.
    """
    if not data or len(data) % UF2_BLOCK_SIZE:
        raise UF2Error("UF2 size must be a non-zero multiple of 512 bytes")
    blocks = tuple(_parse_block(data[offset:offset + UF2_BLOCK_SIZE], offset // UF2_BLOCK_SIZE)
                   for offset in range(0, len(data), UF2_BLOCK_SIZE))
    expected_count = blocks[0].block_count
    if expected_count != len(blocks) or any(block.block_count != expected_count for block in blocks):
        raise UF2Error(f"UF2 block count is {expected_count}, file contains {len(blocks)}")
    numbers = [block.block_number for block in blocks]
    if sorted(numbers) != list(range(expected_count)):
        raise UF2Error("UF2 blocks are missing or duplicated")
    ordered = tuple(sorted(blocks, key=lambda block: block.target_address))
    for previous, current in zip(ordered, ordered[1:]):
        if previous.target_end > current.target_address:
            raise UF2Error("UF2 target blocks overlap")
    if min(block.target_address for block in blocks) != APP_START:
        raise UF2Error("UF2 application does not begin at 0x1000")
    return UF2Image(blocks=blocks, raw=data)


def validate_uf2_file(path: str | Path) -> UF2Image:
    path = Path(path).expanduser()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise UF2Error("UF2 source must be a regular file")
            data = stream.read()
    except OSError as exc:
        raise UF2Error(f"cannot read UF2 {path}: {exc}") from exc
    return parse_uf2(data)


@dataclass(frozen=True)
class BootVolume:
    """An open directory fd pins the discovered mounted volume during flashing."""
    path: Path
    source: str
    filesystem: str
    usb_sysfs_path: Path
    serial: str
    board_id: str
    directory_fd: int

    def close(self) -> None:
        os.close(self.directory_fd)


def _unescape_mountinfo(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _mount_entries() -> Iterable[tuple[Path, str, str]]:
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError as exc:
        raise UF2Error(f"cannot inspect mounted volumes: {exc}") from exc
    for line in text.splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields = left.split()
        right_fields = right.split()
        if len(fields) < 6 or len(right_fields) < 2:
            continue
        yield Path(_unescape_mountinfo(fields[4])), right_fields[0], right_fields[1]


def _read_sysfs(path: Path, name: str) -> str:
    try:
        return (path / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _usb_identity_for_source(source: str) -> tuple[Path, str, str, str] | None:
    if not source.startswith("/dev/"):
        return None
    name = os.path.basename(source)
    class_path = Path("/sys/class/block") / name
    try:
        current = Path(os.path.realpath(class_path))
    except OSError:
        return None
    for candidate in (current, *current.parents):
        vendor = _read_sysfs(candidate, "idVendor").lower()
        product = _read_sysfs(candidate, "idProduct").lower()
        serial = _read_sysfs(candidate, "serial")
        if vendor and product and serial:
            return candidate, vendor, product, serial
    return None


def _read_at(directory_fd: int, name: str, limit: int = 65536) -> str:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise UF2Error("boot volume INFO must be a regular file")
        return stream.read(limit).decode("utf-8", errors="strict")


def _open_volume(path: Path, source: str, filesystem: str) -> BootVolume | None:
    if filesystem.lower() not in {"vfat", "msdos", "exfat"}:
        return None
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(path, flags)
    except OSError:
        return None
    verified = False
    try:
        identity = _usb_identity_for_source(source)
        if identity is None:
            return None
        try:
            device = os.stat(source)
            if not stat.S_ISBLK(device.st_mode) or device.st_rdev != os.fstat(directory_fd).st_dev:
                return None
        except OSError:
            return None
        sysfs_path, vendor, product, serial = identity
        if (vendor, product, serial) != (BOOT_USB_VENDOR, BOOT_USB_PRODUCT, expected_left_serial()):
            return None
        info = _read_at(directory_fd, "INFO_UF2.TXT")
        fields = {}
        for line in info.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                fields[key.strip().lower()] = value.strip()
        if fields.get("board-id") != BOOT_BOARD_ID:
            return None
        verified = True
        return BootVolume(path, source, filesystem, sysfs_path, serial, fields["board-id"], directory_fd)
    except (OSError, UnicodeError):
        return None
    finally:
        if not verified:
            os.close(directory_fd)


def find_left_boot_volume(explicit_path: str | Path | None = None) -> BootVolume:
    """Find and pin the exact left bootloader volume, never by label alone."""
    candidates = list(_mount_entries())
    if explicit_path is not None:
        requested = Path(explicit_path).expanduser()
        candidates = [entry for entry in candidates if entry[0] == requested]
    for path, filesystem, source in candidates:
        volume = _open_volume(path, source, filesystem)
        if volume is not None:
            return volume
    target = f" at {explicit_path}" if explicit_path is not None else ""
    raise UF2Error(f"exact left Corne boot volume not found{target}")


def _revalidate_volume(volume: BootVolume) -> None:
    """Recheck the mount, USB identity and INFO through a fresh pinned directory."""
    if (volume.path, volume.filesystem, volume.source) not in _mount_entries():
        raise UF2Error("left boot volume mount changed before copying")
    current = _open_volume(volume.path, volume.source, volume.filesystem)
    if current is None:
        raise UF2Error("left boot volume identity changed before copying")
    try:
        before, after = os.fstat(volume.directory_fd), os.fstat(current.directory_fd)
        if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or current.usb_sysfs_path != volume.usb_sysfs_path):
            raise UF2Error("left boot volume was replaced before copying")
    finally:
        current.close()


def flash_uf2(path: str | Path, *, volume_path: str | Path | None = None,
              expected_data: bytes | None = None) -> int:
    """Write the checked bytes and fsync; this does not verify reboot or BLE.

    expected_data binds the source to the successful build selected by the UI.
    After validation the file is never reloaded, even if its path is replaced.
    """
    image = validate_uf2_file(path)
    if expected_data is not None and image.raw != expected_data:
        raise UF2Error("UF2 changed since the successful build; rebuild before flashing")
    volume = find_left_boot_volume(volume_path)
    try:
        _revalidate_volume(volume)
        flags = os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW
        fd = os.open(UF2_FILENAME, flags, 0o644, dir_fd=volume.directory_fd)
        try:
            destination = os.fstat(fd)
            if not stat.S_ISREG(destination.st_mode) or destination.st_dev != os.fstat(volume.directory_fd).st_dev:
                raise UF2Error("UF2 destination is not a file on the verified boot volume")
            os.ftruncate(fd, 0)
            view = memoryview(image.raw)
            written = 0
            while written < len(view):
                count = os.write(fd, view[written:])
                if count <= 0:
                    raise UF2Error("bootloader volume stopped accepting UF2 data")
                written += count
            try:
                os.fsync(fd)
            except OSError as exc:
                raise UF2SyncUnconfirmed(written, exc) from exc
        finally:
            os.close(fd)
        return written
    finally:
        volume.close()
