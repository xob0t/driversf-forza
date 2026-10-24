// mod_garage: garage list and save flags for added cars (model IDs up to 999).
//
// Design and evidence: research/asi.md, "Garage and save". In short:
//   save   Per-vehicle profile data for IDs 304..999 lives in this module
//          (g_ext), never in the stock 0x4CF8 save block, and is persisted to
//          <savegames>\dsfforza-garage.dat next to autosave.dat. The stock
//          block keeps its layout, so the save still loads in an unmodded game.
//   garage The maker-list builder walks IDs 0..999 instead of 0..303, with a
//          relocated sort-key table; per-group (20) and group-count caps are
//          enforced instead of overrunning memory; optionally the group table
//          (stock 40) and the menu's maker index array are moved to larger
//          storage, and the maker menu's script lists (menusmaster.sp, 40
//          entries each) are grown when the menu is filled.
//   lua    ProfileSettings.GetVehicleUnlocked(8000000 + k) answers
//          capability queries, so register.lua can tell the patch is active.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <string.h>

#include <intrin.h>

#include "patch.h"
#include "shared.h"

namespace {

// ---- constants ------------------------------------------------------------
const int kStockIds = 0x130;     // 304 slots in the stock flag/owned lists
const int kStockBits = 320;      // the stock bit arrays hold IDs 0..319
const int kExtMax = 1000;        // model IDs below 1000 (N+1000/N+2000 are parts)
const int kExtFirst = 304;       // first ID kept outside the stock block
const int kWords = (kExtMax + 31) / 32;
const uint32_t kMagic = 8000000;  // GetVehicleUnlocked(kMagic + k) queries; the game's Lua numbers are
                                     // floats, so the value must stay below 2^23
const int kStockGroups = 40;
const int kGroupSize = 0x6C;     // one maker group: 20 IDs, count, name[16], badge, manufacturer
const int kGroupCars = 20;
const uint32_t kGarageSize = 0x3728;  // operator new size of the garage object

// Game globals and functions (Driver.exe 1.04).
char** const kEngine = (char**)0x00FE7228;
typedef char* (__cdecl* GetEngineFn)();
const GetEngineFn Engine_Get = (GetEngineFn)0x0040A690;
typedef int (__cdecl* LuaToIntFn)(void* L, int idx);
const LuaToIntFn lua_checkint = (LuaToIntFn)0x005F6BE0;
typedef void (__cdecl* LuaPushIntFn)(void* L, int v);
const LuaPushIntFn lua_pushbool = (LuaPushIntFn)0x005E9660;
const LuaPushIntFn lua_pushint = (LuaPushIntFn)0x005E9640;
typedef int (__fastcall* CountBitsFn)(void* bits, void* edx, int value);  // thiscall, ret 4
const CountBitsFn CountBits304 = (CountBitsFn)0x00678960;
typedef FILE* (__cdecl* FopenFn)(const char*, const char*);
const FopenFn game_fopen = (FopenFn)0x00C2CC9A;
typedef void* (__cdecl* MemcpyFn)(void*, const void*, size_t);
const MemcpyFn game_memcpy = (MemcpyFn)0x00C28820;

// Profile manager ([engine+0x36c]) offsets of the bit arrays (relative to the
// stats object at +0x1BA8, as the setters see them: 0x4E0, 0x508, 0x530, 0x558).
const uint32_t kProfBits[4] = {0x2088, 0x20B0, 0x20D8, 0x2100};  // unlocked-ever, driven, ?, take-a-ride

// ---- extended profile data ------------------------------------------------
struct Slot {
    int32_t id;
    uint32_t flags;  // bit 0 unlocked, 1 owned, 2 new (stock meaning)
};

struct Ext {
    Slot slots[kExtMax];          // flag list entries, index = ID (id = -1: unused)
    uint32_t bits[4][kWords];     // bit arrays, bit = ID (IDs 320..999 used)
    int32_t owned[kExtMax];       // owned-list entries, index = ID (-1: unused)
};

CRITICAL_SECTION g_lock;
Ext g_ext, g_saved, g_loaded, g_backup;
bool g_saved_valid = false, g_loaded_valid = false;
Slot g_dummy_slot = {-1, 0};
int32_t g_dummy_owned = -1;
char g_side_name[64] = "dsfforza-garage.dat";

// Garage list.
int32_t g_sortkey[kExtMax];
int g_max_id = kExtMax;          // IDs the garage walks: 0 .. g_max_id-1
int g_max_groups = kStockGroups;
int g_max_cars = kGroupCars;     // cars per maker group ([garage] max_cars once the car list window is in)
uint32_t g_groups_off = 0x20FC;  // groups table offset in the garage object
uint32_t g_count_rel = kStockGroups * kGroupSize;  // count offset from the table
int32_t* g_maker_index = nullptr;  // menu maker -> group index (index 1..groups)
bool g_garage_active = false, g_save_active = false;

struct Lock {
    Lock() { EnterCriticalSection(&g_lock); }
    ~Lock() { LeaveCriticalSection(&g_lock); }
};

void ext_clear(Ext& e)
{
    for (int i = 0; i < kExtMax; i++) {
        e.slots[i].id = -1;
        e.slots[i].flags = 0;
        e.owned[i] = -1;
    }
    memset(e.bits, 0, sizeof e.bits);
}

bool is_ext(int id) { return id >= kExtFirst && id < kExtMax; }
// IDs the plugin's list tables can hold: IDs 304..999 always, 0..303 when the stock list is full.
bool in_table(int id) { return id >= 0 && id < kExtMax; }

char* profile()
{
    char* engine = *kEngine;
    if (!engine) engine = Engine_Get();
    return engine ? *(char**)(engine + 0x36C) : nullptr;
}

int ext_count(const Ext& e)
{
    int n = 0;
    for (int id = 0; id < kExtMax; id++) {
        bool any = e.slots[id].id == id || e.owned[id] == id;
        for (int k = 0; k < 4 && !any; k++) any = (e.bits[k][id >> 5] >> (id & 31)) & 1;
        n += any;
    }
    return n;
}

// ---- side file ------------------------------------------------------------
// "DSFG", version 1, entry count, FNV-1a of the entries, then per ID:
// u16 id, u16 what (bit 0..3: bit arrays, bit 4: flag-list slot, bit 5: owned
// list), u32 flags.
#pragma pack(push, 1)
struct SideHeader {
    char magic[4];
    uint32_t version, count, hash;
};
struct SideEntry {
    uint16_t id, what;
    uint32_t flags;
};
#pragma pack(pop)

uint32_t fnv(const void* p, size_t n)
{
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < n; i++) h = (h ^ ((const uint8_t*)p)[i]) * 16777619u;
    return h;
}

bool side_path(const char* save_path, char* out, size_t size)
{
    const char* slash = strrchr(save_path, '\\');
    const char* slash2 = strrchr(save_path, '/');
    if (slash2 > slash) slash = slash2;
    size_t dir = slash ? (size_t)(slash - save_path + 1) : 0;
    if (dir + strlen(g_side_name) + 1 > size) return false;
    memcpy(out, save_path, dir);
    strcpy_s(out + dir, size - dir, g_side_name);
    return true;
}

