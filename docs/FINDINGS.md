# Linux 上跑 DLSS 5 Neural Rendering 的九堵墙

2026-09-04，Vast.ai 实例 49812079：RTX 5090 / 驱动 590.48.01 / Ubuntu 24.04 容器 /
GE-Proton10-34 / ComfyUI。全部结论来自这台机器上的一手日志，不是推测。

先说结论：**走 Merserk worker + ReShade + `renodx-dlss5.addon64` 那条路（ComfyUI-DonutNodes
PR #29）在 Wine 下是死的。** 前八堵墙都能拆，第九堵是上游未修的 bug。换成
kos94ok 的 host（自己持有 D3D12/NGX 会话，不加载 ReShade/RenoDX）则一次跑通。

---

## ① `KeyError: STEAM_COMPAT_CLIENT_INSTALL_PATH`

Proton 启动脚本必须要这个环境变量。

```bash
mkdir -p /root/.steam/root
export STEAM_COMPAT_CLIENT_INSTALL_PATH=/root/.steam/root
export XDG_RUNTIME_DIR=/tmp/xdg
```

## ② `files/bin/wine` 是 32 位 ELF

GE-Proton 的 `files/bin/wine` 需要 `/lib/ld-linux.so.2`，容器里没装 32 位 libc，
于是 sh 报 `wine: not found`，winetricks 整个走不下去、prefix 连 `drive_c` 都建不出来。

用 `files/bin/wine64`（64 位，`ldd` 缺失库数 0）+ `WINEARCH=win64`。

## ③ Proton 后端吞掉 worker 的全部输出

同一个 worker 用 `proton run` 跑，stderr 一个字都没有、退出码 0；换成裸 `wine64`
立刻打出自己的 banner。**排障时别用 Proton 后端。**

## ④ `[host] window creation failed`

ReShade 要 swapchain，swapchain 要窗口，无头容器里没有 X display。
给一个虚拟显示即可 —— 但见第 ⑥ 条，Xvfb 不够。

## ⑤ `D3D12CreateDevice failed 0x80004002`（E_NOINTERFACE）

prefix 的 system32 里四个翻译层文件都在（vkd3d-proton 的 `d3d12.dll`/`d3d12core.dll`、
DXVK 的 `dxgi.dll`、dxvk-nvapi 的 `nvapi64.dll`），但 **DllOverrides 里一个都没有**。
没有 native override，Wine 加载**内建 d3d12**（老 vkd3d），它向 DXVK 的 IDXGIAdapter
查一个只有内建 vkd3d 才认的接口，直接 E_NOINTERFACE。

`donut_dlss5_linux.py` 的 `_append_dll_overrides` 只补了 `dxgi=n,b` 和 `nvapi64=n,b`，
**漏了 d3d12 / d3d12core** —— 这是上游节点自己的 bug。

正确的一组是：

```
WINEDLLOVERRIDES="d3d12,d3d12core,nvapi64,dxgi=n,b"
```

补上后 vkd3d-proton 正常接管（DXR / SM 6.8 / DX Ultimate 全开）。

> 旁证：kos94ok 前端里 `env.setdefault("WINEDLLOVERRIDES", "d3d12,d3d12core,nvapi64,dxgi=n,b")`
> 与这里独立得出的结论完全一致。它还注明：只用 builtin(`=n`) 会让 NGX 起来但
> NvAPI 看不到物理显卡。

## ⑥ DXVK 整数除零 —— **Xvfb 不行，必须 Xorg + dummy**

```
Exception 0xc0000094 = EXCEPTION_INT_DIVIDE_BY_ZERO
addr = C:\windows\system32\dxgi.dll + 0x266CC        (DXVK，不是 ReShade)
指令 = idivq %r12      r12 = 0
```

`+loaddll` 确认崩溃地址属于 DXVK 的 dxgi（基址 0x6FFFFD6A0000），不是 ReShade 那份同名 DLL。

崩溃前两行：

```
err: readMonitorEdidFromKey: Failed to get EDID reg key size
err: DXGI: Failed to parse display metadata + colorimetry info, using blank.
```

Xvfb 的 RandR 输出：

```
screen connected 1920x1080+0+0   0mm x 0mm
   1920x1080      0.00*                      ← 刷新率 0
属性里只有 non-desktop，没有 EDID
```

