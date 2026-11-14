// Vehicle-provider keyed maps. Native layouts and the research credit are in
// research/traffic.md, "Vehicle provider maps and model audio".
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <initializer_list>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "patch.h"

namespace {

// 0086C340 / 0086B4A0: map+0x0C..+0x2DC holds 45 sixteen-byte nodes (nodes from
// provider+0x248, sentinel at provider+0x518: (0x518 - 0x248) / 16 = 45).
// 00556EE0: the audio provider has the same map at object+4.
constexpr uint32_t kNodeBytes = 16;
constexpr uint32_t kInlineNodes = 45;
constexpr uint32_t kHead = 12 + kInlineNodes * kNodeBytes;
constexpr uint32_t kProviderVtable = 0x00E3966C;  // set by both ED/EF constructors
// The vehicle spec manager [Engine+0x778] (class EC, factory 0086C070, constructor 0086BFA0,
// vtable 00E395D0, 0x6A4 bytes) keeps its ID->spec map at +0x254 in the same pool layout with
// 24-byte nodes {prev, next, key, spec, state, flag}: 45 of them from +0x260, the sentinel at
// +0x698 ((0x698 - 0x260) / 24 = 45). Its only insert is operator[] 0086BE10's call 0086BE67 ->
// 008CCAA0, a bump by 0x18 with no end check. Node 46 lands on the sentinel and past the object.
constexpr uint32_t kSpecNodeBytes = 24;
constexpr uint32_t kSpecMap = 0x254;
constexpr uint32_t kSpecVtable = 0x00E395D0;
constexpr uint32_t kSpecSite = 0x0086BE67;
const uint8_t kSpecCall[] = {0xE8, 0x34, 0x0C, 0x06, 0x00};  // call 008CCAA0
constexpr uint32_t kAudioVtable = 0x00DE3824;     // Engine+0x280's model-audio provider (factory 00557000)
// Peaks are logged from 40 nodes: below vanilla's own peak of 43 at 35 slots
// (research/traffic.md), so slots_trace at 35 shows them. A logging choice, not a limit.
constexpr uint32_t kLogFrom = 40;
// Log label for the model-audio map; the audio provider has no class tag of its own.
constexpr uint32_t kAudioLabel = 0xA0;
using Insert = int**(__thiscall*)(int*, int**, int*, const int*);  // 007D7270, the map template's insert
using Index = int*(__thiscall*)(int*, const int*);                  // 00556F60, operator[]
using Append = int*(__thiscall*)(int*, const int*);                 // 008CCAA0, the 24-byte pool's node append

struct Page {
    Page* next;
    uint8_t* begin;
    uint8_t* end;
};
struct Map {
    Map* next;
    int* pool;
    Page* pages;
    uint32_t peak;
    uint32_t node;  // node bytes: 16 (ED/EF, model audio) or 24 (the spec map)
};
Map* g_maps = nullptr;
SRWLOCK g_lock = SRWLOCK_INIT;
bool g_installed = false;
bool g_fail_allocation = false;  // offline allocation-failure injection only

bool readable_map(const void* p, size_t size)
{
    MEMORY_BASIC_INFORMATION m;
    return VirtualQuery(p, &m, sizeof m) && m.State == MEM_COMMIT &&
           !(m.Protect & (PAGE_NOACCESS | PAGE_GUARD)) &&
           (uintptr_t)p >= (uintptr_t)m.BaseAddress &&
           size <= m.RegionSize - ((uintptr_t)p - (uintptr_t)m.BaseAddress);
}

uint32_t map_kind(int* pool)
{
    uint8_t* p = (uint8_t*)pool;
    if (readable_map(p - 4, 4) && *(uint32_t*)(p - 4) == kAudioVtable) return kAudioLabel;
    uint8_t* owner = p - 0x23C;
    if (readable_map(owner, 0x238) && *(uint32_t*)owner == kProviderVtable) {
        uint32_t kind = *(uint32_t*)(owner + 0x234);
        if (kind == 0xED || kind == 0xEF) return kind;
    }
    return 0;
}

// The spec manager's map: the owner's vtable and class tag (0086BFA0: mov [esi+248h],0ECh).
bool spec_map(int* pool)
{
    uint8_t* owner = (uint8_t*)pool - kSpecMap;
    return readable_map(owner, 0x24C) && *(uint32_t*)owner == kSpecVtable && *(uint32_t*)(owner + 0x248) == 0xEC;
}

Map* find_map(int* pool, uint32_t node = kNodeBytes)
{
    for (Map* m = g_maps; m; m = m->next)
        if (m->pool == pool && m->node == node) return m;
    if (g_fail_allocation) return nullptr;
    Map* m = (Map*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(Map));
    if (!m) return nullptr;
    m->pool = pool;
    m->node = node;
    m->next = g_maps;
    g_maps = m;
    return m;
}

Page* new_page(uint32_t node)
{
    if (g_fail_allocation) return nullptr;
    Page* page = (Page*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(Page));
    if (!page) return nullptr;
    SYSTEM_INFO info;
    GetSystemInfo(&info);
    // One OS page per allocation, a storage granularity rather than a model limit.
    page->begin = (uint8_t*)VirtualAlloc(nullptr, info.dwPageSize, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!page->begin) {
        HeapFree(GetProcessHeap(), 0, page);
        return nullptr;
    }
    page->end = page->begin + info.dwPageSize / node * node;  // whole nodes only
    return page;
}

// Keep native free-list nodes and sentinel links. Only move a fresh bump cursor
// when it reaches the end of its storage. Pages stay valid for the process lifetime.
// A constructor/reset starts at the inline array again and reuses the first page.
bool make_room(Map* m)
{
    int* pool = m->pool;
    if (pool[1]) return true;
    uintptr_t cur = (uint32_t)pool[0];
    uintptr_t begin = (uintptr_t)(pool + 3), end = begin + kInlineNodes * m->node;
    Page** next = &m->pages;
    if (cur >= begin && cur < end && (cur - begin) % m->node == 0) return true;
    if (cur != end) {
        Page* page = m->pages;
        while (page && !(cur >= (uintptr_t)page->begin && cur <= (uintptr_t)page->end)) page = page->next;
        if (!page || (cur - (uintptr_t)page->begin) % m->node) return false;
        if (cur < (uintptr_t)page->end) return true;
        next = &page->next;
    }
    if (!*next) *next = new_page(m->node);
    if (!*next) return false;
    pool[0] = (int)(uintptr_t)(*next)->begin;
    return true;
}

__declspec(noreturn) void map_failure(int* pool, uint32_t kind)
{
    dsf_log("slots: FATAL provider map %p class %#x: no valid storage for node %u; stopped before a write",
            pool, kind, (uint32_t)pool[2] + 1);
    // Native callers immediately dereference the returned node. A null return
    // cannot recover an exhausted allocation; do not overwrite a neighboring object.
    TerminateProcess(GetCurrentProcess(), ERROR_NOT_ENOUGH_MEMORY);
    __assume(0);
}

int** __fastcall insert(int* pool, void*, int** out, int* pos, const int* kv)
{
    uint32_t kind = map_kind(pool);
    if (!kind) return ((Insert)0x007D7270)(pool, out, pos, kv);
    AcquireSRWLockExclusive(&g_lock);
    Map* m = find_map(pool);
    if (!m || !make_room(m)) {
        ReleaseSRWLockExclusive(&g_lock);
        map_failure(pool, kind);
    }
    int** result = ((Insert)0x007D7270)(pool, out, pos, kv);
    uint32_t used = (uint32_t)pool[2];
    bool peak = used > m->peak;
    if (peak) m->peak = used;
    ReleaseSRWLockExclusive(&g_lock);
    if (peak && used >= kLogFrom)
        dsf_log("slots: provider map %p class %#x holds %u nodes (peak; %u inline)", pool, kind, used, kInlineNodes);
    return result;
}

// 0086BE67: the spec map's node append. With 46 resident models the native append wrote
// node 46 over the sentinel: lookups of lower IDs then miss, and get() 0086B9E0 hands
// out its fallback, the first listed spec, so construction 008D1380 builds that model
// (research/traffic.md, "Vehicle spec manager map").
int* __fastcall spec_append(int* pool, void*, const int* value)
{
    if (!spec_map(pool)) return ((Append)0x008CCAA0)(pool, value);
    AcquireSRWLockExclusive(&g_lock);
    Map* m = find_map(pool, kSpecNodeBytes);
    if (!m || !make_room(m)) {
        ReleaseSRWLockExclusive(&g_lock);
        map_failure(pool, 0xEC);
    }
    int* node = ((Append)0x008CCAA0)(pool, value);
    uint32_t used = (uint32_t)pool[2];
    bool peak = used > m->peak;
    if (peak) m->peak = used;
    ReleaseSRWLockExclusive(&g_lock);
    if (peak && used >= kLogFrom)
        dsf_log("slots: provider map %p class 0xec holds %u nodes (peak; %u inline)", pool, used, kInlineNodes);
    return node;
}

// 00556E30: native registration calls map::operator[](key), then stores profile.
// Replace its separate 45-entry gate only for the verified model-audio provider.
void __fastcall audio_register(uint8_t* self, void*, int profile, int key)
{
    if (*(uint32_t*)self != kAudioVtable) {
        // Preserve the native bound for any other use of this registration method.
        if (*(uint32_t*)(self + 12) >= kInlineNodes) return;
    }
    int* value = ((Index)0x00556F60)((int*)(self + 4), &key);
    *value = profile;
}

// The three of 007D7270's 60 direct callers that insert into these maps (research/traffic.md,
// "Vehicle provider maps and model audio"): the EF registrations at 0086B5C9 and 0086B3DE, and
// operator[] 00556F60's insert at 00556F9B (the ED map and model audio). kCalls are their
// original `call 007D7270` encodings.
constexpr uint32_t kSites[] = {0x0086B5C9, 0x0086B3DE, 0x00556F9B};
const uint8_t kCalls[][5] = {
    {0xE8, 0xA2, 0xBC, 0xF6, 0xFF}, {0xE8, 0x8D, 0xBE, 0xF6, 0xFF}, {0xE8, 0xD0, 0x02, 0x28, 0x00}};
// 00556E30, model-audio registration as shipped: cmp dword [ecx+0Ch],2Dh (the 45-entry gate);
// jae ret; lea eax,[esp+8]; push eax; add ecx,4; call 00556F60; mov ecx,[esp+4]; mov [eax],ecx; ret 8.
const uint8_t kRegister[] = {0x83, 0x79, 0x0C, 0x2D, 0x73, 0x13, 0x8D, 0x44, 0x24, 0x08, 0x50, 0x83,
    0xC1, 0x04, 0xE8, 0x1D, 0x01, 0x00, 0x00, 0x8B, 0x4C, 0x24, 0x04, 0x89, 0x08, 0xC2, 0x08, 0x00};
// Layout checks in the ED (0086C340) and EF (0086B4A0) constructors: lea ecx,[eax+518h] (the
// sentinel), lea ecx,[eax+248h] (the first node), mov dword [eax+234h],0EDh / 0EFh (the class tag).
const uint8_t kCtor[] = {0x8D, 0x88, 0x18, 0x05, 0x00, 0x00};
const uint8_t kNodeStart[] = {0x8D, 0x88, 0x48, 0x02, 0x00, 0x00};
const uint8_t kEdKind[] = {0xC7, 0x80, 0x34, 0x02, 0x00, 0x00, 0xED, 0x00, 0x00, 0x00};
const uint8_t kEfKind[] = {0xC7, 0x80, 0x34, 0x02, 0x00, 0x00, 0xEF, 0x00, 0x00, 0x00};
// Model-audio layout: lookup 00556E90 takes its sentinel at +0x2E0 (add edi,2E0h at 00556EBB), and
// factory 00557000 allocates 0x2F0 bytes (push 2F0h).
const uint8_t kAudioLookup[] = {0x81, 0xC7, 0xE0, 0x02, 0x00, 0x00};
const uint8_t kAudioFactory[] = {0x68, 0xF0, 0x02, 0x00, 0x00};
// Spec manager layout, constructor 0086BFA0: mov dword [esi+248h],0ECh; mov dword [esi],00E395D0h;
// lea eax,[esi+698h] (the sentinel); lea edx,[esi+260h] (the first node). Lookup 0086BDC0: lea
// eax,[ecx+444h] (the sentinel from the map). Append 008CCAA0: cmp dword [ecx+4],0; mov eax,[ecx],
// and its bump at 008CCAFA: add dword [ecx],18h.
const uint8_t kSpecKind[] = {0xC7, 0x86, 0x48, 0x02, 0x00, 0x00, 0xEC, 0x00, 0x00, 0x00};
const uint8_t kSpecVt[] = {0xC7, 0x06, 0xD0, 0x95, 0xE3, 0x00};
const uint8_t kSpecSentinel[] = {0x8D, 0x86, 0x98, 0x06, 0x00, 0x00};
const uint8_t kSpecFirst[] = {0x8D, 0x96, 0x60, 0x02, 0x00, 0x00};
const uint8_t kSpecFind[] = {0x8D, 0x81, 0x44, 0x04, 0x00, 0x00};
const uint8_t kSpecAppend[] = {0x83, 0x79, 0x04, 0x00, 0x8B, 0x01};
const uint8_t kSpecBump[] = {0x83, 0x01, 0x18};
// Vtable 00DE3824's slot 1 (at 00DE3828) is the registration 00556E30.
constexpr uint32_t kAudioRegisterSlot = 0x00DE3828;
bool g_audio_installed = false;

// What isn't the vanilla code, or null when every site and layout matches.
const char* preflight_mismatch()
{
    for (int i = 0; i < 3; i++)
        if (!check_bytes(kSites[i], kCalls[i], 5)) return "an insert call site";
    if (!check_bytes(0x0086B4E4, kCtor, sizeof kCtor) || !check_bytes(0x0086C384, kCtor, sizeof kCtor) ||
        !check_bytes(0x0086B4D8, kNodeStart, sizeof kNodeStart) ||
        !check_bytes(0x0086C378, kNodeStart, sizeof kNodeStart) || !check_bytes(0x0086B4BE, kEfKind, sizeof kEfKind) ||
        !check_bytes(0x0086C35E, kEdKind, sizeof kEdKind))
        return "the ED/EF constructors' layout";
    if (!check_bytes(0x00556E30, kRegister, sizeof kRegister)) return "the model-audio registration 00556E30";
    if (!check_bytes(0x00556EBB, kAudioLookup, sizeof kAudioLookup) ||
        !check_bytes(0x00557000, kAudioFactory, sizeof kAudioFactory))
        return "the model-audio provider's layout";
    if (*(uint32_t*)kAudioRegisterSlot != 0x00556E30) return "vtable 00DE3824's registration slot";
    if (!check_bytes(kSpecSite, kSpecCall, sizeof kSpecCall)) return "the spec map's append call 0086BE67";
    if (!check_bytes(0x0086BFC2, kSpecKind, sizeof kSpecKind) || !check_bytes(0x0086BFD6, kSpecVt, sizeof kSpecVt) ||
        !check_bytes(0x0086BFE2, kSpecSentinel, sizeof kSpecSentinel) ||
        !check_bytes(0x0086BFE8, kSpecFirst, sizeof kSpecFirst) ||
        !check_bytes(0x0086BDC6, kSpecFind, sizeof kSpecFind) ||
        !check_bytes(0x008CCAA0, kSpecAppend, sizeof kSpecAppend) || !check_bytes(0x008CCAFA, kSpecBump, sizeof kSpecBump))
        return "the spec manager's layout";
    return nullptr;
}

bool preflight()
{
    const char* what = preflight_mismatch();
    if (what) dsf_log("slots: %s is not the vanilla code; the ID maps keep their 45 nodes", what);
    return !what;
}

void restore_sites()
{
    for (int i = 0; i < 3; i++) patch_bytes(kSites[i], kCalls[i], 5);
    patch_bytes(kSpecSite, kSpecCall, sizeof kSpecCall);
    patch_bytes(0x00556E30, kRegister, 6);
    g_installed = g_audio_installed = false;
}

}  // namespace

