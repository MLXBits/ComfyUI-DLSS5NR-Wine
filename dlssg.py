# -*- coding: utf-8 -*-
"""DLSS Frame Generation (NGX DLSSG) transport, ported for the Wine path.

Ported from ComfyUI-NVIDIA-DLSS-Frame-Interpolation by Konohamaru04 (MIT),
which is itself derived from DLSS 5 Visual Enhancer by Merserk (MIT). Both
licences are reproduced in LICENSE-ComfyUI-NVIDIA-DLSS-Frame-Interpolation.txt
and LICENSE-DLSS-Visual-Enhancer.txt.

What was taken: the dlssg-worker wire protocol (DirectDLSSGSession) and the
optical-flow guide generator. What was left behind: their ffmpeg/PyAV file
pipeline, job control and output naming - a ComfyUI node gets frames as tensors
and hands video handling back to the graph, so none of that applies here.

What changed: the worker is launched through wine with a caller-supplied
environment rather than started directly on Windows, and the session takes
frames as one batch instead of streaming a file.

`dlssg-worker.exe` itself is NOT redistributed by this project - no source for
it is published anywhere, so it is treated the same way as the NVIDIA runtimes:
you supply it. See the README.
"""

from __future__ import annotations

import collections
import struct
import subprocess
import threading
from fractions import Fraction

import numpy as np

SETUP_MAGIC = 0x31534746
SETUP_OUT_MAGIC = 0x31524746
FRAME_MAGIC = 0x31464746
FRAME_OUT_MAGIC = 0x314F4746

SETUP = struct.Struct("<5I")
SETUP_OUT = struct.Struct("<4I")
FRAME = struct.Struct("<4I2q")
FRAME_OUT = struct.Struct("<4I")


class DlssgError(RuntimeError):
    pass


def _read_exact(stream, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        block = stream.read(size - len(data))
        if not block:
            raise DlssgError("the DLSSG worker closed its output unexpectedly")
        data.extend(block)
    return bytes(data)


class GuideGenerator:
    """Optical-flow motion vectors, ported from the upstream guide generator.

    DLSSG is built for game frames that arrive with true motion vectors. Video
    has none, so these are estimated with DIS optical flow at reduced
    resolution and scaled back up. A large frame-to-frame difference is treated
    as a cut and resets history rather than interpolating across it.
    """

    def __init__(self, width: int, height: int, flow_width: int = 640) -> None:
        import cv2

        self.cv2 = cv2
        self.width = width
        self.height = height
        scale = min(1.0, flow_width / max(1, width))
        self.flow_width = max(64, int(round(width * scale / 2) * 2))
        self.flow_height = max(64, int(round(height * scale / 2) * 2))
        self.previous = None
        self.zero = np.zeros((height, width, 2), dtype=np.float16)
        self.flow = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
        self.flow.setUseSpatialPropagation(True)
        self.flow.setFinestScale(1)

    def _gray(self, rgba: np.ndarray) -> np.ndarray:
        gray = self.cv2.cvtColor(rgba, self.cv2.COLOR_RGBA2GRAY)
        return self.cv2.resize(gray, (self.flow_width, self.flow_height),
                               interpolation=self.cv2.INTER_AREA)

    def process(self, rgba: np.ndarray, force_reset: bool = False):
        """Return (motion fp16 [h,w,2], reset, scene_score, duplicate)."""
        cv2 = self.cv2
        current = self._gray(rgba)
        if self.previous is None:
            self.previous = current
            return self.zero, True, 1.0, False
        score = float(np.mean(cv2.absdiff(current, self.previous))) / 255.0
        duplicate = score < 0.0005
        reset = force_reset or score > 0.24
        if reset or duplicate:
            self.previous = current
            return self.zero, reset, score, duplicate
        flow = self.flow.calc(current, self.previous, None)
        flow = cv2.resize(flow, (self.width, self.height), interpolation=cv2.INTER_LINEAR)
        flow[..., 0] *= self.width / self.flow_width
        flow[..., 1] *= self.height / self.flow_height
        finite = np.isfinite(flow).all(axis=2)
        confidence = float(np.mean(finite))
        flow[~finite] = 0
        self.previous = current
        if confidence < 0.98:
            return self.zero, True, score, duplicate
        return np.ascontiguousarray(flow.astype(np.float16)), False, score, duplicate


class DlssgSession:
    """One `dlssg-worker.exe --serve` process under wine, driven over pipes."""

    def __init__(self, wine, worker, cwd, env, width, height, frame_count,
                 generated_count):
        self.width = int(width)
        self.height = int(height)
        self.frame_bytes = self.width * self.height * 4
        self.generated_count = int(generated_count)
        self.logs: collections.deque = collections.deque(maxlen=300)
        self.closed = False
        self._next_index = 0
        self.process = subprocess.Popen(
            [str(wine), str(worker), "--serve"],
            cwd=str(cwd), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._log_thread = threading.Thread(target=self._read_logs, daemon=True)
        self._log_thread.start()
        self.process.stdin.write(SETUP.pack(SETUP_MAGIC, self.width, self.height,
                                            max(1, int(frame_count)), self.generated_count))
        self.process.stdin.flush()
        magic, status, maximum, _ = SETUP_OUT.unpack(_read_exact(self.process.stdout, SETUP_OUT.size))
        self.maximum = maximum
        if magic != SETUP_OUT_MAGIC or status:
            self.close()
            raise DlssgError("DLSSG session creation failed (status %d); runtime maximum is %d\n%s"
                             % (status, maximum + 1, self.log_text()))
        if self.generated_count > maximum:
            self.close()
            raise DlssgError("worker rejected %d generated frame(s); MultiFrameCountMax is %d"
                             % (self.generated_count, maximum))

    def _read_logs(self) -> None:
        for raw in iter(self.process.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.logs.append(line)

    def log_text(self) -> str:
        return "\n".join(self.logs)

    def process_frame(self, rgba, motion, timestamp: Fraction, reset: bool):
        if self.closed:
            raise DlssgError("session is closed")
        color = np.ascontiguousarray(rgba, dtype=np.uint8)
        vectors = np.ascontiguousarray(motion, dtype=np.float16)
        if color.shape != (self.height, self.width, 4):
            raise DlssgError("colour frame has shape %s, expected %s"
                             % (color.shape, (self.height, self.width, 4)))
        if vectors.shape != (self.height, self.width, 2):
            raise DlssgError("motion field has shape %s, expected %s"
                             % (vectors.shape, (self.height, self.width, 2)))
        self.process.stdin.write(FRAME.pack(FRAME_MAGIC, self._next_index, int(reset), 0,
                                            timestamp.numerator, timestamp.denominator))
        self.process.stdin.write(memoryview(color).cast("B"))
        self.process.stdin.write(memoryview(vectors).cast("B"))
        self.process.stdin.flush()
        self._next_index += 1
        magic, status, generated, disabled = FRAME_OUT.unpack(
            _read_exact(self.process.stdout, FRAME_OUT.size))
        if magic != FRAME_OUT_MAGIC or status:
            raise DlssgError("DLSSG evaluation failed at input frame %d (status %d)\n%s"
                             % (self._next_index - 1, status, self.log_text()))
        if disabled:
            return []
        return [np.frombuffer(_read_exact(self.process.stdout, self.frame_bytes), np.uint8)
                  .reshape(self.height, self.width, 4).copy()
                for _ in range(generated)]

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        p = self.process
        try:
            if p.stdin and not p.stdin.closed:
                p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        self._log_thread.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()
