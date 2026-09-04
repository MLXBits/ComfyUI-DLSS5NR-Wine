# Frame interpolation: where the code came from

`dlssg.py` is a port. The chain is:

1. **[Merserk/dlss5-visual-enhancer](https://github.com/Merserk/dlss5-visual-enhancer)**
   (MIT, Copyright (c) 2026 Merserk) - the original DLSSG worker protocol and
   optical-flow guide approach. Licence: `LICENSE-DLSS-Visual-Enhancer.txt`.
2. **[Konohamaru04/ComfyUI-NVIDIA-DLSS-Frame-Interpolation](https://github.com/Konohamaru04/ComfyUI-NVIDIA-DLSS-Frame-Interpolation)**
   (MIT, Copyright (c) 2026 Konohamaru04) - the Python implementation this port
   is taken from. Licence: `LICENSE-ComfyUI-NVIDIA-DLSS-Frame-Interpolation.txt`.
3. This repository - adapted to the Wine path and to ComfyUI tensor batches.

## A note on the upstream licence files

At the time of porting (2026-09-04), the Konohamaru04 repository carried a
single licence file named `LICENSE-DLSS-Visual-Enhancer.txt` whose copyright
line had been changed from Merserk to Konohamaru04 in commit `9eb31d3`, leaving
Merserk's notice absent. MIT requires that notice be preserved.

Rather than copy that arrangement, this repository ships both notices
separately: Merserk's text is taken verbatim from the Merserk repository, and
Konohamaru04's is kept as their file presents it. No judgement is intended
about intent - it looks like a rename that went wrong - but the notices belong
to their respective authors and both are reproduced here.

## What is NOT redistributed

- `dlssg-worker.exe` - no source is published for it in either upstream
  repository, so it is not vendored here. You supply it, the same way you
  supply the NVIDIA runtimes.
- NVIDIA NGX runtimes, including `nvngx_dlssg.dll`. That one is published by
  NVIDIA in the public DLSS SDK and also ships with many Linux drivers, so
  `install/setup.sh` can fetch it rather than bundle it.
- ReShade and RenoDX binaries. The frame-interpolation path does not use them.
