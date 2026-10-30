// paintprobe.asi: research probe for research/paint-re.md. Not shipped; loaded only in test
// runs (run-series.ps1 -Override 'paintprobe.asi=out\re\paintprobe.asi;paintprobe.ini=...').
//
// Hooks Direct3DCreate9 in Driver.exe's import table, then IDirect3D9::CreateDevice and four
// device methods through their vtables (DSF Enhanced detours the same functions' prologues in
// d3d9.dll; the vtable hooks call through to them). The device is created without
// D3DCREATE_PUREDEVICE so the probe can read the state back at a draw.
//
// For each draw whose pixel shader is one of the watched programs (the paint, chrome and glass
// programs of dngvehicles.sp, by DSF Enhanced's FNV-1a hash of the bytecode, as in
// tools/re/gpch.py), it logs:
//   - at most once per `summary_ms` per program: the pixel shader constants c0..c55;
//   - in a dump frame (every `dump_ms` from `dump_start_s` after the first Present, at most
//     `dump_count` times): every watched draw with its vertex shader, primitive count, pixel
//     constants c0..c55, vertex constants c96..c111, the sRGB-read state and the description of
//     samplers 0..5, and the sRGB-write and blend states. The scene target the paint was drawn
//     into is written as a PFM (linear float RGB) when the game switches away from it, the
//     back buffer as a BMP at the next Present, and the textures bound at samplers 1 and 2
//     (environment map, light map) as raw level-0 files (`.tex`: a text header, then the rows).
// The log goes to [probe] log= in paintprobe.ini beside the .asi ("%s" = start time), dumps to
// [probe] dir=.

#include <windows.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

namespace {

// --- minimal D3D9 ABI (no d3d9.h in this SDK) ---------------------------------------------

typedef void* IFace;
inline void** vt(IFace p) { return *reinterpret_cast<void***>(p); }

struct SurfaceDesc {
    UINT Format, Type;
    DWORD Usage;
    UINT Pool, MultiSampleType;
    DWORD MultiSampleQuality;
    UINT Width, Height;
};
struct LockedRect {
    INT Pitch;
    void* pBits;
};

const UINT FMT_A16B16G16R16F = 113, FMT_A32B32G32R32F = 116, FMT_X8R8G8B8 = 22, FMT_A8R8G8B8 = 21;
const UINT POOL_DEFAULT = 0, POOL_MANAGED = 1, POOL_SYSTEMMEM = 2;
const DWORD USAGE_RENDERTARGET = 1;
const UINT RTYPE_TEXTURE = 3, RTYPE_CUBETEXTURE = 5;
const DWORD SAMP_SRGBTEXTURE = 11, RS_SRGBWRITEENABLE = 194, RS_ALPHABLENDENABLE = 27, RS_SRCBLEND = 19,
            RS_DESTBLEND = 20;
const DWORD CREATE_PUREDEVICE = 0x10;

// IDirect3DDevice9 vtable slots
enum {
    DEV_Present = 17, DEV_GetBackBuffer = 18, DEV_CreateRenderTarget = 28, DEV_GetRenderTargetData = 32,
    DEV_StretchRect = 34, DEV_CreateOffscreenPlainSurface = 36, DEV_SetRenderTarget = 37, DEV_GetRenderTarget = 38,
    DEV_GetRenderState = 58, DEV_GetTexture = 64, DEV_GetSamplerState = 68, DEV_DrawIndexedPrimitive = 82,
    DEV_GetVertexShader = 93, DEV_GetVertexShaderConstantF = 95, DEV_GetPixelShader = 108,
    DEV_GetPixelShaderConstantF = 110,
};
// IDirect3DSurface9: GetDesc 12, LockRect 13, UnlockRect 14; textures: GetLevelDesc 17,
// GetSurfaceLevel / GetCubeMapSurface 18; resources: GetType 10; shaders: GetFunction 4.

typedef ULONG(__stdcall* ReleaseFn)(IFace);
ULONG release(IFace p) { return p ? reinterpret_cast<ReleaseFn>(vt(p)[2])(p) : 0; }

typedef HRESULT(__stdcall* GetDescFn)(IFace, SurfaceDesc*);
typedef HRESULT(__stdcall* LockRectFn)(IFace, LockedRect*, const RECT*, DWORD);
typedef HRESULT(__stdcall* UnlockRectFn)(IFace);
typedef UINT(__stdcall* GetTypeFn)(IFace);
typedef HRESULT(__stdcall* GetLevelDescFn)(IFace, UINT, SurfaceDesc*);
typedef HRESULT(__stdcall* GetSurfaceLevelFn)(IFace, UINT, IFace*);
typedef HRESULT(__stdcall* GetCubeSurfaceFn)(IFace, UINT, UINT, IFace*);
typedef HRESULT(__stdcall* GetFunctionFn)(IFace, void*, UINT*);

typedef HRESULT(__stdcall* PresentFn)(IFace, const RECT*, const RECT*, HWND, const void*);
typedef HRESULT(__stdcall* GetBackBufferFn)(IFace, UINT, UINT, UINT, IFace*);
typedef HRESULT(__stdcall* CreateRenderTargetFn)(IFace, UINT, UINT, UINT, UINT, DWORD, BOOL, IFace*, HANDLE*);
typedef HRESULT(__stdcall* GetRenderTargetDataFn)(IFace, IFace, IFace);
typedef HRESULT(__stdcall* StretchRectFn)(IFace, IFace, const RECT*, IFace, const RECT*, UINT);
typedef HRESULT(__stdcall* CreateOffscreenFn)(IFace, UINT, UINT, UINT, UINT, IFace*, HANDLE*);
typedef HRESULT(__stdcall* SetRenderTargetFn)(IFace, DWORD, IFace);
typedef HRESULT(__stdcall* GetRenderTargetFn)(IFace, DWORD, IFace*);
typedef HRESULT(__stdcall* GetRenderStateFn)(IFace, DWORD, DWORD*);
typedef HRESULT(__stdcall* GetTextureFn)(IFace, DWORD, IFace*);
typedef HRESULT(__stdcall* GetSamplerStateFn)(IFace, DWORD, DWORD, DWORD*);
typedef HRESULT(__stdcall* DrawIndexedFn)(IFace, UINT, INT, UINT, UINT, UINT, UINT);
typedef HRESULT(__stdcall* GetShaderFn)(IFace, IFace*);
typedef HRESULT(__stdcall* GetConstFn)(IFace, UINT, float*, UINT);
typedef HRESULT(__stdcall* CreateDeviceFn)(IFace, UINT, UINT, HWND, DWORD, void*, IFace*);
typedef IFace(__stdcall* Create9Fn)(UINT);

template <class F> F slot(IFace p, int i) { return reinterpret_cast<F>(vt(p)[i]); }

// --- watched programs ----------------------------------------------------------------------

struct Program {
    uint32_t hash;
    const char* name;
};
const Program kPrograms[] = {
    {0x1B046073, "gp14_chassis_p0"}, {0x4056BE55, "gp15_chassis_p1"}, {0x6B170018, "gp16_chassis_p3"},
    {0x4B2A5476, "gp17_chassis_p4"}, {0x61228AA0, "gp18_chassis_p5"}, {0x4CFBFAB5, "gp19_chassis_p6"},
    {0x761B0520, "gp26_chassisLOD1_p0"}, {0xCA2F4856, "gp27_chassisLOD1_p1"}, {0x9E787D51, "gp28_chassisLOD1_p3"},
    {0x290EAF9F, "gp29_chassisLOD1_p4"}, {0xC166AB4F, "gp20_chrome_p0"}, {0xA7BF28A6, "gp21_chrome_p1"},
    {0xEB103BB1, "gp66_lodchrome_p0"}, {0x328CDAF7, "gp67_lodchrome_p1"}, {0x59ECEF29, "gp46_glass_p0"},
    {0x30EEC3BF, "gp47_glass_p1"}, {0xEA6D0AC2, "gp33_simple_p0"}, {0x00CC7511, "gp41_simpleLOD1_p0"},
};
const int kNumPrograms = sizeof(kPrograms) / sizeof(kPrograms[0]);
const int kPaintPrograms = 10;  // the first ten: paint (they get the env/lightmap dumps)

uint32_t fnv(const uint8_t* p, size_t n)
{
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < n; ++i) {
        h = (h ^ p[i]) * 16777619u;
    }
    return h;
}

