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

bool patch_bytes(uint32_t va, const void* bytes, size_t n, const void* expect)
{
    void* p = (void*)va;
    if (expect && memcmp(p, expect, n) != 0) {
        char have[200], want[200];
        hex(have, sizeof have, p, n < 48 ? n : 48);
        hex(want, sizeof want, expect, n < 48 ? n : 48);
        dsf_log("patch %08X: original bytes differ, not patched\n  have %s\n  want %s", va, have, want);
        return false;
    }
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

bool patch_u32(uint32_t va, uint32_t value, const uint32_t* expect_old)
{
    return patch_bytes(va, &value, 4, expect_old);
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

// ---- function hooks --------------------------------------------------------

// Length of one x86 instruction from the subset that system-DLL prologues
// use; 0 for anything else, including relative branches and calls (they
// can't be copied into a trampoline as they are).
static size_t insn_len(const BYTE* p)
{
    const BYTE* s = p;
    bool opsize = false;
    while (*p == 0x64 || *p == 0x65 || *p == 0x66 || *p == 0x2E || *p == 0x3E || *p == 0x26 || *p == 0x36) {
        if (*p == 0x66) opsize = true;
        p++;
        if (p - s > 3) return 0;
    }
    BYTE op = *p++;
    if (op >= 0x50 && op <= 0x5F) return p - s;                       // push/pop r32
    if (op == 0x90) return p - s;                                     // nop
    if (op == 0x6A) return p - s + 1;                                 // push imm8
    if (op == 0x68) return p - s + 4;                                 // push imm32
    if (op >= 0xB8 && op <= 0xBF) return p - s + (opsize ? 2 : 4);   // mov r32, imm32
    if (op >= 0xB0 && op <= 0xB7) return p - s + 1;                   // mov r8, imm8
    if (op == 0xA1 || op == 0xA3) return p - s + 4;                   // mov eax, [abs] / mov [abs], eax
    int imm = 0;
    switch (op) {
    case 0x01: case 0x03: case 0x09: case 0x0B: case 0x21: case 0x23: case 0x29: case 0x2B:
    case 0x31: case 0x33: case 0x39: case 0x3B: case 0x85: case 0x87: case 0x89: case 0x8B: case 0x8D:
        break;
    case 0x83: case 0xC6: imm = 1; break;
    case 0x81: case 0xC7: imm = opsize ? 2 : 4; break;
    case 0xFF: {
        BYTE reg = (*p >> 3) & 7;    // inc, dec, call, jmp, push r/m: absolute, fine to copy
        if (reg == 3 || reg == 5 || reg == 7) return 0;
        break;
    }
    default:
        return 0;
    }
    BYTE m = *p++;
    BYTE mod = m >> 6, rm = m & 7;
    if (mod != 3 && rm == 4) {
        BYTE sib = *p++;
        if (mod == 0 && (sib & 7) == 5) p += 4;
    }
    if (mod == 1) p += 1;
    else if (mod == 2) p += 4;
    else if (mod == 0 && rm == 5) p += 4;
    return p - s + imm;
}

void* hook_code(void* target, void* detour, const char* what)
{
    if (!target) {
        dsf_log("hook %s: no target", what);
        return nullptr;
    }
    BYTE* t = (BYTE*)target;
    BYTE* tramp = (BYTE*)alloc_exec(64);
    if (!tramp) return nullptr;
    size_t n = 0;
    if (t[0] == 0xE9 || t[0] == 0xEB) {
        // Someone hooked it already (or it is a jump stub): the trampoline
        // jumps where that jump went.
        uint32_t dest = t[0] == 0xE9 ? (uint32_t)t + 5 + *(int32_t*)(t + 1) : (uint32_t)t + 2 + *(int8_t*)(t + 1);
        n = t[0] == 0xE9 ? 5 : 2;
        tramp[0] = 0xE9;
        *(int32_t*)(tramp + 1) = (int32_t)(dest - ((uint32_t)tramp + 5));
        if (n < 5) {
            // A short jump is too small to hold ours; take the following
            // instructions too (never executed through the trampoline).
            while (n < 5) {
                size_t len = insn_len(t + n);
                if (!len) { n = 0; break; }
                n += len;
            }
        }
    } else {
        while (n < 5) {
            size_t len = insn_len(t + n);
            if (!len) { n = 0; break; }
            n += len;
        }
        if (n) {
            memcpy(tramp, t, n);
            tramp[n] = 0xE9;
            *(int32_t*)(tramp + n + 1) = (int32_t)(((uint32_t)t + n) - ((uint32_t)tramp + n + 5));
        }
    }
    if (!n) {
        char have[64];
        hex(have, sizeof have, t, 12);
        dsf_log("hook %s at %p: prologue not supported (%s)", what, t, have);
        return nullptr;
    }
    if (!patch_jmp((uint32_t)t, detour, n)) {
        dsf_log("hook %s at %p: patch failed", what, t);
        return nullptr;
    }
    return tramp;
}

void* hook_export(const char* dll, const char* name, void* detour)
{
    char what[128];
    _snprintf_s(what, sizeof what, _TRUNCATE, "%s!%s", dll, name);
    HMODULE m = GetModuleHandleA(dll);
    if (!m) {
        dsf_log("hook %s: %s not loaded", what, dll);
        return nullptr;
    }
    void* target = (void*)GetProcAddress(m, name);
    if (!target) {
        dsf_log("hook %s: no such export", what);
        return nullptr;
    }
    return hook_code(target, detour, what);
}

void* patch_vtable(void** vtbl, int index, void* detour)
{
    if (!vtbl) return nullptr;
    void* old = vtbl[index];
    if (old == detour) return nullptr;
    if (!patch_u32((uint32_t)&vtbl[index], (uint32_t)detour)) return nullptr;
    return old;
}
