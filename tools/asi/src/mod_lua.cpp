// Lua heap: room for the game's Lua state past its fixed 10 MB block.
// See research/asi.md ("Lua heap") and research/in-game-tests.md.
//
// Vanilla (Driver.exe 1.04, confirmed-static): FUN_005E4BF0 creates heap 3 of
// the engine's heap manager ([0x012536AC]) as one 10 MB block (push 0xA00000
// at 0x005E4C05; FUN_0075A190 -> FUN_00758EE0 mallocs it once, it never
// grows). The Lua state is made by lua_newstate(FUN_005E4AB0, 0) at
// 0x005E7DBC; FUN_005E4AB0, the lua_Alloc, allocates, reallocates and frees in
// that block and returns NULL ("Lua heap out of memory") when it is full. Lua
// 5.1 turns that into a "not enough memory" error at whatever allocation hit
// it: inside the VM, inside the collector, inside game C++ code between two
// API calls. The game's pcall wrapper (FUN_005E4760) drops the error, so
// nothing is logged and the state is left half-updated.
//
// The vanilla game stays well under 10 MB. With the full roster (573 cars: their
// vehicleStats entries, dsfforza\ported.lua, hud.lua) the arena refuses
// requests once Lua holds about 8.5 MB, which a drive through traffic or a few
// minutes in the garage reach; the game then dies in the Lua VM a minute or
// so later (issue #56: run vm2-20261026-122308 logged the first failures at
// 12:27:43 and faulted at 12:28:48).
//
// This module:
//   1. makes the block bigger (`[lua] arena_mb`, default 32), so the game's own
//      allocator keeps serving every request of a large roster;
//   2. puts its own lua_Alloc in the lua_newstate call: blocks come from the
//      game's arena while it has room, and a request the arena can't serve
//      comes from a private Win32 heap instead (`overflow=1`), so there is no
//      fixed limit at all. Frees and reallocs go to whichever heap owns the
//      pointer (the arena is one address range).
// Both are logged: the arena size, each new MB Lua passes, arena failures.
#include "patch.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <string.h>

typedef void*(__cdecl* LuaAlloc)(void* ud, void* ptr, size_t osize, size_t nsize);

static const uint32_t kVanillaAlloc = 0x005E4AB0;     // the game's lua_Alloc (heap 3)
static const uint32_t kNewStatePush = 0x005E7DB7;   // push 0x005E4AB0 before call lua_newstate (0x005F4E40)
static const uint8_t kNewStatePushBytes[5] = {0x68, 0xB0, 0x4A, 0x5E, 0x00};
static const uint32_t kArenaSizePush = 0x005E4C05;  // push 0xA00000: heap 3's size, in FUN_005E4BF0
static const uint8_t kArenaSizePushBytes[5] = {0x68, 0x00, 0x00, 0xA0, 0x00};
static const uint32_t kHeapManager = 0x012536AC;    // heap manager: heap pointers by index
static const uint32_t kLuaState = 0x0122B498;       // lua_State* of the game scripts
static const int kLuaHeap = 3;

static HANDLE g_heap = nullptr;    // overflow heap (null: overflow=0)
static long g_fails = 0;           // requests the arena refused
static size_t g_over_bytes = 0;    // bytes in the overflow heap now
static uint32_t g_over_mark = 0;   // overflow use logged at each new 256 KB
static uint32_t g_total_mark = 0;  // Lua's own byte count logged at each new MB

// The arena: one block from FUN_00758EE0 with its header at the start (+4 = size).
static uint8_t* arena(uint32_t* size)
{
    uint8_t** heaps = *(uint8_t***)kHeapManager;
    uint8_t* a = heaps ? heaps[kLuaHeap] : nullptr;
    *size = a ? *(uint32_t*)(a + 4) : 0;
    return a;
}

static bool in_arena(const void* p)
{
    uint32_t size;
    uint8_t* a = arena(&size);
    return a && (const uint8_t*)p >= a && (const uint8_t*)p < a + size;
}

static uint32_t lua_total_bytes()
{
    uint8_t* L = *(uint8_t**)kLuaState;
    uint8_t* g = L ? *(uint8_t**)(L + 0x10) : nullptr;   // L->l_G
    return g ? *(uint32_t*)(g + 0x44) : 0;               // g->totalbytes
}

