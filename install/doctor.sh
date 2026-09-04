#!/usr/bin/env bash
# Read-only preflight for the DLSS 5 NR (Wine) node.
#
# Touches nothing, installs nothing, needs no root. Prints what is present,
# what is missing, and the one thing to do about each gap. Exits non-zero if
# anything required is missing, so it also works as a CI/setup gate.
#
#   install/doctor.sh [--root DIR] [--prefix DIR] [--wine PATH] [--display :N]
#
# Defaults follow the DLSS5NR_* environment variables the node itself reads.
# No pipefail on purpose: this script is full of `producer | grep -q` and
# `producer | head -1`, where the reader exits first and the writer dies of
# SIGPIPE. With pipefail those all read as failures.
set -u

ROOT=${DLSS5NR_ROOT:-}
PREFIX=${DLSS5NR_PREFIX:-}
WINE=${DLSS5NR_WINE:-}
DISPLAY_ARG=${DLSS5NR_DISPLAY:-}
while [ $# -gt 0 ]; do
    case $1 in
        --root)    ROOT=$2;        shift 2 ;;
        --prefix)  PREFIX=$2;      shift 2 ;;
        --wine)    WINE=$2;        shift 2 ;;
        --display) DISPLAY_ARG=$2; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

FAIL=0
WARN=0
ok()   { printf '  \033[32mok\033[0m    %-22s %s\n' "$1" "${2-}"; }
bad()  { printf '  \033[31mMISS\033[0m  %-22s %s\n' "$1" "${2-}"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33mwarn\033[0m  %-22s %s\n' "$1" "${2-}"; WARN=$((WARN+1)); }
hint() { printf '        -> %s\n' "$1"; }
head_() { printf '\n%s\n' "$1"; }

# ---------------------------------------------------------------- GPU ----
head_ "GPU and driver"
if command -v nvidia-smi >/dev/null 2>&1; then
    read -r GPU DRV <<<"$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | head -1 | awk -F', *' '{print $1"|"$2}' | tr '|' ' ')"
    GPU=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
    DRV=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | tr -d ' ')
    ok "nvidia-smi" "$GPU, driver $DRV"
    case $GPU in
        *RTX\ 50*|*Blackwell*) ok "generation" "Blackwell - the only one feature 18 is confirmed on" ;;
        *) warn "generation" "$GPU: feature 18 support is decided by your nvngx_dlssnr.dll build" 
           hint "0xBAD00001 at runtime means that runtime rejects this GPU" ;;
    esac
else
    bad "nvidia-smi" "not found - this is not an NVIDIA machine"
fi

