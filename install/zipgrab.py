#!/usr/bin/env python3
"""从远端 zip 里只取一个成员：EOCD -> 中央目录 -> 局部头 -> inflate。

全程 HTTP Range，不落整包。实测取 Electron 包里的 d3dcompiler_47.dll
只需下载 2.15 MB，而整包是 158 MB。

用法: zipgrab.py <zip-url> <文件名后缀> <输出路径> <期望解压后字节数>
"""
import struct
import sys
import zlib
import urllib.request


def get(url, rng=None):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    if rng:
        req.add_header("Range", "bytes=%d-%d" % rng)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read(), r.headers, r.geturl()


def main():
    if len(sys.argv) != 5:
        print(__doc__)
        return 2
    url, want, out, expect = sys.argv[1], sys.argv[2].lower(), sys.argv[3], int(sys.argv[4])

    _, hdr, real = get(url, (0, 0))
    size = int(hdr.get("Content-Range", "bytes 0-0/0").split("/")[-1])

    tail, _, _ = get(real, (max(0, size - 65600), size - 1))
    i = tail.rfind(b"PK\x05\x06")
    if i < 0:
        print("ZIPGRAB_FAIL 找不到 EOCD")
        return 1
    cd_size, cd_off = struct.unpack_from("<II", tail, i + 12)
    if cd_off == 0xFFFFFFFF:                      # ZIP64
        j = tail.rfind(b"PK\x06\x06")
        cd_size, cd_off = struct.unpack_from("<QQ", tail, j + 40)

    cd, _, _ = get(real, (cd_off, cd_off + cd_size - 1))
    p = 0
    while p < len(cd) - 46 and cd[p:p + 4] == b"PK\x01\x02":
        meth, = struct.unpack_from("<H", cd, p + 10)
        csz, _usz = struct.unpack_from("<II", cd, p + 20)
        nl, el, cl = struct.unpack_from("<HHH", cd, p + 28)
        lho, = struct.unpack_from("<I", cd, p + 42)
        name = cd[p + 46:p + 46 + nl].decode("utf-8", "replace")
        if name.lower().endswith(want):
            lh, _, _ = get(real, (lho, lho + 29))
            nl2, el2 = struct.unpack_from("<HH", lh, 26)
            start = lho + 30 + nl2 + el2
            blob, _, _ = get(real, (start, start + csz - 1))
            data = zlib.decompress(blob, -15) if meth == 8 else blob
            if len(data) != expect:
                print("ZIPGRAB_FAIL 大小不符 got=%d want=%d" % (len(data), expect))
                return 1
            with open(out, "wb") as fh:
                fh.write(data)
            print("ZIPGRAB_OK %s %d 字节（只下了 %.2f MB，整包 %.0f MB）"
                  % (name, len(data), csz / 1e6, size / 1e6))
            return 0
        p += 46 + nl + el + cl
    print("ZIPGRAB_FAIL 包里没有 " + want)
    return 1


if __name__ == "__main__":
    sys.exit(main())
