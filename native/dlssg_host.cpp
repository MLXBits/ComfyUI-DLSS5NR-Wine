// SPDX-License-Identifier: MIT
// Copyright (c) 2026 ComfyUI-DLSS5NR-Wine contributors
//
// Probe-only first milestone of a project-owned DLSSG host, intended to
// replace the third-party dlssg-worker.exe. Full scope: docs/DLSSG-HOST.md.
//
// This binary deliberately touches no frames. It answers exactly one
// question, which is the only one that can sink the whole plan:
//
//     does NGX feature 11 (FrameGeneration) initialize and report itself
//     available on this Wine + vkd3d-proton stack?
//
// If it does, the rest is a port of dlss5nr_host.cpp plus a parameter map.
// If it does not, we learn that before writing any of it.
//
// The NGX ABI declarations, the _nvngx.dll DriverStore search and the
// adapter/device selection are adapted from native/dlss5nr_bridge.cpp in
// kos94ok/ComfyUI-DLSS5-NR-Linux (MIT, Copyright (c) 2026
// ComfyUI-DLSS5-NR contributors). NVIDIA publishes no headers we need here:
// the NGX parameter interface is string-keyed, so the helper structs in the
// SDK are sugar over Set/Get calls this file makes directly.
//
// Parameter key strings are the literals the reference worker uses, read out
// of its string table rather than inferred from the SDK macro names.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <wrl/client.h>

#include <algorithm>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cwchar>
#include <string>
#include <vector>

using Microsoft::WRL::ComPtr;
using NGXResult = int;
static constexpr NGXResult NGX_SUCCESS = 1;

// NVSDK_NGX_Feature_FrameGeneration. DLSS-FG SDK 310.7.0 programming guide,
// p107: the enum runs ... DeepResolve=10, FrameGeneration=11, DeepDVC=12 ...
static constexpr int FG_FEATURE_ID = 11;

// Same identifiers the bridge uses. A snippet may reject an unrelated project
// id with FAIL_Denied even when D3D12 itself is healthy, so these stay
// overridable for probing.
static constexpr const char* PROJECT_ID = "53f803cc-a12f-4d69-90d5-19b7599cad19";

// Capability keys, verbatim from the reference worker's string table.
static constexpr const char* KEY_AVAILABLE = "FrameGeneration.Available";
static constexpr const char* KEY_MFG_MAX = "DLSSG.MultiFrameCountMax";

// ---------------------------------------------------------------- NGX ABI --

struct NGXHandle { unsigned int Id; };

// Minimal ABI-compatible view of the NVIDIA NGX parameter object. The vtable
// order is load-bearing: getting it wrong calls the wrong virtual.
struct NGXParameter {
    virtual void Set(const char*, unsigned long long) = 0;
    virtual void Set(const char*, float) = 0;
    virtual void Set(const char*, double) = 0;
    virtual void Set(const char*, unsigned int) = 0;
    virtual void Set(const char*, int) = 0;
    virtual void Set(const char*, ID3D11Resource*) = 0;
    virtual void Set(const char*, ID3D12Resource*) = 0;
    virtual void Set(const char*, void*) = 0;
    virtual NGXResult Get(const char*, unsigned long long*) const = 0;
    virtual NGXResult Get(const char*, float*) const = 0;
    virtual NGXResult Get(const char*, double*) const = 0;
    virtual NGXResult Get(const char*, unsigned int*) const = 0;
    virtual NGXResult Get(const char*, int*) const = 0;
    virtual NGXResult Get(const char*, ID3D11Resource**) const = 0;
    virtual NGXResult Get(const char*, ID3D12Resource**) const = 0;
    virtual NGXResult Get(const char*, void**) const = 0;
    virtual void Reset() = 0;
};

