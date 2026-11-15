// Full chapter traffic pools, streamed through the native model-reference API.
// Evidence and source layouts: research/runtime-traffic-pools.md.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <float.h>
#include <initializer_list>
#include "patch.h"

bool archive_package_loaded(int id);
uint32_t slots_expected();  // mod_archive.cpp: the kind-0 vehicle slots the game has (or will have)

namespace {
// Package base IDs stay below 1000; 59 AI sets, five frequencies, byte range indices.
constexpr uint32_t kModels = 1000, kMaxRows = 59 * 5 * 256;
const char kFeatureMarker[] = "DSFCARS-TRAFFIC-POOLS: 2";
// 005A6D20: +5B0..+998 stores 50 twenty-byte nontraffic nodes.
constexpr uint32_t kNodes = (0x998 - 0x5B0) / 20;
// slots: the vehicle slots the manager planned the caps for. reserve: the slots the cap rule
// leaves to the set's native list, the mission pot, the player and the interesting vehicles
// (runtime_traffic.py live_reserve), so live_cap() can apply the rule to the slots the game got.
struct Header { char magic[8]; uint32_t rows, pairs, packages, slots; };
struct Row { uint32_t hash, frequency, start, count, signature, first, size, live_cap, reserve; float total; };
struct Pair { uint32_t id; float weight; uint32_t package; };
struct Range { uint32_t start, count; float total; };
static_assert(sizeof(Header) == 24 && sizeof(Row) == 40 && sizeof(Pair) == 12, "pool file layout");
void* g_file;
Header* g_header;
Row* g_rows;
Pair* g_pairs;
uint8_t g_packages[kModels], g_valid[kModels], g_seen[kModels];
uint32_t g_unique;
uint8_t g_pending[kModels], g_retiring[kModels];
volatile uint8_t g_spec_failed[kModels];  // our lease's model loaded with no spec (spec_register)
int g_selected = -1;
bool g_rotate_selected;
uint8_t* g_owner;
DWORD g_thread;
bool g_force_skip; // native offline fixture only
bool g_weight_warning;
bool g_validation_logged;
uint32_t g_fixture_slots;   // native offline fixture only: the slots live_cap() sees
uint32_t g_logged_slots;    // the slot count live_cap() last logged
using Pick = int(__thiscall*)(const Range*, uint32_t);
using Request = bool(__thiscall*)(void*, uint32_t);
using Release = void(__thiscall*)(void*, uint32_t);
using RefCount = uint32_t(__thiscall*)(void*, uint32_t);
using Set = void(__thiscall*)(void*, uint32_t, uint32_t);
using Ctor = void*(__thiscall*)(void*);
using Clear = void(__thiscall*)(void*);
using Capture = void(__thiscall*)(void*, uint32_t*);
Ctor g_ctor;
Clear g_clear, g_restore;
Set g_set;

uint8_t* engine() { return *(uint8_t**)0x00FE7228; }
uint8_t* spooler() { uint8_t* e = engine(); return e ? *(uint8_t**)(e + 0x214) : nullptr; }
// The spooler's state and its one active queue operation (research/runtime-traffic-pools.md,
// "Spooler state and active operation", confirmed-static): +DAC 2 = loading +D9C (005A7B33),
// 3 = unloading +D9C (005A7AFD), 5 = reset, no requests or releases (005A6DED, 005A7480;
// 005A7860 and 005A78D0 return at once). +D9C is the active operation's model ID and +DA4 its
// reference count, both copied from the queue head by 005A7A50 (005A7A69, 005A7A7E).
constexpr uint32_t kSpoolState = 0xDAC, kActiveId = 0xD9C;
constexpr uint32_t kStateUnloading = 3, kStateReset = 5;

uint32_t fnv(const void* data, size_t size, uint32_t hash = 2166136261u)
{
    const uint8_t* p = (const uint8_t*)data;
    while (size--) hash = (hash ^ *p++) * 16777619u;
    return hash;
}

void forget(const char* reason, bool release)
{
    uint8_t* owner = g_owner;
    g_owner = nullptr; g_selected = -1; g_rotate_selected = false;
    // Retiring flags stay: they track served models' native owners (vehicles
    // that release through 008D4F8A -> 005A78D0), which none of these
    // transitions cancels. decide() clears a flag only when the model has left
    // the resident lists (005A6EA0's tests), as after a native reset.
    for (uint32_t id = 0; id < kModels; ++id) if (g_pending[id]) {
        g_pending[id] = 0;
        if (release && owner && owner == spooler() && *(uint32_t*)(owner + kSpoolState) != kStateReset) {
            // 005A6B00 prefers a resident node, even a zero-ref unloading one,
            // over its queued reload. Release exactly our known ownership.
            // 005A7100 skips the currently unloading ID (005A7123..005A7133); its
            // queued-load release helper 005A66E0 cancels that reload explicitly.
            if (*(uint32_t*)(owner + kSpoolState) == kStateUnloading && *(uint32_t*)(owner + kActiveId) == id)
                ((Request)0x005A66E0)(owner, id);
            ((Release)0x005A78D0)(owner, id);
            dsf_log("live-pool: cancel %u (%s)", id, reason);
        } else dsf_log("live-pool: drop %u (%s)", id, reason);
    }
}

// 0091689A..009168BB keeps the native RNG result extended until multiplication,
// then rounds once before subtracting float weights. State size is 0x48 bytes.
float draw(uint32_t seed, float total, bool second = false)
{
    uint32_t rng[18]; float x;
    __asm {
        push seed
        lea ecx, rng
        mov eax, 00866510h
        call eax
        lea ecx, rng
        mov eax, 00866320h
        call eax
        cmp second, 0
        je use_draw
        fstp st(0)
        lea ecx, rng
        mov eax, 00866320h
        call eax
    use_draw:
        fmul total
        fstp x
    }
    return x;
}

const Pair* sample(const Pair* choices, uint32_t size, float total, uint32_t seed,
                   const uint8_t* valid = nullptr, const uint8_t* ready = nullptr,
                   int added_group = -1, bool second = false)
{
    if (valid || ready || added_group != -1) {
        float filtered_total = 0;
        bool filtered = false;
        for (uint32_t i = 0; i < size; ++i)
            if ((!valid || valid[choices[i].id]) && (!ready || ready[choices[i].id]) &&
                (added_group == -1 || (choices[i].package != 0) == (added_group != 0))) filtered_total += choices[i].weight;
            else if (choices[i].weight > 0) filtered = true;
        if (filtered) total = filtered_total;
    }
    if (total <= 0) return nullptr;
    float x = draw(seed, total, second);
    for (uint32_t i = 0; i < size; ++i) {
        if ((valid && !valid[choices[i].id]) || (ready && !ready[choices[i].id])) continue;
        if (added_group != -1 && (choices[i].package != 0) != (added_group != 0)) continue;
        x -= choices[i].weight;
        if (x < 0) return choices + i;
    }
    return nullptr;
}

bool ready_models(uint8_t* s, uint8_t* ready)
{
    memset(ready, 0, kModels);
    // Same positive tests/fields as 005A6EA0; traverse each list once rather
    // than scanning it for each of the 721 candidates.
    uint8_t* head = s + 0x330;
    uint8_t* p = *(uint8_t**)(head + 4);
    uint32_t count = 0;
    for (; p != head && count < kNodes; p = *(uint8_t**)(p + 4), ++count) {
        uint32_t id = *(uint16_t*)(p + 8);
        if (id < kModels) ready[id] = 1;
    }
    if (p != head) return false;
    head = s + 0x998; p = *(uint8_t**)(head + 4); count = 0;
    for (; p != head && count < kNodes; p = *(uint8_t**)(p + 4), ++count) {
        uint32_t id = *(uint32_t*)(p + 8);
        if (id < kModels && *(int*)(p + 16) > 0) ready[id] = 1;
    }
    return p == head;
}

// 005A6EA0's tests (above) say a model is listed: requested and owned. Vehicle
// construction 008D1380 needs more. It builds the vehicle from the spec that the
// spec manager [Engine+0x778] get(id) (vtable +0x14, 0086B9E0) returns, and it
// requests that spec's model ([[spec+0x4850]+0x14], 0088C8F0; 008D144A, 008D1475).
// A model's spec registers only once its UPVH has loaded (0086C0C0 -> vtable
// +0x48), and until then get() returns the first listed spec, the lowest resident
// ID (research/traffic.md, "Vehicle spec manager map"). A model loaded while 45 specs
// exist gets none at all: specs come from a 45-object pool (class 0xF8, 0088A4F2) and
// registration 0086BAB0 skips when it is empty. Vanilla traffic never asks for such a
// model, since a set is fully loaded before traffic starts and 35 slots stay under 45.
bool spec_ready(uint32_t id)
{
    using SpecGet = void**(__thiscall*)(void*, void**, uint32_t);
    uint8_t* e = engine();
    void* manager = e ? *(void**)(e + 0x778) : nullptr;
    if (!manager) return true;  // no manager: nothing to check against; native readiness stands
    void* spec = nullptr;
    ((SpecGet)0x0086B9E0)(manager, &spec, id);
    if (!spec) return false;
    uint8_t* info = *(uint8_t**)((uint8_t*)spec + 0x4850);
    return info && *(uint32_t*)(info + 0x14) == id;
}

// Free vehicle specs. Registration 0086BAB0 takes each spec from the class-0xF8 pool of the
// allocator at [0144BCFC] (00888610 -> 00887A90; +0x38 storage, +0x3C one used flag per object,
// +0x48 count, 45 from 0088A4F2) and registers nothing when it is empty. Its +0x4C only counts
// down (free 008879F0 never adds back), so count the clear flags (set at 00887AFD, cleared at
// 00887A28). -1: no allocator (the offline fixture), nothing known.
constexpr uint32_t kSpecAllocator = 0x0144BCFC;
int spec_free()
{
    uint8_t* a = *(uint8_t**)kSpecAllocator;
    if (!a) return -1;
    const uint8_t* used = *(uint8_t**)(a + 0x3C);
    uint32_t count = *(uint32_t*)(a + 0x48);
    if (!used || count > kModels) return -1;
    int free = 0;
    for (uint32_t i = 0; i < count; ++i) free += !used[i];
    return free;
}

// Spec registration, vtable 00E395D0 slot +0x48 (00E39618 -> 0086BCF0): the UPVH payload's model
// ID is at +0x14 (0086BCFA). A model registers once, when it loads (0086C0C0), so a lease whose
// model comes out of registration without its spec never gets spawnable; decide() releases it.
using SpecRegister = void(__thiscall*)(void*, uint8_t*);
constexpr uint32_t kSpecRegisterSlot = 0x00E39618, kSpecRegister = 0x0086BCF0;
void __fastcall spec_register(void* manager, void*, uint8_t* payload)
{
    ((SpecRegister)kSpecRegister)(manager, payload);
    uint32_t id = payload ? *(uint32_t*)(payload + 0x14) : kModels;
    if (id < kModels && g_pending[id] && !spec_ready(id)) g_spec_failed[id] = 1;
}

// Release our leases whose models loaded without a spec, on the picker's thread.
void release_specless(uint8_t* s)
{
    for (uint32_t id = 0; id < kModels; ++id) if (g_spec_failed[id]) {
        g_spec_failed[id] = 0;
        if (!g_pending[id] || g_owner != s || *(uint32_t*)(s + kSpoolState) == kStateReset) continue;
        g_pending[id] = 0;
        if (*(uint32_t*)(s + kSpoolState) == kStateUnloading && *(uint32_t*)(s + kActiveId) == id)
            ((Request)0x005A66E0)(s, id);
        ((Release)0x005A78D0)(s, id);
        dsf_log("live-pool: release %u: it loaded with no spec (%d of the spec pool free)", id, spec_free());
    }
}

bool valid_model(uint32_t id, bool package)
{
    if (id >= kModels) return false;
    if (!g_valid[id]) {
        using Info = void*(__thiscall*)(void*, uint32_t);
        void* provider = *(void**)(engine() + 0x780);
        bool valid = provider && (!package || archive_package_loaded(id)) && ((Info)0x0086C5B0)(provider, id);
        g_valid[id] = valid ? 1 : 2;
    }
    return g_valid[id] == 1;
}

Row* find_row(const Range* range)
{
    if (!g_header) return nullptr;
    uint8_t* e = engine();
    if (!e) return nullptr;
    uint8_t* tm = *(uint8_t**)(e + 0x758);
    if (!tm || *(int*)(tm + 0x5CA9F0) != -1) return nullptr; // preserve model override
    uint32_t hash = *(uint32_t*)(tm + 0x5CA840), frequency = *(uint32_t*)(tm + 0x5CA844);
    for (uint32_t i = 0; i < g_header->rows; ++i) {
        Row& r = g_rows[i];
        if (r.hash == hash && r.frequency == frequency && r.start == range->start && r.count == range->count) {
            const uint8_t* native = *(uint8_t**)0x016DA940 + range->start * 8;
            uint32_t signature = fnv(native, range->count * 8, fnv(range, sizeof(Range)));
            if (signature != r.signature) {
                if (!g_weight_warning) dsf_log("live-pool: native row %u/%u hash %08X != %08X; traffic unchanged",
                        r.start,r.count,signature,r.signature);
                g_weight_warning=true; forget("traffic weights changed", true); return nullptr;
            }
            return &r;
        }
    }
    return nullptr;
}

// The row's streaming cap for the slots the game has. pools.bin's caps follow
// runtime_traffic.py live_cap() at the planned slots (header): max(0, min(slots - reserve,
// 50 - pot - 11, ports)). With fewer slots (the slot raise failed its preflight and stayed at
// 35, or the ini changed after the manager ran) min(live_cap, slots - reserve) is the same rule
// at those slots. Past them the spool refuses requests and the provider maps outgrow their
// inline nodes with no page storage (vehicle_maps.cpp is installed only with the raise).
// More slots than planned keep the planned cap.
uint32_t live_cap(const Row* row)
{
    uint32_t slots = g_fixture_slots ? g_fixture_slots : slots_expected();
    if (slots != g_logged_slots) {
        g_logged_slots = slots;
        if (slots < g_header->slots)
            dsf_log("live-pool: pools.bin was planned for %u vehicle slots, the game has %u; streaming caps "
                    "follow %u slots", g_header->slots, slots, slots);
        else
            dsf_log("live-pool: %u vehicle slots (pools.bin planned for %u)", slots, g_header->slots);
    }
    if (slots >= g_header->slots) return row->live_cap;
    uint32_t room = slots > row->reserve ? slots - row->reserve : 0;
    return room < row->live_cap ? room : row->live_cap;
}

// A request is useful only if its model can get a spec: the free specs must exceed our leases
// still waiting for theirs (a model without one never spawns; spec_free()).
bool spec_room()
{
    int free = spec_free();
    if (free < 0) return true;
    int waiting = 0;
    for (uint32_t id = 0; id < kModels; ++id) waiting += g_pending[id] && !spec_ready(id);
    return free > waiting;
}

int __fastcall decide(const Range* range, void*, uint32_t seed)
{
    if (g_thread && GetCurrentThreadId() != g_thread) return ((Pick)0x00916870)(range, seed);
    g_selected = -1; g_rotate_selected = false;
    if (g_force_skip) return -1;
    Row* row = find_row(range);
    uint8_t* s = spooler();
    if (!row || !s || *(uint32_t*)(s + kSpoolState) == kStateReset) return ((Pick)0x00916870)(range, seed);
    if (!g_thread) g_thread = GetCurrentThreadId();
    uint8_t ready[kModels], valid[kModels] = {};
    if (!ready_models(s, ready)) return ((Pick)0x00916870)(range, seed);
    const Pair* choices = g_pairs + row->first;
    for (uint32_t i = 0; i < row->size; ++i) {
        valid[choices[i].id] = valid_model(choices[i].id, (choices[i].package & 1) != 0);
    }
    if (!g_validation_logged) {
        uint32_t wanted=0, accepted=0;
        for (uint32_t i=0; i<row->size; ++i) if ((choices[i].package & 1) && choices[i].weight>0) {
            ++wanted; accepted += valid[choices[i].id] != 0;
        }
        dsf_log("live-pool: active set %08X/%u validates %u of %u positive package choices",
                row->hash,row->frequency,accepted,wanted);
        g_validation_logged=true;
    }
    if (g_owner && g_owner != s) forget("owner replaced", false);
    release_specless(s);
    uint8_t dynamic[kModels]; memcpy(dynamic, g_packages, sizeof dynamic);
    uint32_t added = 0;
    for (uint32_t i = 0; i < row->size; ++i) if (choices[i].package) {
        dynamic[choices[i].id] = 1;
        added += valid[choices[i].id] && choices[i].weight > 0;
    }
    uint32_t occupied = 0;
    for (uint32_t id = 0; id < kModels; ++id) {
        occupied += (ready[id] && (dynamic[id] || g_retiring[id])) || g_pending[id];
        if (g_retiring[id] && !ready[id]) {
            dsf_log("live-pool: retired %u through native cleanup", id);
            g_retiring[id] = 0;
        }
    }
    // A pool that fits keeps ordinary weighted reuse. A larger pool gives
    // each loaded added model one primary birth, then lets native owners
    // drain it. This explicit variety policy avoids an immortal small cache.
    uint32_t cap = live_cap(row);
    bool rotate = added > cap;
    const Pair* chosen = sample(choices, row->size, row->total, seed, valid);
    if (chosen && chosen->package && !ready[chosen->id] && !g_pending[chosen->id] && occupied < cap &&
        spec_room()) {
        // +9A0 owns fifty pending nodes, independent of the single active
        // loader at +D9C. 005A6FD0 admits multiple queued model requests.
        if (*(uint32_t*)(s + 0x9A8) < kNodes && ((Request)0x005A7860)(s, chosen->id)) {
            g_pending[chosen->id] = 1; g_owner = s;
            dsf_log("live-pool: request %u, %u choices, package cache %u/%u, thread %lu", chosen->id,
                    row->size, occupied, cap, g_thread);
        }
    }
    // Retain the chosen native/added group, redistributing only within its
    // resident models. Use the next native RNG draw so a conditional full
    // draw isn't reused as a biased fallback draw. Cold groups fall back to
    // the other group until a model is ready. Retired models cannot fill a
    // loading gap; otherwise the first ready model populates whole roads.
    // A listed model is spawnable only once the spec manager resolves it: until
    // its spec registers, construction would build the first listed spec instead.
    int group = chosen ? (chosen->package != 0) : -1;
    for (uint32_t i = 0; i < row->size; ++i)
        if ((choices[i].package && g_retiring[choices[i].id]) || (ready[choices[i].id] && !spec_ready(choices[i].id)))
            ready[choices[i].id] = 0;
    const Pair* fallback = chosen && ready[chosen->id] ? chosen :
            sample(choices,row->size,row->total,seed,valid,ready,group,true);
    if (!fallback && group != -1) fallback = sample(choices,row->size,row->total,seed,valid,ready,!group,true);
    if (fallback && fallback->package) {
        g_selected = fallback->id; g_rotate_selected = rotate;
    }
    return fallback ? (int)fallback->id : -1;
}

// Call site 0077ADCB is before any vehicle/appearance allocation. The native
// failed-spawn epilogue (0077B2C6) returns the out pointer with *out == null.
__declspec(naked) void primary_pick()
{
    __asm {
        push dword ptr [esp + 4]
        call decide
        test eax, eax
        js no_model
        ret 4
    no_model:
        add esp, 8
        mov eax, dword ptr [esp + 0D8h]
        mov dword ptr [eax], 0
        pop edi
        pop esi
        pop ebp
        pop ebx
        add esp, 0C4h
        ret 10h
    }
}

bool __fastcall retain(void* s, void*, uint32_t id)
{
    bool result = ((Request)0x005A7860)(s, id);
    if (GetCurrentThreadId() == g_thread && result && id < kModels && g_pending[id] && g_owner == s) {
        unsigned before = ((RefCount)0x005A6B00)(s, id);
        g_pending[id] = 0;
        ((Release)0x005A78D0)(s, id);
        dsf_log("live-pool: transfer %u to vehicle, refs %u -> %u", id, before, ((RefCount)0x005A6B00)(s, id));
    }
    if (GetCurrentThreadId() == g_thread && result && id < kModels && g_selected == (int)id) {
        if (g_rotate_selected) {
            g_retiring[id] = 1;
            dsf_log("live-pool: served %u, waiting for native owners before reuse", id);
        }
        g_selected = -1; g_rotate_selected = false;
    }
    if (GetCurrentThreadId() == g_thread && result && id < kModels && g_packages[id] && !g_seen[id]) {
        g_seen[id]=1;
        dsf_log("live-pool: vehicle %u, unique %u", id, ++g_unique);
    }
    return result;
}

uint32_t __fastcall capture_refs(void* s, void*, uint32_t id)
{
    uint32_t refs = ((RefCount)0x005A6B00)(s, id);
    // 0074B7A0 serializes owners' counts, not this transient prefetch lease.
    return refs && s == g_owner && id < kModels && g_pending[id] ? refs - 1 : refs;
}

void __fastcall restore(void* self, void*) { forget("replay restore", true); g_restore(self); }
void* __fastcall construct(void* self, void*)
{
    forget("spooler constructed", false); memset(g_valid, 0, sizeof g_valid); g_validation_logged=false; return g_ctor(self);
}
void __fastcall clear(void* self, void*)
{
    if (self == g_owner || self == spooler()) forget("native reset", false);
    memset(g_valid, 0, sizeof g_valid); g_validation_logged=false; g_clear(self);
}
void __fastcall set(void* self, void*, uint32_t hash, uint32_t frequency)
{
    forget("traffic set change", true); memset(g_valid, 0, sizeof g_valid); g_validation_logged=false; g_set(self, hash, frequency);
}

bool bind_file(void* bytes, size_t size)
{
    if (size < sizeof(Header)) return false;
    Header* h = (Header*)bytes;
    if (memcmp(h->magic, "DSFPOOL2", 8) || h->rows > kMaxRows || h->packages > kModels || h->slots > kModels ||
        (uint64_t)h->pairs > (uint64_t)h->rows * kModels) return false;
    uint64_t expected = sizeof(Header) + (uint64_t)h->packages * 4 + (uint64_t)h->rows * sizeof(Row) +
                        (uint64_t)h->pairs * sizeof(Pair);
    if (expected != size) return false;
    uint32_t* ids = (uint32_t*)(h + 1);
    Row* rows = (Row*)(ids + h->packages);
    Pair* pairs = (Pair*)(rows + h->rows);
    for (uint32_t i = 0; i < h->packages; ++i) if (ids[i] >= kModels) return false;
    for (uint32_t i = 0; i < h->rows; ++i) {
        const Row& r = rows[i];
        if (r.frequency >= 5 || r.count > kModels || r.size > kModels || r.first > h->pairs ||
            r.size > h->pairs - r.first || r.live_cap > kNodes || r.reserve > kModels || !_finite(r.total) ||
            r.total < 0) return false;
    }
    for (uint32_t i = 0; i < h->pairs; ++i)
        if (pairs[i].id >= kModels || pairs[i].package > 2 || !_finite(pairs[i].weight) || pairs[i].weight < 0) return false;
    memset(g_packages, 0, sizeof g_packages);
    for (uint32_t i = 0; i < h->packages; ++i) g_packages[ids[i]] = 1;
    g_header = h; g_rows = rows; g_pairs = pairs;
    return true;
}

bool read_file()
{
    char relative[MAX_PATH], path[MAX_PATH];
    cfg_str("traffic", "pool_file", "dsfcars\\traffic\\pools.bin", relative, sizeof relative);
    GetModuleFileNameA(nullptr, path, sizeof path);
    char* slash = strrchr(path, '\\');
    if (!slash) return false;
    slash[1] = 0;
    if (strcat_s(path, relative)) return false;
    FILE* f = nullptr;
    if (fopen_s(&f, path, "rb") || !f) return false;
    if (fseek(f, 0, SEEK_END)) { fclose(f); return false; }
    long size = ftell(f);
    if (size < (long)sizeof(Header)) { fclose(f); return false; }
    rewind(f);
    void* data = malloc(size);
    bool valid = data && fread(data, 1, size, f) == (size_t)size && bind_file(data, size);
    fclose(f);
    if (!valid) { free(data); dsf_log("live-pool: invalid file %s; traffic unchanged", path); return false; }
    g_file = data;
    dsf_log("live-pool: %u rows, %u weighted choices, %u package IDs, planned for %u vehicle slots in %s",
            g_header->rows, g_header->pairs, g_header->packages, g_header->slots, path);
    return true;
}

struct Site { uint32_t address; uint8_t bytes[10]; unsigned size; void* hook; bool call; void** original; };
Site g_sites[] = {
    {0x0077ADCB,{0xE8,0xA0,0xBA,0x19,0x00},5,(void*)primary_pick,true,nullptr},
    {0x008D1475,{0xE8,0xE6,0x63,0xCD,0xFF},5,(void*)retain,true,nullptr},
    {0x005A6D20,{0x51,0x53,0x56,0x8B,0xF1},5,(void*)construct,false,(void**)&g_ctor},
    {0x005A7360,{0x56,0x8B,0xF1,0x83,0xBE,0xAC,0x0D,0x00,0x00,0x05},10,(void*)clear,false,(void**)&g_clear},
    {0x0076FDF0,{0x81,0xEC,0xA0,0x00,0x00,0x00},6,(void*)set,false,(void**)&g_set},
    {0x0074B8B6,{0xE8,0x45,0xB2,0xE5,0xFF},5,(void*)capture_refs,true,nullptr},
    {0x0074BA20,{0x81,0xEC,0x74,0x02,0x00,0x00},6,(void*)restore,false,(void**)&g_restore},
};

void* trampoline(const Site& site)
{
    uint8_t* p = (uint8_t*)alloc_exec(site.size + 5);
    if (!p) return nullptr;
    memcpy(p, site.bytes, site.size); p[site.size] = 0xE9;
    *(uint32_t*)(p + site.size + 1) = site.address + site.size - (uint32_t)(p + site.size + 5);
    return p;
}

bool install()
{
    const uint8_t capture[] = {0x81,0xEC,0xD8,0x04,0x00,0x00};
    if (!check_bytes(0x0074B7A0, capture, sizeof capture)) return false;
    if (*(uint32_t*)kSpecRegisterSlot != kSpecRegister) return false;
    for (const Site& s : g_sites) if (!check_bytes(s.address, s.bytes, s.size)) return false;
    for (const Site& s : g_sites) {
        if (!s.original) continue;
        *s.original = trampoline(s);
        if (!*s.original) return false;
    }
    for (const Site& s : g_sites) {
        bool ok = s.call ? patch_call(s.address, s.hook, s.bytes) : patch_jmp(s.address, s.hook, s.size, s.bytes);
        if (!ok) { for (const Site& saved : g_sites) patch_bytes(saved.address, saved.bytes, saved.size); return false; }
    }
    uint32_t hook = (uint32_t)(uintptr_t)spec_register, original = kSpecRegister;
    if (!patch_bytes(kSpecRegisterSlot, &hook, 4, &original)) {
        for (const Site& saved : g_sites) patch_bytes(saved.address, saved.bytes, saved.size);
        return false;
    }
    return true;
}

void uninstall()
{
    for (const Site& saved : g_sites) patch_bytes(saved.address, saved.bytes, saved.size);
    uint32_t original = kSpecRegister;
    patch_bytes(kSpecRegisterSlot, &original, 4);
}
} // namespace

