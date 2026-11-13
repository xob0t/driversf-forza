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
constexpr uint32_t kAudioVtable = 0x00DE3824;     // Engine+0x280's model-audio provider (factory 00557000)
// Peaks are logged from 40 nodes: below vanilla's own peak of 43 at 35 slots
// (research/traffic.md), so slots_trace at 35 shows them. A logging choice, not a limit.
constexpr uint32_t kLogFrom = 40;
// Log label for the model-audio map; the audio provider has no class tag of its own.
constexpr uint32_t kAudioLabel = 0xA0;
using Insert = int**(__thiscall*)(int*, int**, int*, const int*);  // 007D7270, the map template's insert
using Index = int*(__thiscall*)(int*, const int*);                  // 00556F60, operator[]

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

Map* find_map(int* pool)
{
    for (Map* m = g_maps; m; m = m->next)
        if (m->pool == pool) return m;
    if (g_fail_allocation) return nullptr;
    Map* m = (Map*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(Map));
    if (!m) return nullptr;
    m->pool = pool;
    m->next = g_maps;
    g_maps = m;
    return m;
}

Page* new_page()
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
    page->end = page->begin + info.dwPageSize;
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
    uintptr_t begin = (uintptr_t)(pool + 3), end = (uintptr_t)pool + kHead;
    Page** next = &m->pages;
    if (cur >= begin && cur < end && (cur - begin) % kNodeBytes == 0) return true;
    if (cur != end) {
        Page* page = m->pages;
        while (page && !(cur >= (uintptr_t)page->begin && cur <= (uintptr_t)page->end)) page = page->next;
        if (!page || (cur - (uintptr_t)page->begin) % kNodeBytes) return false;
        if (cur < (uintptr_t)page->end) return true;
        next = &page->next;
    }
    if (!*next) *next = new_page();
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
        g_installed = true;
    }
    if (audio) {
        if (!patch_jmp(0x00556E30, (void*)audio_register, 6, kRegister)) {
            restore_sites();
            dsf_log("slots: patching 00556E30 failed; the ID maps keep their 45 nodes");
            return false;
        }
        g_audio_installed = true;
        dsf_log("slots: ED/EF and model-audio ID maps continue beyond 45 in page storage; native lookup/removal retained");
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

