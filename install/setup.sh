#!/usr/bin/env bash
# Set up the DLSS 5 NR (Wine) node on an ordinary Linux box.
#
#   install/setup.sh [--root DIR] [--proton-root DIR] [--proton-tag TAG]
#
# Everything here runs as your normal user. Nothing needs root, nothing is
# installed system-wide, and no package manager is invoked: the only step that
# genuinely needs root is creating a virtual display, which only applies to a
# headless machine and is documented in the README instead.
#
# Idempotent - re-running only fills gaps.
#
# What it does NOT do: download nvngx_dlssnr.dll. That file is NVIDIA
# proprietary and pre-release; this project does not redistribute it or point
# at mirrors. Supply your own legally obtained copy.
set -u

ROOT=${DLSS5NR_ROOT:-$HOME/dlss5nr}
PROTON_ROOT=${DLSS5_PROTON_ROOT:-$HOME/dlss5}
PROTON_TAG=${PROTON_TAG:-GE-Proton11-6}
# Note the trade-off: the NR snippet is 310.8 and NVIDIA publishes nothing past
# 310.7.0, so this carrier is one minor version behind the snippet it runs
# under. kos94ok's README asks for a matched set. It works, but if you already
# have a matched 310.8 carrier, drop it in first - an existing file is kept.
#
# Pinned deliberately, and not just for reproducibility: the model_preset
# letters (J/K/L/M) index networks *inside this DLL*, not in the driver. On
# SDK v310.4.0 preset M does not exist and silently falls back to K, producing
# pixel-identical output with no error. v310.7.0 does implement it. Moving this
# backwards will quietly disable the widget.
DLSS_SDK_TAG=${DLSS_SDK_TAG:-v310.7.0}
while [ $# -gt 0 ]; do
    case $1 in
        --root)        ROOT=$2;        shift 2 ;;
        --proton-root) PROTON_ROOT=$2; shift 2 ;;
        --proton-tag)  PROTON_TAG=$2;  shift 2 ;;
        -h|--help)     sed -n '2,17p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
PFX="$PROTON_ROOT/prefix/pfx"
BRIDGE="$PROTON_ROOT/ngx_bridge"
UPSTREAM=https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux

say()  { printf '\n== %s\n' "$1"; }
ok()   { printf '   ok   %s\n' "$1"; }
info() { printf '        %s\n' "$1"; }
die()  { printf '   FAIL %s\n' "$1" >&2; exit 1; }

command -v nvidia-smi >/dev/null 2>&1 || die "no nvidia-smi; this is not an NVIDIA machine"
say "target: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1), driver $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | tr -d ' ')"
info "root=$ROOT  proton-root=$PROTON_ROOT"

# --------------------------------------------------------- 1. sources ----
say "1) upstream source tree"
if [ -f "$ROOT/tools/dlss5nr_video.py" ]; then
    ok "already at $ROOT"
elif command -v git >/dev/null 2>&1; then
    # The node imports the wire protocol from upstream rather than duplicating
    # it, so this tree is a runtime dependency, not just build scaffolding.
    git clone -q --depth 1 "$UPSTREAM" "$ROOT" || die "git clone $UPSTREAM failed"
    ok "cloned $UPSTREAM"
else
    die "git not found; clone $UPSTREAM to $ROOT by hand"
fi

# --------------------------------------------------------- 2. natives ----
say "2) native binaries (MinGW cross-built, project-owned)"
if [ -s "$ROOT/native/bin/dlss5nr_host.exe" ] && [ -s "$ROOT/native/bin/dlss5nr_bridge.dll" ]; then
    ok "already present"
elif [ -f "$REPO/dlss5nr-native-mingw-x64.tar.gz" ]; then
    tar xzf "$REPO/dlss5nr-native-mingw-x64.tar.gz" -C "$ROOT" || die "untar failed"
    ok "unpacked from the checkout"
else
    bash "$HERE/build_native.sh" "$ROOT" || die "no prebuilt tarball and build_native.sh failed"
fi
mkdir -p "$ROOT/runtime/caller"
[ -s "$ROOT/runtime/caller/nvngx.dll_comfy.dll" ] || \
    cp -f "$ROOT/native/bin/nvngx.dll_comfy.dll" "$ROOT/runtime/caller/" 2>/dev/null
if [ "$(strings -a "$ROOT/native/bin/dlss5nr_bridge.dll" 2>/dev/null | grep -c DLSS5NR_MODEL_PRESET)" -gt 0 ]; then
    ok "bridge honours DLSS5NR_MODEL_PRESET"
else
    info "warning: bridge is unpatched; the model_preset widget will do nothing"
fi

