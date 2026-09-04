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
# 或者从本仓库 Releases 下预编好的 dlss5nr-native-mingw-x64.tar.gz 解到 /workspace/dlss5nr/（内含 native/bin/ 与 runtime/caller/）
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

## 调参

### 🔴 最大的杠杆：DLSS 模型预设

**上游 bridge 从来没设过 `DLSS.Hint.Render.Preset.*`**，所以 SR carrier 一直跑在驱动默认档上。
本仓库用 `install/patches/0001-dlss-model-preset.patch` 补上，并做成节点参数 `model_preset`。

实测（RTX 5090，H3 出片 736×1280 → 1.724×，`structure=2.0`，高频能量 vs Lanczos 放大）：

| model_preset | 高频能量 | vs Lanczos |
|---|---:|---:|
| Default（驱动默认） | 0.3158 | +1.9% |
| J (10) | 0.3159 | +2.0% |
| K (11) | 0.3158 | +1.9%（与 Default **逐字节相同** → 默认就是 K） |
| **L (12)** | 0.3540 | **+14.3%** |
| **M (13)** | 0.3546 | **+14.5%** |

与社区口径一致：50 系驱动默认 Quality/Balanced 用 K、Performance 用 M；[hardforum](https://hardforum.com/threads/591-74-new-dlss-4-5.2045793/page-3) 上的说法是 K 相对 M "looked too blurry"。做法沿用 [Merserk v4.0 changelog](https://github.com/Merserk/dlss5-visual-enhancer/releases)：*"Added forced J/K/L/M preset support across DLAA, Quality, Balanced, Performance, Ultra Performance, and Ultra Quality hint parameters."* 键名从 `nvngx_dlss.dll` 字符串表确认。

### 效果分解

| 配置 | 高频能量 | vs Lanczos |
|---|---:|---:|
| 默认预设 · NR 关（只有 carrier） | 0.3016 | −2.6% |
| 默认预设 · structure 2.0 | 0.3158 | +1.9% |
| **M · NR 关（只有 carrier）** | 0.3387 | **+9.3%** |
| M · structure 1.0 | 0.3422 | +10.5% |
| **M · structure 2.0** | 0.3546 | **+14.5%** |
| M · structure 3.0（超文档） | 0.3757 | **+21.3%** |
| 默认预设 · structure 2.0 · auto_mask 开 | 0.3086 | −0.4% |
| 1.0x · structure 2.0（无 carrier） | — | **−3.9% vs 原片** |

**光换预设就值 12 个百分点，NR 的 structure 再加 5 个。**

### 建议档位

| 场景 | scale | model_preset | structure | auto_mask |
|---|---|---|---|---|
| **通用（推荐）** | 1.724x / 2.0x | **M** | 2.0 | 关 |
| 更锐（超文档范围） | 1.724x / 2.0x | M | 3.0 – 4.0 | 关 |
| 保护皮肤质感 | 1.724x | M | 1.5 | 开（会损失大部分锐度） |
| ❌ 不要用 | **1.0x** | — | — | — |

### 各参数是否有效

| 参数 | 有效？ | 说明 |
|---|---|---|
| `model_preset` | ✅✅ | **最大杠杆**，见上表 |
| `structure` | ✅ | NVIDIA：Structure Intensity 管高频细节（环境光遮蔽/接触阴影/反射/次表面散射） |
| `tone` | ✅ | 低频：整体光照与色彩响应。设 `0` = 完全保留原片配色 |
| `auto_mask` | ✅ | 保护皮肤不被过度锐化 —— 因此会**降低**整体锐度 |
| `skin` | ⚠️ | 只有 `auto_mask` 开启时才有效果。`-1` = 跟随 structure |
| `intensity` | ❌ | 本路径无效。NVIDIA SDK 里没有独立 intensity |
| `preset` / `style` | ❌ | inert：出厂 DLL 只含单一网络 |

### 范围

NVIDIA SDK 文档写 **0–1**，Merserk UI 放到 **0–2**。实测 `structure` 越过 2.0 仍继续起效（3.0 → +21.3%），但已无文档背书。社区把拉满叫 **"AI slop look"**，人脸过锐会出假毛孔假皱纹 —— ⚠️ 高频能量只是锐度的代理指标，**上生产前必须用眼睛在真实素材上确认**。

### 来源

- [NVIDIA · DLSS 5 3D-Guided Neural Rendering](https://www.nvidia.com/en-us/geforce/news/dlss-5-3d-guided-neural-rendering/) —— Structure/Tone 语义
- [TechPowerUp · DLSS 5 Technical Preview](https://www.techpowerup.com/review/nvidia-dlss-5-technical-preview/3.html) —— 0–1 范围、「环境拉满人脸保守」的工作室共识
- [Merserk releases](https://github.com/Merserk/dlss5-visual-enhancer/releases) —— 强制 J/K/L/M 预设的做法；控制表 0–2 范围
- [hardforum](https://hardforum.com/threads/591-74-new-dlss-4-5.2045793/page-3) —— K 相对 M 偏软
- [nexusmods/site/mods/2224](https://www.nexusmods.com/site/mods/2224) —— skin=−1 跟随 structure、preset/style 可能无效
- [ThunderRuler/dlss5-installer-skill · config-reference.md](https://github.com/ThunderRuler/dlss5-installer-skill/blob/main/references/config-reference.md) —— NRPreset/NRStyle "Currently inert"

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
