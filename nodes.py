# -*- coding: utf-8 -*-
"""DLSS 5 Neural Rendering（feature 18）· Wine 路径 · ComfyUI 节点

为什么另起一个节点包，而不是修 ComfyUI-DonutNodes：
  DonutNodes 的 DLSS5 分支走 Merserk worker + ReShade + renodx-dlss5.addon64。
  那条路在 Wine 下必崩：feature 18 create 之后 addon 静默 abort。
  一手依据 NIGos/dlss5-bridge#22（同一个 addon 构建 sha256 D5ADF82E…，
  同为 Blackwell + vkd3d-proton，对方用参考版 nvngx_dlssnr 也一样崩），
  issue 至今 open，维护者没有 Linux 环境无法复现。

  本节点走 kos94ok/ComfyUI-DLSS5-NR-Linux 的 host：它自己持有 D3D12/NGX 会话，
  **完全不加载 ReShade / RenoDX**，所以那个 bug 不在路径上。2026-09-04 在
  49812079（RTX 5090 / 驱动 590.48.01）实测：1.0x 改变像素（平均绝对差
  28.049/255）、2× 480x736→960x1472、1.5× →720x1104，host 的
  CreateFeature(18) failed / DLSSNR EvaluateFeature failed 两条错误路径均未触发。
  吞吐 8.72 帧/秒（2×），15s@24fps 约 41 秒。

协议原语直接 import 上游的 tools/dlss5nr_video.py，不复制粘贴 ——
上游改协议时我们跟着走，不会悄悄对不上。
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

# 默认路径：装机脚本把仓库放这里
DEFAULT_REPO = "/workspace/dlss5nr"
DEFAULT_WINE = "/workspace/dlss5/GE-Proton10-34/files/bin/wine64"
DEFAULT_PFX = "/workspace/dlss5/prefix/pfx"
DEFAULT_DISPLAY = ":99"

# 档位 -> (倍率, PerfQualityValue)。取自上游 _PERF_QUALITY，写死在这里只是为了
# 让下拉框有稳定顺序；真正的值仍从上游模块校验。
SCALE_CHOICES = {
    "1.0x (DLAA / 原尺寸神经渲染)": 1.0,
    "1.5x (Quality)": 1.5,
    "1.724x (Balanced)": 1.724,
    "2.0x (Performance)": 2.0,
    "3.0x (Ultra Performance)": 3.0,
}
STYLE_CHOICES = {"Default": 0, "Natural": 1, "Cinematic": 2}


class Dlss5NRWineError(RuntimeError):
    pass


def _load_upstream(repo: Path):
    """把上游前端当模块加载，借它的协议常量与时域向导。"""
    tool = repo / "tools" / "dlss5nr_video.py"
    if not tool.is_file():
        raise Dlss5NRWineError(
            "找不到 %s。先 git clone https://github.com/kos94ok/ComfyUI-DLSS5-NR-Linux %s"
            % (tool, repo)
        )
    spec = importlib.util.spec_from_file_location("_dlss5nr_video", tool)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_dlss5nr_video"] = mod
    spec.loader.exec_module(mod)
    return mod


def _even(v: float) -> int:
    return max(2, int(round(v / 2.0) * 2))


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
    """IMAGE 批 -> DLSS 5 神经渲染 -> IMAGE 批。整批共用一个持久 NGX 会话。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "scale": (list(SCALE_CHOICES),),
                "repo_dir": ("STRING", {"default": DEFAULT_REPO, "multiline": False}),
                "wine": ("STRING", {"default": DEFAULT_WINE, "multiline": False}),
                "prefix": ("STRING", {"default": DEFAULT_PFX, "multiline": False}),
                "display": ("STRING", {"default": DEFAULT_DISPLAY, "multiline": False,
                                       "tooltip": "必填。去掉 DISPLAY 后 DXVK 连 Vulkan instance 都建不出来（实测）。"}),
                "warmup_frames": ("INT", {"default": 8, "min": 0, "max": 600}),
                "style": (list(STYLE_CHOICES),),
                "preset": ("INT", {"default": 0, "min": 0, "max": 7}),
                "intensity": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 4.0, "step": 0.05}),
                "tone": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 4.0, "step": 0.05}),
                "structure": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 4.0, "step": 0.05}),
                "skin": ("FLOAT", {"default": -1.0, "min": -1.0, "max": 4.0, "step": 0.05,
                                   "tooltip": "-1 = 交给运行时自己决定（上游 CLI 的默认）"}),
                "auto_mask": ("BOOLEAN", {"default": False}),
                "reset_each_frame": ("BOOLEAN", {"default": False,
                                                 "tooltip": "关掉时域复用。静态图批量走这个，视频不要开。"}),
                "channel_order": (["auto", "RGBA", "BGRA"],),
                "gpu_index": ("INT", {"default": 0, "min": 0, "max": 7}),
            }
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "report")
    FUNCTION = "upscale"
    CATEGORY = "image/DLSS 5 NR (Wine)"

    def upscale(self, image, scale, repo_dir, wine, prefix, display, warmup_frames,
                style, preset, intensity, tone, structure, skin, auto_mask,
                reset_each_frame, channel_order, gpu_index):
        repo = Path(repo_dir).expanduser().resolve()
        V = _load_upstream(repo)

        host = repo / "native" / "bin" / "dlss5nr_host.exe"
        runtime = repo / "runtime"
        shim = runtime / "caller" / "nvngx.dll_comfy.dll"
        for p, what in ((host, "dlss5nr_host.exe（用 native/build_host_mingw.sh 编）"),
                        (runtime / "nvngx_dlssnr.dll", "nvngx_dlssnr.dll（自备）"),
                        (shim, "caller shim nvngx.dll_comfy.dll")):
            if not p.is_file():
                raise Dlss5NRWineError("缺 %s：%s" % (what, p))
        bridge = repo / "native" / "bin" / "dlss5nr_bridge.dll"
        if not bridge.is_file():
            raise Dlss5NRWineError("缺 dlss5nr_bridge.dll：%s" % bridge)

        factor = SCALE_CHOICES[scale]
        img = image.detach().cpu().float().clamp(0.0, 1.0).numpy()
        if img.ndim != 4 or img.shape[-1] < 3:
            raise Dlss5NRWineError("IMAGE 形状不对：%s" % (tuple(img.shape),))
        img = np.ascontiguousarray(img[..., :3])
        n, in_h, in_w = img.shape[0], img.shape[1], img.shape[2]
        if in_w % 2 or in_h % 2:
            raise Dlss5NRWineError(
                "输入必须是偶数尺寸，当前 %dx%d。H3 出片是偶数，若在中间裁过请对齐到 2 的倍数。"
                % (in_w, in_h))
        out_w, out_h = (_even(in_w * factor), _even(in_h * factor)) if factor != 1.0 else (in_w, in_h)
        # 让上游自己校验倍率合法性，不自己另起一套判断
        perf_quality = V._perf_quality_for_size(in_w, in_h, out_w, out_h)
        if factor > 1.0 and not (runtime / "nvngx_dlss.dll").is_file():
            raise Dlss5NRWineError(
                "倍率 >1x 需要普通 DLSS carrier：把 nvngx_dlss.dll 放进 %s" % runtime)

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
            proc.stdin.write(V.HEADER.pack(
                V.MAGIC_DNR2, in_w, in_h, out_w, out_h,
                int(warmup_frames), int(n), int(perf_quality),
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
                    fail("第 %d 帧运动向量 %d 字节，应为 %d" % (i, motion.nbytes, motion_bytes))
                reset = bool(reset_each_frame or guide_reset)
                proc.stdin.write(V.FRAME_HEADER.pack(V.MAGIC_FRM2, i, 1 if reset else 0))
                proc.stdin.write(src.tobytes(order="C"))
                proc.stdin.write(motion.tobytes(order="C"))
                proc.stdin.flush()

                try:
                    magic, idx, ok, count = V.REPLY_HEADER.unpack(
                        V._read_exact(proc.stdout, V.REPLY_HEADER.size))
                except Exception as e:
                    fail("第 %d 帧读回复失败：%s" % (i, e))
                if magic != V.MAGIC_OUT1 or idx != i:
                    fail("第 %d 帧回复错乱：magic=%r index=%d" % (i, magic, idx))
                if not ok:
                    try:
                        ln = struct.unpack("<I", V._read_exact(proc.stdout, 4))[0]
                        detail = V._read_exact(proc.stdout, min(ln, 65535)).decode("utf-8", "replace")
                    except Exception:
                        detail = "(读不到详情)"
                    fail("第 %d 帧 host 报错：%s" % (i, detail))
                if count != out_w * out_h * 3:
                    fail("第 %d 帧返回 %d 个 float，应为 %d" % (i, count, out_w * out_h * 3))
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
                fail("host 没有发 END1（收到 %r）" % (done,))
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
            "DLSS 5 Neural Rendering (feature 18) · Wine 路径",
            "  %d 帧   %dx%d -> %dx%d   %s   PerfQualityValue=%d" % (
                n, in_w, in_h, out_w, out_h, scale, perf_quality),
            "  通道序 %s   warmup=%d   reset_each_frame=%s   auto_mask=%s" % (
                chosen, warmup_frames, reset_each_frame, auto_mask),
            "  host=%s" % host,
            "  运行时 nvngx_dlssnr.dll=%d B%s" % (
                (runtime / "nvngx_dlssnr.dll").stat().st_size,
                "   carrier nvngx_dlss.dll=%d B" % (runtime / "nvngx_dlss.dll").stat().st_size
                if (runtime / "nvngx_dlss.dll").is_file() else ""),
            "  host 最后几行：",
        ] + ["    " + l for l in log[-6:]])
        return (torch.from_numpy(out), report)