# ---------------------------------------------------------- 3. proton ----
say "3) GE-Proton"
WINE=""
# Newest first: a plain glob is alphabetical, which puts GE-Proton11-5 ahead of
# 11-6 and can pair an older wine with a prefix a newer Proton built.
_installed_wines() {
    ls -d "$HOME"/.local/share/Steam/compatibilitytools.d/*/files/bin/wine \
          "$HOME"/.steam/root/compatibilitytools.d/*/files/bin/wine 2>/dev/null
}
# Order of preference: the explicitly requested tag, then GE-Proton builds
# newest first, then any other Proton. Sorting all of them together is wrong
# twice over - it is alphabetical (11-5 before 11-6) and it lets an unrelated
# family such as UMU-Proton outrank GE-Proton entirely.
CANDIDATES=$( { printf '%s\n' "$PROTON_ROOT/$PROTON_TAG/files/bin/wine"
                _installed_wines | grep -i 'GE-Proton'  | sort -Vr
                _installed_wines | grep -iv 'GE-Proton' | sort -Vr ; } )
for c in $CANDIDATES; do
    # GE-Proton 10 ships a 32-bit launcher at files/bin/wine and the real one at
    # wine64; 11 and later ship a 64-bit files/bin/wine and no wine64.
    [ -x "$c" ] && file -b "$c" 2>/dev/null | grep -q 'ELF 64-bit' && { WINE=$c; break; }
    [ -x "${c}64" ] && { WINE=${c}64; break; }
done
if [ -n "$WINE" ]; then
    ok "using $WINE"
else
    mkdir -p "$PROTON_ROOT"
    url="https://github.com/GloriousEggroll/proton-ge-custom/releases/download/$PROTON_TAG/$PROTON_TAG.tar.gz"
    info "downloading $PROTON_TAG (~500 MB)"
    curl -fL --retry 3 --no-progress-meter -o "$PROTON_ROOT/proton.tgz" "$url" || die "download failed: $url"
    tar xzf "$PROTON_ROOT/proton.tgz" -C "$PROTON_ROOT" || die "extract failed"
    rm -f "$PROTON_ROOT/proton.tgz"
    for c in "$PROTON_ROOT/$PROTON_TAG"*/files/bin/wine; do
        [ -x "$c" ] && file -b "$c" | grep -q 'ELF 64-bit' && WINE=$c && break
        [ -x "${c}64" ] && WINE=${c}64 && break
    done
    [ -n "$WINE" ] || die "no usable wine binary after extracting $PROTON_TAG"
    ok "installed $WINE"
fi
PROTON_DIR=$(cd "$(dirname "$WINE")/../.." && pwd)

# ---------------------------------------------------------- 4. prefix ----
say "4) wine prefix"
if [ -f "$PFX/drive_c/windows/system32/nvapi64.dll" ]; then
    ok "already built at $PFX"
else
    # The prefix MUST come from Proton. A bare `wineboot -u` produces a minimal
    # prefix carrying Wine's builtin d3d12/dxgi stubs instead of DXVK,
    # vkd3d-proton and dxvk-nvapi. That fails later and confusingly: first
    # LoadLibrary(dlss5nr_bridge.dll) -> 126, then once you hand-copy the DLLs,
    # NvAPI_EnumPhysicalGPUs -> -2 and NGX Init -> 0xBAD00001.
    PROTON_BIN="$PROTON_DIR/proton"
    [ -x "$PROTON_BIN" ] || die "no proton script at $PROTON_BIN (needed to build the prefix)"
    # Proton creates pfx.lock inside STEAM_COMPAT_DATA_PATH but will not create
    # that directory itself, and it KeyErrors without the client install path.
    mkdir -p "$HOME/.steam/root" "$PROTON_ROOT/prefix" "${XDG_RUNTIME_DIR:-/tmp/xdg}"
    info "building via 'proton run wineboot -u' (this takes a minute)"
    STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.steam/root" \
    STEAM_COMPAT_DATA_PATH="$PROTON_ROOT/prefix" \
    XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/xdg}" \
    PROTON_DISABLE_XALIA=1 WINEDEBUG=-all \
        timeout 600 "$PROTON_BIN" run wineboot -u >/dev/null 2>&1
    sleep 3
    [ -d "$PFX/drive_c" ] || die "prefix was not created at $PFX"
    ok "built $PFX"
fi

# ------------------------------------------------------ 5. NGX loader ----
say "5) NVIDIA NGX loader (nvngx.dll / _nvngx.dll)"
S="$PFX/drive_c/windows/system32"
mkdir -p "$BRIDGE"
if [ ! -s "$BRIDGE/_nvngx.dll" ]; then
    # Most distros ship these PE bridges with the driver package already, in
    # which case they are guaranteed to match the running driver.
    for d in /usr/lib/nvidia/wine /usr/lib/x86_64-linux-gnu/nvidia/wine \
             /usr/lib64/nvidia/wine /opt/nvidia/wine; do
        if [ -s "$d/_nvngx.dll" ] && [ -s "$d/nvngx.dll" ]; then
            cp -f "$d/nvngx.dll" "$d/_nvngx.dll" "$BRIDGE/" && ok "copied from $d" && break
        fi
    done
