#!/usr/bin/env bash
# Build only: never flash, reset settings, or alter Bluetooth bonds.
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
IMAGE='docker.io/zmkfirmware/zmk-build-arm@sha256:edb1c953438c6f720ddb79c3762f3972013b7fbbaf4fff3592fc869983e7afc5'
CACHE="$ROOT/configs/corne/.cache"
MODE=custom
OUTPUT=
JOBS=8

usage() {
    cat <<'HELP'
Usage: build-corne-firmware.sh [--baseline] [--cache DIR] [--output FILE] [--jobs N]

Build blecorne_left in the pinned official ZMK Docker image (Docker required).
--baseline uses the unchanged maker configuration, without layer telemetry.
Default: build the generated custom keymap and central-only layer telemetry.
Sources, build products and baseline.log/custom.log stay in configs/corne/.cache.
The UF2 defaults to CACHE/blecorne_left-MODE.uf2. No flashing is performed.
Run generate-corne-keymap.py after editing configs/corne/layout.json.
A saved Studio keymap may override the compiled map. If present, use Studio's
Restore Stock Settings for the keymap, NOT a settings reset or bond clearing.
HELP
}

while (($#)); do
    case "$1" in
        --baseline) MODE=baseline; shift ;;
        --cache|--output|--jobs)
            (($# >= 2)) || { usage >&2; exit 2; }
            case "$1" in
                --cache) CACHE=$2 ;;
                --output) OUTPUT=$2 ;;
                --jobs) JOBS=$2 ;;
            esac
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || { echo 'Invalid --jobs' >&2; exit 2; }
command -v docker >/dev/null
if [[ "$MODE" == custom ]]; then
    python3 "$ROOT/scripts/generate-corne-keymap.py" --check
fi
mkdir -p "$CACHE"
CACHE=$(cd -- "$CACHE" && pwd -P)
OUTPUT=${OUTPUT:-"$CACHE/blecorne_left-$MODE.uf2"}
exec > >(tee "$CACHE/$MODE.log") 2>&1
printf 'Mode: %s\nImage: %s\nCache: %s\n' "$MODE" "$IMAGE" "$CACHE"
mkdir -p "$CACHE/workspace/config" "$CACHE/home"
cp "$ROOT/configs/corne/west.yml" "$CACHE/workspace/config/west.yml"

docker run --rm --user "$(id -u):$(id -g)" \
    -e HOME=/cache/home -e MODE="$MODE" -e JOBS="$JOBS" \
    -v "$ROOT:/repo:ro" -v "$CACHE:/cache" -w /cache/workspace \
    "$IMAGE" bash -euo pipefail -c '
        if [[ ! -d .west ]]; then
            west init -l config
        fi
        west update --fetch-opt=--filter=tree:0 --narrow
        west zephyr-export
        west list -f "{name}: {sha}" zmk zephyr wireless-corne_zmk_config
        args=(-DZMK_CONFIG=/cache/workspace/maker/config)
        if [[ "$MODE" == custom ]]; then
            args=(-DZMK_CONFIG=/repo/configs/corne
                  -DZMK_EXTRA_MODULES=/repo/configs/corne/telemetry)
        fi
        west build -p always -s zmk/app -d "/cache/build/$MODE" -o="-j$JOBS" \
            -b blecorne_left -S studio-rpc-usb-uart \
            -- "${args[@]}" -DCONFIG_ZMK_STUDIO=y
    '
mkdir -p -- "$(dirname -- "$OUTPUT")"
cp -- "$CACHE/build/$MODE/zephyr/zmk.uf2" "$OUTPUT"
printf '\nUF2: %s\nBuild log: %s/%s.log\n' "$OUTPUT" "$CACHE" "$MODE"
