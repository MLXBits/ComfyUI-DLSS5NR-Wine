# -*- coding: utf-8 -*-
"""DLSS 5 Neural Rendering (feature 18) - Wine path - ComfyUI node

Why a separate node pack rather than patching ComfyUI-DonutNodes:
  DonutNodes' DLSS5 branch goes through the Merserk worker + ReShade +
  renodx-dlss5.addon64. That route always crashes under Wine: the add-on
  aborts silently right after feature 18 is created. First-hand evidence in
  NIGos/dlss5-bridge#22 (identical add-on build, sha256 D5ADF82E...; also
  Blackwell + vkd3d-proton; the reporter crashed the same way using the
  reference nvngx_dlssnr). That issue is still open - the maintainer has no
  Linux environment and cannot reproduce it.

  This node uses the host from kos94ok/ComfyUI-DLSS5-NR-Linux instead. It owns
  its own D3D12/NGX session and loads neither ReShade nor RenoDX, so that bug
  is not on the path. Measured 2026-09-04 on 49812079 (RTX 5090, driver
  590.48.01): 1.0x changes pixels (mean abs diff 28.049/255); 2x 480x736 ->
  960x1472; 1.5x -> 720x1104; neither of the host's CreateFeature(18) failed
  nor DLSSNR EvaluateFeature failed paths was hit. Throughput 8.72 fps at 2x,
  so 15s at 24fps takes about 41 seconds.

Protocol primitives are imported directly from the upstream
tools/dlss5nr_video.py rather than copy-pasted, so a protocol change upstream
carries over instead of silently drifting out of sync.
"""

from __future__ import annotations

import importlib.util
import os
import struct
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import torch

# 默认路径：装机脚本把仓库放这里。四项都可以用环境变量覆盖，这样同一份
# 节点不用改代码就能跑在别的机器上（在 ComfyUI 的 systemd unit 或启动脚本
# 里 export 即可）。
#
# 注意 GE-Proton 11 起 files/bin 里只有 wine（已是 64 位），不再有 wine64；
# 老的 GE-Proton 10 才需要显式指到 wine64。
DEFAULT_REPO = os.environ.get("DLSS5NR_ROOT", "/workspace/dlss5nr")
DEFAULT_WINE = os.environ.get(
    "DLSS5NR_WINE", "/workspace/dlss5/GE-Proton10-34/files/bin/wine64")
DEFAULT_PFX = os.environ.get("DLSS5NR_PREFIX", "/workspace/dlss5/prefix/pfx")
DEFAULT_DISPLAY = os.environ.get("DLSS5NR_DISPLAY", ":99")

# 档位 -> (倍率, PerfQualityValue)。取自上游 _PERF_QUALITY，写死在这里只是为了
# 让下拉框有稳定顺序；真正的值仍从上游模块校验。
SCALE_CHOICES = {
    # ⚠️ 1.0x 没有 carrier，feature 18 直接作用在已经清晰的原片上会把它磨软
    #    （实测 -3.9%）。feature 18 是给「放大后变软的画面」补结构的，别用 1.0x 求清晰。
    "1.0x (DLAA / native - softens, not for sharpening)": 1.0,
    "1.5x (Quality)": 1.5,
    "1.724x (Balanced)": 1.724,
    "2.0x (Performance)": 2.0,
    "3.0x (Ultra Performance)": 3.0,
}
STYLE_CHOICES = {"Default": 0, "Natural": 1, "Cinematic": 2}

# DLSS 模型预设（SR carrier 用的，不是 NR 的 preset）。SDK 值 J=10 K=11 L=12 M=13。
# 🔴 这是清晰度最大的一个杠杆，而上游 bridge 从来没设过 —— 它一直跑在驱动默认档上。
#    实测（RTX 5090，H3 出片 736x1280 -> 1.724x，structure=2.0，高频能量 vs Lanczos）：
#        默认  +1.9%      J  +2.0%      K  +1.9%（与默认逐字节相同 => 默认就是 K）
#        L    +14.3%      M +14.5%
#    与社区口径一致：50 系默认 Quality/Balanced=K、Performance=M；K 偏软、M 最锐。
#    实现方式：给 nvngx_dlss.dll 的六个 DLSS.Hint.Render.Preset.* 键统一写入该值
#    （键名从 DLL 字符串表确认），做法沿用 Merserk v4.0 changelog 的描述。
#    需要打过补丁的 dlss5nr_bridge.dll（读环境变量 DLSS5NR_MODEL_PRESET）。
MODEL_PRESETS = {"Default (driver default - measured as K)": 0, "J (10)": 10, "K (11)": 11,
                 "L (12) sharp": 12, "M (13) sharpest": 13}