// The insert hooks, and with `audio` the model-audio registration without its 45-entry gate.
// slots_trace at 35 slots passes audio=false: it only logs the maps' peaks and keeps vanilla's gate.
bool vehicle_maps_install(bool audio)
{
    if (g_installed && (g_audio_installed || !audio)) return true;
    if (!g_installed) {
        if (!preflight()) return false;
        for (int i = 0; i < 3; i++) {
            if (!patch_call(kSites[i], (void*)insert, kCalls[i])) {
                for (int j = 0; j < i; j++) patch_bytes(kSites[j], kCalls[j], 5);
                dsf_log("slots: patching insert site %08X failed; the ID maps keep their 45 nodes", kSites[i]);
                return false;
            }
        }
        if (!patch_call(kSpecSite, (void*)spec_append, kSpecCall)) {
            for (int j = 0; j < 3; j++) patch_bytes(kSites[j], kCalls[j], 5);
            dsf_log("slots: patching the spec map's append 0086BE67 failed; the ID maps keep their 45 nodes");
            return false;
        }
        g_installed = true;
    }
    if (audio) {
        if (!patch_jmp(0x00556E30, (void*)audio_register, 6, kRegister)) {
            restore_sites();
            dsf_log("slots: patching 00556E30 failed; the ID maps keep their 45 nodes");
            return false;
        }
        g_audio_installed = true;
        dsf_log("slots: ED/EF, spec and model-audio ID maps continue beyond 45 in page storage; native lookup/removal "
                "retained");
    } else {
        dsf_log("slots_trace: the ID maps' peaks are logged from %u nodes; model audio keeps its 45-entry gate", kLogFrom);
    }
    return true;
}

