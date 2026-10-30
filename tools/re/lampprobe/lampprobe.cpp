// lampprobe.asi: research probe for research/lights-re.md. Not shipped; loaded only in test
// runs (run-series.ps1 -Override 'lampprobe.asi=out\re\lampprobe.asi;lampprobe.ini=...').
//
// Driver.exe 1.04 fills the Lights vertex shader's g_v4LightColours[32] (c160) in
// 0x008A78E0(float4* out, LampComponent* lamps, float4* tint), called from 0x0087AB68 and
// 0x008A9184. The probe redirects both calls to a wrapper that calls the original and then,
// at most every `interval_ms` per lamp component, logs:
//   - the tint vector and its address, and the 0x200-byte MoodParametersStruct that holds it
//     when the tint is that struct's emissiveColMod (+0x40), once per change;
//   - each lamp object (flags, type, variant, colour index, brightness) and the colour the
//     game wrote for it (out[id + 1], w = broken bit);
//   - once per change: the lights manager's colour table (*(0x014551F0) + 0x3F0 + 16 * b2)
//     and the per-type scale table at 0x00F3FB68 (32 floats).
// The log goes to [probe] log= in lampprobe.ini beside the .asi (default lampprobe.log
// beside it); "%s" in the path is replaced by the start time (yyyymmdd-hhmmss).

#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

