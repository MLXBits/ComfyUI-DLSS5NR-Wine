# Replacing `dlssg-worker.exe` with our own host

Scope for `native/dlssg_host.cpp`: a project-owned worker that drives NGX
feature 11 (Frame Generation) and speaks the wire protocol `dlssg.py` already
implements, so it drops in without touching the Python side.

**Why:** `dlssg-worker.exe` is the last binary in this stack that we neither
build nor can obtain from NVIDIA. No source is published for it anywhere, it is
unsigned, and the node executes it under Wine. Everything else — host, bridge,
caller shim — we cross-compile ourselves.

**Verdict: tractable.** The hard parts are already solved in this repo, and the
API is fully documented by NVIDIA. The risk is not the plumbing; it is that
DLSS-FG is built for a renderer that has depth and camera matrices, and video
has neither.

---

## 1. What the reference worker actually is

From the binary (`sha256 8a747f9e…`, 66,560 bytes):

| | |
|---|---|
| `.text` | 41,886 bytes — a thin harness, not an engine |
| imports | `d3d12.dll`, `dxgi.dll`, `ADVAPI32`, `KERNEL32`, `USER32`, MSVC CRT |
| NGX | statically linked; build paths leak `NGX/core/rel_310_7/source/lib/nvsdk_ngx_lib_windows.cpp` |
| CLI | `dlssg-worker.exe --probe|--serve [--adapter-luid <16hex>]` |
| adapter | `D3DKMTEnumAdapters2` / `QueryAdapterInfo`, picks an NVIDIA D3D12 adapter |

Its entire NGX surface:

```
NVSDK_NGX_D3D12_Init_with_ProjectID / Init_Ext
NVSDK_NGX_D3D12_GetCapabilityParameters / GetFeatureRequirements
NVSDK_NGX_D3D12_AllocateParameters / GetParameters / DestroyParameters
NVSDK_NGX_D3D12_CreateFeature / EvaluateFeature / ReleaseFeature
NVSDK_NGX_D3D12_Shutdown / Shutdown1
```

No Streamline, no swapchain, no Reflex. It is offline batch evaluation.

## 2. What we already own

`dlss5nr_bridge.dll` calls eight of those ten entry points today, for feature
18, cross-compiled with MinGW and running under Wine.

| problem | status |
|---|---|
| NGX under MinGW (NVIDIA ships MSVC-only `nvsdk_ngx_s.lib`) | **solved** — the bridge loads `_nvngx.dll` dynamically and resolves `NVSDK_NGX_D3D12_*` by name, with a DriverStore search and an explicit-override path |
| D3D12 device/queue/command list under vkd3d-proton | **solved** — same session shape |
| Upload heaps, readback, resource barriers | **solved** — reusable |
| String-keyed parameter maps (`DLSSNR.*`) | **solved** — `DLSSG.*` is the same mechanism |
| Init_ProjectID, capability params, feature lifecycle | **solved** |
| Wire protocol to Python over pipes | **solved** — `dlss5nr_host.cpp` already does this shape |

The genuinely new work is the parameter vocabulary (96 `DLSSG.*` keys vs ~15
`DLSSNR.*`) and a per-generated-frame evaluate loop.

## 3. The wire protocol (fixed — `dlssg.py` is the client)

Little-endian. Magics are ASCII tags read as `<I`.

```
SETUP      "<5I"    0x31534746 'FGS1'  width height frame_count generated_count
SETUP_OUT  "<4I"    0x31524746 'FGR1'  status multi_frame_count_max _pad
FRAME      "<4I2q"  0x31464746 'FGF1'  index reset _pad ts_numerator ts_denominator
             + width*height*4 bytes RGBA8 colour
             + width*height*2 bytes fp16 (RG) motion vectors
FRAME_OUT  "<4I"    0x314F4746 'FGO1'  status generated disabled
             + generated * width*height*4 bytes RGBA8, only if disabled == 0
```

Rules the client depends on:

- `status != 0` anywhere is fatal; the client raises and shows stderr.
- `SETUP_OUT.multi_frame_count_max` is reported back before any frame; the
  client refuses `generated_count > max` itself, but the worker must also
  enforce it (`Requested %u generated frames exceeds reported MultiFrameCountMax %u`).
- `disabled == 1` means the runtime declined to interpolate this interval —
  the client returns zero frames and keeps the real frame. Maps to
  `DLSSG.OutputDisableInterpolation`.