struct NGXPathListInfo {
    wchar_t const* const* Path;
    unsigned int Length;
};
enum NGXLoggingLevel { NGX_LOG_OFF = 0, NGX_LOG_ON = 1, NGX_LOG_VERBOSE = 2 };
using NGXLogCallback = void(__cdecl*)(const char*, NGXLoggingLevel, int);
struct NGXLoggingInfo {
    // SDK 0x14 layout: callback first, then the minimum level. The older
    // internal ordering makes the NGX core read a function pointer as an enum
    // and reject init with FAIL_OutOfDate under Wine.
    NGXLogCallback LoggingCallback;
    NGXLoggingLevel MinimumLoggingLevel;
    bool DisableOtherLoggingSinks;
};
struct NGXFeatureCommonInfoInternal;
struct NGXFeatureCommonInfo {
    NGXPathListInfo PathListInfo;
    NGXFeatureCommonInfoInternal* InternalData;
    NGXLoggingInfo LoggingInfo;
};

using InitExtFn = NGXResult(__cdecl*)(unsigned long long, const wchar_t*, ID3D12Device*, int, const void*);
using InitProjectIdFn = NGXResult(__cdecl*)(const char*, int, const char*, const wchar_t*, ID3D12Device*, int, const void*);
using GetCapabilityParamsFn = NGXResult(__cdecl*)(NGXParameter**);
using DestroyParamsFn = NGXResult(__cdecl*)(NGXParameter*);
using ShutdownFn = NGXResult(__cdecl*)();

// ------------------------------------------------------------- utilities --

static std::string g_detail;
static void Detail(const char* fmt, ...) {
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    g_detail = buf;
}

static void Log(const char* fmt, ...) {
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    // stderr only. stdout is the protocol stream in --serve mode and the JSON
    // line in --probe mode; nothing else may ever be written there.
    fprintf(stderr, "[dlssg] %s\n", buf);
    fflush(stderr);
}

static void __cdecl NGXLog(const char* message, NGXLoggingLevel, int) {
    if (!message) return;
    std::string s(message);
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r')) s.pop_back();
    if (!s.empty()) fprintf(stderr, "[ngx] %s\n", s.c_str());
}

static std::string JsonEscape(const std::string& in) {
    std::string out;
    for (char c : in) {
        if (c == '"' || c == '\\') { out += '\\'; out += c; }
        else if (c == '\n' || c == '\r' || c == '\t') out += ' ';
        else if (static_cast<unsigned char>(c) < 0x20) out += ' ';
        else out += c;
    }
    return out;
}

static int EnvInt(const char* name, int fallback) {
    const char* v = getenv(name);
    if (!v || !*v) return fallback;
    return static_cast<int>(strtol(v, nullptr, 0));
}

static std::string EnvString(const char* name, const char* fallback) {
    const char* v = getenv(name);
    return (v && *v) ? std::string(v) : std::string(fallback);
}

