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

namespace {
// Package base IDs stay below 1000; 59 AI sets, five frequencies, byte range indices.
constexpr uint32_t kModels = 1000, kMaxRows = 59 * 5 * 256;
const char kFeatureMarker[] = "DSFCARS-TRAFFIC-POOLS: 1";
// 005A6D20: +5B0..+998 stores 50 twenty-byte nontraffic nodes.
constexpr uint32_t kNodes = (0x998 - 0x5B0) / 20;
struct Header { char magic[8]; uint32_t rows, pairs, packages; };
struct Row { uint32_t hash, frequency, start, count, signature, first, size, live_cap; float total; };
struct Pair { uint32_t id; float weight; uint32_t package; };
struct Range { uint32_t start, count; float total; };
static_assert(sizeof(Header) == 20 && sizeof(Row) == 36 && sizeof(Pair) == 12, "pool file layout");
void* g_file;
Header* g_header;
Row* g_rows;
Pair* g_pairs;
uint8_t g_packages[kModels], g_valid[kModels], g_seen[kModels];
uint32_t g_unique;
int g_pending = -1;
uint8_t* g_owner;
DWORD g_thread;
bool g_force_skip; // native offline fixture only
bool g_weight_warning;
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

uint32_t fnv(const void* data, size_t size, uint32_t hash = 2166136261u)
{
    const uint8_t* p = (const uint8_t*)data;
    while (size--) hash = (hash ^ *p++) * 16777619u;
    return hash;
}

void forget(const char* reason, bool release)
{
    if (g_pending < 0) return;
    int id = g_pending;
    uint8_t* owner = g_owner;
    g_pending = -1; g_owner = nullptr;
    if (release && owner == spooler() && *(uint32_t*)(owner + 0xDAC) != 5) {
        unsigned refs = ((RefCount)0x005A6B00)(owner, id);
        if (refs) ((Release)0x005A78D0)(owner, id);
        dsf_log("live-pool: cancel %d (%s), refs %u -> %u", id, reason, refs,
                ((RefCount)0x005A6B00)(owner, id));
    } else dsf_log("live-pool: drop %d (%s)", id, reason);
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

int __fastcall decide(const Range* range, void*, uint32_t seed)
{
    if (g_force_skip) return -1;
    Row* row = find_row(range);
    uint8_t* s = spooler();
    if (!row || !s || *(uint32_t*)(s + 0xDAC) == 5) return ((Pick)0x00916870)(range, seed);
    if (!g_thread) g_thread = GetCurrentThreadId();
    if (GetCurrentThreadId() != g_thread) return ((Pick)0x00916870)(range, seed);
    uint8_t ready[kModels], valid[kModels] = {};
    if (!ready_models(s, ready)) return ((Pick)0x00916870)(range, seed);
    const Pair* choices = g_pairs + row->first;
    for (uint32_t i = 0; i < row->size; ++i) {
        valid[choices[i].id] = valid_model(choices[i].id, (choices[i].package & 1) != 0);
    }
    if (g_pending >= 0) {
        if (g_owner != s) forget("owner replaced", false);
    }
    const Pair* chosen = sample(choices, row->size, row->total, seed, valid);
    if (chosen && ready[chosen->id]) return chosen->id;
    if (chosen && g_pending < 0) {
        uint8_t dynamic[kModels]; memcpy(dynamic, g_packages, sizeof dynamic);
        for (uint32_t i = 0; i < row->size; ++i) if (choices[i].package & 2) dynamic[choices[i].id] = 1;
        uint32_t loaded = 0;
        for (uint32_t id = 0; id < kModels; ++id) loaded += ready[id] && dynamic[id];
        if (loaded < row->live_cap && ((Request)0x005A7860)(s, chosen->id)) {
            g_pending = chosen->id; g_owner = s;
            dsf_log("live-pool: request %u, %u choices, package cache %u/%u, refs %u, thread %lu", chosen->id,
                    row->size, loaded, row->live_cap, ((RefCount)0x005A6B00)(s, chosen->id), g_thread);
        }
    }
    // Retain the chosen native/added group, redistributing only within its
    // resident models. Use the next native RNG draw so a conditional full
    // draw isn't reused as a biased fallback draw. Cold groups fall back to
    // the other group until a model is ready. No eligible model means failure.
    int group = chosen ? (chosen->package != 0) : -1;
    const Pair* fallback = sample(choices,row->size,row->total,seed,valid,ready,group,true);
    if (!fallback && group != -1) fallback = sample(choices,row->size,row->total,seed,valid,ready,!group,true);
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
    if (GetCurrentThreadId() == g_thread && result && g_pending == (int)id && g_owner == s) {
        unsigned before = ((RefCount)0x005A6B00)(s, id);
        g_pending = -1; g_owner = nullptr;
        ((Release)0x005A78D0)(s, id);
        dsf_log("live-pool: transfer %u to vehicle, refs %u -> %u", id, before, ((RefCount)0x005A6B00)(s, id));
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
    return refs && s == g_owner && (int)id == g_pending ? refs - 1 : refs;
}

void __fastcall restore(void* self, void*) { forget("replay restore", true); g_restore(self); }
void* __fastcall construct(void* self, void*)
{
    forget("spooler constructed", false); memset(g_valid, 0, sizeof g_valid); return g_ctor(self);
}
void __fastcall clear(void* self, void*)
{
    if (self == g_owner) forget("native reset", false);
    memset(g_valid, 0, sizeof g_valid); g_clear(self);
}
void __fastcall set(void* self, void*, uint32_t hash, uint32_t frequency)
{
    forget("traffic set change", true); memset(g_valid, 0, sizeof g_valid); g_set(self, hash, frequency);
}

bool bind_file(void* bytes, size_t size)
{
    if (size < sizeof(Header)) return false;
    Header* h = (Header*)bytes;
    if (memcmp(h->magic, "DSFPOOL1", 8) || h->rows > kMaxRows || h->packages > kModels ||
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
            r.size > h->pairs - r.first || r.live_cap > kNodes || !_finite(r.total) || r.total < 0) return false;
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
    dsf_log("live-pool: %u rows, %u weighted choices, %u package IDs in %s", g_header->rows,
            g_header->pairs, g_header->packages, path);
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
    return true;
}
} // namespace

void mod_traffic_init()
{
    dsf_log("%s", kFeatureMarker);
    if (!cfg_bool("traffic", "runtime_pool", true) || !read_file()) return;
    dsf_log("live-pool: native runtime selection installed %d", install());
}

static bool __fastcall fixture_exists(void*, void*, uint32_t, uint32_t) { return true; }

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
    memcpy(file.h.magic,"DSFPOOL1",8); file.h.rows=1; file.h.pairs=4; file.h.packages=1; file.id=700;
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
    uint32_t node[5] = {};
    uint32_t* head = (uint32_t*)(s+0x330); head[0]=head[1]=(uint32_t)head;
    head=(uint32_t*)(s+0x998); head[0]=head[1]=(uint32_t)node;
    node[0]=node[1]=(uint32_t)head; node[2]=700; node[4]=1;
    *(uint32_t*)(s+0x5AC)=1;
    head=(uint32_t*)(s+0xD94); head[0]=head[1]=(uint32_t)head;
    *(uint32_t*)(s+0x9A0)=(uint32_t)(s+0x9AC);
    *(int*)(s+0xDBC)=*(int*)(s+0xDC0)=*(int*)(s+0xDB0)=-1; *(int*)(s+0xDC4)=1;
    g_thread=GetCurrentThreadId(); g_owner=s; g_pending=700;
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
    trafficHead[0]=trafficHead[1]=(uint32_t)trafficHead;
    LIVE_CHECK(retain(s,nullptr,700));
    LIVE_CHECK(node[4]==1 && g_pending==-1 && g_owner==nullptr);
    // The native vehicle cleanup API releases its own reference and queues
    // unloading only after the last reference is gone.
    ((Release)0x005A78D0)(s,700);
    LIVE_CHECK(node[4]==0 && *(uint32_t*)(s+0x9A8)==1);
    uint32_t* queued=(uint32_t*)head[1]; LIVE_CHECK(queued[2]==700 && queued[3]==1 && queued[4]==1);
    // Reset the synthetic queue, then compare native replay capture before/
    // after the hook. The transient reference must not survive restoration.
    head[0]=head[1]=(uint32_t)head; *(uint32_t*)(s+0x9A0)=(uint32_t)(s+0x9AC);
    *(uint32_t*)(s+0x9A8)=0; node[4]=1; g_pending=700; g_owner=s;
    uint32_t snapshot[156] = {}, captured[157] = {};
    captured[153]=0xAABBCCDD;
    ((Capture)0x0074B7A0)(snapshot,captured);
    LIVE_CHECK(captured[0x33]==1 && captured[0x34]==700 && captured[0x35]==1);
    *(uint8_t*)0x0074B7A0 ^= 1;
    LIVE_CHECK(!install());
    for(const Site& site:g_sites) LIVE_CHECK(bytes_equal(site.address,site.bytes,site.size));
    *(uint8_t*)0x0074B7A0 ^= 1;
    LIVE_CHECK(install());
    ((Capture)0x0074B7A0)(snapshot,captured);
    LIVE_CHECK(captured[0x33]==1 && captured[0x34]==700 && captured[0x35]==0 && node[4]==1);
    LIVE_CHECK(captured[153]==0xAABBCCDD);
    node[4]=2; LIVE_CHECK(capture_refs(s,nullptr,700)==1); node[4]=1;
    memcpy(snapshot+3,captured,153*4);
    ((Clear)0x0074BA20)(snapshot);
    LIVE_CHECK(g_pending==-1 && node[4]==0 && snapshot[2]==2);
    // Execute the real spawn prologue and failure epilogue, proving the
    // no-ready-model path balances its four stack arguments and out pointer.
    using Spawn = uint32_t*(__thiscall*)(void*,uint32_t*,uint32_t,const Range*,int);
    g_force_skip=true; uint32_t out=0xDEADBEEF;
    LIVE_CHECK(((Spawn)0x0077ADA0)(tm,&out,42,&range,0)==&out && out==0);
    g_force_skip=false;
    g_pending=700; g_owner=s; ((Clear)0x005A7360)(s);
    LIVE_CHECK(g_pending==-1 && *(uint32_t*)(s+0xDAC)==5);
    for(const Site& site:g_sites) LIVE_CHECK(patch_bytes(site.address,site.bytes,site.size));
    LIVE_CHECK(patch_bytes(0x0059C620,existsBytes,5));
    *(void**)0x00FE7228=savedEngine; *(void**)0x016DA940=savedPairs; free(tm);
    g_header=nullptr; g_rows=nullptr; g_pairs=nullptr; g_thread=0; g_unique=0;
    memset(g_packages,0,sizeof g_packages); memset(g_valid,0,sizeof g_valid); memset(g_seen,0,sizeof g_seen);
    printf("live-pool native fixture: %d failures\n",failures);
    return failures;
#undef LIVE_CHECK
}