void side_write(const char* save_path, const Ext& e)
{
    char path[MAX_PATH], tmp[MAX_PATH];
    if (!side_path(save_path, path, sizeof path)) return;
    static SideEntry entries[kExtMax];
    uint32_t n = 0;
    for (int id = 0; id < kExtMax; id++) {
        SideEntry x = {(uint16_t)id, 0, 0};
        for (int k = 0; k < 4; k++)
            if ((e.bits[k][id >> 5] >> (id & 31)) & 1) x.what |= 1 << k;
        if (e.slots[id].id == id) {
            x.what |= 0x10;
            x.flags = e.slots[id].flags;
        }
        if (e.owned[id] == id) x.what |= 0x20;
        if (x.what) entries[n++] = x;
    }
    SideHeader h = {{'D', 'S', 'F', 'G'}, 1, n, fnv(entries, n * sizeof(SideEntry))};
    _snprintf_s(tmp, sizeof tmp, _TRUNCATE, "%s.tmp", path);
    FILE* f = nullptr;
    if (fopen_s(&f, tmp, "wb") != 0 || !f) {
        dsf_log("garage: can't write %s", tmp);
        return;
    }
    bool ok = fwrite(&h, sizeof h, 1, f) == 1 && (n == 0 || fwrite(entries, sizeof(SideEntry), n, f) == n);
    ok = fclose(f) == 0 && ok;
    if (ok && MoveFileExA(tmp, path, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        dsf_log("garage: saved %u added-car entries to %s", n, path);
    } else {
        dsf_log("garage: writing %s failed (%lu)", path, GetLastError());
        DeleteFileA(tmp);
    }
}

// Returns false (and leaves e empty) when there is no valid file.
bool side_read(const char* save_path, Ext& e)
{
    ext_clear(e);
    char path[MAX_PATH];
    if (!side_path(save_path, path, sizeof path)) return false;
    FILE* f = nullptr;
    if (fopen_s(&f, path, "rb") != 0 || !f) {
        dsf_log("garage: no %s (no added-car flags yet)", path);
        return false;
    }
    static SideEntry entries[kExtMax];
    SideHeader h;
    bool ok = fread(&h, sizeof h, 1, f) == 1 && memcmp(h.magic, "DSFG", 4) == 0 && h.version == 1 &&
              h.count <= (uint32_t)kExtMax && fread(entries, sizeof(SideEntry), h.count, f) == h.count &&
              fnv(entries, h.count * sizeof(SideEntry)) == h.hash;
    fclose(f);
    if (!ok) {
        dsf_log("garage: %s is damaged or from another version; ignored", path);
        return false;
    }
    for (uint32_t i = 0; i < h.count; i++) {
        const SideEntry& x = entries[i];
        if (!in_table(x.id)) continue;
        for (int k = 0; k < 4; k++)
            if (x.what & (1 << k)) e.bits[k][x.id >> 5] |= 1u << (x.id & 31);
        if (x.what & 0x10) {
            e.slots[x.id].id = x.id;
            e.slots[x.id].flags = x.flags;
        }
        if (x.what & 0x20) e.owned[x.id] = x.id;
    }
    dsf_log("garage: loaded %u added-car entries from %s", h.count, path);
    return true;
}

// ---- profile hooks: flag list (FUN_00507CF0 / FUN_00507D30) ---------------
// thiscall(list object, id) -> pointer to the {id, flags} slot (find or insert).
int32_t* __fastcall h_flag_slot(char* list, void*, int id)
{
    int32_t* base = (int32_t*)(list + 0x38);
    int32_t* empty = nullptr;
    for (int i = 0; i < kStockIds; i++) {
        int32_t* s = base + 2 * i;
        if (*s == id) return s;
        if (!empty && *s == -1) empty = s;
    }
    if (is_ext(id) || (!empty && in_table(id))) {
        // 304..999 never go into the block; a stock-range ID does only when the
        // stock list is full (stock: write through NULL).
        if (!empty && !is_ext(id)) {
            static int noted = 0;
            if (noted++ < 5) dsf_log("garage: vehicle flag list full; id %d kept in %s", id, g_side_name);
        }
        Lock lock;
        Slot& s = g_ext.slots[id];
        s.id = id;
        return &s.id;
    }
    {
        Lock lock;
        Slot& s = g_ext.slots[id >= 0 && id < kExtMax ? id : 0];
        if (in_table(id) && s.id == id) return &s.id;   // overflowed earlier, list has room again
    }
    if (!empty) {
        // Stock: writes through NULL (crash). Hand out a scratch slot instead.
        static int warned = 0;
        if (warned++ < 5) dsf_log("garage: vehicle flag list full, id %d not stored", id);
        g_dummy_slot.id = id;
        g_dummy_slot.flags = 0;
        return &g_dummy_slot.id;
    }
    *empty = id;
    return empty;
}

int magic_query(uint32_t q)
{
    uint32_t k = q - kMagic;
    if (k == 0) return g_save_active ? 1 : 0;                  // save hooks
    if (k == 1) return g_garage_active ? 1 : 0;                // garage walks IDs beyond 303
    if (k >= 0x1000 && k < 0x2000) return (int)(k - 0x1000) <= g_max_groups;   // n <= max groups
    if (k >= 0x2000 && k < 0x3000) return (int)(k - 0x2000) < (g_garage_active ? g_max_id : kStockIds);  // id listable
    if (k >= 0x3000 && k < 0x4000) return (int)(k - 0x3000) <= g_max_cars;    // n <= cars per maker group
    return 0;
}

// thiscall(list object, id, bit) -> al = flag bit set.
uint32_t __fastcall h_flag_test(char* list, void*, int id, uint32_t bit)
{
    if ((uint32_t)id - kMagic < 0x4000) return magic_query((uint32_t)id);
    int32_t* base = (int32_t*)(list + 0x38);
    for (int i = 0; i < kStockIds; i++)
        if (base[2 * i] == id) return (base[2 * i + 1] >> (bit & 31)) & 1;
    if (in_table(id)) {
        Lock lock;
        const Slot& s = g_ext.slots[id];
        return s.id == id ? (s.flags >> (bit & 31)) & 1 : 0;
    }
    return 0;
}

// ---- owned list (FUN_0050A500 -> FUN_0050A4C0) ----------------------------
int32_t* __fastcall h_owned_slot(char* stats, void*, int id)
{
    int32_t* base = (int32_t*)(stats + 0xBDC);
    int32_t* empty = nullptr;
    for (int i = 0; i < kStockIds; i++) {
        if (base[i] == id) return base + i;
        if (!empty && base[i] == -1) empty = base + i;
    }
    if (in_table(id) && (is_ext(id) || !empty || g_ext.owned[id] == id)) {
        Lock lock;
        g_ext.owned[id] = id;
        return &g_ext.owned[id];
    }
    if (!empty) {
        static int warned = 0;
        if (warned++ < 5) dsf_log("garage: owned list full, id %d not stored", id);
        g_dummy_owned = id;
        return &g_dummy_owned;
    }
    *empty = id;
    return empty;
}

// ---- bit arrays (FUN_0050A1A0 unlocked-ever, FUN_0050A120 driven, FUN_0050A170 take-a-ride)
// The setters index [this + off + (id >> 5) * 4]. For IDs 304..999 the stubs
// swap `this` for a base that lands in g_ext.bits; IDs of 1000 and up are
// dropped (stock: silent corruption of the next arrays).
uint32_t g_fake_4e0, g_fake_508, g_fake_558;

__declspec(naked) void s_unlocked()  // at 0x50A1A0
{
    __asm {
        mov eax, [esp + 4]
        cmp eax, 304
        jb orig
        cmp eax, 1000
        jae drop
        mov ecx, g_fake_4e0
    orig:
        mov eax, [esp + 4]
        push ebx
        push 0x0050A1A5
        ret
    drop:
        ret 4
    }
}

__declspec(naked) void s_driven()  // at 0x50A120
{
    __asm {
        mov eax, [esp + 4]
        cmp eax, 304
        jb orig
        cmp eax, 1000
        jae drop
        mov ecx, g_fake_508
    orig:
        push ebx
        push esi
        mov esi, [esp + 0xC]
        push 0x0050A126
        ret
    drop:
        ret 4
    }
}

__declspec(naked) void s_takeride()  // at 0x50A170
{
    __asm {
        mov eax, [esp + 4]
        cmp eax, 304
        jb orig
        cmp eax, 1000
        jae drop
        mov ecx, g_fake_558
    orig:
        mov edx, ecx
        mov ecx, [esp + 4]
        push 0x0050A176
        ret
    drop:
        ret 4
    }
}

// Bit k (0 unlocked-ever, 1 driven, 3 take-a-ride) of an ID, stock or extended.
int bit_get(int k, uint32_t id)
{
    int bit = 0;
    if (id < (uint32_t)kStockBits) {
        // IDs 304..319 fit the stock arrays; older saves may have them set there.
        char* p = profile();
        if (p) bit = (((uint32_t*)(p + kProfBits[k]))[id >> 5] >> (id & 31)) & 1;
        if (id < (uint32_t)kExtFirst) return bit;
    }
    if (id < (uint32_t)kExtMax) {
        Lock lock;
        bit |= (g_ext.bits[k][id >> 5] >> (id & 31)) & 1;
    }
    return bit;
}

// ProfileSettings.GetVehicleDriven (0x67C320): stock reads [profile+0x20B0] inline.
int __cdecl lua_GetVehicleDriven(void* L)
{
    int id = lua_checkint(L, 1);
    lua_pushbool(L, bit_get(1, (uint32_t)id));
    return 1;
}

// ProfileSettings.GetNumVehiclesDrivenTakeARide (0x67C3C0): stock counts IDs 0..303.
int __cdecl lua_GetNumTakeARide(void* L)
{
    char* p = profile();
    int n = p ? CountBits304(p + 0x2100, nullptr, 1) : 0;
    for (int id = kExtFirst; id < kExtMax; id++) n += bit_get(3, (uint32_t)id);
    lua_pushint(L, n);
    return 1;
}

// 0x6FD51E (take-a-ride candidate scan): unlocked-ever and not yet taken.
int __stdcall takeride_candidate(uint32_t id) { return bit_get(0, id) && !bit_get(3, id); }
int __stdcall takeride_bit(uint32_t id) { return bit_get(3, id); }

__declspec(naked) void s_candidate()  // at 0x6FD51E, eax = model ID
{
    __asm {
        push eax
        call takeride_candidate
        test eax, eax
        jnz yes
        push 0x006FD53F
        ret
    yes:
        push 0x006FD557
        ret
    }
}

__declspec(naked) void s_menu_takeride()  // at 0x711759, eax = model ID; result in edi
{
    __asm {
        push eax
        call takeride_bit
        mov edi, eax
        push 0x00711779
        ret
    }
}

// Byte counter of owned cars at profile+0x4D6F (FUN_00508990): saturate
// instead of wrapping past 255.
__declspec(naked) void s_owned_counter()  // at 0x5089E4, eax = profile
{
    __asm {
        cmp byte ptr [eax + 0x4D6F], 0xFF
        je done
        inc byte ptr [eax + 0x4D6F]
    done:
        ret
    }
}

// ---- save lifecycle: wrappers for the game's memcpy of the profile block ----
void* __cdecl m_save(void* dst, const void* src, size_t n)  // live -> save buffer
{
    game_memcpy(dst, src, n);
    Lock lock;
    g_saved = g_ext;
    g_saved_valid = true;
    return dst;
}

void* __cdecl m_load(void* dst, const void* src, size_t n)  // save buffer -> live
{
    game_memcpy(dst, src, n);
    Lock lock;
    if (g_loaded_valid) {
        g_ext = g_loaded;
        g_loaded_valid = false;
        g_saved = g_ext;  // the buffer now matches what is on disk
        g_saved_valid = true;
        dsf_log("garage: profile loaded, %d added cars with flags", ext_count(g_ext));
    } else if (g_saved_valid) {
        g_ext = g_saved;
    }
    return dst;
}

void* __cdecl m_reset(void* dst, const void* src, size_t n)  // defaults -> live
{
    game_memcpy(dst, src, n);
    Lock lock;
    ext_clear(g_ext);
    dsf_log("garage: profile reset to defaults; added-car flags cleared");
    return dst;
}

void* __cdecl m_newsave(void* dst, const void* src, size_t n)  // defaults -> save buffer
{
    game_memcpy(dst, src, n);
    Lock lock;
    ext_clear(g_saved);
    g_saved_valid = true;
    return dst;
}

void* __cdecl m_backup(void* dst, const void* src, size_t n)  // live -> temp copy
{
    game_memcpy(dst, src, n);
    Lock lock;
    g_backup = g_ext;
    return dst;
}

void* __cdecl m_restore(void* dst, const void* src, size_t n)  // temp copy -> live
{
    game_memcpy(dst, src, n);
    Lock lock;
    g_ext = g_backup;
    return dst;
}

void* __cdecl m_clear_progress(void* dst, const void* src, size_t n)  // ClearProgression
{
    game_memcpy(dst, src, n);
    Lock lock;
    for (int i = 0; i < kExtMax; i++) {
        g_ext.slots[i].id = -1;
        g_ext.slots[i].flags = 0;
    }
    return dst;
}

void* __cdecl m_clear_stats(void* dst, const void* src, size_t n)  // ClearStatistics
{
    game_memcpy(dst, src, n);
    Lock lock;
    memset(g_ext.bits, 0, sizeof g_ext.bits);
    for (int i = 0; i < kExtMax; i++) g_ext.owned[i] = -1;
    return dst;
}

// fopen of HDD:<profile>\savegames\autosave.dat in the storage device's
// write (FUN_00853620) and read (FUN_008534A0).
FILE* __cdecl f_write(const char* path, const char* mode)
{
    FILE* f = game_fopen(path, mode);
    if (f) {
        Lock lock;
        side_write(path, g_saved_valid ? g_saved : g_ext);
    }
    return f;
}

FILE* __cdecl f_read(const char* path, const char* mode)
{
    FILE* f = game_fopen(path, mode);
    if (f) {
        Lock lock;
        side_read(path, g_loaded);
        g_loaded_valid = true;
    }
    return f;
}

// ---- maker groups of more than 20 cars ------------------------------------
// A group keeps its first 20 IDs inline (+0x00..+0x4C) and its car count at
// +0x50, as stock; IDs 21 and up live here, keyed by the group's name (+0x54,
// 16 bytes). The builder files cars by that name, so it is unique, and it
// travels with the group when FUN_006C7BB0 shifts the table to insert one.
// Every reader outside the garage object goes through vfunc 0x12C (car i of
// group g), which the plugin replaces, with the count from vfunc 0x130.
const uint32_t kNameOff = 0x54;
struct Over {
    char name[16];
    int32_t* ids;  // IDs 21.. of the group (kExtMax - 20 entries)
};
Over g_over[1000];
int g_over_n = 0;

int32_t* over_ids(const int32_t* group, bool create)
{
    const char* name = (const char*)group + kNameOff;
    for (int i = 0; i < g_over_n; i++)
        if (memcmp(g_over[i].name, name, 16) == 0) return g_over[i].ids;
    if (!create || g_over_n >= (int)(sizeof g_over / sizeof g_over[0])) return nullptr;
    int32_t* ids = (int32_t*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, (kExtMax - kGroupCars) * sizeof(int32_t));
    if (!ids) return nullptr;
    Over& o = g_over[g_over_n++];
    memcpy(o.name, name, 16);
    o.ids = ids;
    return ids;
}

int32_t& group_at(int32_t* group, int32_t* over, int i) { return i < kGroupCars ? group[i] : over[i - kGroupCars]; }

// FUN_006C5740 (thiscall garage, ret 8): file `id` into `group`, before the
// first car whose sort key is larger (stock order; stock skips IDs past the
// walked range). Stock has no capacity check: the 21st car overwrote the count.
void __fastcall group_add_car(char* garage, void*, int32_t* group, int id)
{
    (void)garage;
    int n = group[kGroupCars];
    if (n < 0) n = 0;
    int32_t* over = nullptr;
    if (n >= g_max_cars || (n >= kGroupCars && !(over = over_ids(group, true)))) {
        static int noted = 0;
        if (noted++ < 5) {
            char name[17] = {};
            memcpy(name, (const char*)group + kNameOff, 16);
            dsf_log("garage: maker group %s is full (%d cars); model %d not listed", name, n, id);
        }
        return;
    }
    int pos = n;
    for (int i = 0; i < n; i++) {
        uint32_t c = (uint32_t)group_at(group, over, i);
        if (c < (uint32_t)g_max_id && g_sortkey[c] > g_sortkey[id]) {
            pos = i;
            break;
        }
    }
    for (int i = n; i > pos; i--) group_at(group, over, i) = group_at(group, over, i - 1);
    group_at(group, over, pos) = id;
    group[kGroupCars] = n + 1;
}

int32_t* group_ptr(char* garage, int g) { return (int32_t*)(garage + g_groups_off + g * kGroupSize); }

// ---- vehicles screen: a window of cards over a long group ------------------
// The screen (vtable 0xE27394) has 20 cards: per-card arrays at +0x30 (picture
// UID), +0xD0 (model ID), +0x120 (Header_Sprite_NN), +0x170 (title), +0x1C0
// (status text), +0x218 (itemState_N), filled by the car list FUN_007113A0
// for the group of categorySelected ([+0x268], 1-based) with card index =
// itemBrowserIndex ([+0x26C], 0-based). A group of more than 20 listed cars
// is shown through a window of 19 cards (card 20's Header_Sprite_20 stays
// free for [hud] card_emblem): the car list starts at car `first`, stops
// after 19 cards, and the window moves when itemBrowserIndex nears its edge.
const uint32_t kRetCarListCar = 0x00711599;    // vfunc 0x12C call in the car list
const uint32_t kRetCarListGroup = 0x00711443;  // vfunc 0x130 call in the car list
const uint32_t kRetUpdateGroup = 0x007112FB;   // vfunc 0x130 call in the picture update (FUN_007112A0)
const int kWindowCards = kGroupCars - 1;

struct View {
    int group = -1;           // group shown
    int cars = 0;             // its car count
    int listed = 0;           // its listed cars (ManufacturerNumList as the fill wrote it)
    int first = 0;            // car index of the window's first card
    int limit = kGroupCars;   // cards the car list fills at most
    bool in_list = false;     // inside the car list (vfunc hooks apply)
    int cards = 0;            // cards filled so far / by the last fill
    bool stopped = false;     // the last fill stopped at `limit` cards
    int last_car = -1;        // car index of the last card filled
    int card_car[kGroupCars]; // car index of each card of the last fill
    int8_t seen[kExtMax];     // per car index: -1 unknown, 0 no card, 1 a card
};
View g_view;
bool g_window_on = false;   // [garage] max_cars > 20 and the screen hooks applied
int g_margin_back = 2, g_margin_ahead = 8;
int g_listed[1000];          // listed cars per maker menu entry, as FUN_006C6940 wrote them
int g_listed_n = 0;
bool g_menu_debug = false;

// vfunc 0x12C (FUN_006C55A0, thiscall garage, ret 8): car i of group g.
int32_t __fastcall group_car(char* garage, void*, int g, int i)
{
    int32_t* group = group_ptr(garage, g);
    if (g_view.in_list && (uint32_t)_ReturnAddress() == kRetCarListCar) i += g_view.first;
    if ((uint32_t)i < (uint32_t)kGroupCars) return group[i];
    int32_t* over = i < group[kGroupCars] ? over_ids(group, false) : nullptr;
    return over ? over[i - kGroupCars] : group[0];
}

// vfunc 0x130 (FUN_006C55C0, thiscall garage, ret 8): copy of group g's 0x6C
// bytes. The car list gets the count from the window's first car on; the
// picture update walks its per-card arrays by this count, so it gets at most
// the cards the car list fills.
void __fastcall group_copy(char* garage, void*, int g, int32_t* out)
{
    memcpy(out, group_ptr(garage, g), kGroupSize);
    uint32_t ret = (uint32_t)_ReturnAddress();
    if (ret == kRetCarListGroup && g_view.in_list) {
        out[kGroupCars] -= g_view.first;
        if (out[kGroupCars] < 0) out[kGroupCars] = 0;
    } else if (ret == kRetUpdateGroup && out[kGroupCars] > g_view.limit) {
        out[kGroupCars] = g_view.limit;
    }
}

int garage_card_limit_impl() { return g_view.limit; }

// FUN_006C7BB0 (vector<group>::insert): drop the group when the table is full.
__declspec(naked) void s_group_insert()
{
    __asm {
        mov eax, g_count_rel
        mov eax, [ecx + eax]
        cmp eax, g_max_groups
        jae full
        push esi
        mov esi, ecx
        push 0x006C7BB9
        ret
    full:
        ret 0xC
    }
}

// ---- maker menu lists -----------------------------------------------------------
// The garage's vfunc 0x124 (FUN_006C6940, called by the vehicles screen at
// 0x006DD08F) copies every maker group that has a listed car into four MmDk
// menu-script lists of Install\menusmaster.sp: ManufacturerDataList (name),
// ManufacturerLogoReferenceList (badge string), ManufacturerNumList (car
// count) and ManufacturerNew (only its length is set here). They are declared
// with 40 entries; element 40 of a string list is NULL, and the string set at
// 0x00ACE11F faulted on it (runs host-20261023-080433/-080858/-081321/-101840).
// The wrapper grows each list to the group table's size before the fill and
// clamps the lengths after it; the two string sets skip a missing element.
//
// MmDk list (confirmed-static, FUN_00ABD2E0/ABD5B0/ABDA20): +0x08 element type
// (0x0C int, 0x0D float, 0x0E byte stored in 4-byte cells, others are pointers
// to objects; FUN_00ABD990 tells which), +0x0C int16 length, +0x10 data. A
// string element: {char* text, u8 external, u8 set, i16 capacity}; with
// external = 0 the text is copied into the element's buffer (FUN_00ACE180).
typedef int(__fastcall* ListIsObjFn)(void* list, void*);
const ListIsObjFn List_IsObjects = (ListIsObjFn)0x00ABD990;
typedef void(__fastcall* FillMakersFn)(void* garage, void*, void* l1, void* l2, void* l3, void* l4);
const FillMakersFn Garage_FillMakers = (FillMakersFn)0x006C6940;

struct GrownList {
    void* list;   // list object
    void** data;  // our element array (capacity entries)
    int capacity;
};
GrownList g_grown[16];
int g_grown_n = 0;

int16_t& list_len(void* l) { return *(int16_t*)((char*)l + 0x0C); }
void**& list_data(void* l) { return *(void***)((char*)l + 0x10); }

// Grows list to `need` entries (at least); returns the capacity it then has.
int list_grow(void* list, int need, const char* name)
{
    int len = list_len(list);
    if (len >= need) return len;
    void** data = list_data(list);
    GrownList* g = nullptr;
    for (int i = 0; i < g_grown_n; i++)
        if (g_grown[i].list == list) g = &g_grown[i];
    if (g && g->data == data && g->capacity >= need) {
        list_len(list) = (int16_t)g->capacity;  // the last fill trimmed it to the groups shown
        return g->capacity;
    }
    if (!data || len <= 0 || need > 0x7FFF) return len;
    int cap = need > g_max_groups ? need : g_max_groups;
    bool objects = List_IsObjects(list, nullptr) != 0;
    void** grown = (void**)VirtualAlloc(nullptr, cap * sizeof(void*), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!grown) return len;
    memcpy(grown, data, len * sizeof(void*));
    if (objects) {
        // New string elements: element 0's flags, an own 64-byte buffer.
        const uint8_t* proto = (const uint8_t*)data[0];
        if (!proto) {
            VirtualFree(grown, 0, MEM_RELEASE);
            return len;
        }
        const int kBuf = 64;
        uint8_t* block = (uint8_t*)VirtualAlloc(nullptr, (cap - len) * (8 + kBuf), MEM_COMMIT | MEM_RESERVE,
                                                PAGE_READWRITE);
        if (!block) {
            VirtualFree(grown, 0, MEM_RELEASE);
            return len;
        }
        for (int i = len; i < cap; i++) {
            uint8_t* obj = block + (i - len) * (8 + kBuf);
            char* text = (char*)obj + 8;
            memcpy(obj, proto, 8);
            *(char**)obj = text;
            obj[4] = 0;                          // own buffer: the setter copies into it
            *(int16_t*)(obj + 6) = kBuf;
            grown[i] = obj;
        }
    }
    list_data(list) = grown;
    list_len(list) = (int16_t)cap;
    if (!g && g_grown_n < (int)(sizeof g_grown / sizeof g_grown[0])) g = &g_grown[g_grown_n++];
    if (g) {
        g->list = list;
        g->data = grown;
        g->capacity = cap;
    }
    dsf_log("garage: menu list %s %p grown from %d to %d entries (%s)", name, list, len, cap,
            objects ? "strings" : "values");
    return cap;
}

// ManufacturerNumList holds each shown maker's listed cars; the menu script
// sizes the card browser by it. A maker of more than 20 is shown through the
// car list's window of 19 cards, so the script is told 19 (the true count is
// kept for the car list).
int g_numlist_mode = 1;   // [garage] debug: 0 = leave the true count
void numlist_window(void* list)
{
    g_listed_n = 0;
    if (!list) return;
    int n = list_len(list), type = *(int*)((char*)list + 8);
    void** data = list_data(list);
    if (!data || n <= 0 || (type != 0x0C && type != 0x0D)) return;
    if (n > (int)(sizeof g_listed / sizeof g_listed[0])) n = sizeof g_listed / sizeof g_listed[0];
    int windowed = 0;
    for (int k = 0; k < n; k++) {
        int v = type == 0x0C ? (int)(intptr_t)data[k] : (int)*(float*)&data[k];
        g_listed[k] = v;
        if (v > kGroupCars && g_window_on && g_numlist_mode) {
            if (type == 0x0C) data[k] = (void*)(intptr_t)kWindowCards;
            else *(float*)&data[k] = (float)kWindowCards;
            windowed++;
        }
    }
    g_listed_n = n;
    if (windowed && g_menu_debug) dsf_log("garage: %d makers of more than %d cars shown through a window", windowed, kGroupCars);
}

// Replaces the garage vtable's slot 0x124 (FUN_006C6940).
void __fastcall fill_makers(char* garage, void*, void* l1, void* l2, void* l3, void* l4)
{
    void* lists[4] = {l1, l2, l3, l4};
    static const char* const names[4] = {"ManufacturerDataList", "ManufacturerLogoReferenceList",
                                         "ManufacturerNumList", "ManufacturerNew"};
    int caps[4];
    if (!garage[8]) {
        // FUN_006C6940 builds the maker table first when it isn't built yet
        // (vfunc 0x120, FUN_006C57D0); do it here so the group count is known.
        typedef void(__fastcall * BuildFn)(char*, void*);
        ((BuildFn)(*(void***)garage)[0x120 / 4])(garage, nullptr);
    }
    int groups = *(int32_t*)(garage + g_groups_off + g_count_rel);
    for (int i = 0; i < 4; i++) caps[i] = lists[i] ? list_grow(lists[i], groups, names[i]) : 0;
    Garage_FillMakers(garage, nullptr, l1, l2, l3, l4);
    for (int i = 0; i < 4; i++) {
        if (lists[i] && list_len(lists[i]) > caps[i]) {
            static int noted = 0;
            if (noted++ < 5)
                dsf_log("garage: %d makers shown, %s holds %d; the rest are not listed", list_len(lists[i]),
                        names[i], caps[i]);
            list_len(lists[i]) = (int16_t)caps[i];
        }
    }
    numlist_window(l3);
}

// Call sites 0x6C6AC8/0x6C6AE2: thiscall FUN_00ACE110(element, text, n) on the
// list element FUN_00ABD9F0 returned (NULL past the list's end).
__declspec(naked) void s_menu_set_string()
{
    __asm {
        test ecx, ecx
        jz missing
        mov eax, 0x00ACE110
        jmp eax
    missing:
        ret 8
    }
}

// ---- the car list (FUN_007113A0) -----------------------------------------------
typedef void(__cdecl* SprintfFn)(char* out, int size, const char* fmt, ...);
const SprintfFn game_snprintf = (SprintfFn)0x00903E90;
typedef void(__cdecl* SetTextIdFn)(void* menu, void* text, const char* id, int value);
const SetTextIdFn set_text_id = (SetTextIdFn)0x006CB160;   // "ID:244203" = "%d/%d OWNED" style total
typedef char(__fastcall* FlagTestFn)(void* list, void*, int id, int bit);
const FlagTestFn flag_test = (FlagTestFn)0x00507D30;
typedef int*(__fastcall* FindVarFn)(void* vars, void*, const char* name, int* info);
const FindVarFn find_var = (FindVarFn)0x00ACBE20;

char* garage_object()
{
    char* engine = *kEngine;
    if (!engine) engine = Engine_Get();
    return engine ? *(char**)(engine + 0x718) : nullptr;
}

int* screen_var(uint8_t* screen, uint32_t off) { return *(int**)(screen + off); }

// A variable of the vehicles screen's menu script (MmDk menu [[screen+4]+8], variables at +0x50).
int* menu_var(uint8_t* screen, const char* name)
{
    uint8_t* menu = *(uint8_t**)(screen + 4) ? *(uint8_t**)(*(uint8_t**)(screen + 4) + 8) : nullptr;
    return menu ? find_var(menu + 0x50, nullptr, name, nullptr) : nullptr;
}

// Group index of the vehicles screen's selected maker (as 0x00711411 reads it).
int screen_group(uint8_t* screen, int cat)
{
    if (g_maker_index) return (cat >= 0 && cat <= g_max_groups) ? g_maker_index[cat] : -1;
    return (cat >= 0 && cat <= kStockGroups) ? *(int*)(screen + 0x270 + cat * 4) : -1;
}

// Called by loop_stub at the end of each car of the car list loop: `done` cars
// of the window walked, `cards` filled. Returns nonzero to end the loop.
int __cdecl loop_next(int done, int cards)
{
    int car = g_view.first + done - 1;
    bool card = cards > g_view.cards;
    if (car >= 0 && car < kExtMax) g_view.seen[car] = card ? 1 : 0;
    if (card && cards <= kGroupCars) {
        g_view.card_car[cards - 1] = car;
        g_view.last_car = car;
    }
    g_view.cards = cards;
    g_view.stopped = cards >= g_view.limit;
    return g_view.stopped;
}

// Replaces `mov [esp+0x30],eax; cmp eax,[esp+0x28]` (+ 3 NOPs) at 0x007118C3;
// a `jb` (next car) follows: CF must say "below" to go on.
__declspec(naked) void loop_stub()
{
    __asm {
        mov [esp + 0x34], eax       // [esp+0x30] at the site
        pushad
        push ebp                    // cards filled
        push eax                    // cars walked
        call loop_next
        add esp, 8
        test eax, eax
        popad
        jnz stop
        cmp eax, [esp + 0x2C]       // [esp+0x28] at the site: cars in the window
        ret
    stop:
        cmp eax, eax                // CF = 0: leave the loop
        ret
    }
}

// Moves the window so that itemBrowserIndex keeps `back` cards before it and
// `ahead` cards after it where the group has them. Returns the cards moved.
int window_move(int* index)
{
    int idx = *index, moved = 0;
    int hi = g_view.limit - 1 - g_margin_ahead;
    int lo = g_margin_back;
    bool more = g_view.stopped && g_view.last_car + 1 < g_view.cars;
    if (idx > hi && more && g_view.cards > 0) {
        int s = idx - hi;
        if (s >= g_view.cards) s = g_view.cards - 1;
        if (s > 0) {
            g_view.first = g_view.card_car[s];
            moved = s;
        }
    } else if (idx < lo && g_view.first > 0) {
        int want = lo - idx, car = g_view.first;
        while (moved < want && car > 0) {
            car--;
            if (g_view.seen[car] != 0) moved++;   // a card (or not yet seen: counted as one)
        }
        while (car > 0 && g_view.seen[car - 1] == 0) car--;
        g_view.first = car;
        moved = -moved;
    }
    if (moved) *index = idx - moved;
    return moved;
}

void debug_resolve(uint8_t* screen);

int g_last_index = 0;
int g_shift_mode = 0;   // [garage] debug_shift: how a window move tells the script its new index

// thiscall [screen+4]->vfunc 0x18(name, value): what the car list uses to set
// script variables ("itemStateChanged", "vehicleStats_Top").
void script_set(uint8_t* screen, const char* name, int value)
{
    void* ctl = *(void**)(screen + 4);
    if (!ctl) return;
    typedef void(__fastcall * SetFn)(void*, void*, const char*, int);
    ((SetFn)(*(void***)ctl)[0x18 / 4])(ctl, nullptr, name, value);
}

void set_index(uint8_t* screen, int* index, int value)
{
    static bool logged = false;
    if (!logged) {
        logged = true;
        void* ctl = *(void**)(screen + 4);
        dsf_log("garage: screen controller %p vtable %08X, set %08X (shift mode %d)", ctl,
                ctl ? *(uint32_t*)ctl : 0, ctl ? (*(uint32_t**)ctl)[0x18 / 4] : 0, g_shift_mode);
    }
    if (g_shift_mode == 2 || g_shift_mode == 3) script_set(screen, "itemBrowserIndex", value);
    else *index = value;
    if (g_shift_mode == 1) {
        int* sort = menu_var(screen, "sortCardsNow");
        if (sort) *sort = 1;
    }
    if (g_shift_mode == 3) script_set(screen, "sortCardsNow", 1);
}

void carlist_pre(uint8_t* screen)
{
    g_view.in_list = false;
    if (!screen[0x270]) return;
    debug_resolve(screen);
    char* garage = garage_object();
    int* cat_var = screen_var(screen, 0x268);
    int* index = screen_var(screen, 0x26C);
    if (!garage || !cat_var || !index) return;
    int cat = *cat_var;
    int g = screen_group(screen, cat);
    int groups = *(int32_t*)(garage + g_groups_off + g_count_rel);
    if (g < 0 || g >= groups) return;
    int cars = group_ptr(garage, g)[kGroupCars];
    int listed = (cat >= 1 && cat <= g_listed_n) ? g_listed[cat - 1] : cars;
    if (g != g_view.group || cars != g_view.cars || listed != g_view.listed) {
        g_view.group = g;
        g_view.cars = cars;
        g_view.listed = listed;
        g_view.first = 0;
        g_view.stopped = false;
        g_view.cards = 0;
        g_view.last_car = -1;
        memset(g_view.seen, -1, sizeof g_view.seen);
    }
    bool window = g_window_on && listed > kGroupCars;
    g_view.limit = window ? kWindowCards : kGroupCars;
    int before = *index, from = g_view.first;
    // A jump of more than one card is the script resetting the browser (entering
    // it, or back to the maker list): the window starts over.
    if (g_view.first > 0 && (before - g_last_index > 1 || g_last_index - before > 1)) {
        g_view.first = 0;
        g_view.stopped = false;
    }
    int moved = 0;
    if (window) {
        int idx = before;
        moved = window_move(&idx);
        if (moved) set_index(screen, index, idx);
    }
    g_last_index = *index;
    if (g_menu_debug || moved)
        dsf_log("garage: car list: maker %d group %d, %d cars (%d listed), index %d%s, window from car %d%s", cat, g,
                cars, listed, before, moved ? " -> moved" : "", g_view.first, moved ? "" : " (unchanged)");
    (void)from;
    g_view.cards = 0;
    g_view.stopped = false;
    g_view.last_car = -1;
    g_view.in_list = true;
}

// After the fill: the "owned / total" texts count the whole group, not the window.
void carlist_post(uint8_t* screen)
{
    if (!g_view.in_list) return;
    g_view.in_list = false;
    if (g_view.limit >= kGroupCars) return;
    char* garage = garage_object();
    char* engine = *kEngine;
    if (!garage || !engine) return;
    char* list = *(char**)(engine + 0x36C) + 0x14C;
    int owned = 0;
    for (int i = 0; i < g_view.cars; i++)
        if (flag_test(list, nullptr, group_car(garage, nullptr, g_view.group, i), 1)) owned++;
    *(int*)(screen + 0x28) = owned;
    char text[32];
    game_snprintf(text, sizeof text, (const char*)0x00DDB758, owned);
    void* owned_text = *(void**)(screen + 0x210);
    if (owned_text) {
        typedef void(__fastcall * SetTextFn)(void*, void*, const char*);
        ((SetTextFn)(*(void***)owned_text)[0xBC / 4])(owned_text, nullptr, text);
    }
    set_text_id(*(void**)(screen + 4), *(void**)(screen + 0x214), "ID:244203", g_view.cars);
}

// The original car list from its second instruction (its first is `sub esp,0x208`).
__declspec(naked) void carlist_orig()
{
    __asm {
        sub esp, 0x208
        mov eax, 0x007113A6
        jmp eax
    }
}

// Jumped to from 0x007113A0 (thiscall screen, plain ret).
void __fastcall carlist_hook(uint8_t* screen)
{
    carlist_pre(screen);
    __asm {
        mov ecx, screen
        call carlist_orig
    }
    carlist_post(screen);
}

// [garage] menu_debug=1: a thread logs the vehicles screen's script variables when they change.
const char* const kDebugVars[] = {"itemBrowserIndex", "categorySelected", "currentItemNum", "NumItemsVisible",
                                  "startPageSelected", "ItemBrowserIndex", "currentItemMenuState", "itemSelected",
                                  "sortCardsNow", "savedCategorySelected", "categoryIndex", "selectedItemState",
                                  "itemBrowserExitLock", "prevPageMenuIndexLevel2", "prevPageMenuIndexLevel3"};
const int kDebugN = sizeof kDebugVars / sizeof kDebugVars[0];
int* volatile g_debug_ptr[kDebugN];
uint8_t* volatile g_debug_screen = nullptr;

DWORD WINAPI debug_thread(void*)
{
    int last[kDebugN];
    for (int i = 0; i < kDebugN; i++) last[i] = 0x7FFFFFFF;
    for (;;) {
        Sleep(15);
        for (int i = 0; i < kDebugN; i++) {
            int* p = g_debug_ptr[i];
            if (!p) continue;
            int v = *p;
            if (v != last[i]) {
                dsf_log("garage: var %s = %d (%08X)", kDebugVars[i], v, v);
                last[i] = v;
            }
        }
    }
}

void debug_resolve(uint8_t* screen)
{
    if (!g_menu_debug || g_debug_screen == screen) return;
    uint8_t* page = *(uint8_t**)(screen + 4) ? *(uint8_t**)(*(uint8_t**)(screen + 4) + 8) : nullptr;
    if (!page) return;
    for (int i = 0; i < kDebugN; i++) {
        g_debug_ptr[i] = find_var(page + 0x50, nullptr, kDebugVars[i], nullptr);
        dsf_log("garage: debug var %s at %p", kDebugVars[i], g_debug_ptr[i]);
    }
    g_debug_screen = screen;
}

// ---- patch helpers ------------------------------------------------------------
struct Edit {
    uint32_t va;
    uint8_t n;
    uint8_t old[8];
    uint8_t neu[8];
};

Edit edit_u32(uint32_t va, const uint8_t* prefix, uint8_t plen, uint32_t old_value, uint32_t new_value)
{
    Edit e = {};
    e.va = va;
    e.n = (uint8_t)(plen + 4);
    memcpy(e.old, prefix, plen);
    memcpy(e.neu, prefix, plen);
    memcpy(e.old + plen, &old_value, 4);
    memcpy(e.neu + plen, &new_value, 4);
    return e;
}

// A patch set is checked as a whole (*_match) before any of it is written;
// apply_* write it and, should a write still fail, undo what they wrote.
bool edits_match(const char* what, const Edit* edits, int n)
{
    for (int i = 0; i < n; i++) {
        if (!check_bytes(edits[i].va, edits[i].old, edits[i].n)) {
            dsf_log("garage: %s not applied (site %08X differs)", what, edits[i].va);
            return false;
        }
    }
    return true;
}

void undo_edits(const Edit* edits, int n)
{
    for (int i = 0; i < n; i++) patch_bytes(edits[i].va, edits[i].old, edits[i].n);
}

bool apply_edits(const char* what, const Edit* edits, int n)
{
    for (int i = 0; i < n; i++) {
        if (!patch_bytes(edits[i].va, edits[i].neu, edits[i].n)) {
            undo_edits(edits, i);
            dsf_log("garage: %s not applied (writing %08X failed)", what, edits[i].va);
            return false;
        }
    }
    return true;
}

struct Hook {
    uint32_t va;
    void* target;
    uint8_t len;
    uint8_t old[40];
    bool call;
};

bool hooks_match(const char* what, const Hook* hooks, int n)
{
    for (int i = 0; i < n; i++) {
        if (!check_bytes(hooks[i].va, hooks[i].old, hooks[i].len)) {
            dsf_log("garage: %s not applied (site %08X differs)", what, hooks[i].va);
            return false;
        }
    }
    return true;
}

void undo_hooks(const Hook* hooks, int n)
{
    for (int i = 0; i < n; i++) patch_bytes(hooks[i].va, hooks[i].old, hooks[i].len);
}

bool apply_hooks(const char* what, const Hook* hooks, int n)
{
    for (int i = 0; i < n; i++) {
        uint8_t buf[40];
        memset(buf, 0x90, hooks[i].len);
        buf[0] = hooks[i].call ? 0xE8 : 0xE9;
        int32_t rel = (int32_t)((uint32_t)hooks[i].target - (hooks[i].va + 5));
        memcpy(buf + 1, &rel, 4);
        if (!patch_bytes(hooks[i].va, buf, hooks[i].len)) {
            undo_hooks(hooks, i);
            dsf_log("garage: %s not applied (writing %08X failed)", what, hooks[i].va);
            return false;
        }
    }
    return true;
}

// Original bytes of a `call rel32` to target at va.
void call_bytes(uint8_t* out, uint32_t va, uint32_t target)
{
    out[0] = 0xE8;
    int32_t rel = (int32_t)(target - (va + 5));
    memcpy(out + 1, &rel, 4);
}

// ---- install ----------------------------------------------------------------
bool install_save()
{
    InitializeCriticalSection(&g_lock);
    ext_clear(g_ext);
    ext_clear(g_saved);
    ext_clear(g_loaded);
    ext_clear(g_backup);
    g_fake_4e0 = (uint32_t)g_ext.bits[0] - 0x4E0;
    g_fake_508 = (uint32_t)g_ext.bits[1] - 0x508;
    g_fake_558 = (uint32_t)g_ext.bits[3] - 0x558;

    Hook hooks[] = {
        {0x00507CF0, (void*)h_flag_slot, 5, {0x56, 0x57, 0x8B, 0x7C, 0x24}},
        {0x00507D30, (void*)h_flag_test, 5, {0x8D, 0x51, 0x38, 0x8B, 0x4C}},
        {0x0050A500, (void*)h_owned_slot, 6, {0x81, 0xC1, 0x9C, 0x0A, 0x00, 0x00}},
        {0x0050A1A0, (void*)s_unlocked, 5, {0x8B, 0x44, 0x24, 0x04, 0x53}},
        {0x0050A120, (void*)s_driven, 6, {0x53, 0x56, 0x8B, 0x74, 0x24, 0x0C}},
        {0x0050A170, (void*)s_takeride, 6, {0x8B, 0xD1, 0x8B, 0x4C, 0x24, 0x04}},
        {0x0067C320, (void*)lua_GetVehicleDriven, 5, {0xA1, 0x28, 0x72, 0xFE, 0x00}},
        {0x0067C3C0, (void*)lua_GetNumTakeARide, 5, {0xA1, 0x28, 0x72, 0xFE, 0x00}},
        {0x006FD51E, (void*)s_candidate, 0x21,
         {0x8B, 0xC8, 0x83, 0xE1, 0x1F, 0xBA, 0x01, 0x00, 0x00, 0x00, 0xD3, 0xE2, 0xC1, 0xE8, 0x05, 0x85, 0x94,
          0x85, 0x88, 0x20, 0x00, 0x00, 0x74, 0x09, 0x85, 0x94, 0x85, 0x00, 0x21, 0x00, 0x00, 0x74, 0x18}},
        {0x00711759, (void*)s_menu_takeride, 0x20,
         {0x8B, 0xC8, 0x83, 0xE1, 0x1F, 0xBF, 0x01, 0x00, 0x00, 0x00, 0xD3, 0xE7, 0x8B, 0x4C, 0x24, 0x44,
          0xC1, 0xE8, 0x05, 0x23, 0xBC, 0x81, 0x00, 0x21, 0x00, 0x00, 0xF7, 0xDF, 0x1B, 0xFF, 0xF7, 0xDF}},
        {0x005089E4, (void*)s_owned_counter, 6, {0xFE, 0x80, 0x6F, 0x4D, 0x00, 0x00}, true},
    };
    // memcpy call sites of the 0x4CF8-byte profile block and the two Clear* bindings.
    struct Site {
        uint32_t va;
        void* wrapper;
    } sites[] = {
        {0x00503D36, (void*)m_save},           // FUN_00503CD0 live -> buffer
        {0x005040D0, (void*)m_save},           // FUN_00504090 live -> buffer
        {0x00505A77, (void*)m_load},           // FUN_00505A10 buffer -> live (after the version check)
        {0x00503E2C, (void*)m_reset},          // FUN_00503D80 defaults -> live (new profile)
        {0x00504CA0, (void*)m_reset},          // FUN_00504C60 initialiseProfileData
        {0x00504053, (void*)m_newsave},        // FUN_00503FF0 defaults -> buffer
        {0x00505DBC, (void*)m_backup},         // FUN_00505D80 live -> stack copy
        {0x00505E81, (void*)m_restore},        // FUN_00505D80 stack copy -> live
        {0x00679F5A, (void*)m_clear_progress}, // ProfileSettings.ClearProgression
        {0x00679FAA, (void*)m_clear_stats},    // ProfileSettings.ClearStatistics
    };
    const int nsites = sizeof sites / sizeof sites[0];
    Hook calls[nsites + 2];
    for (int i = 0; i < nsites; i++) {
        calls[i] = {sites[i].va, sites[i].wrapper, 5, {0}, true};
        call_bytes(calls[i].old, sites[i].va, (uint32_t)game_memcpy);
    }
    calls[nsites] = {0x008536E7, (void*)f_write, 5, {0}, true};
    call_bytes(calls[nsites].old, 0x008536E7, (uint32_t)game_fopen);
    calls[nsites + 1] = {0x0085354A, (void*)f_read, 5, {0}, true};
    call_bytes(calls[nsites + 1].old, 0x0085354A, (uint32_t)game_fopen);

    // Verify everything before changing anything: the hooks only work as a set.
    const int nhooks = sizeof hooks / sizeof hooks[0];
    if (!hooks_match("save hooks", hooks, nhooks) || !hooks_match("save lifecycle", calls, nsites + 2)) return false;
    if (!apply_hooks("save lifecycle", calls, nsites + 2)) return false;
    if (!apply_hooks("save hooks", hooks, nhooks)) {
        undo_hooks(calls, nsites + 2);
        return false;
    }
    dsf_log("garage: save hooks on: flags of IDs %d..%d kept in %s next to autosave.dat", kExtFirst, kExtMax - 1,
            g_side_name);
    return true;
}

bool install_list()
{
    // Walk IDs 0..max_id-1, sort keys in g_sortkey, caps on cars per group and groups.
    const uint32_t key = (uint32_t)g_sortkey;
    const uint8_t cmp_edi[] = {0x81, 0xFF}, cmp_edx[] = {0x81, 0xFA};
    Edit edits[6];
    edits[0] = {0x006C57FB, 6, {0x8D, 0xAE, 0x64, 0x32, 0x00, 0x00}, {0xBD, 0, 0, 0, 0, 0x90}};
    memcpy(edits[0].neu + 1, &key, 4);
    edits[1] = edit_u32(0x006C587C, cmp_edi, 2, 0x130, (uint32_t)g_max_id);
    edits[2] = edit_u32(0x006C5A55, cmp_edi, 2, 0x130, (uint32_t)g_max_id);
    edits[3] = edit_u32(0x006C5762, cmp_edx, 2, 0x12F, (uint32_t)g_max_id - 1);
    edits[4] = {0x006C576A, 7, {0x8B, 0xAC, 0x9F, 0x64, 0x32, 0x00, 0x00}, {0x8B, 0x2C, 0x9D}};
    memcpy(edits[4].neu + 3, &key, 4);
    edits[5] = {0x006C5771, 7, {0x3B, 0xAC, 0x97, 0x64, 0x32, 0x00, 0x00}, {0x3B, 0x2C, 0x95}};
    memcpy(edits[5].neu + 3, &key, 4);
    Hook caps[] = {
        {0x006C5740, (void*)group_add_car, 5, {0x53, 0x8B, 0x5C, 0x24, 0x0C}},
        {0x006C7BB0, (void*)s_group_insert, 9, {0x56, 0x8B, 0xF1, 0x8B, 0x86, 0xE0, 0x10, 0x00, 0x00}},
    };
    if (!edits_match("garage list", edits, 6) || !hooks_match("garage caps", caps, 2)) return false;
    if (!apply_hooks("garage caps", caps, 2)) return false;
    if (!apply_edits("garage list", edits, 6)) {
        undo_hooks(caps, 2);
        return false;
    }
    dsf_log("garage: maker list walks model IDs 0..%d (stock 0..303); at most %d cars per group", g_max_id - 1,
            g_max_cars);
    return true;
}

// Maker groups of more than 20 cars: vfuncs 0x12C/0x130 read the plugin's
// storage, the car list shows such a group through a window of 19 cards (all
// or nothing; without it a group stays at 20 cars).
bool install_window()
{
    const uint8_t none[1] = {0};
    Edit edits[5];
    int n = 0;
    edits[n++] = edit_u32(0x00E18B1C, none, 0, 0x006C55A0, (uint32_t)group_car);    // garage vtable slot 0x12C
    edits[n++] = edit_u32(0x00E18B20, none, 0, 0x006C55C0, (uint32_t)group_copy);   // slot 0x130
    // The car list: prologue `sub esp,0x208` -> jmp carlist_hook; loop end -> loop_stub.
    Edit& pro = edits[n++];
    pro = {0x007113A0, 6, {0x81, 0xEC, 0x08, 0x02, 0x00, 0x00}, {0xE9, 0, 0, 0, 0, 0x90}};
    int32_t rel = (int32_t)((uint32_t)carlist_hook - (0x007113A0 + 5));
    memcpy(pro.neu + 1, &rel, 4);
    Edit& loop = edits[n++];
    loop = {0x007118C3, 8, {0x89, 0x44, 0x24, 0x30, 0x3B, 0x44, 0x24, 0x28}, {0xE8, 0, 0, 0, 0, 0x90, 0x90, 0x90}};
    rel = (int32_t)((uint32_t)loop_stub - (0x007118C3 + 5));
    memcpy(loop.neu + 1, &rel, 4);
    // The maker menu fill (slot 0x124) tells the script 19 for a windowed maker;
    // install_groups has already pointed it at fill_makers when it ran.
    if (bytes_equal(0x00E18B14, "\x40\x69\x6C\x00", 4))
        edits[n++] = edit_u32(0x00E18B14, none, 0, 0x006C6940, (uint32_t)fill_makers);
    if (!edits_match("big maker groups", edits, n) || !apply_edits("big maker groups", edits, n)) return false;
    g_window_on = true;
    dsf_log("garage: maker groups of up to %d cars; one of more than %d is shown %d cards at a time "
            "(vfuncs 12C/130 at E18B1C, car list 7113A0, 7118C3)",
            g_max_cars, kGroupCars, kWindowCards);
    return true;
}

// Move the maker-group table (stock: 40 groups at garage+0x20FC, count at
// +0x31DC) behind the object, which operator new allocates larger, and the
// menu's maker index array (stock: int[40] at menu+0x274) into g_maker_index.
bool install_groups(int groups)
{
    const uint32_t off = kGarageSize;                       // new table offset
    const uint32_t rel = (uint32_t)groups * kGroupSize;     // count, relative to the table
    const uint32_t count = off + rel;
    const uint32_t size = (count + 4 + 15) & ~15u;
    g_maker_index = (int32_t*)VirtualAlloc(nullptr, (groups + 2) * 4, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!g_maker_index) return false;
    const uint32_t maker0 = (uint32_t)g_maker_index, maker1 = maker0 + 4;

    struct Site {
        uint32_t va;
        uint8_t prefix[3];
        uint8_t plen;
        uint32_t old_value, new_value;
    } sites[] = {
        {0x006C7D90, {0x68}, 1, kGarageSize, size},       // push 0x3728 (operator new)
        {0x006C54C2, {0x8D, 0x81}, 2, 0x20FC, off},
        {0x006C5534, {0x81, 0xC1}, 2, 0x20FC, off},
        {0x006C55AB, {0x8B, 0x84, 0x81}, 3, 0x20FC, off},
        {0x006C55CD, {0x8D, 0xB4, 0x08}, 3, 0x20FC, off},
        {0x006C5989, {0x8D, 0xAE}, 2, 0x20FC, off},
        {0x006C6995, {0x8D, 0x86}, 2, 0x20FC, off},
        {0x006C6BC0, {0x81, 0xC6}, 2, 0x20FC, off},
        {0x006C57F3, {0x89, 0x9E}, 2, 0x31DC, count},
        {0x006C5978, {0x8B, 0xBE}, 2, 0x31DC, count},
        {0x006C6983, {0x8B, 0x8E}, 2, 0x31DC, count},
        {0x006C6BAB, {0x8B, 0xAE}, 2, 0x31DC, count},
        {0x006C7A8D, {0x89, 0x9E}, 2, 0x31DC, count},
        {0x006C7D40, {0x8B, 0x81}, 2, 0x31DC, count},
        {0x006C54C9, {0x8B, 0xB8}, 2, 0x10E0, rel},
        {0x006C553B, {0x8B, 0xB9}, 2, 0x10E0, rel},
        {0x006C7BE0, {0xFF, 0x86}, 2, 0x10E0, rel},
        {0x006C7C00, {0xFF, 0x86}, 2, 0x10E0, rel},
        {0x007110A8, {0x05}, 1, 0x274, 0},                     // add eax,0x274 -> mov eax,maker1 (below)
    };
    const int n = sizeof sites / sizeof sites[0];
    Edit edits[n + 5];
    for (int i = 0; i < n; i++)
        edits[i] = edit_u32(sites[i].va, sites[i].prefix, sites[i].plen, sites[i].old_value, sites[i].new_value);
    edits[n - 1].neu[0] = 0xB8;
    memcpy(edits[n - 1].neu + 1, &maker1, 4);
    // mov eax,[ebp+edx*4+0x270] / [esi+edx*4+0x270] -> mov eax,[edx*4+maker0]
    edits[n] = {0x007112DB, 7, {0x8B, 0x84, 0x95, 0x70, 0x02, 0x00, 0x00}, {0x8B, 0x04, 0x95}};
    memcpy(edits[n].neu + 3, &maker0, 4);
    edits[n + 1] = {0x00711411, 7, {0x8B, 0x84, 0x96, 0x70, 0x02, 0x00, 0x00}, {0x8B, 0x04, 0x95}};
    memcpy(edits[n + 1].neu + 3, &maker0, 4);
    // The menu-script lists the maker menu is filled into hold 40 entries: the
    // garage vtable's fill (slot 0x124) goes through fill_makers, which grows
    // them, and its two string sets skip a missing element.
    const uint8_t none[1] = {0};
    edits[n + 2] = edit_u32(0x00E18B14, none, 0, 0x006C6940, (uint32_t)fill_makers);
    for (int k = 0; k < 2; k++) {
        const uint32_t va = k ? 0x006C6AE2 : 0x006C6AC8;
        Edit& e = edits[n + 3 + k];
        e = {va, 5};
        call_bytes(e.old, va, 0x00ACE110);
        call_bytes(e.neu, va, (uint32_t)s_menu_set_string);
    }
    if (!edits_match("maker groups", edits, n + 5) || !apply_edits("maker groups", edits, n + 5)) {
        VirtualFree(g_maker_index, 0, MEM_RELEASE);
        g_maker_index = nullptr;
        return false;
    }
    g_groups_off = off;
    g_count_rel = rel;
    g_max_groups = groups;
    dsf_log("garage: maker groups %d (stock 40): table at garage+0x%X, count at +0x%X, object 0x%X bytes; "
            "maker menu lists grown on fill (E18B14), string sets checked (6C6AC8, 6C6AE2)",
            groups, off, count, size);
    return true;
}

}  // namespace