# 🔴 哪些参数真的起作用（2026-09-04 实测 + 一手来源）
#
#   structure  ✅ 唯一真正的清晰度杠杆。NVIDIA 官方：Structure Intensity 管高频细节
#                （环境光遮蔽/接触阴影/反射/次表面散射），SDK 范围 0-1。
#                Merserk UI 放到 0-2。实测 3.0 -> 高频能量 +9.7%、4.0 -> +12.8%，
#                越过文档上限仍在起效，但已无任何文档背书，自担风险。
#   tone       ✅ 低频：整体光照与色彩响应。设 0 = 完全保留原片配色。效果比 structure 小。
#   skin       ⚠️ 只有 auto_mask=True 时才有效果（实测：mask 关时改它输出逐字节不变）。
#                -1 的语义是「跟随 structure」，不是关闭。
#   auto_mask  ✅ 自动识别皮肤区域并保护它不被过度锐化 —— 因此会**降低**整体锐度。
#                人脸特写建议开，产品/环境建议关。
#   intensity  ❌ 本路径无效。NVIDIA SDK 里根本没有独立的 intensity（只有 Structure/Tone
#                两个滑块）；RenoDX 的 NRIntensity 应是它自己混合层的乘数，而这条 bridge
#                直接调 DLSSNR 快照、没有那层混合。实测改它输出逐字节不变。
#   preset     ❌ inert。出厂 DLL 只含单一网络，没有可切换的对象。
#   style      ❌ inert，同上。
#
# 依据：NVIDIA DLSS 5 新闻稿 / TechPowerUp 技术预览（Structure & Tone 语义、0-1 范围、
#      「环境拉满、人脸保守」的工作室共识）；Merserk README 控制表（0-2 范围与默认值）；
#      nexusmods/site/mods/2224（skin=-1 跟随 structure、preset/style 可能无效）；
#      ThunderRuler/dlss5-installer-skill config-reference.md（从 addon 二进制提取的键表，
#      明确写 NRPreset/NRStyle "Currently inert"）；以及本机 RTX 5090 上的高频能量实测。
#
# ⚠️ 高频能量只是锐度的代理指标，不等于「好看」。社区共识是拉满 = "AI slop look"，
#    人脸过锐会出假毛孔假皱纹。上生产前必须用眼睛在真实素材上确认。


class Dlss5NRWineError(RuntimeError):
    pass


def _resolve_choice(value, table, what):
    """Look a widget value up in its choice table, falling back to the leading token.

    Saved workflows store the choice *label*, so any relabelling - localising
    these from Chinese to English included - would orphan existing graphs. The
    leading token ("1.0x", "M", "Default") is the stable part, so match on that
    when the full label misses.

    This matters most for model_preset, which previously went through
    ``MODEL_PRESETS.get(value, 0)``: an unrecognised label silently resolved to
    the driver default, quietly disabling the single biggest sharpness lever
    with no error anywhere.
    """
    if value in table:
        return table[value]
    head = str(value).split(" ")[0]
    for key, resolved in table.items():
        if key.split(" ")[0] == head:
            return resolved
    raise Dlss5NRWineError(
        "Unknown %s %r; expected one of: %s" % (what, value, list(table)))


def _load_upstream(repo: Path):
    """Load the upstream frontend as a module for its protocol constants and temporal guide."""
    tool = repo / "tools" / "dlss5nr_video.py"
    if not tool.is_file():
        raise Dlss5NRWineError(
            "Not found: %s. Run git clone https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux %s first"
            % (tool, repo)
        )
    spec = importlib.util.spec_from_file_location("_dlss5nr_video", tool)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_dlss5nr_video"] = mod
    spec.loader.exec_module(mod)
    return mod


def _even(v: float) -> int:
    return max(2, int(round(v / 2.0) * 2))