struct ShaderId {
    IFace ptr;
    uint32_t hash;
    int program;  // index into kPrograms, -1 none
};
ShaderId g_ids[4096];
int g_nids = 0;

uint32_t shader_hash(IFace sh)
{
    UINT size = 0;
    if (FAILED(slot<GetFunctionFn>(sh, 4)(sh, nullptr, &size)) || size == 0 || size > (1u << 20)) {
        return 0;
    }
    uint8_t* buf = static_cast<uint8_t*>(malloc(size));
    uint32_t h = 0;
    if (SUCCEEDED(slot<GetFunctionFn>(sh, 4)(sh, buf, &size))) {
        h = fnv(buf, size);
    }
    free(buf);
    return h;
}

const ShaderId* identify(IFace sh)
{
    for (int i = 0; i < g_nids; ++i) {
        if (g_ids[i].ptr == sh) {
            return &g_ids[i];
        }
    }
    ShaderId id = {sh, shader_hash(sh), -1};
    for (int k = 0; k < kNumPrograms; ++k) {
        if (kPrograms[k].hash == id.hash) {
            id.program = k;
        }
    }
    if (g_nids < 4096) {
        g_ids[g_nids] = id;
        return &g_ids[g_nids++];
    }
    g_ids[4095] = id;
    return &g_ids[4095];
}

// --- state --------------------------------------------------------------------------------

FILE* g_log = nullptr;
char g_dir[MAX_PATH];
ULONGLONG g_start = 0;  // first Present
DWORD g_summary_ms = 1000, g_dump_ms = 3000, g_dump_start_s = 60, g_dump_count = 100, g_draw_cap = 96;
long g_lines_left = 400000;
ULONGLONG g_last_summary[32];
ULONGLONG g_last_dump = 0;
DWORD g_dumps = 0;
DWORD g_frame = 0;
DWORD g_draws_since = 0;
bool g_dump_frame = false;
IFace g_scene_rt = nullptr;  // AddRef'd: the target the paint went into this dump frame
bool g_scene_written = false;
int g_frame_draws = 0;
IFace g_tex_seen[64];
int g_ntex_seen = 0;

void* g_orig[128];
void* g_first[128];  // the first original seen per slot: called on re-entry (another hook wrapped ours)
__declspec(thread) int t_in_draw = 0, t_in_rt = 0, t_in_present = 0;
Create9Fn g_create9 = nullptr;
CreateDeviceFn g_create_device = nullptr;

void logf(const char* fmt, ...)
{
    if (!g_log || g_lines_left <= 0) {
        return;
    }
    --g_lines_left;
    va_list ap;
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fputc('\n', g_log);
}

double now_s() { return g_start ? (GetTickCount64() - g_start) / 1000.0 : 0.0; }