// Undo every patch of this file (a later slot patch failed, so the slots stay 35).
void vehicle_maps_remove()
{
    if (g_installed) restore_sites();
}

int vehicle_maps_selftest()
{
    int failures = 0;
#define MAP_CHECK(x) do { if (!(x)) { printf("FAIL vehicle maps:%d %s\n", __LINE__, #x); failures++; } } while (0)
    const bool installed = g_installed, audio_installed = g_audio_installed;
    // A hook the capacity checks rely on: without it their inserts run past the 45-node fixture
    // (a stack buffer), so stop there instead of corrupting the test's own stack.
#define MAP_REQUIRE(x) do { if (!(x)) { \
        printf("FAIL vehicle maps:%d %s (stopped before the capacity checks)\n", __LINE__, #x); \
        restore_sites(); \
        if (installed) vehicle_maps_install(audio_installed); \
        return failures + 1; } } while (0)
    if (installed) restore_sites();
    const Index index = (Index)0x00556F60;
    using Ctor = void*(__thiscall*)(void*);
    using Remove = void(__thiscall*)(void*, int);               // 00556E50, model audio's removal by key
    using RemoveKey = int(__thiscall*)(int*, const int*);       // 00556FB0, the map template's erase
    using Register = void(__thiscall*)(void*, int, int);
    using Lookup = int(__thiscall*)(void*, int);
    uint8_t provider[0x534] = {};
    auto reset = [&](uint32_t kind) {
        ((Ctor)(kind == 0xED ? 0x0086C340 : 0x0086B4A0))(provider);
        memset(provider + 0x524, 0xAB, 16);
    };
    auto guard = [&]() {
        for (int i = 0; i < 16; i++) if (provider[0x524 + i] != 0xAB) return false;
        return true;
    };
    int* pool = (int*)(provider + 0x23C);
    int* head = (int*)((uint8_t*)pool + kHead);
    // The original insertion46 writes its value at owner+0x524, outside the
    // 0x524-byte provider. Keep spare guarded storage so this reproduction is bounded.
    reset(0xEF);
    for (int key = 0; key < 46; key++) *index(pool, &key) = 1000 + key;
    MAP_CHECK(!guard() && pool[2] == 46);
    reset(0xEF);

    // The spec manager's map, set up as 0086BFA0 does (without its class-factory call). Models
    // register through operator[] 0086BE10 and are read back through get() 0086B9E0, as
    // vehicle construction 008D1380 reads them. Spec k's model is [[spec + 4850h] + 14h]
    // (0088C8F0): spec k sits at specs + 16k, so its +4850h is a pointer to infos[k].
    using SpecIndex = int*(__thiscall*)(int*, const int*);   // 0086BE10: &node->spec
    using SpecGet = void**(__thiscall*)(void*, void**, uint32_t);
    using SpecErase = int(__thiscall*)(int*, const int*);   // 0086BE90
    static uint8_t spec_owner[0x6A4 + 0x40];
    static uint8_t specs[0x4850 + 128 * 16];
    static uint32_t infos[128][6];
    int* spec_pool = (int*)(spec_owner + kSpecMap);
    auto spec_reset = [&]() {
        memset(spec_owner, 0, sizeof spec_owner);
        memset(spec_owner + 0x6A4, 0xAB, 0x40);  // past the 0x6A4-byte object (factory 0086C070)
        *(uint32_t*)spec_owner = kSpecVtable;
        *(uint32_t*)(spec_owner + 0x248) = 0xEC;
        *(uint8_t**)(spec_owner + 0x254) = spec_owner + 0x260;
        *(uint8_t**)(spec_owner + 0x698) = spec_owner + 0x698;
        *(uint8_t**)(spec_owner + 0x69C) = spec_owner + 0x698;
    };
    auto spec_guard = [&]() {
        for (int i = 0; i < 0x40; i++) if (spec_owner[0x6A4 + i] != 0xAB) return false;
        return true;
    };
    // Model IDs: the player's car 3 registers first (the owner's RX-7 in vm1-20261114-132735),
    // then traffic models 100, 102, ...; slot k holds model ids[k].
    int ids[128];
    for (int k = 0; k < 128; k++) {
        ids[k] = k ? 98 + 2 * k : 3;
        infos[k][5] = (uint32_t)ids[k];
        *(uint32_t**)(specs + 0x4850 + 16 * k) = infos[k];
    }
    auto spec_register = [&](int k) {
        int* value = ((SpecIndex)0x0086BE10)(spec_pool, &ids[k]);
        value[0] = (int)(uintptr_t)(specs + 16 * k);
        value[1] = 0;  // +10h: state; get() skips nodes above 0
    };
    auto spec_model = [&](int id) {
        void* spec = nullptr;
        ((SpecGet)0x0086B9E0)(spec_owner, &spec, (uint32_t)id);
        return spec ? (int)*(uint32_t*)(*(uint8_t**)((uint8_t*)spec + 0x4850) + 0x14) : -1;
    };
    // As shipped: 45 models resolve to themselves; a 46th (here 151, between two others)
    // overwrites the sentinel, and every lower ID then resolves to another model.
    spec_reset();
    for (int k = 0; k < 45; k++) spec_register(k);
    int wrong = 0;
    for (int k = 0; k < 45; k++) wrong += spec_model(ids[k]) != ids[k];
    MAP_CHECK(wrong == 0 && spec_pool[2] == 45 && spec_guard());
    {
        int id46 = 151;
        int* value = ((SpecIndex)0x0086BE10)(spec_pool, &id46);
        value[0] = (int)(uintptr_t)(specs + 16 * 127);
        value[1] = 0;
    }
    wrong = 0;
    for (int k = 0; k < 45; k++) wrong += spec_model(ids[k]) != ids[k];
    MAP_CHECK(wrong >= 27 && spec_model(3) != 3 && !spec_guard());
    printf("vehicle maps: as shipped, 46 specs: %d of 45 earlier models resolve to another (model 3 -> %d)\n", wrong,
           spec_model(3));
    spec_reset();

    // A late preflight mismatch must leave every earlier site unmodified.
    *(uint8_t*)0x00556EBB ^= 1;
    MAP_CHECK(!vehicle_maps_install(true));
    for (int i = 0; i < 3; i++) MAP_CHECK(bytes_equal(kSites[i], kCalls[i], 5));
    MAP_CHECK(bytes_equal(0x00556E30, kRegister, 6));
    *(uint8_t*)0x00556EBB ^= 1;

    // slots_trace (audio=false): ED/EF still continue past 45, model audio keeps vanilla's gate.
    MAP_REQUIRE(vehicle_maps_install(false) && bytes_equal(0x00556E30, kRegister, 6));
    {
        reset(0xEF);
        for (int key = 0; key < 50; key++) *index(pool, &key) = key;
        MAP_CHECK(pool[2] == 50 && guard());
        uint8_t gated[0x300] = {};
        ((void*(__thiscall*)(void*))0x00556EE0)(gated);
        for (int key = 0; key < 46; key++) ((void(__thiscall*)(void*, int, int))0x00556E30)(gated, key, key);
        MAP_CHECK(*(int*)(gated + 12) == 45);
    }
    // vehicle_maps_remove() (a later slot patch failed) restores every site, the audio gate too.
    MAP_CHECK(vehicle_maps_install(true) && !bytes_equal(0x00556E30, kRegister, 6));
    vehicle_maps_remove();
    for (int i = 0; i < 3; i++) MAP_CHECK(bytes_equal(kSites[i], kCalls[i], 5));
    MAP_CHECK(bytes_equal(0x00556E30, kRegister, 6) && !g_installed && !g_audio_installed);
    MAP_REQUIRE(vehicle_maps_install(true));

    // With the append hooked: 128 specs (45 inline, 83 on a page) all resolve to themselves,
    // the object's end stays untouched, and native erase/re-register recycles both kinds of node.
    spec_reset();
    for (int k = 0; k < 128; k++) spec_register(k);
    MAP_CHECK(spec_pool[2] == 128 && spec_guard());
    wrong = 0;
    for (int k = 0; k < 128; k++) wrong += spec_model(ids[k]) != ids[k];
    MAP_CHECK(wrong == 0);
    for (int k : {0, 100, 127}) {
        ((SpecErase)0x0086BE90)(spec_pool, &ids[k]);
        MAP_CHECK(spec_pool[2] == 127 && spec_pool[1] == 1 && spec_model(ids[k]) != ids[k]);
        spec_register(k);
        MAP_CHECK(spec_pool[2] == 128 && spec_pool[1] == 0 && spec_model(ids[k]) == ids[k]);
    }
    // A missing ID still gets get()'s native fallback, the first listed spec (model 3).
    MAP_CHECK(spec_model(999) == 3 && spec_guard());
    for (int k = 0; k < 128; k++) ((SpecErase)0x0086BE90)(spec_pool, &ids[k]);
    MAP_CHECK(spec_pool[2] == 0 && spec_pool[1] == 128);
    // Another manager at the same address (a new session) starts inline again and reuses the
    // pages; 400 models cross into a third page (170 nodes of 24 bytes per 4 KiB page).
    spec_reset();
    static int many[400];
    for (int k = 0; k < 400; k++) {
        many[k] = 1000 + k;
        int* value = ((SpecIndex)0x0086BE10)(spec_pool, &many[k]);
        value[0] = (int)(uintptr_t)(specs + 16 * (k % 128));
        value[1] = 0;
    }
    MAP_CHECK(spec_pool[2] == 400 && spec_guard());
    {
        int count = 0, last = -1;
        bool ordered = true;
        uint8_t* sentinel = spec_owner + 0x698;
        for (uint8_t* n = *(uint8_t**)(sentinel + 4); n != sentinel && count <= 400; n = *(uint8_t**)(n + 4)) {
            int key = *(int*)(n + 8);
            ordered &= key > last;
            last = key;
            count++;
        }
        MAP_CHECK(count == 400 && ordered);
    }
    spec_reset();

    for (uint32_t kind : {0xEDu, 0xEFu}) {
        reset(kind);
        int* values[64] = {};
        for (int key = 63; key >= 0; key--) {
            values[key] = index(pool, &key);
            *values[key] = 1000 + key;
        }
        MAP_CHECK(pool[2] == 64 && guard());
        int count = 0;
        for (int* n = (int*)(uintptr_t)head[1]; n != head && count <= 64; n = (int*)(uintptr_t)n[1]) {
            MAP_CHECK(n[2] == count && n[3] == 1000 + count);
            MAP_CHECK(((int*)(uintptr_t)n[1])[0] == (int)(uintptr_t)n);
            count++;
        }
        MAP_CHECK(count == 64);
        for (int key = 0; key < 64; key++) MAP_CHECK(index(pool, &key) == values[key]);
        MAP_CHECK(pool[2] == 64);  // duplicate keys do not allocate
        // Native unlink/free-list recycling of an external node, then an inline node.
        int keys[2] = {0, 63};
        for (int key : keys) {
            int* old = values[key];
            ((RemoveKey)0x00556FB0)(pool, &key);
            MAP_CHECK(pool[1] == 1 && pool[2] == 63);
            MAP_CHECK(index(pool, &key) == old && pool[1] == 0 && pool[2] == 64);
        }
        MAP_CHECK(guard());
    }
    // Cross more than one page, recycle all nodes with the native remover, then
    // reset the owner at the same address and reuse its storage.
    reset(0xEF);
    for (int key = 0; key < 600; key++) *index(pool, &key) = key + 1;
    MAP_CHECK(pool[2] == 600 && guard());
    for (int key = 0; key < 600; key++) ((RemoveKey)0x00556FB0)(pool, &key);
    MAP_CHECK(pool[2] == 0 && pool[1] == 600);
    Map* state = find_map(pool);
    Page* first_page = state->pages;
    auto page_count = [&]() {
        int count = 0;
        for (Page* page = state->pages; page; page = page->next) count++;
        return count;
    };
    int pages_before = page_count();
    reset(0xED);
    for (int key = 0; key < 64; key++) {
        int* value = index(pool, &key);
        // The first external node must reuse the original page, not just retain
        // its list head while silently allocating another page.
        if (key == (int)kInlineNodes) MAP_CHECK(value == (int*)(first_page->begin + 12));
        *value = key;
    }
    MAP_CHECK(state->pages == first_page && page_count() == pages_before && pool[2] == 64 && guard());

    // More distinct owners than the old fixed-size provider bookkeeping. Every
    // owner must get storage without exhausting a plugin registry after reloads.
    uint8_t owners[40][0x534] = {};
    for (auto& owner : owners) {
        ((Ctor)0x0086B4A0)(owner);
        memset(owner + 0x524, 0xAB, 16);
        int* map = (int*)(owner + 0x23C);
        for (int key = 0; key < 64; key++) *index(map, &key) = key;
        MAP_CHECK(map[2] == 64);
        for (int i = 0; i < 16; i++) MAP_CHECK(owner[0x524 + i] == 0xAB);
    }

    // The audio provider's native registration/lookup/removal use the same map.
    uint8_t audio[0x300] = {};
    ((Ctor)0x00556EE0)(audio);
    memset(audio + 0x2F0, 0xAB, 16);
    for (int key = 0; key < 64; key++) ((Register)0x00556E30)(audio, key + 100, key);
    MAP_CHECK(*(int*)(audio + 12) == 64);
    for (int key = 0; key < 64; key++) MAP_CHECK(((Lookup)0x00556E90)(audio, key) == key + 100);
    MAP_CHECK(((Lookup)0x00556E90)(audio, 1000) == 0);
    ((Register)0x00556E30)(audio, 9000, 5);
    MAP_CHECK(*(int*)(audio + 12) == 64 && ((Lookup)0x00556E90)(audio, 5) == 9000);
    ((Remove)0x00556E50)(audio, 5);
    MAP_CHECK(((Lookup)0x00556E90)(audio, 5) == 0 && *(int*)(audio + 12) == 63);
    for (int i = 0; i < 16; i++) MAP_CHECK(audio[0x2F0 + i] == 0xAB);
    ((Ctor)0x00556EE0)(audio);  // another session at the same owner address
    *(uint32_t*)(audio + 0x2EC) = 0x12345678;  // the sentinel's value field: the common/default profile
    for (int key = 0; key < 64; key++) ((Register)0x00556E30)(audio, key + 200, key);
    MAP_CHECK(*(int*)(audio + 12) == 64 && *(uint32_t*)(audio + 0x2EC) == 0x12345678);
    for (int key = 0; key < 64; key++) MAP_CHECK(((Lookup)0x00556E90)(audio, key) == key + 200);

    // Allocation refusal must leave both the cursor and adjacent storage untouched.
    reset(0xEF);
    for (int key = 0; key < 45; key++) *index(pool, &key) = key;
    int cursor = pool[0];
    Map refusal = {};
    refusal.pool = pool;
    g_fail_allocation = true;
    MAP_CHECK(!make_room(&refusal) && pool[0] == cursor && guard());
    g_fail_allocation = false;

    // Unrelated native map users retain the original path and 45-entry gate.
    ((Ctor)0x00556EE0)(audio);
    *(uint32_t*)audio = 0;
    for (int key = 0; key < 46; key++) ((Register)0x00556E30)(audio, key, key);
    MAP_CHECK(*(int*)(audio + 12) == 45);
    restore_sites();
    if (installed) MAP_CHECK(vehicle_maps_install(audio_installed));
    printf("vehicle maps selftest: %d failures\n", failures);
#undef MAP_CHECK
#undef MAP_REQUIRE
    return failures;
}