class DLSS5NRWineStatus:
    """先跑这个：不碰 GPU，只检查文件与环境齐不齐。"""

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
        lines = ["DLSS 5 NR (Wine) 自检", "  仓库: %s" % repo]
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
                lines.append("  ✗ %-8s 缺失: %s" % (name, p))
        lines.append("  DISPLAY=%s（进程内 %s）" % (display, os.environ.get("DISPLAY", "<未设>")))
        try:
            import cv2  # noqa: F401
            lines.append("  ✓ cv2 %s（光流运动向量可用）" % cv2.__version__)
        except Exception:
            lines.append("  ⚠️ 无 cv2 —— 运动向量退化为零、每帧重置，视频会丢时域收益")
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
                                "--format=csv,noheader"], capture_output=True, text=True, timeout=20)
            lines.append("  GPU: %s" % (r.stdout.strip() or "查不到"))
        except Exception as e:
            lines.append("  GPU: 查不到 (%s)" % e)
        return ("\n".join(lines),)


NODE_CLASS_MAPPINGS = {
    "DLSS5NRWineUpscale": DLSS5NRWineUpscale,
    "DLSS5NRWineStatus": DLSS5NRWineStatus,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DLSS5NRWineUpscale": "DLSS 5 神经渲染 · Wine (Linux)",
    "DLSS5NRWineStatus": "DLSS 5 NR · 自检 (Wine)",
}