namespace {

typedef void(__cdecl* FillFn)(float* out, uint8_t* lamps, float* tint);
const uint32_t kFill = 0x008A78E0;
const uint32_t kCallSites[2] = {0x0087AB68, 0x008A9184};
const uint32_t kManager = 0x014551F0;  // lights manager pointer; colours at +0x3F0 + 16 * b2
const uint32_t kTypeScale = 0x00F3FB68;

FILE* g_log = nullptr;
CRITICAL_SECTION g_lock;
ULONGLONG g_start = 0;
DWORD g_interval = 500;
long g_lines_left = 200000;
float g_colours[16][4];
bool g_colours_seen = false;
float g_mood[128];
bool g_mood_seen = false;
float g_scale[32];
bool g_scale_seen = false;

struct Seen {
    uint8_t* comp;
    ULONGLONG last;
};
Seen g_seen[64];

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

bool copy_guarded(void* dst, const void* src, size_t n)
{
    __try {
        memcpy(dst, src, n);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

void log_tables(ULONGLONG t, float* tint)
{
    uint32_t manager = 0;
    float colours[16][4];
    if (copy_guarded(&manager, reinterpret_cast<void*>(kManager), 4) && manager &&
        copy_guarded(colours, reinterpret_cast<void*>(manager + 0x3F0), sizeof(colours)) &&
        (!g_colours_seen || memcmp(colours, g_colours, sizeof(colours)) != 0)) {
        memcpy(g_colours, colours, sizeof(colours));
        g_colours_seen = true;
        logf("t=%llu colours manager=%08X", t, manager);
        for (int k = 0; k < 16; ++k) {
            logf("  colour %2d: %.6g %.6g %.6g %.6g", k, colours[k][0], colours[k][1], colours[k][2], colours[k][3]);
        }
    }
    float scale[32];
    if (copy_guarded(scale, reinterpret_cast<void*>(kTypeScale), sizeof(scale)) &&
        (!g_scale_seen || memcmp(scale, g_scale, sizeof(scale)) != 0)) {
        memcpy(g_scale, scale, sizeof(scale));
        g_scale_seen = true;
        char line[1024];
        int n = sprintf_s(line, "t=%llu typescale", t);
        for (int k = 0; k < 32; ++k) {
            n += sprintf_s(line + n, sizeof(line) - n, " %g", scale[k]);
        }
        logf("%s", line);
    }
    float mood[128];
    if (tint && copy_guarded(mood, reinterpret_cast<uint8_t*>(tint) - 0x40, sizeof(mood)) &&
        (!g_mood_seen || memcmp(mood, g_mood, sizeof(mood)) != 0)) {
        memcpy(g_mood, mood, sizeof(mood));
        g_mood_seen = true;
        logf("t=%llu mood at %p (tint - 0x40):", t, reinterpret_cast<uint8_t*>(tint) - 0x40);
        for (int k = 0; k < 20; ++k) {
            logf("  v%-2d +0x%03X %.6g %.6g %.6g %.6g", k, k * 16, mood[4 * k], mood[4 * k + 1], mood[4 * k + 2],
                mood[4 * k + 3]);
        }
        for (int k = 0; k < 48; ++k) {
            logf("  f%-2d +0x%03X %.6g", k, 0x140 + 4 * k, mood[80 + k]);
        }
    }
}

void __cdecl fill_hook(float* out, uint8_t* lamps, float* tint)
{
    reinterpret_cast<FillFn>(kFill)(out, lamps, tint);
    if (!g_log || !lamps) {
        return;
    }
    EnterCriticalSection(&g_lock);
    ULONGLONG now = GetTickCount64();
    int slot = -1;
    for (int i = 0; i < 64; ++i) {
        if (g_seen[i].comp == lamps) {
            slot = i;
            break;
        }
        if (slot < 0 && g_seen[i].comp == nullptr) {
            slot = i;
        }
    }
    bool due = slot >= 0 && (g_seen[slot].comp != lamps || now - g_seen[slot].last >= g_interval);
    if (due) {
        g_seen[slot].comp = lamps;
        g_seen[slot].last = now;
        ULONGLONG t = now - g_start;
        log_tables(t, tint);
        uint32_t count = 0;
        copy_guarded(&count, lamps + 0x230, 4);
        float tv[4] = {};
        copy_guarded(tv, tint, sizeof(tv));
        logf("t=%llu fill comp=%p n=%u tint=%.6g,%.6g,%.6g,%.6g tintptr=%p out0=%.3g,%.3g,%.3g,%.3g", t, lamps, count,
            tv[0], tv[1], tv[2], tv[3], tint, out[0], out[1], out[2], out[3]);
        if (count > 32) {
            count = 32;
        }
        for (uint32_t i = 0; i < count; ++i) {
            uint8_t obj[16];
            if (!copy_guarded(obj, lamps + 16 * i, 16)) {
                break;
            }
            float bright;
            memcpy(&bright, obj + 4, 4);
            const float* o = out + 4 * (i + 1);
            logf("  lamp %2u flags=%02X%02X%02X%02X type=%02X b0=%02X b1=%02X col=%02X bright=%.4g out=%.4g,%.4g,%.4g,%.0f",
                i, obj[3], obj[2], obj[1], obj[0], obj[12], obj[13], obj[15], obj[14], bright, o[0], o[1], o[2], o[3]);
        }
        fflush(g_log);
    }
    LeaveCriticalSection(&g_lock);
}

bool patch_call(uint32_t site, void* target)
{
    uint8_t* p = reinterpret_cast<uint8_t*>(site);
    int32_t rel;
    memcpy(&rel, p + 1, 4);
    if (p[0] != 0xE8 || site + 5 + rel != kFill) {
        return false;
    }
    DWORD old;
    if (!VirtualProtect(p, 5, PAGE_EXECUTE_READWRITE, &old)) {
        return false;
    }
    rel = static_cast<int32_t>(reinterpret_cast<uint32_t>(target) - (site + 5));
    memcpy(p + 1, &rel, 4);
    VirtualProtect(p, 5, old, &old);
    FlushInstructionCache(GetCurrentProcess(), p, 5);
    return true;
}

void start(HMODULE self)
{
    char dir[MAX_PATH];
    GetModuleFileNameA(self, dir, MAX_PATH);
    char* slash = strrchr(dir, '\\');
    if (slash) {
        *slash = '\0';
    }
    char ini[MAX_PATH], pattern[MAX_PATH], path[MAX_PATH], stamp[32];
    sprintf_s(ini, "%s\\lampprobe.ini", dir);
    char def[MAX_PATH];
    sprintf_s(def, "%s\\lampprobe.log", dir);
    GetPrivateProfileStringA("probe", "log", def, pattern, MAX_PATH, ini);
    g_interval = GetPrivateProfileIntA("probe", "interval_ms", 500, ini);
    g_lines_left = GetPrivateProfileIntA("probe", "max_lines", 200000, ini);
    SYSTEMTIME st;
    GetLocalTime(&st);
    sprintf_s(stamp, "%04d%02d%02d-%02d%02d%02d", st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
    const char* s = strstr(pattern, "%s");
    if (s) {
        sprintf_s(path, "%.*s%s%s", static_cast<int>(s - pattern), pattern, stamp, s + 2);
    } else {
        strcpy_s(path, pattern);
    }
    fopen_s(&g_log, path, "w");
    if (!g_log) {
        return;
    }
    g_start = GetTickCount64();
    InitializeCriticalSection(&g_lock);
    bool a = patch_call(kCallSites[0], reinterpret_cast<void*>(&fill_hook));
    bool b = patch_call(kCallSites[1], reinterpret_cast<void*>(&fill_hook));
    logf("lampprobe %s: call sites %08X %s, %08X %s; interval %lu ms", stamp, kCallSites[0], a ? "hooked" : "NOT hooked",
        kCallSites[1], b ? "hooked" : "NOT hooked", g_interval);
    fflush(g_log);
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