- Frame 0 is sent with `reset=1` and produces no generated frames.
- stderr is free-form log text, drained on a thread, surfaced in the node's
  `report`. Keep the `[dlssg]` prefix convention so `_host_highlights()` ranks
  our lines above NGX noise.

`--probe` prints one JSON line and exits:

```json
{"available":true,"multi_frame_count_max":3,"runtime_version":"310.7.0.0","worker_version":"1","detail":"..."}
```

Nothing in this repo consumes `--probe` yet. Worth keeping — it is what a
doctor check should call instead of guessing from file presence.

## 4. NGX sequence

Per NVIDIA's *DLSS Frame Generation SDK 310.7.0 Programming Guide*, §5–6.
`NVSDK_NGX_Feature_FrameGeneration = 11`.

**Create** (once per session, needs an open command list):

```
NVSDK_NGX_DLSSG_Create_Params:
  Width, Height                 output/backbuffer resolution
  NativeBackbufferFormat        DXGI_FORMAT_R8G8B8A8_UNORM for our RGBA8
  RenderWidth, RenderHeight     = Width/Height (no upscaler in this path)
  DynamicResolutionScaling      false
```

Recreate only on resolution change — not applicable, one session per batch.

**Evaluate** — one call per generated frame, `multiFrameIndex` running 1..n.
For an n× multiplier you call it n−1 times per real frame, which is exactly
the `generated_count` the client sends and why 2× yields 2n−1 frames overall.

Resource states matter: inputs `NON_PIXEL_SHADER_RESOURCE`, outputs
`UNORDERED_ACCESS`.

## 5. Where every parameter comes from

96 keys, four groups.

### From the client (5)

| key | source |
|---|---|
| `Backbuffer` | RGBA8 frame, uploaded per frame |
| `MVecs` | fp16 RG field, uploaded per frame |
| `Reset` | `FRAME.reset` — set on frame 0 and at scene cuts |
| `MultiFrameCount` | `SETUP.generated_count` |
| `MultiFrameIndex` | 1..count, the evaluate loop counter |

### Ours to allocate (2)

`OutputInterpolated` (read back, one per index) and `OutputReal` (optional,
debug-only — omit).

### Synthesized, because video has none of this (24)

This is the interesting group. The client sends **no depth and no camera**, so
the host must invent both. The escape hatches exist:

| key | proposed value | rationale |
|---|---|---|
| `Depth` | flat constant buffer | required input; nothing better available |
| `DepthInverted` | 0 | irrelevant when depth is constant |
| `OrthoProjection` | 1 | documented: "orthographic projection is used by the camera" — the honest description of footage with no perspective information |
| `CameraMotionIncluded` | 1 | optical flow already contains all motion, camera and object alike |
| `ClipToPrevClip`, `PrevClipToClip`, `CameraViewToClip`, `ClipToCameraView`, `ClipToLensClip` | identity | no inter-frame camera transform is known |
| `CameraPos/Up/Right/Fwd` (12 keys) | origin + unit basis | must be *consistent*, values otherwise arbitrary |
| `CameraNear/Far` | 0.1 / 1000 | plausible, unused under ortho |
| `CameraFOV`, `CameraAspectRatio` | π/2, width/height | |
| `JitterOffsetX/Y` | 0 | no TAA jitter in video |
| `CameraPinholeOffsetX/Y` | 0 | |

### Constants and geometry (35)

`InternalWidth/Height`, `BackbufferFormat`, `DynamicResolution=0`,
`ColorBuffersHDR=0`, `MinRelativeLinearDepthObjectSeparation=40.0` (the
documented default), and every `*SubrectBaseX/Y` = 0 with `*SubrectWidth/Height`
= full size. Subrects are mechanical; get them uniformly right once.

Motion vector conventions — **the highest-risk constants**:
`MvecScaleX/Y`, `MvecDilated=0`, `MvecInvalidValue`.

### Deliberately not provided (30)

`HUDLess`, `UI`, `UIAlpha`, `BidirectionalDistortionField` and their subrect
and precision keys. Video has no HUD and no UI layer. The guide calls HUDLess
and UI "strongly recommended" for games; for film content the backbuffer *is*
the hudless image, so the correct action is to omit them, not to fake them.

## 6. The three real unknowns

Everything above is mechanical. These are not, and they should be settled
empirically **before** committing to the full build.

