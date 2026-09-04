# ComfyUI-DLSS5NR-Wine

**NVIDIA DLSS 5 Neural Rendering (NGX feature 18) as a ComfyUI node on Linux.**

> A fork of **[LQCCS/ComfyUI-DLSS5NR-Wine](https://github.com/LQCCS/ComfyUI-DLSS5NR-Wine)**,
> which did the hard part: proving feature 18 can be driven from ComfyUI on
> Linux at all, finding the nine things that stop it, and cross-compiling
> kos94ok's host with MinGW-w64. **Read that repo first** - its
> [docs/FINDINGS.md](https://github.com/LQCCS/ComfyUI-DLSS5NR-Wine/blob/main/docs/FINDINGS.md)
> is the primary source for everything here, and it is where new discoveries
> should land.
>
> This fork adds English documentation and UI, an installer that does not
> assume a root vast.ai container, and a read-only preflight. It is a
> packaging fork, not a better implementation.

An IMAGE batch goes in, feature 18 runs over it, an IMAGE batch comes out. The
whole batch shares one persistent NGX session, so temporal state carries across
frames.

```
ComfyUI IMAGE  ->  wine  ->  dlss5nr_host.exe  ->  D3D12 (vkd3d-proton) + DXVK-NVAPI
                                                ->  nvngx_dlss.dll   (SR carrier, when scale > 1x)
                                                ->  nvngx_dlssnr.dll (feature 18)
                                                ->  ComfyUI IMAGE
```

Feature 18 is a neural *post-process* at native resolution. The enlargement
comes from ordinary DLSS Super Resolution acting as a carrier underneath it,
which is why anything above 1x needs both DLLs.

---

## Status

Verified end to end on two machines:

| | machine A | machine B |
|---|---|---|
| GPU | RTX 5090 | RTX 5090 |
| driver | 590.48.01 | 610.57.04 |
| OS | Ubuntu 24.04 container (vast.ai) | CachyOS (Arch) |
| Proton | GE-Proton10-34 | GE-Proton11-6 |
| display | Xorg + dummy on `:99` | existing desktop session on `:0` |
| result | 480x736 -> 960x1472, 8.72 fps at 2x | 1344x992 -> 2318x1710, 4.78 fps at 1.724x |

On both, the host's two failure paths (`CreateFeature(18) failed` and
`DLSSNR EvaluateFeature failed`) never fired, and the report ends with
`[dlss5nr] Feature18 EvaluateFeature succeeded`.

DLSS 5 Neural Rendering requires **Blackwell** (RTX 50-series). `0xBAD00001`
is `FAIL_FeatureNotSupported` - the runtime declining the GPU it was given.

## Why this repo exists

There are two routes to DLSS 5 NR on Linux. The first is dead:

| route | status |
|---|---|
| Merserk worker + ReShade + `renodx-dlss5.addon64` (ComfyUI-DonutNodes PR #29 takes this one) | **broken** - under Wine the add-on aborts silently right after feature 18 is created, exit code 3 |
| [kos94ok/ComfyUI-DLSS5-NR-Linux](https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux)'s host (**this repo**) | works - it owns its own D3D12/NGX session and loads neither ReShade nor RenoDX |

The first route's failure is not a configuration problem, it is an unfixed
upstream bug: [NIGos/dlss5-bridge#22](https://github.com/NIGos/dlss5-bridge/issues/22).
The add-on build that fails is byte-identical to the reporter's, on the same
Blackwell + vkd3d-proton combination, and it fails the same way with the
*reference* `nvngx_dlssnr.dll` - so changing NR runtime version does not help.
The issue is still open; the maintainer has no Linux machine to reproduce on.

Upstream kos94ok documents Windows usage only, publishes no releases, and its
build script says the D3D12 bridge needs MSVC and the Windows SDK. That turns
out to be conservative: **MinGW-w64 13 ships `d3d12.h` and cross-compiles the
host, bridge and caller shim in one pass.** This repo does that, packages the
result as a ComfyUI node, and scripts the Linux-side prerequisites.

Nine specific walls, with first-hand logs, are documented in
[docs/FINDINGS.md](docs/FINDINGS.md) (still in Chinese).

## Requirements

- An RTX 50-series (Blackwell) GPU - feature 18 is Blackwell-only
- A GE-Proton build (Steam's `compatibilitytools.d` counts; the installer will
  download one if you have none)
- An X display that reports a **non-zero refresh rate** - see below
- ComfyUI, with `cv2` importable in its environment (without it, motion vectors
  are zero and history resets every frame, losing all temporal benefit)
- Your own `nvngx_dlssnr.dll`

## Install

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/MLXBits/ComfyUI-DLSS5NR-Wine
cd ComfyUI-DLSS5NR-Wine

install/doctor.sh          # what is already here, what is missing (read-only)
install/setup.sh           # fills the gaps; no root, no package manager
```

`install/bootstrap_vast.sh` is the original one-shot installer for a root
vast.ai container - `/workspace`, `apt-get`, supervisord. Use `setup.sh` on
anything else.

`setup.sh` clones the upstream source tree, unpacks the prebuilt MinGW
binaries, finds or downloads GE-Proton, builds the wine prefix **through
Proton**, installs the NGX loader, points the registry at it, and fetches the
DLSS SR carrier. It is idempotent; re-run it after fixing anything it reported.

Then supply the one file nobody can supply for you:

```
<root>/runtime/nvngx_dlssnr.dll
```

**This project does not ship, mirror, or link to it.** It is NVIDIA
proprietary and pre-release.

Finally point ComfyUI at the install. `setup.sh` prints the exact block; for a
systemd user service it goes in
`~/.config/systemd/user/comfyui.service.d/dlss5nr.conf`:

```ini
[Service]
Environment=DLSS5NR_ROOT=/home/you/dlss5nr
Environment=DLSS5NR_WINE=/home/you/.local/share/Steam/compatibilitytools.d/GE-Proton11-6-x86_64/files/bin/wine
Environment=DLSS5NR_PREFIX=/home/you/dlss5/prefix/pfx
Environment=DLSS5NR_DISPLAY=:0
```

These four set the node's widget defaults, so nobody has to edit `nodes.py`.
Restart ComfyUI - custom nodes are only scanned at startup.

### The display, and why Xvfb will not do

DXVK cannot create a Vulkan instance without an X display, and it divides by
the reported refresh rate when creating a swapchain. **Xvfb reports 0.00 Hz**,
so you get `EXCEPTION_INT_DIVIDE_BY_ZERO` inside `dxgi.dll` and exit code 148.
Writing EDID into the Wine registry does not help - winex11 overwrites it with
`BAD_EDID` at process start.

If you already have a desktop session, use it (`DLSS5NR_DISPLAY=:0`) and skip
this entirely. `doctor.sh` reports the refresh rate it finds.

For a headless machine you need Xorg with `xf86-video-dummy` and a real
Modeline. This is the one step needing root, and it is distro-specific:

```bash
# Debian/Ubuntu: xserver-xorg-core xserver-xorg-video-dummy x11-xserver-utils
# Arch:          xorg-server xf86-video-dummy xorg-xrandr
sudo cp install/xorg-dummy.conf /etc/X11/xorg-dummy.conf
sudo Xorg :99 -config /etc/X11/xorg-dummy.conf -noreset -nolisten tcp &
DISPLAY=:99 xrandr | grep '\*'      # must not say 0.00
```

ComfyUI must also see a valid `XAUTHORITY`. A service started from a desktop
session inherits one; a service started from a bare ssh shell does not.

## Usage

Run **DLSS 5 NR - Self Check (Wine)** first. It touches no GPU and confirms the
seven files and the environment in a couple of seconds.

Then **DLSS 5 Neural Rendering - Wine (Linux)**: `image` in, `image` + `report`
out. Wire `report` into a **Preview as Text** node - it ends with the line that
tells you whether feature 18 actually evaluated.

Scale is restricted to NVIDIA's fixed quality tiers:
`1.0x (DLAA) / 1.5x (Quality) / 1.724x (Balanced) / 2.0x (Performance) / 3.0x (Ultra Performance)`.
Input must have even dimensions.

| widget | note |
|---|---|
| `display` | Required. Without it DXVK cannot even create a Vulkan instance. |
| `warmup_frames` | Temporal warm-up hint; costs no extra frames. Clamped to the batch size. |
| `reset_each_frame` | On for batches of unrelated stills; **off for video**, or you lose temporal reuse. |
| `channel_order` | `auto` is fine. Decided once per batch, so colour cannot flicker mid-sequence. |

## Tuning

### Model preset - the carrier's network

Upstream's bridge never sets `DLSS.Hint.Render.Preset.*`, so the SR carrier
runs on whatever the driver defaults to. The bundled bridge is patched to honour
`DLSS5NR_MODEL_PRESET`, exposed as the `model_preset` widget.

Measured by [LQCCS](https://github.com/LQCCS/ComfyUI-DLSS5NR-Wine) on machine A
(RTX 5090, driver 590.48.01, 736x1280 source at 1.724x, `structure=2.0`,
high-frequency energy vs Lanczos):

| model_preset | HF energy | vs Lanczos |
|---|---:|---:|
| Default | 0.3158 | +1.9% |
| J (10) | 0.3159 | +2.0% |
| K (11) | 0.3158 | +1.9% (byte-identical to Default, so the default *is* K) |
| **L (12)** | 0.3540 | **+14.3%** |
| **M (13)** | 0.3546 | **+14.5%** |

> **This did not reproduce on machine B.** On driver 610.57.04 with NVIDIA's
> official SDK carrier, switching Default -> M changed 75.5% of pixels (so the
> setting is definitely being applied) but moved high-frequency energy by
> **-0.5%**, not +12 points. Candidate explanations, none isolated: a newer
> driver may have moved the default preset; the official `nvngx_dlss.dll`
> v310.7.0 is a different build from the one in Merserk's pack; and the source
> material differs. Treat the table as machine A's result, and judge preset
> choice on your own footage.

### Effect breakdown (machine A)

| configuration | HF energy | vs Lanczos |
|---|---:|---:|
| default preset, NR off (carrier only) | 0.3016 | -2.6% |
| default preset, structure 2.0 | 0.3158 | +1.9% |
| **M, NR off (carrier only)** | 0.3387 | **+9.3%** |
| M, structure 1.0 | 0.3422 | +10.5% |
| **M, structure 2.0** | 0.3546 | **+14.5%** |
| M, structure 3.0 (beyond documented range) | 0.3757 | +21.3% |
| default preset, structure 2.0, auto_mask on | 0.3086 | -0.4% |
| 1.0x, structure 2.0 (no carrier) | - | **-3.9% vs source** |

### Suggested starting points

| case | scale | model_preset | structure | auto_mask |
|---|---|---|---|---|
| **general** | 1.724x / 2.0x | M | 2.0 | off |
| sharper (undocumented range) | 1.724x / 2.0x | M | 3.0 - 4.0 | off |
| protect skin texture | 1.724x | M | 1.5 | on (costs most of the sharpness) |
| **avoid** | **1.0x** | - | - | - |

### Which parameters actually do anything

| parameter | effective? | note |
|---|---|---|
| `structure` | yes | The real sharpness lever. NVIDIA: Structure Intensity drives high-frequency detail (AO, contact shadows, reflections, subsurface scattering). |
| `model_preset` | yes | Applied and measurable; see the caveat above on how much it buys. |
| `tone` | yes | Low frequency: overall lighting and colour response. `0` keeps the source grade exactly. |
| `auto_mask` | yes | Protects skin from over-sharpening, and therefore **reduces** overall sharpness. |
| `skin` | conditional | Only has effect when `auto_mask` is on. `-1` means "follow structure", not "off". |
| `intensity` | **no** | Inert here. The NVIDIA SDK has no separate intensity control. |
| `preset` / `style` | **no** | Inert. The shipping DLL contains a single network. |

NVIDIA documents `structure` as 0-1; Merserk's UI allows 0-2; it keeps having
an effect past that but with no documentation behind it. Community consensus is
that maxing it produces an "AI slop look" with fake pores and wrinkles on faces.
**High-frequency energy is only a proxy for sharpness - confirm on real footage
by eye before shipping.**

## Troubleshooting

Run `install/doctor.sh` first; it explains most of these in place.

| symptom | cause |
|---|---|
| `BrokenPipeError`, host exits in under a second | Header rejected. Usually `warmup_frames > frame_count` on an old build - this fork clamps it. |
| `Value not in list` on a saved workflow | A dropdown label changed. This fork defers `scale` and `model_preset` validation to the node so old labels still resolve. |
| `EXCEPTION_INT_DIVIDE_BY_ZERO`, exit 148 | Display reports 0.00 Hz. Xvfb cannot be used; see the display section. |
| `D3D12CreateDevice failed 0x80004002` | `WINEDLLOVERRIDES` is missing `d3d12`/`d3d12core`. The node sets all four. |
| `LoadLibrary(dlss5nr_bridge.dll) failed: 126` | Prefix built with bare `wineboot`; it has Wine's builtin stubs, not DXVK. Rebuild via `proton run wineboot -u`. |
| `NvAPI_EnumPhysicalGPUs failed: -2`, then `0xBAD00001` | Same cause - `nvapi64.dll` was never loaded. |
| `0xBAD00001` on a good prefix | `FAIL_FeatureNotSupported`. Feature 18 needs Blackwell; on a Blackwell card it means the runtime build itself declined it. |
| NGX `NGXGetPathUsingQAI` errors in the report | Benign. QAI and registry probing always fail under Wine before the working path is found. |

## Known limitations

1. Motion vectors come from **OpenCV optical flow**, not real motion vectors.
   Temporal artifacts on real footage are possible; check by eye.
2. Blackwell only, and only tested on the RTX 5090 within that.
3. Input must have even dimensions.
4. Requires an X display even when headless.

## Credits and licence

- This repository's own code (`nodes.py`, `install/`, `docs/`): MIT, see [LICENSE](LICENSE).
- `dlss5nr_host.exe`, `dlss5nr_bridge.dll`, `nvngx.dll_comfy.dll`: cross-compiled
  with MinGW-w64 from the **project-owned** sources of
  [kos94ok/ComfyUI-DLSS5-NR-Linux](https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux)
  (MIT), and redistributable.
- `nvngx_dlss.dll` is fetched from NVIDIA's public
  [DLSS SDK](https://github.com/NVIDIA/DLSS) and is redistributable under its
  own licence.
- `nvngx_dlssnr.dll` and other NVIDIA NGX runtimes are **proprietary, not in
  this repository**, and are yours to obtain legally.

### Upstream

[**LQCCS/ComfyUI-DLSS5NR-Wine**](https://github.com/LQCCS/ComfyUI-DLSS5NR-Wine)
is the original and the place to look for the reasoning behind any of this. The
node design, the Wine transport work, the environment findings, and every
measurement labelled "machine A" above are theirs. Bug reports about the
*technique* belong there; this fork only owns its packaging and docs.

The binaries come in turn from
[kos94ok/ComfyUI-DLSS5-NR-Linux](https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux),
which owns the D3D12/NGX bridge itself.