fi
if [ -s "$BRIDGE/_nvngx.dll" ]; then
    cp -f "$BRIDGE/nvngx.dll" "$BRIDGE/_nvngx.dll" "$S/"
    WB=$(WINEPREFIX="$PFX" WINEDEBUG=-all timeout 60 "$WINE" winepath -w "$BRIDGE" 2>/dev/null | tr -d '\r')
    if [ -n "$WB" ]; then
        for KV in "HKLM\\SOFTWARE\\NVIDIA Corporation\\Global\\NGXCore|FullPath" \
                  "HKLM\\SOFTWARE\\NVIDIA Corporation\\Global\\NGXCore|NGXPath" \
                  "HKLM\\System\\CurrentControlSet\\Services\\nvlddmkm\\NGXCore|NGXPath" \
                  "HKLM\\System\\CurrentControlSet\\Services\\nvlddmkm\\Parameters\\NGXCore|NGXPath"; do
            WINEPREFIX="$PFX" WINEDEBUG=-all timeout 90 "$WINE" reg add "${KV%|*}" \
                /v "${KV#*|}" /d "$WB" /f >/dev/null 2>&1
        done
        ok "registry NGXCore -> $WB"
    else
        info "warning: winepath failed; registry NGXCore not set"
    fi
else
    DRV=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | tr -d ' ')
    info "not shipped by your distro. Extract them from the MATCHING driver (no install):"
    info "  curl -O https://us.download.nvidia.com/XFree86/Linux-x86_64/$DRV/NVIDIA-Linux-x86_64-$DRV.run"
    info "  sh NVIDIA-Linux-x86_64-$DRV.run --extract-only --target /tmp/nvdrv"
    info "  cp /tmp/nvdrv/nvngx.dll /tmp/nvdrv/_nvngx.dll $BRIDGE/"
    info "then re-run this script."
fi

# --------------------------------------------------------- 6. carrier ----
say "6) DLSS Super Resolution carrier (nvngx_dlss.dll)"
if [ -s "$ROOT/runtime/nvngx_dlss.dll" ]; then
    ok "already present ($(stat -c%s "$ROOT/runtime/nvngx_dlss.dll") B)"
else
    # Redistributable and published by NVIDIA. No third-party repack needed.
    url="https://raw.githubusercontent.com/NVIDIA/DLSS/$DLSS_SDK_TAG/lib/Windows_x86_64/rel/nvngx_dlss.dll"
    info "fetching from NVIDIA's public SDK ($DLSS_SDK_TAG)"
    if curl -fL --retry 3 --no-progress-meter -o "$ROOT/runtime/nvngx_dlss.dll" "$url"; then
        sz=$(stat -c%s "$ROOT/runtime/nvngx_dlss.dll")
        ok "$sz B"
        # v310.7.0 is ~59 MB; the v310.4.0 generation is ~30 MB and carries
        # fewer preset networks. Size is a rough but useful tell.
        [ "$sz" -lt 40000000 ] && info "note: smaller than expected for $DLSS_SDK_TAG; presets above K may be absent"
    else
        rm -f "$ROOT/runtime/nvngx_dlss.dll"
        info "warning: download failed; scales above 1x will not work"
    fi
fi

# ------------------------------------------------------------ 7. done ----
say "7) what you must supply yourself"
if [ -s "$ROOT/runtime/nvngx_dlssnr.dll" ]; then
    ok "nvngx_dlssnr.dll present ($(stat -c%s "$ROOT/runtime/nvngx_dlssnr.dll") B)"
else
    info "MISSING: $ROOT/runtime/nvngx_dlssnr.dll"
    info "This is feature 18 itself - NVIDIA proprietary and pre-release."
    info "This project does not ship it and does not link to mirrors."
fi

# systemd's Environment= does no shell expansion, so resolve the display here
# and print a literal value rather than something that only works in a shell.
DISP=""
for d in ${DISPLAY:-} :0 :1 :99; do
    [ -n "$d" ] || continue
    for xa in "${XAUTHORITY:-}" "$HOME/.Xauthority" $(ls /run/user/$(id -u)/xauth_* 2>/dev/null); do
        rate=$(env DISPLAY="$d" ${xa:+XAUTHORITY="$xa"} xrandr 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\*' | head -1)
        [ -n "$rate" ] && [ "$rate" != "0.00*" ] && { DISP=$d; break 2; }
    done
done
[ -n "$DISP" ] || DISP=":0"

say "point ComfyUI at this install"
cat <<CONF
   Add these to the environment ComfyUI runs in. For a systemd user service,
   ~/.config/systemd/user/comfyui.service.d/dlss5nr.conf:

     [Service]
     Environment=DLSS5NR_ROOT=$ROOT
     Environment=DLSS5NR_WINE=$WINE
     Environment=DLSS5NR_PREFIX=$PFX
     Environment=DLSS5NR_DISPLAY=$DISP

   then: systemctl --user daemon-reload && systemctl --user restart comfyui

   ComfyUI must run with a working DISPLAY and XAUTHORITY. A service started
   from a desktop session inherits both; one started from a bare ssh shell
   does not.
CONF

say "verifying"
DLSS5NR_ROOT="$ROOT" DLSS5NR_PREFIX="$PFX" DLSS5NR_WINE="$WINE" bash "$HERE/doctor.sh"