def _host_highlights(log, keep=8):
    """Pick the diagnostically useful lines out of the host's stderr.

    NGX floods stderr with dozens of CollectGarbage / Releasing-resource lines
    at the end of every session, so a plain log[-6:] shows nothing but teardown
    noise. What you actually want - the host banner, the Init_ProjectID result,
    carrier state, the forced model preset, any failure - is all near the top.
    """
    noise = ("CollectGarbage", "Releasing resource", "Delayed destroy")
    body = [l for l in log if not any(n in l for n in noise)]
    # The bridge's own [dlss5nr] lines and the host banner are the ones that
    # answer "did feature 18 actually run"; NGX's own errors are mostly the
    # benign QAI/registry probing it always does under Wine, so they rank
    # below and only top up whatever room is left.
    primary = [l for l in body if "[dlss5nr]" in l or "host ready" in l]
    secondary = [l for l in body
                 if l not in primary
                 and ("fail" in l.lower() or "error" in l.lower())]
    if primary:
        # Never let NGX noise consume the budget: top up with secondary only
        # once every bridge line fits, so the trailing EvaluateFeature result
        # is always the last thing shown.
        room = keep - len(primary)
        picked = primary + (secondary[:room] if room > 0 else [])
    else:
        picked = secondary or body
    if len(picked) <= keep:
        return picked
    return picked[:keep - 2] + ["..."] + picked[-2:]


def _drain(stream, sink, cap=400):
    def run():
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                sink.append(line)
                del sink[:-cap]
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


