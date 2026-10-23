// dsfforza.asi entry point: core init, exe check, then every module's init.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>

#include "patch.h"

extern "C" IMAGE_DOS_HEADER __ImageBase;

void dsf_core_init();

#define DSF_MODULE(name) void mod_##name##_init();
#include "modules.h"
#undef DSF_MODULE

struct Module {
    const char* name;
    void (*init)();
};

static const Module kModules[] = {
#define DSF_MODULE(name) {#name, mod_##name##_init},
#include "modules.h"
#undef DSF_MODULE
};

// [dsfforza] crash_log=1: a vectored handler logs the first few faults
// (first chance: also ones the game handles itself) with registers and the
// Driver.exe return addresses found on the stack. DriverNGHook's fatal-error
// dialog writes no dump when the process can't, so this is the only trace.
static LONG CALLBACK crash_handler(EXCEPTION_POINTERS* ep)
{
    static volatile LONG logged = 0;
    DWORD code = ep->ExceptionRecord->ExceptionCode;
    if (code != EXCEPTION_ACCESS_VIOLATION && code != EXCEPTION_ILLEGAL_INSTRUCTION &&
        code != EXCEPTION_STACK_OVERFLOW && code != EXCEPTION_INT_DIVIDE_BY_ZERO &&
        code != EXCEPTION_PRIV_INSTRUCTION && code != 0xC0000409 /* stack buffer overrun */ &&
        code != 0xC0000374 /* heap corruption */ && code != EXCEPTION_ARRAY_BOUNDS_EXCEEDED)
        return EXCEPTION_CONTINUE_SEARCH;
    if (InterlockedIncrement(&logged) > 12) return EXCEPTION_CONTINUE_SEARCH;
    CONTEXT* c = ep->ContextRecord;
    const EXCEPTION_RECORD* r = ep->ExceptionRecord;
    dsf_log("fault %08X at eip %08X (%s %08X) thread %lu", code, c->Eip,
            r->NumberParameters >= 2 ? (r->ExceptionInformation[0] ? "write" : "read") : "-",
            r->NumberParameters >= 2 ? (DWORD)r->ExceptionInformation[1] : 0, GetCurrentThreadId());
    dsf_log("  eax %08X ebx %08X ecx %08X edx %08X esi %08X edi %08X ebp %08X esp %08X", c->Eax, c->Ebx,
            c->Ecx, c->Edx, c->Esi, c->Edi, c->Ebp, c->Esp);
    // Return-address candidates: stack words that point into Driver.exe's .text
    // or this plugin. IsBadReadPtr-free: the stack page is readable up to its base.
    uint32_t self = (uint32_t)&__ImageBase;
    NT_TIB* tib = (NT_TIB*)NtCurrentTeb();
    uint32_t top = (uint32_t)tib->StackBase;
    char line[512];
    int n = 0, found = 0;
    for (uint32_t p = c->Esp; p + 4 <= top && p < c->Esp + 0x2000 && found < 24; p += 4) {
        uint32_t v = *(uint32_t*)p;
        bool exe = v >= 0x00401000 && v < 0x00DC9000;
        bool asi = v >= self + 0x1000 && v < self + 0x40000;
        if (!exe && !asi) continue;
        n += _snprintf_s(line + n, sizeof line - n, _TRUNCATE, " [%X]%s%08X", p - c->Esp, asi ? "asi+" : "",
                         asi ? v - self : v);
        found++;
        if (n > 400) {
            dsf_log("  stack%s", line);
            n = 0;
        }
    }
    if (n) dsf_log("  stack%s", line);
    return EXCEPTION_CONTINUE_SEARCH;
}

static void run_modules()
{
    dsf_core_init();
    dsf_log("dsfforza.asi %s, ini %s", DSF_VERSION, dsf_ini_path());
    if (!cfg_bool("dsfforza", "enabled", true)) {
        dsf_log("[dsfforza] enabled=0: no patches");
        return;
    }
    if (!dsf_exe_ok()) return;
    if (cfg_bool("dsfforza", "crash_log", false)) {
        AddVectoredExceptionHandler(1, crash_handler);
        dsf_log("crash_log: faults are logged here (asi base %p)", &__ImageBase);
    }
    for (const Module& m : kModules) {
        if (!cfg_bool(m.name, "enabled", true)) {
            dsf_log("[%s] disabled in ini", m.name);
            continue;
        }
        dsf_log("[%s] init", m.name);
        m.init();
    }
    dsf_log("init done");
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(inst);
        run_modules();
    }
    return TRUE;
}
