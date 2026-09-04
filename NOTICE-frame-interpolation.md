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

`install/setup.sh` fetches both rather than bundling them, and every route
verifies a pinned `sha256` before the file is moved into place.

### `nvngx_dlssg.dll`

Shipped in NVIDIA's own **Streamline SDK** release, at `bin/x64/` (*not*
`bin/x64/development/`, which is a different and larger debug build). The
release asset is a 232 MB zip, but `install/zipgrab.py` pulls the single member
over HTTP Range - **3.75 MB transferred** - so the first-party copy is the
cheapest route as well as the most trustworthy one. Order of preference:

1. your own NVIDIA driver package, if it carries the file - guaranteed to match
   the running driver;
2. a Streamline SDK you already have (`--streamline-sdk DIR|ZIP`), no download;
3. NVIDIA's release zip, one member over HTTP Range.

If all three fail the script says so and stops, rather than falling back to a
community copy. Such copies exist, and one of them is byte-identical to
NVIDIA's - measured 2026-09-04, both `sha256 135eaf07...` - so this costs
nothing in bytes. It is deliberate anyway: the licence restricts
*distribution*, and routing every user's download at somebody else's
redistribution is not a meaningful distance from redistributing it here.
NVIDIA publishes this file, so it comes from NVIDIA.

**It is not vendored here, and cannot be.** The binary is not covered by
Streamline's MIT `license.txt`; the file sitting beside it in `bin/x64/` is the
**NVIDIA RTX SDKs License**, which governs "the DLSS SDK, NGX SDK" and grants
distribution only "as incorporated in object code format into a software
application" with "material additional functionality" (§1c, §2a), while §4(b)
states plainly that you "may not distribute or sublicense the SDK as a
stand-alone product." A bare DLL committed to a public repository is exactly
that. §4(e) additionally forbids using it in a way that would subject it to an
open source licence.

### `dlssg-worker.exe`

One source only, and it is the compromise in this section. NVIDIA publishes no
equivalent, and no source is published for it in either upstream repository, so
there is nothing to build, nothing to compare against, and no first-party route
of the kind the runtime gets. It is an unsigned third-party binary that the node executes
under Wine; the pinned digest proves you get the same bytes that were verified
here, and nothing about what those bytes do.

`--no-dlssg` skips this section entirely. The upscaler needs neither file.
