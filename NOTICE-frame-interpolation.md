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
  repository, so there is nothing to build and it is not vendored here.
- NVIDIA NGX runtimes, including `nvngx_dlssg.dll`.
- ReShade and RenoDX binaries. The frame-interpolation path does not use them.

`install/setup.sh` fetches both rather than bundling them. Neither is
published by NVIDIA in a public SDK repository - checked 2026-09-04:
`NVIDIA-RTX/Streamline` ships no NGX binaries at all - so both come from
Konohamaru04's node pack, which stores them in Git LFS. The LFS pointers give
an exact `sha256`, so the download is pinned to a **commit** rather than a
branch and every file is verified against its digest before it is moved into
place; a mismatch installs nothing. For `nvngx_dlssg.dll` the script prefers a
copy from your own NVIDIA driver package if it finds one, since that is
guaranteed to match the running driver.

`--no-dlssg` skips this entirely. The upscaler does not need either file.