**往 Wine 注册表手写 EDID 没用**：winex11 在进程启动时从 RandR 拿不到 EDID，
会写一个空的 `BAD_EDID` 把你写的那份覆盖掉（`+reg` 追踪可见
`NtSetValueKey (0x7c, L"BAD_EDID", 3, (nil), 0)`）。

改用 **Xorg + xf86-video-dummy，配置里给一条真实的 60 Hz Modeline**，`xrandr` 报
`1920x1080 60.00*`，除零消失。注意 dummy 驱动不认 `Option "CustomEDID"`（日志明说
`is not used`），物理尺寸仍报 0mm×0mm —— 实测**除数是刷新率不是物理尺寸**，够用。

一手佐证：
- [dxvk#3382](https://github.com/doitsujin/dxvk/issues/3382) 最后一条评论，三行日志与本机逐字相同，
  并指出 gamescope 之所以能用是因为它合成了合法 EDID
- [dxvk#3706](https://github.com/doitsujin/dxvk/issues/3706) 报告者换 xdummy 解决同一问题

（`gamescope` 同样有效，但 Ubuntu 24.04 的 apt 里 `Candidate: (none)`。）

## ⑦ NGX loader 不存在，扫描扫到假阳性

```
DLSS5 Generic: NGX module scan (loaded copies):
    nvngx.dll                                   ← 是 worker 自己那个同名 EXE，无导出表
ERROR | vtable::Hook(Failed to find NVSDK_NGX_D3D12_CreateFeature)   ×3 轮
```

Merserk 的 worker 里静态链接着 NVIDIA 自己的 NGX loader（字符串
`NGXGetPathUsingQAI` / `NGXGetPathFromRegistry` / `NGXCore not found next to the application`）。
三条查找路径：QAI 走 D3DKMT，Wine 下是死的；同目录那条撞上 worker 自己就叫
`nvngx.dll` 的 EXE；只剩注册表。

vast 宿主的驱动是**无 wine 组件的服务器版安装**，容器里 `nvngx.dll` / `_nvngx.dll` 根本不存在
（`NVIDIA_DRIVER_CAPABILITIES=all` 也没用）；GE-Proton 不能再分发它们 ——
Proton 源码第 552 行写着 `# Check that nvngx.dll exists here, or fail`，第 1306 行
`for dll in ["_nvngx.dll", "nvngx.dll"]: try_copy(...)`。

解法：从与宿主驱动**同版本**的官方 `.run` 里 `--extract-only` 取出这两个 PE
（`nvngx.dll` 478,976 B / `_nvngx.dll` 1,978,480 B），放**独立目录**避开同名冲突，
再把注册表四处 `NGXCore` 指过去：

```
HKLM\SOFTWARE\NVIDIA Corporation\Global\NGXCore          FullPath / NGXPath
HKLM\System\CurrentControlSet\Services\nvlddmkm\NGXCore              NGXPath
HKLM\System\CurrentControlSet\Services\nvlddmkm\Parameters\NGXCore   NGXPath
```

装对之后日志出现 `detoured NGX module copy [0] ...\ngx_bridge\_nvngx.dll (core)`。

## ⑧ `X3506: unrecognized compiler target 'cs_5_1'`

```
DLSS5 Generic proxy encode compilation failed with HRESULT 0x8876086c:
    error X3506: unrecognized compiler target 'cs_5_1'
```

RenoDX 要在运行时用 `D3DCompile` 编一个 `cs_5_1` 的代理计算着色器。
prefix 里那份是 **winetricks 装的 Windows 8.1 版 6.3.9600.16384**，二进制里有
`cs_5_0` 没有 `cs_5_1`（SM 5.1 是 Win10 SDK 才加的）。Wine 内建那份
（vkd3d-shader 实现）也编不了，报 `0x80004005`。

换成 **Win10 SDK 版 10.0.26100.7705**（含 `cs_5_1`）即可。不必装 SDK ——
Electron 的 win32-x64 包里带 Chromium 用的那一份，用 HTTP Range 只取这一个文件，
**实测只需下载 2.15 MB，整包是 158 MB**（见 `install/zipgrab.py`）。

换上后日志出现 `created reversible NR color bridge pipeline (SDR sRGB / linear HDR BT.709 / PQ BT.2020)`
和 `NGX feature create intercepted: feature=18 (DLSSNR/reserved-18), slot=0`。

### 附带：64-bit CuBIN

dxvk-nvapi 的日志（`DXVK_NVAPI_LOG_LEVEL=info`）显示：

```
NvAPI_D3D12_CreateCubinComputeShaderExV2: Invalid pointer
NvAPI_D3D12_GetCudaMergedTextureSamplerObject: Invalid pointer
```

GE-Proton10-34 自带 dxvk-nvapi **0.9.1** + vkd3d-proton **3.0.0**。
dxvk-nvapi v0.9.2 changelog 第一条原文：

> Pass CuBIN 64-bit function calls into VKD3D-Proton.
> This requires VKD3D-Proton 3.0.1 or newer.

两边都升到 0.9.2 / 3.0.1 后，同一批调用全部 `OK`。
（驱动侧本来就有 `VK_NVX_binary_import` rev 2 和 `VK_NVX_image_view_handle` rev 3。）

## ⑨ 第九堵：`renodx-dlss5.addon64` 在 Wine 下必崩 —— 无解

前八堵全部拆掉之后：

```
✅ NVSDK_NGX_D3D12_Init -> 0x00000001 (Success)
✅ DLSS carrier ready: 256x256 -> 256x256 mode=5
✅ created inline NR resources 256x256 -> 256x256 format=28
✅ NGX feature create intercepted: feature=18 (DLSSNR/reserved-18), slot=0
❌ 然后 worker 静默 abort，退出码 3（Windows CRT abort），无 SEH 异常
```

三种 warmup 配置（2/1、4/2、8/4）表现相同；把 ComfyUI 重启腾出 50 GB 内存后仍然相同，
**排除内存因素**。

这是上游未修的 bug：[NIGos/dlss5-bridge#22](https://github.com/NIGos/dlss5-bridge/issues/22)。
本机 addon sha256 `D5ADF82EB44B065F4C590AC91FE824BAB07AFEA0EB9F994BDE936710C8593952`
与该 issue 报告者的**逐字节相同**，同为 Blackwell + vkd3d-proton + ReShade 6.8.0。
维护者的判断：

> the DLSS 5 add-on hooked NGX **after** the bridge had already created its feature.
> The bridge rebuilds the feature so the add-on sees a create, but **the command list
> it runs on is older than the hooks**, and that add-on keeps state per list.

报告者补充了 Wine 特有成因：`LoadLibrary` 从 `DllMain` 里在 Wine 下直接失败，
导致 addon 的 hook 扫描被推迟，每次都走重建路径，每次都崩。

**关键一点：对方用的是参考版 `nvngx_dlssnr.dll`（165,840,496 B，比 Merserk 包里那个大
10,352 B）一样崩。所以换 dlssnr 版本解决不了，不必去下 900 MB 的 Windows 驱动包。**

issue 至今 open，维护者没有 Linux 环境无法复现。

---

## 换一条路：绕开 addon

[kos94ok/ComfyUI-DLSS5-NR-Linux](https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux) 的
`dlss5nr_host.exe` 自己持有 D3D12/NGX 会话，**不加载 ReShade、不建 swapchain、没有 addon**，
第九堵墙整个不在路径上。

它没有 release，且 `native/build_host_mingw.sh` 注明 D3D12 bridge 需要 MSVC/Windows SDK。
实测那是保守说法：**MinGW-w64 13 自带 `d3d12.h`，host / bridge / caller shim 一次全过。**

结果（同一台机器）：

| 测试 | 结果 |
|---|---|
| 1.0x 与输入逐像素比对 | 平均绝对差 **28.049 / 255**，最大 167 |
| 2× | 480×736 → 960×1472，24/24 帧 |
| 1.5× | 480×736 → 720×1104，24/24 帧 |
| feature 18 是否真跑 | host 的 `CreateFeature(18) failed` / `DLSSNR EvaluateFeature failed` **均未触发** |
| 吞吐 | 96 帧 2× 用 11 s = **8.72 帧/秒** |
| ComfyUI 节点 | `(8,736,480,3)` → `(8,1472,960,3)`，与双三次放大平均绝对差 **0.0578** |

前八堵墙里的 ①②③⑤⑥⑧ 对这条路**仍然全部适用** —— 它一样跑在 Wine + DXVK +
vkd3d-proton 上，一样需要虚拟显示（去掉 `DISPLAY` 后 DXVK 连 Vulkan instance 都建不出来：
`DxvkInstance::createInstance: Failed to create Vulkan instance`）。

只有 ④⑦ 是 ReShade / RenoDX 专属的，这条路不需要 —— 不过 ⑦ 的 NGX loader 仍然要装，
因为 NGX 本身要靠它。
