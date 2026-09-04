# ComfyUI-DLSS5NR-Wine

**在 Linux 上跑通的 NVIDIA DLSS 5 Neural Rendering（NGX feature 18）ComfyUI 节点。**

IMAGE 批进 → feature 18 → IMAGE 批出。整批共用一个持久 NGX 会话，帧间保留时域状态。

```
ComfyUI IMAGE  →  wine  →  dlss5nr_host.exe  →  D3D12(vkd3d-proton) + DXVK-NVAPI
                                              →  nvngx_dlss.dll   (carrier, >1x 时)
                                              →  nvngx_dlssnr.dll (feature 18)
                                              →  ComfyUI IMAGE
```

---

## 为什么会有这个仓库

Linux 上跑 DLSS 5 NR 的现成方案有两条，第一条是死的：

| 方案 | 状态 |
|---|---|
| Merserk worker + ReShade + `renodx-dlss5.addon64`（ComfyUI-DonutNodes PR #29 走这条） | ❌ Wine 下 **feature 18 create 之后 addon 静默 abort**，退出码 3 |
| [kos94ok/ComfyUI-DLSS5-NR-Linux](https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux) 的 host（**本仓库用这条**） | ✅ 自己持有 D3D12/NGX 会话，不加载 ReShade/RenoDX |

第一条的失败不是配置问题，是上游未修的 bug：[NIGos/dlss5-bridge#22](https://github.com/NIGos/dlss5-bridge/issues/22)。
本机 addon sha256 `D5ADF82EB44B065F4C590AC91FE824BAB07AFEA0EB9F994BDE936710C8593952`
与该 issue 报告者的**逐字节相同**，同为 Blackwell + vkd3d-proton；对方用的还是**参考版**
`nvngx_dlssnr.dll`（165,840,496 B）一样崩。issue 至今 open，维护者没有 Linux 环境无法复现。

上游 kos94ok 项目只发 Windows 用法、没有 release，且构建脚本注明 D3D12 bridge 需要
MSVC/Windows SDK。本仓库做的事是：**用 MinGW-w64 把它整套交叉编出来，包成 ComfyUI 节点，
并把 Linux 侧那一串环境前置条件写成脚本。**

## 实测

RTX 5090 / 驱动 590.48.01 / Ubuntu 24.04 容器 / GE-Proton10-34：

| 项 | 结果 |
|---|---|
| 节点输出 | `(8, 736, 480, 3)` → `(8, 1472, 960, 3)` |
| 与双三次放大的平均绝对差 | **0.0578**（不是插值） |
| 1.0x 与输入的平均绝对差 | **28.049 / 255** |
| feature 18 是否真跑 | host 的 `CreateFeature(18) failed` / `DLSSNR EvaluateFeature failed` 两条错误路径**均未触发** |
| 吞吐 | **8.72 帧/秒**（2×）→ 15s@24fps 约 41 秒 |

只测过 480×736 竖屏。倍率只支持 NVIDIA 的固定档：
`1.0x(DLAA) / 1.5x(Quality) / 1.724x(Balanced) / 2.0x(Performance) / 3.0x(Ultra Performance)`。

## 装

### 1. 节点

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/LQCCS/ComfyUI-DLSS5NR-Wine
```

### 2. 上游源码 + 原生二进制

```bash
bash ComfyUI-DLSS5NR-Wine/install/build_native.sh          # clone 上游 + MinGW 交叉编译
# 或者从本仓库 Releases 下预编好的 dist.tar.gz 解到 /workspace/dlss5nr/native/bin/
```

### 3. Wine prefix + 虚拟显示

```bash
bash ComfyUI-DLSS5NR-Wine/install/setup_prefix.sh /workspace/dlss5/prefix/pfx
```

这一步做四件事，缺一个 worker 就起不来，每条的判据见 [docs/FINDINGS.md](docs/FINDINGS.md)：

- Xorg + dummy 驱动的虚拟显示 `:99`，**必须带真实 60 Hz Modeline**（Xvfb 报 0.00 Hz 会让 DXVK 整数除零崩溃）
- Win10 SDK 版 `d3dcompiler_47.dll`（winetricks 给的 8.1 版不认 `cs_5_1`）
- dxvk-nvapi ≥ 0.9.2 + vkd3d-proton ≥ 3.0.1（64-bit CuBIN 要这两个版本）
- 驱动的 `nvngx.dll` / `_nvngx.dll` NGX loader + 注册表 `NGXCore` 指向

### 4. 自备 NVIDIA 运行时

**本仓库不含、也不会含任何 NVIDIA 专有二进制。** 自行取得后放到：

```
/workspace/dlss5nr/runtime/nvngx_dlssnr.dll     # feature 18，必需
/workspace/dlss5nr/runtime/nvngx_dlss.dll       # 普通 DLSS carrier，>1x 时必需
```

### 5. 重启 ComfyUI

自定义节点包只在启动时扫描。重启后节点出现在 `image/DLSS 5 NR (Wine)` 分类下。

## 用

先跑 **DLSS 5 NR · 自检 (Wine)** —— 不碰 GPU，几秒确认七项文件与环境齐不齐。

再接 **DLSS 5 神经渲染 · Wine (Linux)**：`image` 进，`image` + `report` 出。

关键参数：

| 参数 | 说明 |
|---|---|
| `display` | **必填**，默认 `:99`。去掉 DISPLAY 后 DXVK 连 Vulkan instance 都建不出来（实测） |
| `warmup_frames` | 给运行时的时域预热提示，不额外消耗帧 |
| `reset_each_frame` | 静态图批量打开；视频不要开，会丢时域收益 |
| `channel_order` | `auto` 即可。NR DLL 出现过 RGB / BGR 两种通道布局，`auto` 只判一次然后整段沿用，避免色彩闪烁 |

## 已知限制

1. 运动向量来自 **OpenCV 光流估计**（Merserk 兼容口径），不是真 motion vector。真实素材上可能有时域瑕疵，上生产前请肉眼确认。
2. 只在 RTX 5090（Blackwell）上验证过。`0xBAD00001` = `FAIL_FeatureNotSupported`，表示你那份 `nvngx_dlssnr.dll` 不支持当前 GPU。
3. 只测过 480×736。输入必须是偶数尺寸。
4. 需要 X display（哪怕无头）。这条不是 ReShade 的锅 —— DXVK 建 Vulkan instance 时要 `VK_KHR_win32_surface`。

## 来源与许可

- 本仓库自身代码（`nodes.py`、`install/`、`docs/`）：MIT，见 [LICENSE](LICENSE)。
- `dlss5nr_host.exe` / `dlss5nr_bridge.dll` / `nvngx.dll_comfy.dll`：由
  [kos94ok/ComfyUI-DLSS5-NR-Linux](https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux)（MIT）的
  **项目自有源码**用 MinGW-w64 交叉编译得到，可再分发。
- NVIDIA DLSS / NGX 运行时：**专有，不在本仓库内**，由使用者自行合法取得。