class DLSS5NRWineUpscale:
    """IMAGE batch -> DLSS 5 Neural Rendering -> IMAGE batch. One persistent NGX session per batch."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "scale": (list(SCALE_CHOICES), {"default": "1.724x (Balanced)",
                          "tooltip": "Sharpening requires >1x. The 1.0x tier has no carrier and only softens the image."}),
                "repo_dir": ("STRING", {"default": DEFAULT_REPO, "multiline": False}),
                "wine": ("STRING", {"default": DEFAULT_WINE, "multiline": False}),
                "prefix": ("STRING", {"default": DEFAULT_PFX, "multiline": False}),
                "display": ("STRING", {"default": DEFAULT_DISPLAY, "multiline": False,
                                       "tooltip": "Required. Without DISPLAY, DXVK cannot even create a Vulkan instance (measured)."}),
                "warmup_frames": ("INT", {"default": 8, "min": 0, "max": 600}),
                "style": (list(STYLE_CHOICES), {"tooltip": "INERT: the shipping DLL contains a single network, so there is nothing to switch."}),
                "preset": ("INT", {"default": 0, "min": 0, "max": 7,
                                   "tooltip": "INERT, same as style."}),
                "intensity": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.05,
                                        "tooltip": "INERT on this path (measured: changing it leaves output byte-identical). The NVIDIA SDK has no separate intensity control."}),
                "tone": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.05,
                                   "tooltip": "Low frequency: overall lighting and colour response. 0 = keep the source grade exactly."}),
                "structure": ("FLOAT", {"default": 2.0, "min": 0.0, "max": 4.0, "step": 0.05,
                                        "tooltip": "The only real sharpness lever. NVIDIA SDK documents 0-1, Merserk's UI allows 0-2; measured 3.0=+9.7% and 4.0=+12.8%, still effective but beyond any documentation. Faces 1.5-2.0, products/environments 2.0-3.0."}),
                "skin": ("FLOAT", {"default": -1.0, "min": -1.0, "max": 2.0, "step": 0.05,
                                   "tooltip": "-1 = follow structure. Only has any effect when auto_mask is on."}),
                "auto_mask": ("BOOLEAN", {"default": False,
                                          "tooltip": "Turn this off if you want sharpness. Measured: enabling it pushed +1.9% back to -0.4% against Lanczos. The more skin fills the frame, the more it suppresses."}),
                "reset_each_frame": ("BOOLEAN", {"default": False,
                                                 "tooltip": "Disables temporal reuse. Use for batches of unrelated stills; leave off for video."}),
                "channel_order": (["auto", "RGBA", "BGRA"],),
                "gpu_index": ("INT", {"default": 0, "min": 0, "max": 7}),
                # 🔴 新参数一律加在**最后**，绝不插中间。
                #    ComfyUI 的 widgets_values 是**按位置**读的：在中间插一个，
                #    所有老工作流会整体错位一格 —— 2026-09-04 实际发生过，报了 6 个错
                #    （model_preset 缺少连接 / warmup_frames、preset、gpu_index 类型错误 /
                #     style、channel_order 值不适用）。加在末尾则老工作流缺最后一个值，
                #    ComfyUI 自动取 default，完全兼容。
                "model_preset": (list(MODEL_PRESETS), {"default": "M (13) sharpest",
                                  "tooltip": "Biggest sharpness lever. Measured: L/M scored 12 points above the default (K). Requires a patched bridge; without the patch this widget does nothing."}),
            }
        }

    @classmethod
    def VALIDATE_INPUTS(cls, scale, model_preset):
        """Accept choice labels from workflows saved before a relabelling.

        ComfyUI validates COMBO widgets against the *current* option list and
        rejects the whole prompt with "Value not in list", so _resolve_choice()
        would never be reached on a saved graph carrying an old label. Naming
        these two inputs here defers their validation to us; _resolve_choice()
        then matches on the stable leading token, and a genuinely unknown value
        still comes back as a readable validation error rather than silently
        resolving to a default.
        """
        for value, table, what in ((scale, SCALE_CHOICES, "scale"),
                                   (model_preset, MODEL_PRESETS, "model_preset")):
            try:
                _resolve_choice(value, table, what)
            except Dlss5NRWineError as exc:
                return str(exc)
        return True

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "report")
    FUNCTION = "upscale"
    CATEGORY = "image/DLSS 5 NR (Wine)"

    def upscale(self, image, scale, repo_dir, wine, prefix, display, warmup_frames,
                style, preset, intensity, tone, structure, skin, auto_mask,
                reset_each_frame, channel_order, gpu_index,
                model_preset="M (13) sharpest"):
        repo = Path(repo_dir).expanduser().resolve()
        V = _load_upstream(repo)

        host = repo / "native" / "bin" / "dlss5nr_host.exe"
        runtime = repo / "runtime"
        shim = runtime / "caller" / "nvngx.dll_comfy.dll"
        for p, what in ((host, "dlss5nr_host.exe (build it with native/build_host_mingw.sh)"),
                        (runtime / "nvngx_dlssnr.dll", "nvngx_dlssnr.dll (supply your own)"),
                        (shim, "caller shim nvngx.dll_comfy.dll")):
            if not p.is_file():
                raise Dlss5NRWineError("Missing %s: %s" % (what, p))
        bridge = repo / "native" / "bin" / "dlss5nr_bridge.dll"
        if not bridge.is_file():
            raise Dlss5NRWineError("Missing dlss5nr_bridge.dll: %s" % bridge)

        factor = _resolve_choice(scale, SCALE_CHOICES, "scale")
        img = image.detach().cpu().float().clamp(0.0, 1.0).numpy()
        if img.ndim != 4 or img.shape[-1] < 3:
            raise Dlss5NRWineError("Bad IMAGE shape: %s" % (tuple(img.shape),))
        img = np.ascontiguousarray(img[..., :3])
        n, in_h, in_w = img.shape[0], img.shape[1], img.shape[2]
        if in_w % 2 or in_h % 2:
            raise Dlss5NRWineError(
                "Input must have even dimensions, got %dx%d. H3 output is even; if you cropped in between, align to a multiple of 2."
                % (in_w, in_h))
        out_w, out_h = (_even(in_w * factor), _even(in_h * factor)) if factor != 1.0 else (in_w, in_h)
        # 让上游自己校验倍率合法性，不自己另起一套判断
        perf_quality = V._perf_quality_for_size(in_w, in_h, out_w, out_h)
        if factor > 1.0 and not (runtime / "nvngx_dlss.dll").is_file():
            raise Dlss5NRWineError(
                "Factors above 1x need the ordinary DLSS carrier: put nvngx_dlss.dll in %s" % runtime)

        env = os.environ.copy()
        env["DISPLAY"] = display
        env.setdefault("XDG_RUNTIME_DIR", "/tmp/xdg")
        env["WINEPREFIX"] = str(Path(prefix).expanduser().resolve())
        env.setdefault("DLSS5NR_DISABLE_OTHER_SINKS", "1")
        env["DLSS5NR_GPU_INDEX"] = str(int(gpu_index))
        env.setdefault("DXVK_ENABLE_NVAPI", "1")
        env.setdefault("DXVK_NVAPI_DRS_NGX_DLSS_NR_OVERRIDE", "on")
        # 只用 builtin(=n) 会让 NGX 起来但 NvAPI 看不到物理显卡 —— 上游注释与我们实测一致
        env.setdefault("WINEDLLOVERRIDES", "d3d12,d3d12core,nvapi64,dxgi=n,b")
        env.setdefault("WINEDEBUG", "-all")
        mp = _resolve_choice(model_preset, MODEL_PRESETS, "model_preset")
        if mp:
            env["DLSS5NR_MODEL_PRESET"] = str(mp)
        else:
            env.pop("DLSS5NR_MODEL_PRESET", None)
        os.makedirs(env["XDG_RUNTIME_DIR"], exist_ok=True)

        log: list[str] = []
        proc = subprocess.Popen([str(Path(wine).expanduser()), str(host), str(runtime)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, start_new_session=True)
        _drain(proc.stderr, log)

        def fail(msg):
            try:
                proc.kill()
            except Exception:
                pass
            tail = "\n".join(log[-25:])
            raise Dlss5NRWineError("%s\n---- host stderr ----\n%s" % (msg, tail))

        try:
            # host 校验 warmup_frames > frame_count 就直接拒收 DNR2 头（退出码 5），
            # 前端拿到的是一句没有上下文的 BrokenPipeError。批量小于 warmup 时
            # （单张图最常见：默认 warmup=8 而 n=1）必须先夹住。
            proc.stdin.write(V.HEADER.pack(
                V.MAGIC_DNR2, in_w, in_h, out_w, out_h,
                min(int(warmup_frames), int(n)), int(n), int(perf_quality),
                0,                              # profile
                int(preset), int(STYLE_CHOICES[style]),
                1 if auto_mask else 0,
                0,                              # ui correction
                float(intensity), float(tone), float(structure), float(skin)))
            proc.stdin.flush()

            guides = V.TemporalGuideGenerator(in_w, in_h)
            motion_bytes = in_w * in_h * 2 * 2
            float_bytes = out_w * out_h * 3 * 4
            chosen = None if channel_order == "auto" else channel_order
            out = np.empty((n, out_h, out_w, 3), dtype=np.float32)

            for i in range(n):
                src = np.ascontiguousarray(img[i])
                u8 = np.ascontiguousarray(np.rint(src * 255.0).astype(np.uint8))
                motion, guide_reset = guides.process(u8)
                if motion.nbytes != motion_bytes:
                    fail("Frame %d: motion vectors are %d bytes, expected %d" % (i, motion.nbytes, motion_bytes))
                reset = bool(reset_each_frame or guide_reset)
                proc.stdin.write(V.FRAME_HEADER.pack(V.MAGIC_FRM2, i, 1 if reset else 0))
                proc.stdin.write(src.tobytes(order="C"))
                proc.stdin.write(motion.tobytes(order="C"))
                proc.stdin.flush()

                try:
                    magic, idx, ok, count = V.REPLY_HEADER.unpack(
                        V._read_exact(proc.stdout, V.REPLY_HEADER.size))
                except Exception as e:
                    fail("Frame %d: failed to read the reply: %s" % (i, e))
                if magic != V.MAGIC_OUT1 or idx != i:
                    fail("Frame %d: desynchronised reply: magic=%r index=%d" % (i, magic, idx))
                if not ok:
                    try:
                        ln = struct.unpack("<I", V._read_exact(proc.stdout, 4))[0]
                        detail = V._read_exact(proc.stdout, min(ln, 65535)).decode("utf-8", "replace")
                    except Exception:
                        detail = "(no detail available)"
                    fail("Frame %d: host reported an error: %s" % (i, detail))
                if count != out_w * out_h * 3:
                    fail("Frame %d: host returned %d floats, expected %d" % (i, count, out_w * out_h * 3))
                raw = V._read_exact(proc.stdout, float_bytes)
                frame = np.frombuffer(raw, dtype=np.float32).reshape((out_h, out_w, 3))
                corrected, order = V._channel_choice(frame, src, chosen or "auto")
                if chosen is None:
                    chosen = order
                out[i] = np.clip(corrected, 0.0, 1.0)

            proc.stdin.close()
            proc.stdin = None
            done = V._read_exact(proc.stdout, 4)
            if done != V.MAGIC_END1:
                fail("Host did not send END1 (received %r)" % (done,))
        finally:
            if proc.poll() is None:
                # 参考 310.8 运行时在发完 END1 之后可能卡在自己的进程析构里；
                # END1 才是协议完成标志，不拿它的 shutdown 当成败判据。
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        report = "\n".join([
            "DLSS 5 Neural Rendering (feature 18) - Wine path",
            "  %d frames   %dx%d -> %dx%d   %s   PerfQualityValue=%d" % (
                n, in_w, in_h, out_w, out_h, scale, perf_quality),
            "  channel order %s   warmup=%d   reset_each_frame=%s   auto_mask=%s" % (
                chosen, warmup_frames, reset_each_frame, auto_mask),
            "  DLSS model preset %s%s" % (model_preset,
                # Upstream hard-coded "try L or M, 12 points sharper" here. That
                # holds for the carrier they measured on and not in general: the
                # preset letters index networks inside nvngx_dlss.dll, so both
                # the size and the sign of the effect vary by carrier build.
                "" if mp else "   <- L/M worth an A/B; the effect is carrier-specific"),
            "  active params structure=%.2f tone=%.2f%s   (intensity/preset/style are inert here)" % (
                structure, tone,
                ("  skin=%.2f" % skin) if auto_mask else "  skin inactive (auto_mask=off)"),
            "  host=%s" % host,
            "  runtime nvngx_dlssnr.dll=%d B%s" % (
                (runtime / "nvngx_dlssnr.dll").stat().st_size,
                "   carrier nvngx_dlss.dll=%d B" % (runtime / "nvngx_dlss.dll").stat().st_size
                if (runtime / "nvngx_dlss.dll").is_file() else ""),
            "  host highlights:",
        ] + ["    " + l for l in _host_highlights(log)])
        return (torch.from_numpy(out), report)


class DLSS5NRWineStatus:
    """Run this first: touches no GPU, only checks that files and environment are present."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "repo_dir": ("STRING", {"default": DEFAULT_REPO, "multiline": False}),
            "wine": ("STRING", {"default": DEFAULT_WINE, "multiline": False}),
            "prefix": ("STRING", {"default": DEFAULT_PFX, "multiline": False}),
            "display": ("STRING", {"default": DEFAULT_DISPLAY, "multiline": False}),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("report",)
    FUNCTION = "inspect"
    CATEGORY = "image/DLSS 5 NR (Wine)"

    def inspect(self, repo_dir, wine, prefix, display):
        repo = Path(repo_dir).expanduser().resolve()
        lines = ["DLSS 5 NR (Wine) self check", "  repo: %s" % repo]
        items = [
            ("host",    repo / "native" / "bin" / "dlss5nr_host.exe"),
            ("bridge",  repo / "native" / "bin" / "dlss5nr_bridge.dll"),
            ("shim",    repo / "runtime" / "caller" / "nvngx.dll_comfy.dll"),
            ("dlssnr",  repo / "runtime" / "nvngx_dlssnr.dll"),
            ("dlss SR", repo / "runtime" / "nvngx_dlss.dll"),
            ("wine",    Path(wine)),
            ("prefix",  Path(prefix)),
        ]
        for name, p in items:
            if p.exists():
                sz = p.stat().st_size if p.is_file() else 0
                lines.append("  ✓ %-8s %s%s" % (name, p, ("  %d B" % sz) if sz else ""))
            else:
                lines.append("  x %-8s missing: %s" % (name, p))
        lines.append("  DISPLAY=%s (in-process %s)" % (display, os.environ.get("DISPLAY", "<unset>")))
        try:
            import cv2  # noqa: F401
            lines.append("  + cv2 %s (optical-flow motion vectors available)" % cv2.__version__)
        except Exception:
            lines.append("  ! no cv2 - motion vectors fall back to zero and history resets every frame; video loses all temporal benefit")
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
                                "--format=csv,noheader"], capture_output=True, text=True, timeout=20)
            lines.append("  GPU: %s" % (r.stdout.strip() or "unavailable"))
        except Exception as e:
            lines.append("  GPU: unavailable (%s)" % e)
        return ("\n".join(lines),)


NODE_CLASS_MAPPINGS = {
    "DLSS5NRWineUpscale": DLSS5NRWineUpscale,
    "DLSS5NRWineStatus": DLSS5NRWineStatus,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DLSS5NRWineUpscale": "DLSS 5 Neural Rendering - Wine (Linux)",
    "DLSS5NRWineStatus": "DLSS 5 NR - Self Check (Wine)",
}