void stamp(char* out, size_t n)
{
    SYSTEMTIME st;
    GetLocalTime(&st);
    sprintf_s(out, n, "%02d:%02d:%02d.%03d", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
}

void log_consts(const char* tag, const float* c, int first, int count)
{
    char line[8192];
    int at = sprintf_s(line, "  %s", tag);
    for (int i = 0; i < count && at < 7900; ++i) {
        const float* v = c + 4 * i;
        at += sprintf_s(line + at, sizeof(line) - at, " c%d=%.6g,%.6g,%.6g,%.6g", first + i, v[0], v[1], v[2], v[3]);
    }
    logf("%s", line);
}

// --- dumps --------------------------------------------------------------------------------

float half_to_float(uint16_t h)
{
    uint32_t s = (h >> 15) & 1, e = (h >> 10) & 31, m = h & 1023;
    float f;
    if (e == 0) {
        f = ldexpf(static_cast<float>(m), -24);
    } else if (e == 31) {
        f = m ? 0.0f : 65504.0f;
    } else {
        f = ldexpf(static_cast<float>(m | 1024), static_cast<int>(e) - 25);
    }
    return s ? -f : f;
}

// A readable copy of a render-target surface (system memory), or null.
IFace readback(IFace dev, IFace surf, SurfaceDesc* d)
{
    if (FAILED(slot<GetDescFn>(surf, 12)(surf, d))) {
        return nullptr;
    }
    IFace src = surf, tmp = nullptr;
    if (d->MultiSampleType != 0) {
        if (FAILED(slot<CreateRenderTargetFn>(dev, DEV_CreateRenderTarget)(dev, d->Width, d->Height, d->Format, 0, 0,
                FALSE, &tmp, nullptr)) ||
            FAILED(slot<StretchRectFn>(dev, DEV_StretchRect)(dev, surf, nullptr, tmp, nullptr, 0))) {
            release(tmp);
            return nullptr;
        }
        src = tmp;
    }
    IFace sys = nullptr;
    HRESULT hr = slot<CreateOffscreenFn>(dev, DEV_CreateOffscreenPlainSurface)(dev, d->Width, d->Height, d->Format,
        POOL_SYSTEMMEM, &sys, nullptr);
    if (SUCCEEDED(hr)) {
        hr = reinterpret_cast<GetRenderTargetDataFn>(g_orig[DEV_GetRenderTargetData])(dev, src, sys);
    }
    release(tmp);
    if (FAILED(hr)) {
        release(sys);
        return nullptr;
    }
    return sys;
}

bool write_pfm(IFace dev, IFace surf, const char* path)
{
    SurfaceDesc d;
    IFace sys = readback(dev, surf, &d);
    if (!sys) {
        return false;
    }
    bool ok = false;
    LockedRect lr;
    if ((d.Format == FMT_A16B16G16R16F || d.Format == FMT_A32B32G32R32F) &&
        SUCCEEDED(slot<LockRectFn>(sys, 13)(sys, &lr, nullptr, 0x10))) {
        FILE* f = nullptr;
        fopen_s(&f, path, "wb");
        if (f) {
            fprintf(f, "PF\n%u %u\n-1.0\n", d.Width, d.Height);
            float* row = static_cast<float*>(malloc(d.Width * 12));
            for (int y = static_cast<int>(d.Height) - 1; y >= 0; --y) {  // PFM rows run bottom to top
                const uint8_t* src = static_cast<const uint8_t*>(lr.pBits) + y * lr.Pitch;
                for (UINT x = 0; x < d.Width; ++x) {
                    for (int c = 0; c < 3; ++c) {
                        row[3 * x + c] = d.Format == FMT_A16B16G16R16F
                            ? half_to_float(reinterpret_cast<const uint16_t*>(src)[4 * x + c])
                            : reinterpret_cast<const float*>(src)[4 * x + c];
                    }
                }
                fwrite(row, 12, d.Width, f);
            }
            free(row);
            fclose(f);
            ok = true;
        }
        slot<UnlockRectFn>(sys, 14)(sys);
    }
    logf("  dump %s %s fmt=%u %ux%u ms=%u", path, ok ? "ok" : "FAILED", d.Format, d.Width, d.Height, d.MultiSampleType);
    release(sys);
    return ok;
}

bool write_bmp(IFace dev, IFace surf, const char* path)
{
    SurfaceDesc d;
    IFace sys = readback(dev, surf, &d);
    if (!sys) {
        logf("  dump %s FAILED (readback)", path);
        return false;
    }
    bool ok = false;
    LockedRect lr;
    if ((d.Format == FMT_X8R8G8B8 || d.Format == FMT_A8R8G8B8) &&
        SUCCEEDED(slot<LockRectFn>(sys, 13)(sys, &lr, nullptr, 0x10))) {
        FILE* f = nullptr;
        fopen_s(&f, path, "wb");
        if (f) {
            UINT stride = (d.Width * 3 + 3) & ~3u;
            BITMAPFILEHEADER fh = {};
            BITMAPINFOHEADER ih = {};
            fh.bfType = 0x4D42;
            fh.bfOffBits = sizeof(fh) + sizeof(ih);
            fh.bfSize = fh.bfOffBits + stride * d.Height;
            ih.biSize = sizeof(ih);
            ih.biWidth = d.Width;
            ih.biHeight = d.Height;
            ih.biPlanes = 1;
            ih.biBitCount = 24;
            fwrite(&fh, sizeof(fh), 1, f);
            fwrite(&ih, sizeof(ih), 1, f);
            uint8_t* row = static_cast<uint8_t*>(calloc(stride, 1));
            for (int y = static_cast<int>(d.Height) - 1; y >= 0; --y) {
                const uint8_t* src = static_cast<const uint8_t*>(lr.pBits) + y * lr.Pitch;
                for (UINT x = 0; x < d.Width; ++x) {
                    memcpy(row + 3 * x, src + 4 * x, 3);
                }
                fwrite(row, 1, stride, f);
            }
            free(row);
            fclose(f);
            ok = true;
        }
        slot<UnlockRectFn>(sys, 14)(sys);
    }
    logf("  dump %s %s fmt=%u %ux%u", path, ok ? "ok" : "FAILED", d.Format, d.Width, d.Height);
    release(sys);
    return ok;
}

// Level 0 of a texture (each cube face in turn) as raw rows after a one-line header.
struct VolumeDesc {
    UINT Format, Type;
    DWORD Usage;
    UINT Pool, Width, Height, Depth;
};
struct LockedBox {
    INT RowPitch, SlicePitch;
    void* pBits;
};
typedef HRESULT(__stdcall* GetVolumeDescFn)(IFace, UINT, VolumeDesc*);
typedef HRESULT(__stdcall* LockBoxFn)(IFace, UINT, LockedBox*, const void*, DWORD);
typedef HRESULT(__stdcall* UnlockBoxFn)(IFace, UINT);

// Level 0 of a volume texture (the colour-grading LUTs): header line, then depth x height rows.
void write_volume(IFace tex, const char* path)
{
    VolumeDesc d = {};
    slot<GetVolumeDescFn>(tex, 17)(tex, 0, &d);
    LockedBox lb;
    if (FAILED(slot<LockBoxFn>(tex, 19)(tex, 0, &lb, nullptr, 0x10))) {
        logf("  dump %s volume FAILED fmt=%u %ux%ux%u pool=%u", path, d.Format, d.Width, d.Height, d.Depth, d.Pool);
        return;
    }
    FILE* f = nullptr;
    fopen_s(&f, path, "wb");
    if (f) {
        fprintf(f, "VOL fmt=%u w=%u h=%u d=%u rowpitch=%d slicepitch=%d\n", d.Format, d.Width, d.Height, d.Depth,
            lb.RowPitch, lb.SlicePitch);
        for (UINT z = 0; z < d.Depth; ++z) {
            for (UINT y = 0; y < d.Height; ++y) {
                fwrite(static_cast<uint8_t*>(lb.pBits) + z * lb.SlicePitch + y * lb.RowPitch, 1, lb.RowPitch, f);
            }
        }
        fclose(f);
    }
    slot<UnlockBoxFn>(tex, 20)(tex, 0);
    logf("  dump %s volume fmt=%u %ux%ux%u pool=%u", path, d.Format, d.Width, d.Height, d.Depth, d.Pool);
}

void write_texture(IFace dev, IFace tex, const char* path)
{
    UINT type = slot<GetTypeFn>(tex, 10)(tex);
    if (type == 4) {
        write_volume(tex, path);
        return;
    }
    int faces = type == RTYPE_CUBETEXTURE ? 6 : (type == RTYPE_TEXTURE ? 1 : 0);
    FILE* f = nullptr;
    for (int face = 0; face < faces; ++face) {
        IFace surf = nullptr;
        HRESULT hr = type == RTYPE_CUBETEXTURE ? slot<GetCubeSurfaceFn>(tex, 18)(tex, face, 0, &surf)
                                                : slot<GetSurfaceLevelFn>(tex, 18)(tex, 0, &surf);
        if (FAILED(hr)) {
            break;
        }
        SurfaceDesc d;
        slot<GetDescFn>(surf, 12)(surf, &d);
        IFace src = nullptr;
        if (d.Pool == POOL_DEFAULT && (d.Usage & USAGE_RENDERTARGET)) {
            src = readback(dev, surf, &d);
        } else if (d.Pool != POOL_DEFAULT) {
            src = surf;
            reinterpret_cast<ULONG(__stdcall*)(IFace)>(vt(surf)[1])(surf);  // AddRef
        }
        LockedRect lr;
        if (src && SUCCEEDED(slot<LockRectFn>(src, 13)(src, &lr, nullptr, 0x10))) {
            bool block = d.Format == 0x31545844 || d.Format == 0x33545844 || d.Format == 0x35545844;  // DXT1/3/5
            UINT rows = block ? (d.Height + 3) / 4 : d.Height;
            if (!f) {
                fopen_s(&f, path, "wb");
                if (f) {
                    fprintf(f, "TEX type=%u faces=%d fmt=%u w=%u h=%u pitch=%d rows=%u\n", type, faces, d.Format, d.Width,
                        d.Height, lr.Pitch, rows);
                }
            }
            if (f) {
                for (UINT y = 0; y < rows; ++y) {
                    fwrite(static_cast<uint8_t*>(lr.pBits) + y * lr.Pitch, 1, lr.Pitch, f);
                }
            }
            slot<UnlockRectFn>(src, 14)(src);
            logf("  dump %s face %d fmt=%u %ux%u pool=%u usage=%lu", path, face, d.Format, d.Width, d.Height, d.Pool,
                d.Usage);
        } else {
            logf("  dump %s face %d FAILED fmt=%u %ux%u pool=%u usage=%lu", path, face, d.Format, d.Width, d.Height,
                d.Pool, d.Usage);
        }
        release(src);
        release(surf);
    }
    if (f) {
        fclose(f);
    }
}

// --- hooks --------------------------------------------------------------------------------

void describe_samplers(IFace dev, char* out, size_t n)
{
    int at = 0;
    for (DWORD s = 0; s < 6; ++s) {
        DWORD srgb = 0;
        slot<GetSamplerStateFn>(dev, DEV_GetSamplerState)(dev, s, SAMP_SRGBTEXTURE, &srgb);
        IFace tex = nullptr;
        slot<GetTextureFn>(dev, DEV_GetTexture)(dev, s, &tex);
        if (!tex) {
            at += sprintf_s(out + at, n - at, " s%lu=- srgb=%lu", s, srgb);
            continue;
        }
        UINT type = slot<GetTypeFn>(tex, 10)(tex);
        SurfaceDesc d = {};
        slot<GetLevelDescFn>(tex, 17)(tex, 0, &d);
        at += sprintf_s(out + at, n - at, " s%lu=%p:t%u:f%u:%ux%u:p%u:u%lu srgb=%lu", s, tex, type, d.Format, d.Width,
            d.Height, d.Pool, d.Usage, srgb);
        release(tex);
    }
}

HRESULT __stdcall draw_indexed_body(IFace dev, UINT type, INT base, UINT minv, UINT nverts, UINT start, UINT prims);
int g_tone_dumps = 0;

void describe_samplers16(IFace dev, char* out, size_t n)
{
    int at = 0;
    for (DWORD s = 0; s < 16 && at < static_cast<int>(n) - 64; ++s) {
        IFace tex = nullptr;
        slot<GetTextureFn>(dev, DEV_GetTexture)(dev, s, &tex);
        if (!tex) {
            continue;
        }
        UINT type = slot<GetTypeFn>(tex, 10)(tex);
        DWORD srgb = 0;
        slot<GetSamplerStateFn>(dev, DEV_GetSamplerState)(dev, s, SAMP_SRGBTEXTURE, &srgb);
        at += sprintf_s(out + at, n - at, " s%lu=%p:t%u srgb=%lu", s, tex, type, srgb);
        release(tex);
    }
}

HRESULT __stdcall draw_indexed(IFace dev, UINT type, INT base, UINT minv, UINT nverts, UINT start, UINT prims)
{
    if (t_in_draw) {
        return reinterpret_cast<DrawIndexedFn>(g_orig[DEV_DrawIndexedPrimitive])(dev, type, base, minv, nverts, start,
            prims);
    }
    t_in_draw = 1;
    HRESULT hr = draw_indexed_body(dev, type, base, minv, nverts, start, prims);
    t_in_draw = 0;
    return hr;
}

HRESULT __stdcall draw_indexed_body(IFace dev, UINT type, INT base, UINT minv, UINT nverts, UINT start, UINT prims)
{
    ++g_draws_since;
    IFace ps = nullptr;
    slot<GetShaderFn>(dev, DEV_GetPixelShader)(dev, &ps);
    if (ps) {
        const ShaderId* id = identify(ps);
        release(ps);
        if (id->hash == 0x2F0359F4 && g_tone_dumps < 3 && g_start && GetTickCount64() - g_start > 60000ull * (g_tone_dumps + 1)) {
            // the post chain's tone map (research/lights-re.md): its samplers (the colour-grading
            // LUTs g_samColourGradingFrom/To among them) and constants
            ++g_tone_dumps;
            char samp[1024];
            describe_samplers16(dev, samp, sizeof(samp));
            logf("tonemap f=%lu%s", g_frame, samp);
            float c[32 * 4];
            slot<GetConstFn>(dev, DEV_GetPixelShaderConstantF)(dev, 0, c, 32);
            log_consts("ps", c, 0, 32);
            slot<GetConstFn>(dev, DEV_GetVertexShaderConstantF)(dev, 0, c, 8);
            log_consts("vs", c, 0, 8);
            for (DWORD sidx = 0; sidx < 16; ++sidx) {
                IFace tex = nullptr;
                slot<GetTextureFn>(dev, DEV_GetTexture)(dev, sidx, &tex);
                if (!tex) {
                    continue;
                }
                UINT type = slot<GetTypeFn>(tex, 10)(tex);
                if (type == 4 || g_tone_dumps == 1) {
                    char path[MAX_PATH];
                    sprintf_s(path, "%s\\tone%d-s%lu-%p.tex", g_dir, g_tone_dumps, sidx, tex);
                    write_texture(dev, tex, path);
                }
                release(tex);
            }
            fflush(g_log);
        }
        if (id->program >= 0) {
            ULONGLONG t = GetTickCount64();
            float c[56 * 4];
            if (g_dump_frame && g_frame_draws < static_cast<int>(g_draw_cap)) {
                ++g_frame_draws;
                IFace vs = nullptr;
                slot<GetShaderFn>(dev, DEV_GetVertexShader)(dev, &vs);
                uint32_t vh = vs ? identify(vs)->hash : 0;
                release(vs);
                DWORD srgbw = 0, blend = 0, sb = 0, db = 0;
                slot<GetRenderStateFn>(dev, DEV_GetRenderState)(dev, RS_SRGBWRITEENABLE, &srgbw);
                slot<GetRenderStateFn>(dev, DEV_GetRenderState)(dev, RS_ALPHABLENDENABLE, &blend);
                slot<GetRenderStateFn>(dev, DEV_GetRenderState)(dev, RS_SRCBLEND, &sb);
                slot<GetRenderStateFn>(dev, DEV_GetRenderState)(dev, RS_DESTBLEND, &db);
                IFace rt = nullptr;
                slot<GetRenderTargetFn>(dev, DEV_GetRenderTarget)(dev, 0, &rt);
                char samp[1024];
                describe_samplers(dev, samp, sizeof(samp));
                logf(" draw f=%lu %s ps=%08X vs=%08X prims=%u verts=%u start=%u rt=%p srgbwrite=%lu blend=%lu/%lu/%lu%s",
                    g_frame, kPrograms[id->program].name, id->hash, vh, prims, nverts, start, rt, srgbw, blend, sb, db,
                    samp);
                slot<GetConstFn>(dev, DEV_GetPixelShaderConstantF)(dev, 0, c, 56);
                log_consts("ps", c, 0, 56);
                float v[16 * 4];
                slot<GetConstFn>(dev, DEV_GetVertexShaderConstantF)(dev, 96, v, 16);
                log_consts("vs", v, 96, 16);
                slot<GetConstFn>(dev, DEV_GetVertexShaderConstantF)(dev, 0, v, 12);
                log_consts("panels", v, 0, 12);
                if (id->program < kPaintPrograms && rt) {
                    if (!g_scene_rt) {
                        g_scene_rt = rt;
                        reinterpret_cast<ULONG(__stdcall*)(IFace)>(vt(rt)[1])(rt);
                    }
                    for (DWORD s = 1; s <= 2; ++s) {
                        IFace tex = nullptr;
                        slot<GetTextureFn>(dev, DEV_GetTexture)(dev, s, &tex);
                        if (!tex) {
                            continue;
                        }
                        SurfaceDesc d = {};
                        slot<GetLevelDescFn>(tex, 17)(tex, 0, &d);
                        bool dynamic = (d.Usage & USAGE_RENDERTARGET) != 0;
                        bool seen = false;
                        for (int i = 0; i < g_ntex_seen; ++i) {
                            seen |= g_tex_seen[i] == tex;
                        }
                        if ((dynamic && s == 1) || !seen) {
                            char path[MAX_PATH];
                            sprintf_s(path, "%s\\f%06lu-s%lu-%p.tex", g_dir, g_frame, s, tex);
                            write_texture(dev, tex, path);
                            if (!seen && g_ntex_seen < 64) {
                                g_tex_seen[g_ntex_seen++] = tex;
                            }
                        }
                        release(tex);
                    }
                }
                release(rt);
            } else if (t - g_last_summary[id->program] >= g_summary_ms) {
                g_last_summary[id->program] = t;
                slot<GetConstFn>(dev, DEV_GetPixelShaderConstantF)(dev, 0, c, 56);
                char ts[16];
                stamp(ts, sizeof(ts));
                logf("sum t=%.2f %s f=%lu %s prims=%u", now_s(), ts, g_frame, kPrograms[id->program].name, prims);
                log_consts("ps", c, 0, 56);
            }
        }
    }
    return reinterpret_cast<DrawIndexedFn>(g_orig[DEV_DrawIndexedPrimitive])(dev, type, base, minv, nverts, start, prims);
}

void write_scene(IFace dev)
{
    if (g_scene_rt && !g_scene_written) {
        char path[MAX_PATH];
        sprintf_s(path, "%s\\f%06lu-scene.pfm", g_dir, g_frame);
        write_pfm(dev, g_scene_rt, path);
        g_scene_written = true;
    }
}

HRESULT __stdcall set_render_target(IFace dev, DWORD index, IFace surf)
{
    if (t_in_rt) {
        return reinterpret_cast<SetRenderTargetFn>(g_orig[DEV_SetRenderTarget])(dev, index, surf);
    }
    t_in_rt = 1;
    if (g_dump_frame && index == 0 && g_scene_rt && !g_scene_written && surf != g_scene_rt) {
        write_scene(dev);
    }
    HRESULT hr = reinterpret_cast<SetRenderTargetFn>(g_orig[DEV_SetRenderTarget])(dev, index, surf);
    t_in_rt = 0;
    return hr;
}

void end_frame(IFace dev);

HRESULT __stdcall present(IFace dev, const RECT* a, const RECT* b, HWND w, const void* r)
{
    if (t_in_present) {
        return reinterpret_cast<PresentFn>(g_orig[DEV_Present])(dev, a, b, w, r);
    }
    t_in_present = 1;
    end_frame(dev);
    HRESULT hr = reinterpret_cast<PresentFn>(g_orig[DEV_Present])(dev, a, b, w, r);
    t_in_present = 0;
    return hr;
}

typedef HRESULT(__stdcall* ChainPresentFn)(IFace, const RECT*, const RECT*, HWND, const void*, DWORD);
ChainPresentFn g_chain_present = nullptr;
IFace g_dev = nullptr;

HRESULT __stdcall chain_present(IFace chain, const RECT* a, const RECT* b, HWND w, const void* r, DWORD flags)
{
    if (g_dev) {
        end_frame(g_dev);
    }
    return g_chain_present(chain, a, b, w, r, flags);
}

void end_frame(IFace dev)
{
    if (g_draws_since == 0 && g_start) {
        return;  // a second Present hook for the same frame (device and swap chain)
    }
    g_draws_since = 0;
    ULONGLONG t = GetTickCount64();
    if (!g_start) {
        g_start = t;
        logf("first Present");
    }
    if (g_dump_frame) {
        write_scene(dev);
        IFace bb = nullptr;
        if (SUCCEEDED(slot<GetBackBufferFn>(dev, DEV_GetBackBuffer)(dev, 0, 0, 0, &bb)) && bb) {
            char path[MAX_PATH];
            sprintf_s(path, "%s\\f%06lu-final.bmp", g_dir, g_frame);
            write_bmp(dev, bb, path);
            release(bb);
        }
        release(g_scene_rt);
        g_scene_rt = nullptr;
        logf("end dump f=%lu draws=%d", g_frame, g_frame_draws);
        fflush(g_log);
    }
    ++g_frame;
    g_dump_frame = false;
    if (g_dumps < g_dump_count && t - g_start >= g_dump_start_s * 1000ull && t - g_last_dump >= g_dump_ms) {
        g_dump_frame = true;
        g_last_dump = t;
        ++g_dumps;
        g_scene_written = false;
        g_frame_draws = 0;
        char ts[16];
        stamp(ts, sizeof(ts));
        logf("dump %lu f=%lu t=%.2f %s", g_dumps, g_frame, now_s(), ts);
    }
}

// Patches one slot of a vtable; the replaced entry becomes the original the hook calls (never
// the hook itself). Returns true if it changed the slot.
bool patch_slot(void** table, int index, void* hook, void** orig)
{
    void* cur = table[index];
    if (cur == hook) {
        return false;
    }
    *orig = cur;
    if (!g_first[index]) {
        g_first[index] = cur;
    }
    DWORD old;
    if (!VirtualProtect(&table[index], sizeof(void*), PAGE_EXECUTE_READWRITE, &old)) {
        return false;
    }
    table[index] = hook;
    VirtualProtect(&table[index], sizeof(void*), old, &old);
    FlushInstructionCache(GetCurrentProcess(), &table[index], sizeof(void*));
    return true;
}

// Where the hooks go. Something resets the device's vtable entries to the d3d9 functions about
// every 200 ms (run vm1-20261030-152849; likely d3d9's own table swaps), so by default
// ([probe] hooks=vtable) the probe re-applies its vtable hooks every 200 ms and sees most
// frames, not all. hooks=chain instead re-points the 5-byte `jmp rel32` DSF Enhanced puts at
// those d3d9 functions to the probe's hooks, which call DSF Enhanced's (the old target); every
// frame is seen, but both runs with it stalled in the story load (vm1-20261030-154458 and its
// retry), so it is not the default.
void* g_real[128];  // the d3d9 functions, from the vtable at CreateDevice
void* g_real_chain = nullptr;

bool chain_jump(void* fn, void* hook, void** orig)
{
    uint8_t* p = static_cast<uint8_t*>(fn);
    if (!p || p[0] != 0xE9) {
        return false;
    }
    int32_t rel;
    memcpy(&rel, p + 1, 4);
    void* target = p + 5 + rel;
    if (target == hook) {
        return true;
    }
    *orig = target;
    DWORD old;
    if (!VirtualProtect(p, 5, PAGE_EXECUTE_READWRITE, &old)) {
        return false;
    }
    int32_t nrel = static_cast<int32_t>(static_cast<uint8_t*>(hook) - (p + 5));
    InterlockedExchange(reinterpret_cast<volatile LONG*>(p + 1), nrel);  // x86 tolerates the unaligned exchange
    VirtualProtect(p, 5, old, &old);
    FlushInstructionCache(GetCurrentProcess(), p, 5);
    logf("hooks: d3d9 %p jumped to %p, now to the probe", fn, target);
    fflush(g_log);
    return true;
}

bool g_chained[4];
bool g_chain_mode = false;  // [probe] hooks=chain: re-point DSF Enhanced's detours; else the vtable

void hook_device(IFace dev, int tick)
{
    if (g_real[DEV_GetRenderTargetData]) {
        g_orig[DEV_GetRenderTargetData] = g_real[DEV_GetRenderTargetData];
    }
    const int slots[3] = {DEV_Present, DEV_SetRenderTarget, DEV_DrawIndexedPrimitive};
    void* hooks[3] = {reinterpret_cast<void*>(&present), reinterpret_cast<void*>(&set_render_target),
        reinterpret_cast<void*>(&draw_indexed)};
    for (int i = 0; i < 3; ++i) {
        if (g_chain_mode && !g_chained[i]) {
            g_chained[i] = chain_jump(g_real[slots[i]], hooks[i], &g_orig[slots[i]]);
        }
        if (!g_chained[i] && (!g_chain_mode || tick >= 25)) {  // vtable mode, or 5 s and no detour
            void** table = vt(dev);
            void* orig = nullptr;
            if (patch_slot(table, slots[i], hooks[i], &orig)) {
                g_orig[slots[i]] = orig;
            }
        }
    }
    if (g_chain_mode && !g_chained[3]) {
        void* orig = nullptr;
        g_chained[3] = chain_jump(g_real_chain, reinterpret_cast<void*>(&chain_present), &orig);
        if (g_chained[3]) {
            g_chain_present = reinterpret_cast<ChainPresentFn>(orig);
        }
    } else if (!g_chain_mode) {
        IFace chain = nullptr;
        typedef HRESULT(__stdcall* GetSwapChainFn)(IFace, UINT, IFace*);
        if (SUCCEEDED(slot<GetSwapChainFn>(dev, 14)(dev, 0, &chain)) && chain) {
            void* orig = nullptr;
            if (patch_slot(vt(chain), 3, reinterpret_cast<void*>(&chain_present), &orig)) {
                g_chain_present = reinterpret_cast<ChainPresentFn>(orig);
            }
            release(chain);
        }
    }
}

DWORD WINAPI watch_device(LPVOID)
{
    for (int i = 0;; ++i) {
        Sleep(200);
        if (g_dev) {
            hook_device(g_dev, i);
        }
        if (g_log && (i % 25) == 0) {
            fflush(g_log);
        }
    }
}

HRESULT __stdcall create_device(IFace d3d, UINT adapter, UINT type, HWND focus, DWORD flags, void* pp, IFace* out)
{
    DWORD use = flags & ~CREATE_PUREDEVICE;
    HRESULT hr = g_create_device(d3d, adapter, type, focus, use, pp, out);
    logf("CreateDevice flags=%08lX (used %08lX) hr=%08lX", flags, use, hr);
    if (SUCCEEDED(hr) && out && *out) {
        void** table = vt(*out);
        for (int i = 0; i < 128; ++i) {
            g_real[i] = table[i];
        }
        IFace chain = nullptr;
        typedef HRESULT(__stdcall* GetSwapChainFn)(IFace, UINT, IFace*);
        if (SUCCEEDED(slot<GetSwapChainFn>(*out, 14)(*out, 0, &chain)) && chain) {
            g_real_chain = vt(chain)[3];
            release(chain);
        }
        g_dev = *out;
        logf("device %p: Present %p SetRenderTarget %p DrawIndexedPrimitive %p chain Present %p", *out,
            g_real[DEV_Present], g_real[DEV_SetRenderTarget], g_real[DEV_DrawIndexedPrimitive], g_real_chain);
        static bool started = false;
        if (!started) {
            started = true;
            CreateThread(nullptr, 0, &watch_device, nullptr, 0, nullptr);
        }
    }
    fflush(g_log);
    return hr;
}

IFace __stdcall create9(UINT sdk)
{
    IFace d3d = g_create9(sdk);
    if (d3d) {
        void** table = vt(d3d);
        if (table[16] != reinterpret_cast<void*>(&create_device)) {
            DWORD old;
            VirtualProtect(&table[16], sizeof(void*), PAGE_EXECUTE_READWRITE, &old);
            g_create_device = reinterpret_cast<CreateDeviceFn>(table[16]);
            table[16] = reinterpret_cast<void*>(&create_device);
            VirtualProtect(&table[16], sizeof(void*), old, &old);
        }
    }
    logf("Direct3DCreate9(%u) = %p", sdk, d3d);
    fflush(g_log);
    return d3d;
}

bool patch_import()
{
    uint8_t* base = reinterpret_cast<uint8_t*>(GetModuleHandleA(nullptr));
    auto* dos = reinterpret_cast<IMAGE_DOS_HEADER*>(base);
    auto* nt = reinterpret_cast<IMAGE_NT_HEADERS*>(base + dos->e_lfanew);
    auto& dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    for (auto* imp = reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR*>(base + dir.VirtualAddress); imp->Name; ++imp) {
        if (_stricmp(reinterpret_cast<char*>(base + imp->Name), "d3d9.dll") != 0) {
            continue;
        }
        auto* names = reinterpret_cast<IMAGE_THUNK_DATA*>(base + imp->OriginalFirstThunk);
        auto* thunks = reinterpret_cast<IMAGE_THUNK_DATA*>(base + imp->FirstThunk);
        for (; names->u1.AddressOfData; ++names, ++thunks) {
            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal)) {
                continue;
            }
            auto* by = reinterpret_cast<IMAGE_IMPORT_BY_NAME*>(base + names->u1.AddressOfData);
            if (strcmp(reinterpret_cast<char*>(by->Name), "Direct3DCreate9") == 0) {
                DWORD old;
                VirtualProtect(&thunks->u1.Function, 4, PAGE_READWRITE, &old);
                g_create9 = reinterpret_cast<Create9Fn>(thunks->u1.Function);
                thunks->u1.Function = reinterpret_cast<ULONG_PTR>(&create9);
                VirtualProtect(&thunks->u1.Function, 4, old, &old);
                return true;
            }
        }
    }
    return false;
}

