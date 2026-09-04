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

**七个参数里只有三个真的起作用。** 下面每条都有一手来源或本机实测。

| 参数 | 有效？ | 说明 |
|---|---|---|
| `structure` | ✅ | **唯一真正的清晰度杠杆。** NVIDIA：Structure Intensity 管高频细节（环境光遮蔽、接触阴影、反射、次表面散射） |
| `tone` | ✅ | 低频：整体光照与色彩响应。设 `0` = 完全保留原片配色 |
| `auto_mask` | ✅ | 自动识别皮肤并保护它不被过度锐化 —— 因此会**降低**整体锐度 |
| `skin` | ⚠️ | **只有 `auto_mask` 开启时才有效果**。`-1` 的语义是「跟随 structure」，不是关闭 |
| `intensity` | ❌ | 本路径无效。NVIDIA SDK 里没有独立 intensity，只有 Structure/Tone 两个滑块 |
| `preset` | ❌ | inert：出厂 DLL 只含单一网络，没有可切换的对象 |
| `style` | ❌ | inert，同上 |

### 范围

NVIDIA SDK 文档写 **0–1**，Merserk 的 UI 放到 **0–2**。本机实测 `structure` **越过 2.0 仍在继续起效**，但那已无任何文档背书。

### 实测（RTX 5090，H3 出片 736×1280 → 1.724×，高频能量 vs Lanczos 放大）

| 参数组 | 相对 Lanczos |
|---|---:|
| `structure=1.0`（上游 CLI 默认） | **−2.2%** ← 比普通插值还糊 |
| `structure=1.5 skin=1.0` | −1.5% |
| `structure=2.0` | +1.9% |
| `structure=2.0 tone=2.0 skin=2.0` | +4.5% |
| `structure=3.0`（超文档） | **+9.7%** |
| `structure=4.0`（超文档） | **+12.8%** |

**所以本节点把 `structure` 默认设成 2.0** —— 上游 CLI 的 1.0 会得到比双三次插值还软的结果。

### 建议档位

| 场景 | scale | structure | tone | auto_mask |
|---|---|---|---|---|
| 求清晰（通用） | 1.724x / 2.0x | 2.0 | 1.0 | **关** |
| 最大锐度（超文档） | 1.724x / 2.0x | 3.0 – 4.0 | 1.0 | 关 |
| 想保护皮肤质感 | 1.724x | 1.5 | 1.0 | 开（会损失大部分锐度） |
| ❌ 不要用 | **1.0x** | — | — | — |

### 效果分解（实测，RTX 5090，H3 出片 736×1280）

| 配置 | 高频能量 | 相对基准 |
|---|---:|---:|
| 1.724x · NR 关到底（只有 carrier） | 0.3016 | **−2.6%** vs Lanczos |
| 1.724x · structure 2.0 · mask 关 | 0.3158 | **+1.9%** vs Lanczos |
| 1.724x · structure 2.0 · mask 开 | 0.3086 | −0.4% vs Lanczos |
| 1.0x · NR 关到底（等于直通） | 0.4180 | +0.8% vs 原片 |
| 1.0x · structure 2.0 | 0.3987 | **−3.9%** vs 原片 |

三条结论：

1. **DLSS SR carrier 单独并不比 Lanczos 好**（−2.6%）。
2. **feature 18 只在 >1× 时有正作用**（比只有 carrier 高 4.5 个百分点）—— 它是给放大后变软的画面补结构，不是给已经锐的画面加锐。
3. **`auto_mask` 会吃掉大部分收益**（+1.9% → −0.4%）。

### 期望值管理

即使配置全对，相对一个好的 Lanczos 缩放**也只有约 +2%**。原因是结构性的：DLSS 5 NR 是 **3D-guided** 的，需要真实深度缓冲与运动矢量，而这条视频路径里[上游架构文档](https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux/blob/main/docs/ARCHITECTURE.md)自己写着 *"Video has no scene-depth buffer, so the carrier receives a **zero-filled** R32_FLOAT depth surface"*，运动矢量也是 OpenCV 光流估计的。它赖以工作的两路引导信息，一路全零、一路是猜的。

**这套东西给的是「不同质感的细节」，不是「更高的分辨率」。** 想要真的更清晰，先把生成端的分辨率提上去。

### 来源

- [NVIDIA · DLSS 5 3D-Guided Neural Rendering](https://www.nvidia.com/en-us/geforce/news/dlss-5-3d-guided-neural-rendering/) —— Structure/Tone 语义
- [TechPowerUp · DLSS 5 Technical Preview](https://www.techpowerup.com/review/nvidia-dlss-5-technical-preview/3.html) —— 0–1 范围、工作室共识
- [Merserk README 控制表](https://github.com/Merserk/dlss5-visual-enhancer) —— 0–2 UI 范围与默认值
- [nexusmods/site/mods/2224](https://www.nexusmods.com/site/mods/2224) —— skin=−1 跟随 structure、preset/style 可能无效
- [ThunderRuler/dlss5-installer-skill · config-reference.md](https://github.com/ThunderRuler/dlss5-installer-skill/blob/main/references/config-reference.md) —— 从 addon 二进制提取的键表，明写 NRPreset/NRStyle "Currently inert"

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