static bool FileExists(const std::wstring& p) {
    DWORD a = GetFileAttributesW(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

static std::wstring Join(const std::wstring& dir, const wchar_t* leaf) {
    if (dir.empty()) return leaf;
    if (dir.back() == L'\\' || dir.back() == L'/') return dir + leaf;
    return dir + L"\\" + leaf;
}

static unsigned long long FileTimeKey(const FILETIME& ft) {
    ULARGE_INTEGER u;
    u.LowPart = ft.dwLowDateTime;
    u.HighPart = ft.dwHighDateTime;
    return u.QuadPart;
}

// ---------------------------------------------------------- NGX core load --

// Adapted from the bridge. The INF prefix is not always nv_dispi: depending on
// OEM and driver generation it can be nvddi, nvaci, nvhmui, so every
// NVIDIA-looking package is scanned and the newest one wins.
static HMODULE LoadCoreNGX(const std::wstring& runtime) {
    const std::wstring local = Join(runtime, L"_nvngx.dll");
    if (FileExists(local)) {
        if (HMODULE m = LoadLibraryW(local.c_str())) return m;
    }
    if (HMODULE m = LoadLibraryW(L"_nvngx.dll")) return m;

    wchar_t windows_dir[MAX_PATH] = {};
    UINT windows_len = GetWindowsDirectoryW(windows_dir, MAX_PATH);
    if (windows_len == 0 || windows_len >= MAX_PATH) return nullptr;
    const std::wstring repo = std::wstring(windows_dir) + L"\\System32\\DriverStore\\FileRepository";

    struct Candidate { std::wstring path; unsigned long long stamp; };
    std::vector<Candidate> candidates;
    WIN32_FIND_DATAW fd{};
    HANDLE h = FindFirstFileW((repo + L"\\nv*.inf_*").c_str(), &fd);
    if (h != INVALID_HANDLE_VALUE) {
        do {
            if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
            if (wcscmp(fd.cFileName, L".") == 0 || wcscmp(fd.cFileName, L"..") == 0) continue;
            std::wstring candidate = repo + L"\\" + fd.cFileName + L"\\_nvngx.dll";
            WIN32_FILE_ATTRIBUTE_DATA fad{};
            if (GetFileAttributesExW(candidate.c_str(), GetFileExInfoStandard, &fad) &&
                !(fad.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) {
                candidates.push_back({candidate, FileTimeKey(fad.ftLastWriteTime)});
            }
        } while (FindNextFileW(h, &fd));
        FindClose(h);
    }
    std::sort(candidates.begin(), candidates.end(),
              [](const Candidate& a, const Candidate& b) { return a.stamp > b.stamp; });
    for (const Candidate& c : candidates) {
        if (HMODULE m = LoadLibraryW(c.path.c_str())) return m;
    }
    return nullptr;
}

// ------------------------------------------------------------ D3D12 setup --

static std::string g_gpu_name = "unknown";

static ComPtr<ID3D12Device> CreateDevice(int nvidia_index, const LUID* want_luid) {
    ComPtr<IDXGIFactory4> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) return nullptr;

    int seen = 0;
    for (UINT i = 0;; ++i) {
        ComPtr<IDXGIAdapter1> adapter;
        if (factory->EnumAdapters1(i, &adapter) == DXGI_ERROR_NOT_FOUND) break;
        DXGI_ADAPTER_DESC1 desc{};
        adapter->GetDesc1(&desc);
        if ((desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) || desc.VendorId != 0x10DE) continue;
        if (want_luid) {
            if (desc.AdapterLuid.LowPart != want_luid->LowPart ||
                desc.AdapterLuid.HighPart != want_luid->HighPart) continue;
        } else if (seen++ != nvidia_index) {
            continue;
        }
        char gpu_utf8[512] = {};
        WideCharToMultiByte(CP_UTF8, 0, desc.Description, -1, gpu_utf8,
                            static_cast<int>(sizeof(gpu_utf8)), nullptr, nullptr);
        if (gpu_utf8[0]) g_gpu_name = gpu_utf8;
        ComPtr<ID3D12Device> d;
        // Wine can expose a usable D3D12 device while rejecting the 12_0
        // feature-level probe; NGX uses resource/queue interfaces rather than
        // shader-model queries, so 11_0 is a valid fallback.
        if (SUCCEEDED(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&d))) ||
            SUCCEEDED(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&d))))
            return d;
        return nullptr;
    }
    return nullptr;
}

// ----------------------------------------------------------------- probe --

struct ProbeResult {
    bool available = false;
    unsigned int multi_frame_count_max = 0;
};

// Where nvngx_dlssg.dll lives. NGX core loads the snippet itself from the
// search path handed to it at init, so this directory is the whole reason the
// feature can be found at all.
static std::wstring RuntimeDir() {
    if (const char* env = getenv("DLSS5NR_DLSSG_DIR")) {
        if (*env) {
            int n = MultiByteToWideChar(CP_UTF8, 0, env, -1, nullptr, 0);
            if (n > 0) {
                std::wstring w(static_cast<size_t>(n - 1), L'\0');
                MultiByteToWideChar(CP_UTF8, 0, env, -1, &w[0], n);
                // Everything that sets this variable - nodes.py, the systemd
                // drop-in, dlss5nr.local.json - holds a Linux path, but this
                // is a Windows process: LoadLibraryW and GetFileAttributesW
                // want a Windows path and would simply not find the file.
                // Wine maps drive Z: to /, so rewrite it rather than making
                // every caller know it is talking to a PE binary.
                if (!w.empty() && w[0] == L'/') w = L"Z:" + w;
                std::replace(w.begin(), w.end(), L'/', L'\\');
                return w;
            }
        }
    }
    // The node launches the worker with cwd set to the dlssg directory.
    wchar_t cwd[MAX_PATH] = {};
    if (GetCurrentDirectoryW(MAX_PATH, cwd) > 0 && FileExists(Join(cwd, L"nvngx_dlssg.dll")))
        return cwd;
    // Otherwise fall back to wherever this executable sits.
    wchar_t exe[MAX_PATH] = {};
    if (GetModuleFileNameW(nullptr, exe, MAX_PATH) > 0) {
        std::wstring p(exe);
        size_t slash = p.find_last_of(L"\\/");
        if (slash != std::wstring::npos) return p.substr(0, slash);
    }
    return cwd;
}

