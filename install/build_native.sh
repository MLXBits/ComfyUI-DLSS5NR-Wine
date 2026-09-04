#!/usr/bin/env bash
# 取上游源码并用 MinGW-w64 交叉编出三件套。不碰任何 NVIDIA 文件。
#
# 上游 native/build_host_mingw.sh 注明 "The D3D12 bridge still needs the
# Windows SDK/MSVC build"，只肯用 MinGW 编 transport host。实测那是保守说法：
# MinGW-w64 13 自带 d3d12.h，bridge 和 caller shim 一次全过。
set -euo pipefail

ROOT=${1:-/workspace/dlss5nr}
UPSTREAM=https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux
CXX=${CXX:-x86_64-w64-mingw32-g++}

echo "== 1) 上游源码 -> $ROOT"
if [ -d "$ROOT/.git" ]; then
    git -C "$ROOT" pull --ff-only
else
    git clone --depth 1 "$UPSTREAM" "$ROOT"
fi

echo "== 2) MinGW-w64"
if ! command -v "$CXX" >/dev/null 2>&1; then
    echo "   装 mingw-w64…"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q mingw-w64
fi
"$CXX" --version | head -1 | sed 's/^/   /'

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
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