void mod_garage_init()
{
    char name[64];
    cfg_str("garage", "side_file", "dsfforza-garage.dat", name, sizeof name);
    if (name[0] && !strpbrk(name, "\\/:")) strcpy_s(g_side_name, name);

    if (cfg_bool("garage", "save", true)) g_save_active = install_save();
    else dsf_log("garage: [garage] save=0, save hooks off");

    int max_id = cfg_int("garage", "max_id", kExtMax);
    if (max_id < kStockIds) max_id = kStockIds;
    if (max_id > kExtMax) max_id = kExtMax;
    g_max_id = max_id;
    int groups = cfg_int("garage", "max_groups", 120);
    if (groups < kStockGroups) groups = kStockGroups;
    if (groups > 1000) groups = 1000;

    if (!cfg_bool("garage", "list", true)) {
        dsf_log("garage: [garage] list=0, garage list unchanged");
    } else if (max_id > kStockIds && !g_save_active) {
        // Listing IDs the save can't hold would corrupt the stock arrays.
        dsf_log("garage: save hooks are off, so the garage list stays at IDs 0..303");
    } else {
        int cars = cfg_int("garage", "max_cars", kExtMax);
        g_max_cars = cars < kGroupCars ? kGroupCars : (cars > kExtMax ? kExtMax : cars);
        g_menu_debug = cfg_bool("garage", "menu_debug", false);
        g_numlist_mode = cfg_int("garage", "debug_numlist", 1);
        g_shift_mode = cfg_int("garage", "debug_shift", 0);
        g_margin_back = cfg_int("garage", "window_back", 2);
        g_margin_ahead = cfg_int("garage", "window_ahead", 8);
        if (g_margin_back < 0) g_margin_back = 0;
        if (g_margin_ahead < 0) g_margin_ahead = 0;
        if (g_margin_back + g_margin_ahead > kWindowCards - 2) g_margin_ahead = kWindowCards - 2 - g_margin_back;
        int max_cars = g_max_cars;
        g_max_cars = kGroupCars;   // until the window is in
        g_garage_active = install_list();
        if (g_garage_active && groups > kStockGroups && !install_groups(groups))
            dsf_log("garage: maker groups stay at 40");
        g_count_rel = (uint32_t)g_max_groups * kGroupSize;
        if (g_garage_active && max_cars > kGroupCars) {
            g_max_cars = max_cars;
            if (!install_window()) {
                g_max_cars = kGroupCars;
                dsf_log("garage: maker groups stay at %d cars", kGroupCars);
            }
        }
        if (g_menu_debug) CreateThread(nullptr, 0, debug_thread, nullptr, 0, nullptr);
    }
}

int garage_card_limit() { return g_window_on ? garage_card_limit_impl() : 20; }