static bool Probe(int gpu_index, const LUID* want_luid, ProbeResult* out) {
    const std::wstring runtime = RuntimeDir();
    if (!FileExists(Join(runtime, L"nvngx_dlssg.dll"))) {
        Detail("nvngx_dlssg.dll not found in the runtime directory");
        return false;
    }

    ComPtr<ID3D12Device> device = CreateDevice(gpu_index, want_luid);
    if (!device) {
        Detail("no usable NVIDIA D3D12 device (index %d)", gpu_index);
        return false;
    }
    Log("adapter: %s", g_gpu_name.c_str());

    HMODULE core = LoadCoreNGX(runtime);
    if (!core) {
        Detail("could not load _nvngx.dll (runtime dir, loader search, DriverStore all failed)");
        return false;
    }

    auto init_project = reinterpret_cast<InitProjectIdFn>(
        GetProcAddress(core, "NVSDK_NGX_D3D12_Init_ProjectID"));
    auto init_ext = reinterpret_cast<InitExtFn>(
        GetProcAddress(core, "NVSDK_NGX_D3D12_Init_Ext"));
    auto get_caps = reinterpret_cast<GetCapabilityParamsFn>(
        GetProcAddress(core, "NVSDK_NGX_D3D12_GetCapabilityParameters"));
    auto destroy_params = reinterpret_cast<DestroyParamsFn>(
        GetProcAddress(core, "NVSDK_NGX_D3D12_DestroyParameters"));
    auto shutdown = reinterpret_cast<ShutdownFn>(
        GetProcAddress(core, "NVSDK_NGX_D3D12_Shutdown"));
    if ((!init_project && !init_ext) || !get_caps || !shutdown) {
        Detail("required NGX core exports missing");
        return false;
    }

    const wchar_t* paths[1] = { runtime.c_str() };
    NGXFeatureCommonInfo fci{};
    fci.PathListInfo = NGXPathListInfo{ paths, 1 };
    fci.LoggingInfo.LoggingCallback = NGXLog;
    fci.LoggingInfo.MinimumLoggingLevel = NGX_LOG_VERBOSE;
    fci.LoggingInfo.DisableOtherLoggingSinks = EnvInt("DLSS5NR_DISABLE_OTHER_SINKS", 0) != 0;

    const int ver = EnvInt("DLSS5NR_SDK_VERSION", 0x15);
    const std::string project_id = EnvString("DLSS5NR_PROJECT_ID", PROJECT_ID);
    const std::string engine_version = EnvString("DLSS5NR_ENGINE_VERSION", "0.1");

    NGXResult init_result = 0;
    bool inited = false;
    if (init_project) {
        init_result = init_project(project_id.c_str(), 0, engine_version.c_str(),
                                   runtime.c_str(), device.Get(), ver, &fci);
        inited = (init_result == NGX_SUCCESS);
        Log("Init_ProjectID -> 0x%08X", static_cast<unsigned>(init_result));
    }
    if (!inited && init_ext) {
        // Retrying init in the same process is not safe on every driver build,
        // but for a probe that is about to exit either way it is worth the
        // extra datapoint about which route the core accepts.
        init_result = init_ext(0x24480451ULL, runtime.c_str(), device.Get(), ver, &fci);
        inited = (init_result == NGX_SUCCESS);
        Log("Init_Ext -> 0x%08X", static_cast<unsigned>(init_result));
    }
    if (!inited) {
        Detail("NGX initialization failed (0x%08X)", static_cast<unsigned>(init_result));
        return false;
    }

    NGXParameter* params = nullptr;
    NGXResult r = get_caps(&params);
    if (r != NGX_SUCCESS || !params) {
        Detail("GetCapabilityParameters failed (0x%08X)", static_cast<unsigned>(r));
        shutdown();
        return false;
    }

    int available = 0;
    r = params->Get(KEY_AVAILABLE, &available);
    Log("%s -> 0x%08X, value %d", KEY_AVAILABLE, static_cast<unsigned>(r), available);
    out->available = (r == NGX_SUCCESS && available != 0);

    // Absent or <= 1 means single-frame generation only. The client treats
    // this as the cap on generated frames per interval, so a missing key must
    // read as "2x only", never as "unlimited".
    unsigned int mfg_max = 0;
    r = params->Get(KEY_MFG_MAX, &mfg_max);
    if (r != NGX_SUCCESS) mfg_max = 0;
    out->multi_frame_count_max = mfg_max;
    Log("%s -> 0x%08X, value %u", KEY_MFG_MAX, static_cast<unsigned>(r), mfg_max);

    if (!out->available) {
        // The guide points at FrameGeneration.FeatureInitResult for the reason,
        // which is the single most useful number when this fails.
        int why = 0;
        if (params->Get("FrameGeneration.FeatureInitResult", &why) == NGX_SUCCESS)
            Detail("feature %d reported unavailable, FeatureInitResult 0x%08X",
                   FG_FEATURE_ID, static_cast<unsigned>(why));
        else
            Detail("feature %d reported unavailable, no FeatureInitResult", FG_FEATURE_ID);
    } else {
        Detail("feature %d available on %s", FG_FEATURE_ID, g_gpu_name.c_str());
    }

    if (destroy_params) destroy_params(params);
    shutdown();
    return out->available;
}