1. **`mvecScale` semantics — the guide contradicts itself.** §6 says the
   scale rescales "to the required pixel-units", with common values of 1 (already
   in pixels) or the resource's width/height (normalized input). The parameter
   reference on p125 instead says it normalizes "so the values are in [-1,1]
   range". Those are reciprocal. Guess wrong and the output is plausible but
   wrong — smeared or under-warped — with no error anywhere. `dlssg.py` produces
   flow in **pixels** at full resolution, so the candidates are `1.0` and
   `1/width, 1/height`.

2. **`ResourceAlwaysProvidedFlags` / `ResourceNeverProvidedFlags`.** Present in
   the reference worker's string table, absent from the entire programming
   guide. These look like the mechanism for declaring that HUDLess/UI will never
   arrive, which may be what keeps the runtime from waiting on or synthesizing
   them. Undocumented, and likely load-bearing for our use case.

3. **Whether flat depth is acceptable, and what `OrthoProjection` costs.**
   Depth is a *required* input. A constant buffer is legal but tells the
   algorithm every pixel is coplanar, which disables its occlusion reasoning.
   The reference worker faces the identical constraint, so whatever it does is
   an existence proof — but not necessarily the best answer.

**We have an oracle.** The reference worker exists and works. Same frames
through both, compare output pixel-for-pixel. That converts all three from
research questions into a bounded search: sweep `mvecScale`, `OrthoProjection`,
and depth strategies until our output matches, or until we understand why it
differs. This is the same method that settled the carrier-version question.

## 7. Build integration

Add to `install/build_native.sh` beside the existing three artifacts:

```sh
"$CXX" $FLAGS -municode "$ROOT/native/dlssg_host.cpp" \
    -o "$ROOT/native/bin/dlssg_host.exe" $LIBS
```

Then `dlssg_dir` defaults to `native/bin/`, `setup.sh` section 7 drops the
`dlssg-worker.exe` download entirely, and only `nvngx_dlssg.dll` — an NVIDIA
file, fetched from NVIDIA — remains. `NOTICE-frame-interpolation.md` loses its
"what is NOT redistributed" compromise.

Headers: we do **not** need NVIDIA's `nvsdk_ngx*.h`. The bridge already
hand-declares the small ABI it uses and resolves entry points by name, and the
parameter interface is string-keyed, so the helper structs
(`NVSDK_NGX_DLSSG_Create_Params`, `_Eval_Params`, `_Opt_Eval_Params`) are sugar
over `SetI`/`SetF`/`SetVoidPointer` calls we can make directly. The guide
documents every field; §5–6 and the p124–126 reference are the spec.

## 8. Effort

| | |
|---|---|
| Protocol + D3D12 session + upload/readback, ported from `dlss5nr_host.cpp` | half a day |
| Parameter map, all 96 keys | half a day |
| First `EvaluateFeature` success, frames returning | ~1–2 days total |
| Matching the reference worker's quality | open-ended — the three unknowns |

The first milestone is cheap and decisive. If feature 11 creates and evaluates
under Wine with our own host, the rest is tuning against an oracle we already
have. If it does not, we learn that early and have lost two days.

### Status

`native/dlssg_host.cpp` is the probe, written and wired into
`install/build_native.sh` (step 3.5, output `<root>/dlssg/dlssg_host.exe`).
**It has never been compiled** — there is no MinGW cross-compiler on the
machine it was written on, and it has to run on the box with the GPU and the
Wine prefix regardless. First run:

```bash
git pull && install/build_native.sh          # builds it alongside the other three
wine <root>/dlssg/dlssg_host.exe --probe     # or: DLSS5NR_DLSSG_DIR=... wine ...
```

Expect to fix compile errors on that first pass. What the probe reports:

```json
{"available":true,"multi_frame_count_max":3,"worker_version":"probe-1",
 "gpu":"NVIDIA GeForce RTX 5090","detail":"feature 11 available on ..."}
```

with the NGX conversation on stderr — `Init_ProjectID -> 0x…`,
`FrameGeneration.Available -> …`, and on failure the
`FrameGeneration.FeatureInitResult` code, which is the single most useful
number when the answer is no.

The reference worker's own `--probe` is the control: run both, compare. If it
says available and ours does not, the difference is in our init, not in the
stack.

**Recommended first step:** a probe-only build — Init, `GetCapabilityParameters`,
feature-11 support check, `--probe` JSON, exit. It touches no frames, reuses the
bridge's NGX loader verbatim, and answers the only question that could sink the
whole plan: does feature 11 initialize at all on this Wine/vkd3d-proton stack?