# ------------------------------------------------------------ display ----
head_ "X display (DXVK cannot create a Vulkan instance without one)"
probe_display() {
    local d=$1 xa=$2 rate
    rate=$(env DISPLAY="$d" ${xa:+XAUTHORITY="$xa"} xrandr 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\*' | head -1)
    [ -n "$rate" ] && printf '%s' "$rate"
}
FOUND_DISPLAY=""
for d in ${DISPLAY_ARG:-} ${DISPLAY:-} :0 :1 :99; do
    [ -n "$d" ] || continue
    for xa in "${XAUTHORITY:-}" "$HOME/.Xauthority" $(ls /run/user/$(id -u)/xauth_* 2>/dev/null); do
        rate=$(probe_display "$d" "$xa")
        [ -n "$rate" ] || continue
        if [ "$rate" = "0.00*" ]; then
            warn "display $d" "refresh rate 0.00 Hz"
            hint "Xvfb reports 0 Hz and DXVK divides by it (EXCEPTION_INT_DIVIDE_BY_ZERO)."
            hint "Use Xorg + xf86-video-dummy with a real Modeline: install/xorg-dummy.conf"
        else
            ok "display $d" "refresh ${rate%\*} Hz${xa:+, XAUTHORITY=$xa}"
            FOUND_DISPLAY=$d
        fi
        break 2
    done
done
if [ -z "$FOUND_DISPLAY" ]; then
    bad "display" "no usable X display found"
    hint "A headless box needs Xorg + xf86-video-dummy (install/xorg-dummy.conf), not Xvfb."
    hint "If you have a desktop session, run this as that user so XAUTHORITY resolves."
fi

# --------------------------------------------------------------- wine ----
head_ "Proton / wine"
if [ -z "$WINE" ]; then
    for c in "$HOME"/.local/share/Steam/compatibilitytools.d/*/files/bin/wine \
             "$HOME"/.steam/root/compatibilitytools.d/*/files/bin/wine \
             /usr/share/steam/compatibilitytools.d/*/files/bin/wine \
             "$HOME"/.local/share/Steam/compatibilitytools.d/*/files/bin/wine64; do
        [ -x "$c" ] && { WINE=$c; break; }
    done
fi
if [ -n "$WINE" ] && [ -x "$WINE" ]; then
    if file -b "$WINE" 2>/dev/null | grep -q 'ELF 64-bit'; then
        ok "wine" "$WINE ($("$WINE" --version 2>/dev/null | head -1))"
    else
        bad "wine" "$WINE is not a 64-bit ELF"
        hint "GE-Proton 10's files/bin/wine is a 32-bit launcher; use files/bin/wine64 there."
        hint "GE-Proton 11 and later ship a 64-bit files/bin/wine and have no wine64."
    fi
else
    bad "wine" "no GE-Proton found"
    hint "Install a GE-Proton build under ~/.local/share/Steam/compatibilitytools.d/,"
    hint "or pass --wine /path/to/files/bin/wine"
fi

# ------------------------------------------------------------- prefix ----
head_ "Wine prefix"
if [ -z "$PREFIX" ] && [ -n "${WINE:-}" ]; then
    for c in "$HOME"/dlss5/prefix/pfx /workspace/dlss5/prefix/pfx; do
        [ -d "$c/drive_c" ] && { PREFIX=$c; break; }
    done
fi
if [ -n "$PREFIX" ] && [ -d "$PREFIX/drive_c" ]; then
    ok "prefix" "$PREFIX"
    S="$PREFIX/drive_c/windows/system32"
    # A prefix made with bare `wineboot` has Wine's builtin stubs here, not the
    # translation layers. The size is the quickest way to tell them apart.
    for chk in "dxgi.dll:2000000:DXVK" "d3d12core.dll:2000000:vkd3d-proton" "nvapi64.dll:500000:dxvk-nvapi"; do
        f=${chk%%:*}; rest=${chk#*:}; min=${rest%%:*}; who=${rest#*:}
        sz=$(stat -c%s "$S/$f" 2>/dev/null || echo 0)
        if [ "$sz" -ge "$min" ]; then ok "  $f" "$sz B ($who)"
        else
            bad "  $f" "$sz B - too small to be $who"
            hint "Build the prefix with 'proton run wineboot -u', never bare wineboot:"
            hint "Proton installs DXVK/vkd3d-proton/dxvk-nvapi; wineboot leaves builtin stubs."
        fi
    done
    NV=$(strings -a "$S/nvapi64.dll"   2>/dev/null | grep -oE '^v?0\.9\.[0-9]+' | sort -u | head -1)
    VK=$(strings -a "$S/d3d12core.dll" 2>/dev/null | grep -oE '^3\.[0-9]+\.[0-9]+' | sort -u | head -1)
    vge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }
    if [ -n "$NV" ] && vge "${NV#v}" "0.9.2"; then ok "  dxvk-nvapi" "$NV"
    else warn "  dxvk-nvapi" "${NV:-unknown} (want >= 0.9.2)"; hint "64-bit CuBIN calls need 0.9.2+"; fi
    if [ -n "$VK" ] && vge "$VK" "3.0.1"; then ok "  vkd3d-proton" "$VK"
    else warn "  vkd3d-proton" "${VK:-unknown} (want >= 3.0.1)"; hint "64-bit CuBIN calls need 3.0.1+"; fi
    for f in nvngx.dll _nvngx.dll; do
        [ -s "$S/$f" ] && ok "  $f" "$(stat -c%s "$S/$f") B" || {
            bad "  $f" "not in the prefix"
            hint "Copy the driver's PE bridge in. Many distros already ship it:"
            hint "  /usr/lib/nvidia/wine/  or  /usr/lib/x86_64-linux-gnu/nvidia/wine/"
            hint "Otherwise extract from the matching driver: sh NVIDIA-*.run --extract-only"; }
    done
else
    bad "prefix" "${PREFIX:-not found}"
    hint "Create it with: STEAM_COMPAT_CLIENT_INSTALL_PATH=\$HOME/.steam/root \\"
    hint "  STEAM_COMPAT_DATA_PATH=<dir> <proton> run wineboot -u   (the dir must exist first)"
fi

# ------------------------------------------------- project + runtimes ----
head_ "Project tree and NVIDIA runtimes"
if [ -z "$ROOT" ]; then
    for c in "$HOME/dlss5nr" /workspace/dlss5nr; do [ -d "$c" ] && { ROOT=$c; break; }; done
fi
# Report the root but never short-circuit on it: someone pointing --root at a
# directory they have not created yet still wants the full list of what goes in
# it, not a single "not found" line.
[ -n "$ROOT" ] || ROOT="$HOME/dlss5nr"
if [ -d "$ROOT" ]; then ok "root" "$ROOT"; else bad "root" "$ROOT does not exist yet"; fi
if true; then
    [ -f "$ROOT/tools/dlss5nr_video.py" ] && ok "  upstream tree" "tools/dlss5nr_video.py" || {
        bad "  upstream tree" "tools/dlss5nr_video.py missing"
        hint "git clone https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux $ROOT"
        hint "The node imports its protocol constants from there rather than copying them."; }
    for f in native/bin/dlss5nr_host.exe native/bin/dlss5nr_bridge.dll runtime/caller/nvngx.dll_comfy.dll; do
        [ -s "$ROOT/$f" ] && ok "  ${f##*/}" "$(stat -c%s "$ROOT/$f") B" || {
            bad "  ${f##*/}" "missing"
            hint "Untar the prebuilt dlss5nr-native-mingw-x64.tar.gz into $ROOT,"
            hint "or cross-compile with install/build_native.sh"; }
    done
    if [ "$(strings -a "$ROOT/native/bin/dlss5nr_bridge.dll" 2>/dev/null | grep -c DLSS5NR_MODEL_PRESET)" -gt 0 ]; then
        ok "  preset patch" "bridge honours DLSS5NR_MODEL_PRESET"
    else
        warn "  preset patch" "bridge is unpatched - the model_preset widget will do nothing"
    fi
    if [ -s "$ROOT/runtime/nvngx_dlssnr.dll" ]; then
        ok "  nvngx_dlssnr.dll" "$(stat -c%s "$ROOT/runtime/nvngx_dlssnr.dll") B"
    else
        bad "  nvngx_dlssnr.dll" "missing (required - feature 18 itself)"
        hint "NVIDIA proprietary and pre-release. This project does not and will not ship it."
        hint "Place your own legally obtained copy at $ROOT/runtime/nvngx_dlssnr.dll"
    fi
    if [ -s "$ROOT/runtime/nvngx_dlss.dll" ]; then
        ok "  nvngx_dlss.dll" "$(stat -c%s "$ROOT/runtime/nvngx_dlss.dll") B (carrier)"
    else
        bad "  nvngx_dlss.dll" "missing (required for any scale above 1x)"
        hint "This one is redistributable and published by NVIDIA - no repack needed:"
        hint "  curl -Lo $ROOT/runtime/nvngx_dlss.dll \\"
        hint "    https://raw.githubusercontent.com/NVIDIA/DLSS/v310.7.0/lib/Windows_x86_64/rel/nvngx_dlss.dll"
    fi
fi

# ------------------------------------------------------------- python ----
head_ "ComfyUI python"
PY=${COMFYUI_PYTHON:-}
[ -n "$PY" ] || for c in "$HOME"/ComfyUI/venv/bin/python "$HOME"/ComfyUI/.venv/bin/python /workspace/ComfyUI/venv/bin/python; do
    [ -x "$c" ] && { PY=$c; break; }
done
if [ -n "$PY" ] && [ -x "$PY" ]; then
    ok "interpreter" "$PY"
    if "$PY" -c 'import cv2' 2>/dev/null; then
        ok "  cv2" "$("$PY" -c 'import cv2;print(cv2.__version__)')"
    else
        warn "  cv2" "not importable"
        hint "Without it motion vectors are zero and history resets every frame;"
        hint "video loses all temporal benefit. Install opencv-python-headless."
    fi
else
    warn "interpreter" "ComfyUI venv not found (set COMFYUI_PYTHON to check cv2)"
fi

# ------------------------------------------------------------ summary ----
printf '\n'
if [ "$FAIL" -eq 0 ] && [ "$WARN" -eq 0 ]; then
    echo "All checks passed."
elif [ "$FAIL" -eq 0 ]; then
    echo "Usable, with $WARN warning(s) above."
else
    echo "$FAIL required item(s) missing, $WARN warning(s). See the -> lines."
fi
exit $(( FAIL > 0 ? 1 : 0 ))