// ------------------------------------------------------------------ main --

static void Usage() {
    fprintf(stderr,
            "Usage: dlssg_host.exe --probe [--adapter-luid <16hex>] [--gpu-index N]\n"
            "\n"
            "Probe-only build: reports whether NGX feature 11 (FrameGeneration)\n"
            "initializes and is available. --serve is not implemented yet; see\n"
            "docs/DLSSG-HOST.md for the full scope.\n");
}

int wmain(int argc, wchar_t** argv) {
    bool want_probe = false;
    bool want_serve = false;
    int gpu_index = EnvInt("DLSS5NR_GPU_INDEX", 0);
    LUID luid{};
    bool have_luid = false;

    for (int i = 1; i < argc; ++i) {
        std::wstring a = argv[i];
        if (a == L"--probe") want_probe = true;
        else if (a == L"--serve") want_serve = true;
        else if (a == L"--adapter-luid" && i + 1 < argc) {
            unsigned long long v = wcstoull(argv[++i], nullptr, 16);
            luid.LowPart = static_cast<DWORD>(v & 0xFFFFFFFFULL);
            luid.HighPart = static_cast<LONG>(static_cast<int32_t>(v >> 32));
            have_luid = true;
        } else if (a == L"--gpu-index" && i + 1 < argc) {
            gpu_index = static_cast<int>(wcstol(argv[++i], nullptr, 10));
        } else {
            Usage();
            return 2;
        }
    }

    if (want_serve) {
        // Fail loudly and immediately. The client would otherwise block on a
        // SETUP reply that is never coming.
        fprintf(stderr, "[dlssg] --serve is not implemented in this probe-only build.\n");
        return 3;
    }
    if (!want_probe) { Usage(); return 2; }

    ProbeResult result;
    const bool ok = Probe(gpu_index, have_luid ? &luid : nullptr, &result);

    // One JSON line on stdout, shaped like the reference worker's so a doctor
    // check can consume either. runtime_version is not reported: the reference
    // hardcodes its build's number, and guessing is worse than omitting.
    printf("{\"available\":%s,\"multi_frame_count_max\":%u,\"worker_version\":\"probe-1\","
           "\"gpu\":\"%s\",\"detail\":\"%s\"}\n",
           ok ? "true" : "false",
           result.multi_frame_count_max,
           JsonEscape(g_gpu_name).c_str(),
           JsonEscape(g_detail).c_str());
    fflush(stdout);
    return ok ? 0 : 1;
}
