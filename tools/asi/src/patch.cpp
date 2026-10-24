// dsfforza.asi core implementation. See patch.h.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "patch.h"

// Driver.exe 1.04 (PC, 2011-11-14 build). Checked from the in-memory PE
// header, which other ASIs leave alone; .text itself may already be patched by
// dsf_enhanced.asi, so each patch checks its own original bytes instead.
static const DWORD kTimeDateStamp = 0x4EC0D92B;
static const DWORD kSizeOfImage = 0x019C0000;
static const DWORD kTextVirtualSize = 0x009C7171;
static const DWORD kTextVA = 0x00001000;

static CRITICAL_SECTION g_log_lock;
static bool g_log_ready = false;
static char g_dir[MAX_PATH];
static char g_ini[MAX_PATH];
static char g_log[MAX_PATH];
static int g_exe_ok = -1;

extern "C" IMAGE_DOS_HEADER __ImageBase;

void dsf_core_init()
{
    GetModuleFileNameA((HMODULE)&__ImageBase, g_dir, MAX_PATH);
    // ini and log take the plugin's own name (dsfforza.asi -> dsfforza.ini,
    // dsfhostrun.asi -> dsfhostrun.ini); anything that isn't an .asi (the
    // offline test exe) uses dsfforza.
    char base[MAX_PATH] = "dsfforza";
    char* slash = strrchr(g_dir, '\\');
    const char* file = slash ? slash + 1 : g_dir;
    const char* dot = strrchr(file, '.');
    if (dot && _stricmp(dot, ".asi") == 0 && dot > file)
        _snprintf_s(base, MAX_PATH, _TRUNCATE, "%.*s", (int)(dot - file), file);
    if (slash) *slash = 0;
    _snprintf_s(g_ini, MAX_PATH, _TRUNCATE, "%s\\%s.ini", g_dir, base);
    _snprintf_s(g_log, MAX_PATH, _TRUNCATE, "%s\\%s.log", g_dir, base);
    InitializeCriticalSection(&g_log_lock);
    g_log_ready = true;
    if (cfg_bool("dsfforza", "log", true)) {
        FILE* f = nullptr;
        if (fopen_s(&f, g_log, "w") == 0 && f) fclose(f);  // truncate
    }
}

const char* dsf_ini_path() { return g_ini; }
const char* dsf_plugin_dir() { return g_dir; }

void dsf_log(const char* fmt, ...)
{
    if (!g_log_ready) return;
    static int enabled = -1;
    if (enabled < 0) enabled = cfg_bool("dsfforza", "log", true) ? 1 : 0;
    if (!enabled) return;
    char line[1024];
    SYSTEMTIME t;
    GetLocalTime(&t);
    int n = _snprintf_s(line, sizeof line, _TRUNCATE, "%02d:%02d:%02d.%03d ",
                        t.wHour, t.wMinute, t.wSecond, t.wMilliseconds);
    va_list ap;
    va_start(ap, fmt);
    if (n > 0) _vsnprintf_s(line + n, sizeof line - n, _TRUNCATE, fmt, ap);
    va_end(ap);
    EnterCriticalSection(&g_log_lock);
    FILE* f = nullptr;
    if (fopen_s(&f, g_log, "a") == 0 && f) {
        fputs(line, f);
        fputc('\n', f);
        fclose(f);
    }
    LeaveCriticalSection(&g_log_lock);
}

int cfg_int(const char* section, const char* key, int def)
{
    return (int)GetPrivateProfileIntA(section, key, def, g_ini);
}

bool cfg_bool(const char* section, const char* key, bool def)
{
    return cfg_int(section, key, def ? 1 : 0) != 0;
}

const char* cfg_str(const char* section, const char* key, const char* def, char* out, size_t out_size)
{
    GetPrivateProfileStringA(section, key, def, out, (DWORD)out_size, g_ini);
    return out;
}

