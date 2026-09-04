#!/usr/bin/env bash
# Wine prefix + 虚拟显示的四件事。缺一个 worker 就起不来。
# 每条的一手依据见 docs/FINDINGS.md。不下载、不安装任何 NVIDIA 专有运行时。
#
#   ① Xorg + dummy 虚拟显示 :99，带真实 60Hz Modeline
#   ② Win10 SDK 版 d3dcompiler_47.dll（8.1 版不认 cs_5_1）
#   ③ dxvk-nvapi >= 0.9.2 + vkd3d-proton >= 3.0.1（64-bit CuBIN 要这两个）
#   ④ 驱动的 nvngx.dll / _nvngx.dll NGX loader + 注册表 NGXCore 指向
set -uo pipefail

PFX=${1:?用法: setup_prefix.sh <wine-prefix> [wine64] [ngx-bridge-dir]}
WINE=${2:-$(command -v wine64 || command -v wine)}
BRIDGE=${3:-$(dirname "$PFX")/../ngx_bridge}
S="$PFX/drive_c/windows/system32"
ELECTRON_TAG=v44.2.0
DXC_BYTES=4741488          # d3dcompiler_47.dll 10.0.26100.7705 解压后大小
export WINEPREFIX="$PFX" WINEDEBUG=-all

[ -d "$PFX/drive_c" ] || { echo "prefix 不存在或未初始化: $PFX"; exit 1; }
[ -x "$WINE" ] || { echo "找不到 wine64: $WINE"; exit 1; }
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# ── ① 虚拟显示 ──────────────────────────────────────────────
echo "== ① Xorg + dummy 虚拟显示 :99"
command -v Xorg >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    xserver-xorg-core xserver-xorg-video-dummy x11-utils x11-xserver-utils >/dev/null 2>&1
if command -v Xorg >/dev/null 2>&1; then
    cp -f "$HERE/xorg-dummy.conf" /etc/X11/xorg-dummy.conf
    if [ -d /etc/supervisor/conf.d ]; then
        cat > /etc/supervisor/conf.d/dlss5_display.conf <<'EOF'