static void note_over(size_t before)
{
    if (g_over_bytes > before && (g_over_bytes >> 18) > g_over_mark) {
        g_over_mark = (uint32_t)(g_over_bytes >> 18);
        dsf_log("lua: overflow heap holds %u KB, Lua %u KB", (unsigned)(g_over_bytes >> 10), lua_total_bytes() >> 10);
    }
}

static void* __cdecl lua_alloc(void* ud, void* ptr, size_t osize, size_t nsize)
{
    LuaAlloc vanilla = (LuaAlloc)kVanillaAlloc;
    if (ptr && g_heap && !in_arena(ptr)) {   // an overflow block
        size_t before = g_over_bytes;
        g_over_bytes -= HeapSize(g_heap, 0, ptr);
        if (nsize == 0) {
            HeapFree(g_heap, 0, ptr);
            return nullptr;
        }
        void* p = HeapReAlloc(g_heap, 0, ptr, nsize);
        g_over_bytes += HeapSize(g_heap, 0, p ? p : ptr);
        note_over(before);
        return p;
    }
    void* p = vanilla(ud, ptr, osize, nsize);
    if (p || nsize == 0) {
        uint32_t mb = lua_total_bytes() >> 20;
        if (mb > g_total_mark) {
            g_total_mark = mb;
            uint32_t size;
            arena(&size);
            dsf_log("lua: Lua heap passed %u MB (arena %u MB)", mb, size >> 20);
        }
        return p;
    }
    long n = InterlockedIncrement(&g_fails);
    if (n <= 4 || (n & (n - 1)) == 0)
        dsf_log("lua: arena full: request of %u bytes (block of %u) refused, Lua holds %u KB; %s (failure %ld)",
                (unsigned)nsize, (unsigned)osize, lua_total_bytes() >> 10,
                g_heap ? "served from the overflow heap" : "Lua gets NULL (overflow=0)", n);
    if (!g_heap) return nullptr;
    p = HeapAlloc(g_heap, 0, nsize);
    if (!p) return nullptr;
    size_t before = g_over_bytes;
    g_over_bytes += HeapSize(g_heap, 0, p);
    note_over(before);
    if (ptr) {   // an arena block that couldn't grow: move it out (it stays where it was if this fails)
        memcpy(p, ptr, osize < nsize ? osize : nsize);
        vanilla(ud, ptr, osize, 0);
    }
    return p;
}

void mod_lua_init()
{
    uint32_t size;
    if (*(void**)kLuaState || arena(&size)) {
        dsf_log("lua: the Lua state or its heap exists already; Lua heap left alone");
        return;
    }
    int mb = cfg_int("lua", "arena_mb", 32);
    if (mb > 10 && mb <= 512) {
        uint8_t push[5] = {0x68};
        uint32_t bytes = (uint32_t)mb << 20;
        memcpy(push + 1, &bytes, 4);
        if (patch_bytes(kArenaSizePush, push, sizeof push, kArenaSizePushBytes))
            dsf_log("lua: Lua arena %d MB (vanilla 10)", mb);
        else
            dsf_log("lua: arena size push at %08X differs; arena stays as it is", kArenaSizePush);
    }
    if (!cfg_bool("lua", "overflow", true)) {
        dsf_log("lua: overflow=0: a full arena fails requests as vanilla (logged)");
    } else if (!(g_heap = HeapCreate(0, 0, 0))) {
        dsf_log("lua: HeapCreate failed (%lu); no overflow heap", GetLastError());
    }
    uint8_t push[5] = {0x68};
    uint32_t target = (uint32_t)(void*)lua_alloc;
    memcpy(push + 1, &target, 4);
    if (!patch_bytes(kNewStatePush, push, sizeof push, kNewStatePushBytes)) {
        dsf_log("lua: lua_newstate's allocator push at %08X differs; no overflow, no heap log", kNewStatePush);
        return;
    }
    dsf_log("lua: lua_Alloc patched at %08X; a full arena %s", kNewStatePush,
            g_heap ? "overflows into a private heap" : "fails requests (logged)");
}