void mod_traffic_init()
{
    dsf_log("%s", kFeatureMarker);
    if (!cfg_bool("traffic", "runtime_pool", true)) {
        dsf_log("live-pool: [traffic] runtime_pool=0; traffic unchanged");
        return;
    }
    if (!read_file()) return;
    uint32_t slots = slots_expected();
    if (slots < g_header->slots)
        dsf_log("live-pool: the game will have %u vehicle slots, pools.bin was planned for %u; streaming caps "
                "follow %u slots", slots, g_header->slots, slots);
    g_logged_slots = slots;
    dsf_log("live-pool: native runtime selection installed %d", install());
}

static bool __fastcall fixture_exists(void*, void*, uint32_t, uint32_t) { return true; }
static uint32_t __fastcall fixture_capacity(void*, void*, uint32_t, uint32_t) { return 60; }

int mod_traffic_selftest()
{
    int failures = 0;
#define LIVE_CHECK(x) do { if (!(x)) { printf("FAIL live-pool:%d %s\n", __LINE__, #x); ++failures; } } while (0)
    uint8_t e[0x784] = {};
    uint8_t* tm = (uint8_t*)calloc(1, 0x5CA9F4);
    uint8_t s[0xE6C] = {};
    if (!tm) return 1;
    void* savedEngine = *(void**)0x00FE7228;
    void* savedPairs = *(void**)0x016DA940;
    *(void**)0x00FE7228 = e; *(void**)(e + 0x758) = tm; *(void**)(e + 0x214) = s;
    *(int*)(tm + 0x5CA9F0) = -1;
    g_fixture_slots = 60;
    // The spec allocator [0144BCFC] as 0088A4C0 lays out its class-0xF8 pool: storage +0x38,
    // used flags +0x3C, count +0x48, free head +0x50 (-1: none free). Full until a case frees one.
    void* savedAllocator = *(void**)kSpecAllocator;
    static uint8_t allocator[0x70], specUsed[45], specStorage[16];
    memset(allocator, 0, sizeof allocator); memset(specUsed, 1, sizeof specUsed);
    *(uint8_t**)(allocator + 0x38) = specStorage; *(uint8_t**)(allocator + 0x3C) = specUsed;
    *(uint32_t*)(allocator + 0x48) = 45; *(int*)(allocator + 0x50) = -1;
    struct NativePair { uint32_t id; float weight; } native[] = {{1,3}, {2,1}, {700,2}, {701,3.5f}};
    Pair choices[] = {{1,3,0}, {2,1,0}, {700,2,1}, {701,3.5f,1}};
    *(void**)0x016DA940 = native;
    Range range = {0,4,9.5f};
    for (uint32_t seed = 0; seed < 10000; ++seed) {
        const Pair* actual = sample(choices,4,range.total,seed);
        int expected = ((Pick)0x00916870)(&range,seed);
        if (!actual || actual->id != expected) { LIVE_CHECK(actual && actual->id == expected); break; }
    }
    uint8_t ready[kModels] = {}; ready[1] = ready[700] = 1;
    native[1].weight = native[3].weight = 0; range.total = 5;
    for (uint32_t seed = 0; seed < 10000; ++seed) {
        const Pair* actual = sample(choices,4,9.5f,seed,nullptr,ready);
        int expected = ((Pick)0x00916870)(&range,seed);
        if (!actual || actual->id != expected) { LIVE_CHECK(actual && actual->id == expected); break; }
    }
    LIVE_CHECK(fnv("hello",5) == 0x4F9F2CABu);
    struct { Header h; uint32_t id; Row row; Pair choices[4]; } file = {};
    memcpy(file.h.magic,"DSFPOOL2",8); file.h.rows=1; file.h.pairs=4; file.h.packages=1; file.h.slots=60; file.id=700;
    file.row.count=4; file.row.size=4; file.row.live_cap=26; file.row.total=9.5f;
    memcpy(file.choices,choices,sizeof choices);
    LIVE_CHECK(bind_file(&file,sizeof file));
    LIVE_CHECK(!bind_file(&file,sizeof file-1));
    file.row.first=0xFFFFFFFF; LIVE_CHECK(!bind_file(&file,sizeof file)); file.row.first=0;
    file.h.pairs=0xFFFFFFFF; LIVE_CHECK(!bind_file(&file,sizeof file)); file.h.pairs=4;
    *(uint32_t*)&file.choices[0].weight=0x7FC00000; LIVE_CHECK(!bind_file(&file,sizeof file)); file.choices[0].weight=3;
    LIVE_CHECK(bind_file(&file,sizeof file));
    uint8_t existsBytes[5]; memcpy(existsBytes,(void*)0x0059C620,5);
    LIVE_CHECK(patch_jmp(0x0059C620,(void*)fixture_exists,5,existsBytes));
    uint8_t capacityBytes[5]; memcpy(capacityBytes,(void*)0x0059C5C0,5);
    LIVE_CHECK(patch_jmp(0x0059C5C0,(void*)fixture_capacity,5,capacityBytes));
    uint32_t node[5] = {};
    uint32_t* head = (uint32_t*)(s+0x330); head[0]=head[1]=(uint32_t)head;
    head=(uint32_t*)(s+0x998); head[0]=head[1]=(uint32_t)node;
    node[0]=node[1]=(uint32_t)head; node[2]=700; node[4]=1;
    *(uint32_t*)(s+0x5AC)=1;
    head=(uint32_t*)(s+0xD94); head[0]=head[1]=(uint32_t)head;
    *(uint32_t*)(s+0x9A0)=(uint32_t)(s+0x9AC);
    *(int*)(s+0xDBC)=*(int*)(s+0xDC0)=*(int*)(s+0xDB0)=-1; *(int*)(s+0xDC4)=1;
    g_thread=GetCurrentThreadId(); g_owner=s; g_pending[700]=1;
    // A resident fallback must keep the native/added group chosen by the
    // full weighted draw. One of two added models is resident here.
    uint32_t stock1[3]={},stock2[3]={};
    uint32_t* trafficHead=(uint32_t*)(s+0x330);
    trafficHead[0]=(uint32_t)stock2; trafficHead[1]=(uint32_t)stock1;
    stock1[0]=(uint32_t)trafficHead; stock1[1]=(uint32_t)stock2; stock1[2]=1;
    stock2[0]=(uint32_t)stock1; stock2[1]=(uint32_t)trafficHead; stock2[2]=2;
    native[1].weight=1; native[3].weight=3.5f; range.total=9.5f;
    file.row.signature=fnv(native,sizeof native,fnv(&range,sizeof range));
    for(uint32_t id:{1u,2u,700u,701u}) g_valid[id]=1;
    for(uint32_t seed=0;seed<10000;++seed) {
        int expected=((Pick)0x00916870)(&range,seed);
        int actual=decide(&range,nullptr,seed);
        if((actual>=700)!=(expected>=700) || actual==701) {
            LIVE_CHECK((actual>=700)==(expected>=700) && actual!=701); break;
        }
    }
    // A listed model whose spec hasn't registered yet is not spawnable: the spec
    // manager (its map laid out as 0086BFA0 does, models registered through
    // operator[] 0086BE10) would hand construction the first listed spec instead.
    {
        static uint8_t manager[0x6A4 + 0x20];
        static uint8_t specs[0x4850 + 4 * 16];
        static uint32_t infos[4][6];
        memset(manager, 0, sizeof manager);
        *(uint8_t**)(manager + 0x254) = manager + 0x260;
        *(uint8_t**)(manager + 0x698) = manager + 0x698;
        *(uint8_t**)(manager + 0x69C) = manager + 0x698;
        uint32_t ids[4] = {1, 2, 700, 701};
        auto add = [&](int k) {
            infos[k][5] = ids[k];
            *(uint32_t**)(specs + 0x4850 + 16 * k) = infos[k];
            int* value = ((int*(__thiscall*)(void*, const uint32_t*))0x0086BE10)(manager + 0x254, &ids[k]);
            value[0] = (int)(uintptr_t)(specs + 16 * k); value[1] = 0;
        };
        add(0); add(1);
        *(void**)(e + 0x778) = manager;
        bool listed_only = false;
        for(uint32_t seed=0;seed<10000 && !listed_only;++seed) listed_only = decide(&range,nullptr,seed)==700;
        LIVE_CHECK(!listed_only && !spec_ready(700) && spec_ready(1));
        add(2);
        bool spawned = false;
        for(uint32_t seed=0;seed<10000 && !spawned;++seed) spawned = decide(&range,nullptr,seed)==700;
        LIVE_CHECK(spawned && spec_ready(700));
        // Registration with the spec pool full (native 0086BCF0 through the hook; 00887A90 finds
        // the free head at -1): 701 stays without its spec, so our queued lease on it is released.
        *(void**)kSpecAllocator = allocator;
        const bool held701 = g_pending[701] != 0;  // decide()'s earlier draws may hold a lease on it
        if (!held701) LIVE_CHECK(((Request)0x005A7860)(s, 701));
        uint32_t refs_before = ((RefCount)0x005A6B00)(s, 701);
        LIVE_CHECK(refs_before >= 1);
        g_pending[701] = 1; g_owner = s;
        LIVE_CHECK(spec_free() == 0 && !spec_room());  // 701 waits, no spec free
        uint8_t payload[0x18] = {}; *(uint32_t*)(payload + 0x14) = 701;
        uint32_t used = *(uint32_t*)(manager + 0x25C);
        spec_register(manager, nullptr, payload);
        LIVE_CHECK(*(uint32_t*)(manager + 0x25C) == used && !spec_ready(701) && g_spec_failed[701]);
        // A registration of a model we hold no lease on, or one that has its spec, marks nothing.
        *(uint32_t*)(payload + 0x14) = 1; g_pending[1] = 1;
        spec_register(manager, nullptr, payload);
        LIVE_CHECK(!g_spec_failed[1]); g_pending[1] = 0;
        release_specless(s);
        LIVE_CHECK(!g_pending[701] && !g_spec_failed[701] && ((RefCount)0x005A6B00)(s, 701) == refs_before - 1);
        if (held701) { LIVE_CHECK(((Request)0x005A7860)(s, 701)); g_pending[701] = 1; }  // as the later cases expect
        *(void**)kSpecAllocator = nullptr;
        *(void**)(e + 0x778) = nullptr; g_selected = -1; g_rotate_selected = false;
    }
    // A ready first model must not fill whole roads while its peers load.
    // After its first actual birth it drains, retaining the vehicle's owner.
    file.row.live_cap=1;
    for(uint32_t seed=0;seed<10000;++seed) {
        if(((Pick)0x00916870)(&range,seed)!=700) continue;
        LIVE_CHECK(decide(&range,nullptr,seed)==700);
        LIVE_CHECK(retain(s,nullptr,700));
        LIVE_CHECK(g_retiring[700] && !g_pending[700] && node[4]==1);
        break;
    }
    for(uint32_t seed=0;seed<10000;++seed) {
        int actual=decide(&range,nullptr,seed);
        if(actual>=700 || node[4]!=1) { LIVE_CHECK(actual<700 && node[4]==1); break; }
    }
    // The native vehicle cleanup API releases its own reference and queues
    // unloading only after the last reference is gone.
    ((Release)0x005A78D0)(s,700);
    LIVE_CHECK(node[4]==0 && *(uint32_t*)(s+0x9A8)==2);
    uint32_t* queued=(uint32_t*)head[1]; LIVE_CHECK(queued[2]==700 && queued[3]==1 && queued[4]==1);
    // Native request admits multiple cold models. In-flight leases consume
    // our budget too; repeated demand never acquires a second owned lease.
    head[0]=head[1]=(uint32_t)head; *(uint32_t*)(s+0x9A0)=(uint32_t)(s+0x9AC);
    *(uint32_t*)(s+0x9A8)=0;
    uint32_t* residentHead=(uint32_t*)(s+0x998);
    residentHead[0]=residentHead[1]=(uint32_t)residentHead; *(uint32_t*)(s+0x5AC)=0;
    forget("cold fixture",false); file.row.live_cap=2;
    file.choices[1]={702,1,1}; g_valid[702]=1;
    uint32_t seeds[3]={};
    for(uint32_t seed=0;seed<10000;++seed) {
        const Pair* pick=sample(file.choices,4,9.5f,seed);
        if(pick && pick->id>=700 && pick->id<=702) seeds[pick->id-700]=seed;
    }
    // pools.bin planned for 60 slots, the game at 35 (the slot raise failed its preflight):
    // the cap is the same rule at 35, min(live_cap, slots - reserve); more slots keep the plan.
    g_fixture_slots=35; file.row.reserve=35;
    LIVE_CHECK(live_cap(&file.row)==0);
    LIVE_CHECK(decide(&range,nullptr,seeds[0])<700 && !g_pending[700] && *(uint32_t*)(s+0x9A8)==0);
    file.row.reserve=34; LIVE_CHECK(live_cap(&file.row)==1);
    LIVE_CHECK(decide(&range,nullptr,seeds[0])<700 && g_pending[700] && *(uint32_t*)(s+0x9A8)==1);
    LIVE_CHECK(decide(&range,nullptr,seeds[1])<700 && !g_pending[701] && *(uint32_t*)(s+0x9A8)==1);
    g_fixture_slots=70; LIVE_CHECK(live_cap(&file.row)==2);
    forget("slot clamp fixture",true);
    LIVE_CHECK(!g_pending[700] && *(uint32_t*)(s+0x9A8)==0);
    head[0]=head[1]=(uint32_t)head; *(uint32_t*)(s+0x9A0)=(uint32_t)(s+0x9AC);
    *(uint32_t*)(s+0x9A4)=*(uint32_t*)(s+0x9A8)=0;
    g_fixture_slots=60; file.row.reserve=0;
    // No free spec: a request would load a model that never gets one (0086BAB0), so none is made.
    *(void**)kSpecAllocator=allocator;
    LIVE_CHECK(spec_free()==0 && !spec_room());
    LIVE_CHECK(decide(&range,nullptr,seeds[0])<700 && !g_pending[700] && *(uint32_t*)(s+0x9A8)==0);
    specUsed[7]=0; LIVE_CHECK(spec_free()==1 && spec_room());
    LIVE_CHECK(decide(&range,nullptr,seeds[0])<700 && g_pending[700] && *(uint32_t*)(s+0x9A8)==1);
    forget("spec room fixture",true);
    LIVE_CHECK(!g_pending[700] && *(uint32_t*)(s+0x9A8)==0);
    head[0]=head[1]=(uint32_t)head; *(uint32_t*)(s+0x9A0)=(uint32_t)(s+0x9AC);
    *(uint32_t*)(s+0x9A4)=*(uint32_t*)(s+0x9A8)=0;
    specUsed[7]=1; *(void**)kSpecAllocator=nullptr;
    LIVE_CHECK(decide(&range,nullptr,seeds[0])<700 && g_pending[700]);
    LIVE_CHECK(decide(&range,nullptr,seeds[0])<700 && *(uint32_t*)(s+0x9A8)==1);
    queued=(uint32_t*)head[1]; LIVE_CHECK(queued[2]==700 && queued[3]==0 && queued[4]==1);
    LIVE_CHECK(decide(&range,nullptr,seeds[1])<700 && g_pending[701] && *(uint32_t*)(s+0x9A8)==2);
    LIVE_CHECK(decide(&range,nullptr,seeds[2])<700 && !g_pending[702] && *(uint32_t*)(s+0x9A8)==2);
    LIVE_CHECK(((RefCount)0x005A6B00)(s,700)==1 && ((RefCount)0x005A6B00)(s,701)==1);
    forget("cancel queued fixture",true);
    LIVE_CHECK(!g_pending[700] && !g_pending[701] && *(uint32_t*)(s+0x9A8)==0);
    // Cancelling the sole active-load lease schedules its native unload (state 2: loading +D9C,
    // +DA4 its references; 005A7100 at 005A7147..005A717C). Other owners' references remain.
    *(uint32_t*)(s+0xDAC)=2; *(uint32_t*)(s+0xD9C)=702; *(uint32_t*)(s+0xDA4)=1;
    g_pending[702]=1; g_owner=s; forget("cancel active fixture",true);
    queued=(uint32_t*)head[1]; LIVE_CHECK(queued[2]==702 && queued[3]==1 && queued[4]==1);
    head[0]=head[1]=(uint32_t)head; *(uint32_t*)(s+0x9A0)=(uint32_t)(s+0x9AC);
    *(uint32_t*)(s+0x9A4)=*(uint32_t*)(s+0x9A8)=0; *(uint32_t*)(s+0xDA4)=3;
    g_pending[702]=1; g_owner=s; forget("cancel shared active fixture",true);
    LIVE_CHECK(*(uint32_t*)(s+0xDA4)==2 && *(uint32_t*)(s+0x9A8)==0);
    // Native RefCount's zero-ref resident node masks a queued reload. Native
    // Release ignores the active unloading ID, so cancel its queued owner.
    residentHead[0]=residentHead[1]=(uint32_t)node; node[0]=node[1]=(uint32_t)residentHead; node[4]=0;
    *(uint32_t*)(s+0xDAC)=3; *(uint32_t*)(s+0xD9C)=700;
    LIVE_CHECK(((Request)0x005A7860)(s,700)); g_pending[700]=1; g_owner=s;
    LIVE_CHECK(((RefCount)0x005A6B00)(s,700)==0 && *(uint32_t*)(s+0x9A8)==1);
    forget("cancel unloading reload fixture",true);
    LIVE_CHECK(!g_pending[700] && *(uint32_t*)(s+0x9A8)==0 && node[4]==0);
    residentHead[0]=residentHead[1]=(uint32_t)residentHead;
    *(uint32_t*)(s+0xDAC)=0; memcpy(file.choices,choices,sizeof choices);
    // Two models can drain concurrently; neither keeps the other waiting.
    uint32_t node2[5]={}; residentHead[0]=(uint32_t)node2; residentHead[1]=(uint32_t)node;
    node[0]=(uint32_t)residentHead; node[1]=(uint32_t)node2; node[4]=1;
    node2[0]=(uint32_t)node; node2[1]=(uint32_t)residentHead; node2[2]=701; node2[4]=1;
    *(uint32_t*)(s+0x5AC)=2; g_pending[700]=g_pending[701]=1; g_owner=s; file.row.live_cap=1;
    for(uint32_t id:{700u,701u}) {
        for(uint32_t seed=0;seed<10000;++seed) {
            if(((Pick)0x00916870)(&range,seed)!=(int)id) continue;
            LIVE_CHECK(decide(&range,nullptr,seed)==(int)id && retain(s,nullptr,id)); break;
        }
    }
    LIVE_CHECK(g_retiring[700] && g_retiring[701] && node[4]==1 && node2[4]==1);
    LIVE_CHECK(!g_pending[700] && !g_pending[701]);
    // A lifecycle transition (replay restore, set change, owner change) drops
    // only our leases. Served models keep their vehicles' references, so they
    // must stay out of fallback draws: with their flags cleared, every added-
    // group draw fell back to the resident served models (#592 host report).
    uint32_t queuedBefore=*(uint32_t*)(s+0x9A8);
    forget("lifecycle fixture",false);
    LIVE_CHECK(g_retiring[700] && g_retiring[701]);
    {
        uint32_t added=0, reused=0;
        for(uint32_t seed=0;seed<10000;++seed) {
            added += ((Pick)0x00916870)(&range,seed)>=700;
            reused += decide(&range,nullptr,seed)>=700;
        }
        LIVE_CHECK(added>0 && reused==0 && *(uint32_t*)(s+0x9A8)==queuedBefore);
    }
    // A flag clears once its model has left the resident lists.
    g_retiring[702]=1; decide(&range,nullptr,0);
    LIVE_CHECK(!g_retiring[702] && g_retiring[700] && g_retiring[701]);
    // Compare actual replay capture before/after the hook for both leases.
    // No transient model reference may survive restoration.
    trafficHead[0]=trafficHead[1]=(uint32_t)trafficHead;
    forget("replay fixture",false); g_pending[700]=g_pending[701]=1; g_owner=s;
    uint32_t snapshot[156] = {}, captured[157] = {};
    captured[153]=0xAABBCCDD;
    ((Capture)0x0074B7A0)(snapshot,captured);
    LIVE_CHECK(captured[0x33]==2 && captured[0x34]==700 && captured[0x35]==1 && captured[0x37]==1);
    *(uint8_t*)0x0074B7A0 ^= 1;
    LIVE_CHECK(!install());
    for(const Site& site:g_sites) LIVE_CHECK(bytes_equal(site.address,site.bytes,site.size));
    LIVE_CHECK(*(uint32_t*)kSpecRegisterSlot==kSpecRegister);
    *(uint8_t*)0x0074B7A0 ^= 1;
    LIVE_CHECK(install());
    ((Capture)0x0074B7A0)(snapshot,captured);
    LIVE_CHECK(captured[0x33]==2 && captured[0x34]==700 && captured[0x35]==0 && node[4]==1);
    LIVE_CHECK(captured[0x36]==701 && captured[0x37]==0 && node2[4]==1);
    LIVE_CHECK(captured[153]==0xAABBCCDD);
    node[4]=2; LIVE_CHECK(capture_refs(s,nullptr,700)==1); node[4]=1;
    memcpy(snapshot+3,captured,153*4);
    ((Clear)0x0074BA20)(snapshot);
    LIVE_CHECK(!g_pending[700] && !g_pending[701] && node[4]==0 && node2[4]==0 && snapshot[2]==2);
    // Execute the real spawn prologue and failure epilogue, proving the
    // no-ready-model path balances its four stack arguments and out pointer.
    using Spawn = uint32_t*(__thiscall*)(void*,uint32_t*,uint32_t,const Range*,int);
    g_force_skip=true; uint32_t out=0xDEADBEEF;
    LIVE_CHECK(((Spawn)0x0077ADA0)(tm,&out,42,&range,0)==&out && out==0);
    g_force_skip=false;
    g_pending[700]=g_pending[701]=1; g_owner=s; ((Clear)0x005A7360)(s);
    LIVE_CHECK(!g_pending[700] && !g_pending[701] && *(uint32_t*)(s+0xDAC)==5);
    // Native reset unloads the models; the flags clear once decide() sees them gone.
    g_retiring[700]=g_retiring[701]=1; ((Clear)0x005A7360)(s);
    LIVE_CHECK(g_retiring[700] && g_retiring[701]);
    memset(g_retiring,0,sizeof g_retiring);
    LIVE_CHECK(*(uint32_t*)kSpecRegisterSlot==(uint32_t)(uintptr_t)spec_register);
    uninstall();
    for(const Site& site:g_sites) LIVE_CHECK(bytes_equal(site.address,site.bytes,site.size));
    LIVE_CHECK(*(uint32_t*)kSpecRegisterSlot==kSpecRegister);
    *(void**)kSpecAllocator=savedAllocator;
    LIVE_CHECK(patch_bytes(0x0059C620,existsBytes,5));
    LIVE_CHECK(patch_bytes(0x0059C5C0,capacityBytes,5));
    *(void**)0x00FE7228=savedEngine; *(void**)0x016DA940=savedPairs; free(tm);
    g_header=nullptr; g_rows=nullptr; g_pairs=nullptr; g_thread=0; g_unique=0; g_fixture_slots=0; g_logged_slots=0;
    memset(g_packages,0,sizeof g_packages); memset(g_valid,0,sizeof g_valid); memset(g_seen,0,sizeof g_seen);
    printf("live-pool native fixture: %d failures\n",failures);
    return failures;
#undef LIVE_CHECK
}
