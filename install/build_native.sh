#!/usr/bin/env bash
# 取上游源码并用 MinGW-w64 交叉编出三件套。不碰任何 NVIDIA 文件。
#
# 上游 native/build_host_mingw.sh 注明 "The D3D12 bridge still needs the
# Windows SDK/MSVC build"，只肯用 MinGW 编 transport host。实测那是保守说法：
# MinGW-w64 13 自带 d3d12.h，bridge 和 caller shim 一次全过。
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# Same resolution ladder as nodes.py, setup.sh and doctor.sh: argument, then
# DLSS5NR_ROOT, then the installer's config file, then $HOME. /workspace is a
# leftover from bootstrap_vast.sh and is only correct inside that container -
# it used to be the default here, which meant this script tried to git clone
# into a root-owned directory on every ordinary machine.
CFG=${DLSS5NR_CONFIG:-$HERE/../dlss5nr.local.json}
cfg_get() {
    [ -f "$CFG" ] || return 1
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$CFG" | head -1
}
ROOT=${1:-${DLSS5NR_ROOT:-}}
[ -n "$ROOT" ] || ROOT=$(cfg_get root) || true
[ -n "$ROOT" ] || ROOT=$HOME/dlss5nr
UPSTREAM=https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux
CXX=${CXX:-x86_64-w64-mingw32-g++}

echo "== 1) 上游源码 -> $ROOT"
if ! mkdir -p "$ROOT" 2>/dev/null; then
    echo "   FAIL 无法创建 $ROOT —— 换个位置：" >&2
    echo "        install/build_native.sh <dir>   或   export DLSS5NR_ROOT=<dir>" >&2
    exit 1
fi
if [ -d "$ROOT/.git" ]; then
    git -C "$ROOT" pull --ff-only
else
    git clone --depth 1 "$UPSTREAM" "$ROOT"
fi

echo "== 2) MinGW-w64"
if ! command -v "$CXX" >/dev/null 2>&1; then
    # Do not guess a package manager and do not assume root. Say what is
    # missing and how to get it on the distro actually in front of you.
    echo "   FAIL $CXX not found. Install the MinGW-w64 cross-compiler:" >&2
    if   command -v pacman  >/dev/null 2>&1; then echo "        sudo pacman -S mingw-w64-gcc" >&2
    elif command -v apt-get >/dev/null 2>&1; then echo "        sudo apt-get install mingw-w64" >&2
    elif command -v dnf     >/dev/null 2>&1; then echo "        sudo dnf install mingw64-gcc-c++" >&2
    elif command -v zypper  >/dev/null 2>&1; then echo "        sudo zypper install mingw64-cross-gcc-c++" >&2
    else echo "        (no known package manager found; install it however your distro does)" >&2
    fi
    echo "   or point CXX at one: CXX=/path/to/x86_64-w64-mingw32-g++ $0" >&2
    exit 1
fi
"$CXX" --version | head -1 | sed 's/^/   /'

echo "== 2.5) 打补丁：给 bridge 加 DLSS 模型预设覆盖"
# 上游 bridge 从来没设过 DLSS.Hint.Render.Preset.*，SR carrier 一直跑在驱动默认档(K)。
# 实测把它改成 L/M，高频能量从 +1.9% 跳到 +14.5%（vs Lanczos）。详见 README「调参」。
if grep -q DLSS5NR_MODEL_PRESET "$ROOT/native/dlss5nr_bridge.cpp"; then
    echo "   已打过，跳过"
elif patch -p0 -d "$ROOT" --forward --silent < "$HERE/patches/0001-dlss-model-preset.patch"; then
    echo "   ✓ 补丁已应用"
else
    echo "   ⚠️ 补丁没打上（上游改动过？）——仍可编译，但 model_preset 参数会无效"
fi

echo "== 3) 交叉编译"
mkdir -p "$ROOT/native/bin" "$ROOT/runtime/caller"
FLAGS="-std=c++17 -O2 -static -static-libgcc -static-libstdc++"
LIBS="-ld3d12 -ldxgi -ldxguid -luuid -lole32"

"$CXX" $FLAGS -municode "$ROOT/native/dlss5nr_host.cpp" \
    -o "$ROOT/native/bin/dlss5nr_host.exe"
"$CXX" $FLAGS -shared "$ROOT/native/dlss5nr_bridge.cpp" \
    -o "$ROOT/native/bin/dlss5nr_bridge.dll" $LIBS
"$CXX" $FLAGS -shared "$ROOT/native/caller_shim.cpp" \
    -o "$ROOT/native/bin/nvngx.dll_comfy.dll" $LIBS

# shim 必须落在 runtime/caller/，前端按这个路径找
cp -f "$ROOT/native/bin/nvngx.dll_comfy.dll" "$ROOT/runtime/caller/"

# dlssg_host is OURS, not upstream's - it lives in this repo, not in $ROOT.
# Probe-only for now; see docs/DLSSG-HOST.md. It is built into the dlssg
# directory the frame-generation node already points at.
if [ -f "$HERE/../native/dlssg_host.cpp" ]; then
    echo "== 3.5) dlssg_host (probe-only, project-owned)"
    mkdir -p "$ROOT/dlssg"
    if "$CXX" $FLAGS -municode "$HERE/../native/dlssg_host.cpp" \
            -o "$ROOT/dlssg/dlssg_host.exe" $LIBS; then
        printf '   %10s  %s\n' "$(stat -c%s "$ROOT/dlssg/dlssg_host.exe")" "$ROOT/dlssg/dlssg_host.exe"
        echo "   probe it:  wine $ROOT/dlssg/dlssg_host.exe --probe"
    else
        echo "   ⚠️  dlssg_host failed to build - the upscaler is unaffected"
    fi
fi

echo "== 4) 产物"
for f in "$ROOT/native/bin/dlss5nr_host.exe" \
         "$ROOT/native/bin/dlss5nr_bridge.dll" \
         "$ROOT/runtime/caller/nvngx.dll_comfy.dll"; do
    printf '   %10s  %s\n' "$(stat -c%s "$f")" "$f"
done

cat <<'TIP'

下一步：
  1) bash install/setup_prefix.sh <wine-prefix>      # 环境四件事
  2) 自备 NVIDIA 运行时放到:
       <ROOT>/runtime/nvngx_dlssnr.dll    (feature 18，必需)
       <ROOT>/runtime/nvngx_dlss.dll      (carrier，>1x 时必需)
  3) 重启 ComfyUI
TIP