[program:dlss5_display]
command=/usr/bin/Xorg :99 -config /etc/X11/xorg-dummy.conf -noreset -nolisten tcp -logfile /var/log/portal/xorg99.log
autostart=true
autorestart=true
priority=1
redirect_stderr=true
EOF
        supervisorctl reread >/dev/null 2>&1; supervisorctl update >/dev/null 2>&1; sleep 6
    else
        pgrep -f 'Xorg :99' >/dev/null || \
            (nohup Xorg :99 -config /etc/X11/xorg-dummy.conf -noreset -nolisten tcp \
                 >/tmp/xorg99.log 2>&1 & sleep 5)
    fi
    RATE=$(DISPLAY=:99 xrandr 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\*' | head -1)
    if [ -n "$RATE" ] && [ "$RATE" != "0.00*" ]; then
        echo "   ✓ :99 刷新率 $RATE"
    else
        echo "   ✗ :99 刷新率=${RATE:-无} —— DXVK 会在 swapchain 创建时除零崩溃"
    fi
else
    echo "   ✗ Xorg 装不上"
fi

# ── ② Win10 版 d3dcompiler_47 ───────────────────────────────
# ⚠️ 本步骤对 **kos94ok/Wine 这条路是不需要的** —— 实测三个原生二进制里 D3DCompile
#    命中 0，依赖只有 KERNEL32/d3d12/dxgi/msvcrt/ole32。需要 cs_5_1 编译器的是
#    RenoDX/ReShade 那条（在 Wine 下已确认走不通的）路。保留是为了那条路万一被修好；
#    设 DLSS5NR_SKIP_DXC=1 可跳过，省一次 Range 下载。
if [ "${DLSS5NR_SKIP_DXC:-0}" = "1" ]; then
  echo "== ② d3dcompiler_47.dll —— 跳过（本路径不需要，见注释）"
else
echo "== ② d3dcompiler_47.dll（要 Win10 SDK 10.0.x，才认 cs_5_1）"
CUR=$(stat -c%s "$S/d3dcompiler_47.dll" 2>/dev/null || echo 0)
if [ "$CUR" = "$DXC_BYTES" ]; then
    echo "   ✓ 已是 Win10 版（$CUR B）"
else
    URL="https://github.com/electron/electron/releases/download/$ELECTRON_TAG/electron-$ELECTRON_TAG-win32-x64.zip"
    if python3 "$HERE/zipgrab.py" "$URL" d3dcompiler_47.dll /tmp/d3dc47.dll "$DXC_BYTES"; then
        [ -f "$S/d3dcompiler_47.dll.bak_81" ] || cp -f "$S/d3dcompiler_47.dll" "$S/d3dcompiler_47.dll.bak_81" 2>/dev/null
        cp -f /tmp/d3dc47.dll "$S/d3dcompiler_47.dll"
        echo "   ✓ 换成 $(stat -c%s "$S/d3dcompiler_47.dll") B"
    else
        echo "   ✗ 取不到 —— NR 会因 cs_5_1 编译失败静默退回普通 DLAA"
    fi
fi
fi

# ── ③ dxvk-nvapi + vkd3d-proton ─────────────────────────────
echo "== ③ dxvk-nvapi >= 0.9.2 / vkd3d-proton >= 3.0.1"
NV=$(strings -a "$S/nvapi64.dll"   2>/dev/null | grep -oE '^v?0\.9\.[0-9]+' | sort -u | head -1)
VK=$(strings -a "$S/d3d12core.dll" 2>/dev/null | grep -oE '^3\.0\.[0-9]+'   | sort -u | head -1)
if [ "$NV" = "v0.9.2" ] && [ "$VK" = "3.0.1" ]; then
    echo "   ✓ nvapi $NV / vkd3d $VK"
else
    echo "   现有 nvapi=${NV:-?} vkd3d=${VK:-?} —— 升级"
    command -v zstd >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q zstd >/dev/null 2>&1
    T=$(mktemp -d); cd "$T"
    curl -sfL -o n.tgz  https://github.com/jp7677/dxvk-nvapi/releases/download/v0.9.2/dxvk-nvapi-v0.9.2.tar.gz && tar xzf n.tgz
    curl -sfL -o v.tzst https://github.com/HansKristian-Work/vkd3d-proton/releases/download/v3.0.1/vkd3d-proton-3.0.1.tar.zst \
        && zstd -dq v.tzst -o v.tar && tar xf v.tar
    for f in nvapi64.dll d3d12.dll d3d12core.dll; do
        src=$(find "$T" -path '*/x64/*' -name "$f" -print -quit)
        [ -n "$src" ] || { echo "   ✗ 新包里没有 $f"; continue; }
        [ -f "$S/$f.bak_old" ] || cp -f "$S/$f" "$S/$f.bak_old" 2>/dev/null
        cp -f "$src" "$S/$f"
    done
    cd /; rm -rf "$T"
    NV=$(strings -a "$S/nvapi64.dll"   | grep -oE '^v?0\.9\.[0-9]+' | sort -u | head -1)
    VK=$(strings -a "$S/d3d12core.dll" | grep -oE '^3\.0\.[0-9]+'   | sort -u | head -1)
    echo "   -> nvapi ${NV:-?} / vkd3d ${VK:-?}"
fi

# ── ④ NGX loader ────────────────────────────────────────────
echo "== ④ NGX loader（nvngx.dll / _nvngx.dll）"
if [ -s "$BRIDGE/_nvngx.dll" ] && [ -s "$BRIDGE/nvngx.dll" ]; then
    cp -f "$BRIDGE"/nvngx.dll "$BRIDGE"/_nvngx.dll "$S/"
    WBRIDGE=$("$WINE" winepath -w "$BRIDGE" 2>/dev/null | tr -d '\r')
    for KV in "HKLM\\SOFTWARE\\NVIDIA Corporation\\Global\\NGXCore|FullPath" \
              "HKLM\\SOFTWARE\\NVIDIA Corporation\\Global\\NGXCore|NGXPath" \
              "HKLM\\System\\CurrentControlSet\\Services\\nvlddmkm\\NGXCore|NGXPath" \
              "HKLM\\System\\CurrentControlSet\\Services\\nvlddmkm\\Parameters\\NGXCore|NGXPath"; do
        timeout 90 "$WINE" reg add "${KV%|*}" /v "${KV#*|}" /d "$WBRIDGE" /f >/dev/null 2>&1
    done
    echo "   ✓ 已装并把注册表 NGXCore 指向 $WBRIDGE"
else
    cat <<EOF
   ✗ $BRIDGE 里没有 nvngx.dll / _nvngx.dll。
     这两个是 NVIDIA 驱动给 Wine 用的 PE 桥接件，本仓库不提供。
     从与宿主驱动同版本的官方 .run 里取（只解包不安装）：
       DRV=\$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | tr -d ' ')
       curl -O https://us.download.nvidia.com/XFree86/Linux-x86_64/\$DRV/NVIDIA-Linux-x86_64-\$DRV.run
       sh NVIDIA-Linux-x86_64-\$DRV.run --extract-only --target /tmp/nvdrv
       mkdir -p $BRIDGE && cp /tmp/nvdrv/nvngx.dll /tmp/nvdrv/_nvngx.dll $BRIDGE/
     然后重跑本脚本。
EOF
fi
echo "== 完成"