bool dsf_exe_ok()
{
    if (g_exe_ok >= 0) return g_exe_ok != 0;
    g_exe_ok = 0;
    BYTE* base = (BYTE*)GetModuleHandleA(nullptr);
    if ((uint32_t)base != 0x00400000) {
        dsf_log("exe: image base %p, not 0x00400000", base);
        return false;
    }
    IMAGE_DOS_HEADER* dos = (IMAGE_DOS_HEADER*)base;
    IMAGE_NT_HEADERS32* nt = (IMAGE_NT_HEADERS32*)(base + dos->e_lfanew);
    IMAGE_SECTION_HEADER* sec = IMAGE_FIRST_SECTION(nt);
    DWORD ts = nt->FileHeader.TimeDateStamp, size = nt->OptionalHeader.SizeOfImage;
    bool text = nt->FileHeader.NumberOfSections > 0 && memcmp(sec->Name, ".text", 6) == 0 &&
                sec->VirtualAddress == kTextVA && sec->Misc.VirtualSize == kTextVirtualSize;
    if (ts != kTimeDateStamp || size != kSizeOfImage || !text) {
        dsf_log("exe: not Driver.exe 1.04 (timestamp %08X, image size %08X, .text %s) - no patches",
                ts, size, text ? "ok" : "differs");
        return false;
    }
    g_exe_ok = 1;
    return true;
}

bool bytes_equal(uint32_t va, const void* bytes, size_t n)
{
    return memcmp((const void*)va, bytes, n) == 0;
}

static void hex(char* out, size_t out_size, const void* p, size_t n)
{
    size_t o = 0;
    for (size_t i = 0; i < n && o + 4 < out_size; i++)
        o += _snprintf_s(out + o, out_size - o, _TRUNCATE, "%02X ", ((const BYTE*)p)[i]);
    if (o) out[o - 1] = 0; else out[0] = 0;
}

bool check_bytes(uint32_t va, const void* bytes, size_t n)
{
    if (bytes_equal(va, bytes, n)) return true;
    char have[200], want[200];
    hex(have, sizeof have, (const void*)va, n < 48 ? n : 48);
    hex(want, sizeof want, bytes, n < 48 ? n : 48);
    dsf_log("patch %08X: original bytes differ, not patched\n  have %s\n  want %s", va, have, want);
    return false;
}

bool patch_bytes(uint32_t va, const void* bytes, size_t n, const void* expect)
{
    void* p = (void*)va;
    if (expect && !check_bytes(va, expect, n)) return false;
    DWORD old;
    if (!VirtualProtect(p, n, PAGE_EXECUTE_READWRITE, &old)) {
        dsf_log("patch %08X: VirtualProtect failed (%lu)", va, GetLastError());
        return false;
    }
    memcpy(p, bytes, n);
    VirtualProtect(p, n, old, &old);
    FlushInstructionCache(GetCurrentProcess(), p, n);
    return true;
}

static bool patch_rel(BYTE op, uint32_t va, void* target, size_t len, const void* expect)
{
    if (len < 5 || len > 64) {
        dsf_log("patch %08X: bad length %u", va, (unsigned)len);
        return false;
    }
    BYTE buf[64];
    memset(buf, 0x90, len);
    buf[0] = op;
    int32_t rel = (int32_t)((uint32_t)target - (va + 5));
    memcpy(buf + 1, &rel, 4);
    return patch_bytes(va, buf, len, expect);
}

bool patch_jmp(uint32_t va, void* target, size_t len, const void* expect)
{
    return patch_rel(0xE9, va, target, len, expect);
}

bool patch_call(uint32_t va, void* target, const void* expect)
{
    return patch_rel(0xE8, va, target, 5, expect);
}

void* alloc_exec(size_t n)
{
    void* p = VirtualAlloc(nullptr, n, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!p) dsf_log("alloc_exec(%u) failed (%lu)", (unsigned)n, GetLastError());
    return p;
}