void make_dirs(const char* path)
{
    char buf[MAX_PATH];
    strcpy_s(buf, path);
    for (char* p = buf + 3; *p; ++p) {
        if (*p == '\\') {
            *p = '\0';
            CreateDirectoryA(buf, nullptr);
            *p = '\\';
        }
    }
    CreateDirectoryA(buf, nullptr);
}

void start(HMODULE self)
{
    char dir[MAX_PATH];
    GetModuleFileNameA(self, dir, MAX_PATH);
    char* slash = strrchr(dir, '\\');
    if (slash) {
        *slash = '\0';
    }
    char ini[MAX_PATH], pattern[MAX_PATH], path[MAX_PATH], stamp_s[32], def[MAX_PATH];
    sprintf_s(ini, "%s\\paintprobe.ini", dir);
    sprintf_s(def, "%s\\paintprobe.log", dir);
    GetPrivateProfileStringA("probe", "log", def, pattern, MAX_PATH, ini);
    GetPrivateProfileStringA("probe", "dir", dir, g_dir, MAX_PATH, ini);
    g_summary_ms = GetPrivateProfileIntA("probe", "summary_ms", 1000, ini);
    g_dump_ms = GetPrivateProfileIntA("probe", "dump_ms", 3000, ini);
    g_dump_start_s = GetPrivateProfileIntA("probe", "dump_start_s", 60, ini);
    g_dump_count = GetPrivateProfileIntA("probe", "dump_count", 100, ini);
    g_draw_cap = GetPrivateProfileIntA("probe", "draw_cap", 96, ini);
    char mode[16];
    GetPrivateProfileStringA("probe", "hooks", "vtable", mode, sizeof(mode), ini);
    g_chain_mode = _stricmp(mode, "chain") == 0;
    g_lines_left = GetPrivateProfileIntA("probe", "max_lines", 400000, ini);
    make_dirs(g_dir);
    SYSTEMTIME st;
    GetLocalTime(&st);
    sprintf_s(stamp_s, "%04d%02d%02d-%02d%02d%02d", st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
    const char* s = strstr(pattern, "%s");
    if (s) {
        sprintf_s(path, "%.*s%s%s", static_cast<int>(s - pattern), pattern, stamp_s, s + 2);
    } else {
        strcpy_s(path, pattern);
    }
    char parent[MAX_PATH];
    strcpy_s(parent, path);
    if (char* cut = strrchr(parent, '\\')) {
        *cut = '\0';
        make_dirs(parent);
    }
    fopen_s(&g_log, path, "w");
    bool ok = patch_import();
    logf("paintprobe %s: Direct3DCreate9 import %s; dumps every %lu ms from %lu s, %lu at most, to %s", stamp_s,
        ok ? "hooked" : "NOT FOUND", g_dump_ms, g_dump_start_s, g_dump_count, g_dir);
    if (g_log) {
        fflush(g_log);
    }
}

}  // namespace

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(module);
        start(module);
    }
    return TRUE;
}
