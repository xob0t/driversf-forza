// Offline check of dsfforza.asi patches: maps Driver.exe's image at 0x00400000
// in this process (built at another base), runs every module's init against
// it, and exercises the garage hooks that need no game state. Nothing of the
// game runs, so this only proves that every expected byte matches, the
// patches land, and the pure hooks behave. A first child changes one byte of
// three all-or-nothing patch sets and checks that none of each set lands.
//
//   tools\asi\build.ps1 -Test   ->  out\asi\offline.exe <Driver.exe>
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <string.h>

#include "../src/patch.h"

void dsf_core_init();
#define DSF_MODULE(name) void mod_##name##_init();
#include "../src/modules.h"
#undef DSF_MODULE
int mod_hands_selftest();
long mod_render_dropped();

static int failures = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); failures++; } } while (0)

static bool map_image(const char* path)
{
    HANDLE f = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    if (f == INVALID_HANDLE_VALUE) { printf("can't open %s\n", path); return false; }
    DWORD size = GetFileSize(f, nullptr), got = 0;
    BYTE* file = (BYTE*)VirtualAlloc(nullptr, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    ReadFile(f, file, size, &got, nullptr);
    CloseHandle(f);
    IMAGE_NT_HEADERS32* nt = (IMAGE_NT_HEADERS32*)(file + ((IMAGE_DOS_HEADER*)file)->e_lfanew);
    // The parent reserved the range before this process started (see main).
    BYTE* base = (BYTE*)VirtualAlloc((void*)0x00400000, nt->OptionalHeader.SizeOfImage, MEM_COMMIT,
                                     PAGE_EXECUTE_READWRITE);
    if (base != (BYTE*)0x00400000) { printf("0x00400000 is taken (%p)\n", base); return false; }
    memcpy(base, file, nt->OptionalHeader.SizeOfHeaders);
    IMAGE_SECTION_HEADER* s = IMAGE_FIRST_SECTION(nt);
    for (int i = 0; i < nt->FileHeader.NumberOfSections; i++)
        memcpy(base + s[i].VirtualAddress, file + s[i].PointerToRawData,
               min(s[i].SizeOfRawData, s[i].Misc.VirtualSize));
    return true;
}

// Runs the narrow branch's end of FUN_007215E0 (0x007216BE: the length check,
// then the tail: store the length at [edi+0x84], [ebp+0x10] = length - 1, pop
// edi/esi/ebp, ret 4) with the registers its loop leaves: esi the string, ecx
// the byte count with the terminator, dl the OR of the bytes. Returns the length.
#pragma warning(push)
#pragma warning(disable : 4731)  // ebp is saved and restored around the jump
static int text_len(const char* s)
{
    static int ctx[0x88 / 4 + 1];
    static int frame[8];
    int len = 0;
    unsigned char orv = 0;
    for (const char* p = s;; p++) {
        len++;
        orv |= (unsigned char)*p;
        if (!*p) break;
    }
    ctx[0x84 / 4] = -1;
    __asm {
        push ebx
        push 0
        lea eax, back
        push eax
        push ebp
        push esi
        push edi
        mov esi, s
        mov ecx, len
        mov dl, orv
        lea edi, ctx
        lea ebp, frame
        mov eax, 0x007216BE
        jmp eax
    back:
        pop ebx
    }
    return ctx[0x84 / 4];
}
#pragma warning(pop)

// The text key FUN_00724850 (thiscall: key, text): {text, 0, length, hash}.
static int text_key(const char* s)
{
    typedef void(__fastcall * KeyFn)(int* key, void*, const char* text);
    int key[4] = {0, 0, -1, 0};
    ((KeyFn)0x00724850)(key, nullptr, s);
    return key[2];
}

typedef int* (__fastcall* SlotFn)(void* self, void* edx, int id);
typedef unsigned (__fastcall* TestFn)(void* self, void* edx, int id, unsigned bit);
typedef void (__fastcall* SetFn)(void* self, void* edx, int id);

// Lua heap: the lua_Alloc that lua_newstate now gets (the patched push at
// 0x005E7DB7) in front of a stand-in for the game's arena allocator (the real
// one, FUN_005E4AB0, needs the game's CRT): a 64 KB bump arena registered as
// heap 3 that fails when full. Blocks come from the arena until it is full,
// then from the overflow heap; reallocs move them with their contents, frees
// go to the owner.
static unsigned char g_arena[0x10000];
static unsigned g_arena_top;
static int g_arena_frees;

static void* __cdecl fake_arena_alloc(void*, void* ptr, size_t osize, size_t nsize)
{
    if (nsize == 0) { g_arena_frees++; return nullptr; }
    if (ptr && nsize <= osize) return ptr;
    if (g_arena_top + nsize > sizeof g_arena) {
        if (osize == 0 && nsize <= 1024 && g_arena_frees >= 100) g_arena_top = 0x40;   // all freed: start over
        else return nullptr;
    }
    void* p = g_arena + g_arena_top;
    g_arena_top += (unsigned)((nsize + 15) & ~15u);
    if (ptr) memcpy(p, ptr, osize);
    return p;
}

static void lua_heap_test()
{
    typedef void*(__cdecl * AllocFn)(void* ud, void* ptr, size_t osize, size_t nsize);
    static void* mgr[8];
    unsigned saved_mgr = *(unsigned*)0x012536AC;
    BYTE saved_vanilla[5];
    memcpy(saved_vanilla, (void*)0x005E4AB0, 5);
    *(BYTE*)0x005E4AB0 = 0xE9;   // the game's allocator -> the stand-in
    *(int*)0x005E4AB1 = (int)((size_t)fake_arena_alloc - (0x005E4AB0 + 5));
    *(unsigned*)(g_arena + 4) = sizeof g_arena;   // arena header: +4 size
    g_arena_top = 0x40;
    mgr[3] = g_arena;
    *(unsigned*)0x012536AC = (unsigned)(size_t)mgr;
    auto in_arena = [](void* p) { return (unsigned char*)p >= g_arena && (unsigned char*)p < g_arena + sizeof g_arena; };
    AllocFn alloc = (AllocFn)(size_t)*(unsigned*)0x005E7DB8;
    CHECK(*(BYTE*)0x005E7DB7 == 0x68 && (size_t)alloc != 0x005E4AB0);
    CHECK(*(BYTE*)0x005E4C05 == 0x68 && *(unsigned*)0x005E4C06 == 32u << 20);   // arena_mb=32
    void* blocks[160];
    int inside = 0, outside = 0;
    for (int i = 0; i < 160; i++) {
        blocks[i] = alloc(nullptr, nullptr, 0, 1024);
        CHECK(blocks[i] != nullptr);
        if (!blocks[i]) return;
        memset(blocks[i], i, 1024);
        (in_arena(blocks[i]) ? inside : outside)++;
    }
    CHECK(inside == 63 && outside == 97 && in_arena(blocks[0]) && !in_arena(blocks[159]));
    // An arena block that the arena can't grow moves out, with its bytes.
    void* grown = alloc(nullptr, blocks[1], 1024, 0x8000);
    CHECK(grown && !in_arena(grown) && ((unsigned char*)grown)[1023] == 1 && g_arena_frees == 1);
    blocks[1] = grown;
    void* shrunk = alloc(nullptr, blocks[159], 1024, 16);   // an overflow block shrinks where it is
    CHECK(shrunk && !in_arena(shrunk) && ((unsigned char*)shrunk)[15] == 159);
    blocks[159] = shrunk;
    bool kept = true;
    for (int i = 2; i < 159; i++) kept &= ((unsigned char*)blocks[i])[512] == (unsigned char)i;
    CHECK(kept);
    for (int i = 0; i < 160; i++) {
        size_t n = i == 1 ? 0x8000 : i == 159 ? 16 : 1024;
        CHECK(alloc(nullptr, blocks[i], n, 0) == nullptr);
    }
    CHECK(g_arena_frees == 1 + 62);   // the arena saw only its own blocks' frees
    g_arena_frees = 100;
    void* again = alloc(nullptr, nullptr, 0, 1024);   // the arena has room again
    CHECK(again && in_arena(again));
    alloc(nullptr, again, 1024, 0);
    memcpy((void*)0x005E4AB0, saved_vanilla, 5);
    *(unsigned*)0x012536AC = saved_mgr;
}

// The process heap usually sits inside 0x00400000..0x01DC0000 by the time
// main runs, so the test starts itself suspended and reserves the range in
// the child first.
static int spawn_child(const char* exe, const char* mode)
{
    char cmd[2 * MAX_PATH + 48], self[MAX_PATH];
    GetModuleFileNameA(nullptr, self, MAX_PATH);
    _snprintf_s(cmd, sizeof cmd, _TRUNCATE, "\"%s\" --child \"%s\" %s", self, exe, mode);
    STARTUPINFOA si = {sizeof si};
    PROCESS_INFORMATION pi;
    if (!CreateProcessA(self, cmd, nullptr, nullptr, FALSE, CREATE_SUSPENDED, nullptr, nullptr, &si, &pi)) {
        printf("CreateProcess failed (%lu)\n", GetLastError());
        return 2;
    }
    if (!VirtualAllocEx(pi.hProcess, (void*)0x00400000, 0x019C0000, MEM_RESERVE, PAGE_EXECUTE_READWRITE))
        printf("reserving 0x00400000 in the child failed (%lu)\n", GetLastError());
    ResumeThread(pi.hThread);
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 2;
    GetExitCodeProcess(pi.hProcess, &code);
    return (int)code;
}

// All-or-nothing patch sets: one changed byte at the last site of a set must
// leave its earlier sites as they were (as when another plugin got there first).
static int mismatch_test()
{
    *(BYTE*)0x008FC400 ^= 0xFF;   // archive: last replaced function ("open in memory")
    *(BYTE*)0x00711411 ^= 0xFF;   // garage: maker index read, late in the maker-group edits
    *(BYTE*)0x007118A5 ^= 0xFF;   // hud: card emblem site (checked after the picture-slot calls)
    *(BYTE*)0x00BB5A96 ^= 0xFF;   // render: the trap branch (checked after both push calls)
#define DSF_MODULE(name) printf("init %s (changed sites)\n", #name); mod_##name##_init();
#include "../src/modules.h"
#undef DSF_MODULE
    CHECK(*(BYTE*)0x008FB340 == 0x8B && *(BYTE*)0x008FBE90 == 0x53);    // archive: no site replaced
    CHECK(*(BYTE*)0x0085A9B2 == 0xEB);                                 // large_files is its own patch
    CHECK(*(unsigned*)(0x006C7D90 + 1) == 0x3728);                     // garage object size vanilla
    CHECK(*(unsigned*)(0x006C54C2 + 2) == 0x20FC && *(unsigned*)(0x006C57F3 + 2) == 0x31DC);  // groups vanilla
    CHECK(*(unsigned*)0x00E18B1C != 0x006C55A0);                       // big groups are their own set
    CHECK(*(unsigned*)(0x006C587C + 2) == 1000);                       // the list patch still applies
    CHECK(*(int*)0x0070D72B == (int)(0x00ADA7F0 - 0x0070D72F));        // hud: picture-slot calls vanilla
    CHECK(*(BYTE*)0x006C6855 == 0xE8 && *(int*)0x006C6856 != (int)(0x006C40C0 - 0x006C685A));  // text sites on
    CHECK(*(int*)0x00BB5B4D == (int)(0x00BB5710 - 0x00BB5B51) &&
          *(int*)0x00BB5BB6 == (int)(0x00BB5710 - 0x00BB5BBA));        // render: both pushes vanilla
    printf("%s (%d failures, changed sites)\n", failures ? "FAILED" : "ok", failures);
    return failures ? 1 : 0;
}

int main(int argc, char** argv)
{
    if (argc < 2 || strcmp(argv[1], "--child") != 0) {
        const char* exe = argc > 1 ? argv[1] : "D:\\Games\\Driver San Francisco\\Driver.exe";
        int mismatch = spawn_child(exe, "mismatch");
        int normal = spawn_child(exe, "normal");   // last, so the log left behind is the normal run's
        return mismatch ? mismatch : normal;
    }
    const char* exe = argv[2];
    if (!map_image(exe)) return 2;
    dsf_core_init();
    if (argc > 3 && strcmp(argv[3], "mismatch") == 0) return mismatch_test();
#define DSF_MODULE(name) printf("init %s\n", #name); mod_##name##_init();
#include "../src/modules.h"
#undef DSF_MODULE

    // Flag list (FUN_00507CF0 find-or-insert, FUN_00507D30 test).
    static int list[0x38 / 4 + 0x130 * 2];
    memset(list, 0xFF, sizeof list);
    SlotFn slot = (SlotFn)0x00507CF0;
    TestFn test = (TestFn)0x00507D30;
    int* a = slot(list, nullptr, 100);
    CHECK(a == list + 0x38 / 4 && a[0] == 100);           // vanilla ID: vanilla list, first slot
    int* b = slot(list, nullptr, 500);
    CHECK(b && (b < list || b >= list + sizeof list / 4) && b[0] == 500);  // added ID: outside the block
    b[1] |= 2;
    CHECK(test(list, nullptr, 500, 1) == 1 && test(list, nullptr, 500, 0) == 0);
    CHECK(slot(list, nullptr, 500) == b);
    int* c = slot(list, nullptr, 310);                   // legacy entry already in the vanilla list wins
    CHECK(c != nullptr);
    list[0x38 / 4 + 2 * 5] = 320; list[0x38 / 4 + 2 * 5 + 1] = 1;
    CHECK(slot(list, nullptr, 320) == list + 0x38 / 4 + 10 && test(list, nullptr, 320, 0) == 1);
    CHECK(test(list, nullptr, 8000000, 0) == 1);      // save hooks active
    CHECK(test(list, nullptr, 8000000 + 1, 0) == 1);  // garage list active
    CHECK(test(list, nullptr, 8000000 + 0x1000 + 41, 0) == 1);
    CHECK(test(list, nullptr, 8000000 + 0x2000 + 999, 0) == 1);
    CHECK(test(list, nullptr, 8000000 + 0x2000 + 1000, 0) == 0);
    // Full vanilla list: a scratch slot instead of a NULL write.
    for (int i = 0; i < 0x130; i++) list[0x38 / 4 + 2 * i] = 1000 + i;
    int* d = slot(list, nullptr, 50);
    CHECK(d != nullptr && d[0] == 50);

    // Owned list (FUN_0050A500): stats object, list at +0xBDC.
    static int stats[0x2C60 / 4];
    memset(stats, 0xFF, sizeof stats);
    SlotFn owned = (SlotFn)0x0050A500;
    int* o1 = owned(stats, nullptr, 200);
    CHECK(o1 == stats + 0xBDC / 4 && *o1 == 200);
    int* o2 = owned(stats, nullptr, 700);
    CHECK(o2 && (o2 < stats || o2 >= stats + sizeof stats / 4) && *o2 == 700);

    // Take-a-ride setter (FUN_0050A170, no side effects): vanilla bit, extended bit, dropped.
    memset(stats, 0, sizeof stats);
    SetFn ride = (SetFn)0x0050A170;
    ride(stats, nullptr, 33);
    CHECK(stats[(0x558 >> 2) + 1] == 2);
    ride(stats, nullptr, 700);
    ride(stats, nullptr, 5000);
    int nonzero = 0;
    for (int i = 0; i < (int)(sizeof stats / 4); i++) nonzero += stats[i] != 0;
    CHECK(nonzero == 1);

    // Garage list loop bounds and sort-key table.
    CHECK(*(unsigned*)(0x006C587C + 2) == 1000 && *(unsigned*)(0x006C5A55 + 2) == 1000);
    CHECK(*(BYTE*)0x006C57FB == 0xBD);

    // Maker groups past 20 cars: FUN_006C5740 files cars (equal sort keys: in
    // order), vfunc 0x12C reads them back, vfunc 0x130 copies the count.
    {
        typedef void(__fastcall * AddFn)(void* garage, void*, int* group, int id);
        typedef int(__fastcall * CarFn)(void* garage, void*, int g, int i);
        typedef void(__fastcall * CopyFn)(void* garage, void*, int g, int* out);
        static int garage[0x8000 / 4];
        unsigned table = *(unsigned*)(0x006C54C2 + 2);   // relocated table offset
        int* g0 = (int*)((char*)garage + table);
        int* g1 = g0 + 0x6C / 4;
        strcpy_s((char*)g0 + 0x54, 16, "OFFLINE A");
        strcpy_s((char*)g1 + 0x54, 16, "OFFLINE B");
        AddFn add = (AddFn)0x006C5740;
        for (int id = 1; id <= 45; id++) add(garage, nullptr, id % 3 ? g0 : g1, id);
        CHECK(g0[20] == 30 && g1[20] == 15);
        CarFn car = *(CarFn*)0x00E18B1C;
        int ok = 0;
        for (int i = 0, id = 1; id <= 45; id++)
            if (id % 3) ok += car(garage, nullptr, 0, i++) == id;
        CHECK(ok == 30 && car(garage, nullptr, 1, 14) == 45);
        int copy[0x6C / 4];
        (*(CopyFn*)0x00E18B20)(garage, nullptr, 0, copy);
        CHECK(copy[20] == 30 && copy[0] == 1 && strcmp((char*)copy + 0x54, "OFFLINE A") == 0);
    }

    // The maker menu's per-car check (call at 0x007111D1 to FUN_00710DA0) for car i of a
    // group: a car sold at a garage the profile has opened (item type 2, garage 2) is
    // available. Card texts exist for i < 20 only; past them the screen holds other
    // things (issue #181: an int where the text object was read, a crash).
    {
        typedef bool(__fastcall * CheckFn)(void* screen, void*, void* item, int i);
        CHECK(*(BYTE*)0x007111D1 == 0xE8);
        CheckFn check = (CheckFn)(0x007111D1 + 5 + *(int*)(0x007111D1 + 1));
        CHECK((unsigned)(size_t)check != 0x00710DA0);
        static unsigned char engine[0x400], profile[0x14C + 0xA10], item[0x100];
        static unsigned screen[0x318 / 4 + 64];
        *(unsigned**)(engine + 0x36C) = (unsigned*)profile;
        unsigned* opened = (unsigned*)(profile + 0x14C + 0x9B8);   // {garage, bits} x 10
        opened[0] = 2;
        opened[1] = 1;
        *(int*)(item + 0xA0) = 2;
        *(int*)(item + 0xEC) = 2;
        for (unsigned& v : screen) v = (unsigned)(size_t)&screen[0];   // objects whose "vtable" is no vtable
        for (int i = 0; i < 20; i++) screen[0x1C0 / 4 + i] = 0;          // no card texts: nothing to write
        void* saved = *(void**)0x00FE7228;
        *(void**)0x00FE7228 = engine;
        CHECK(check(screen, nullptr, item, 3));
        CHECK(check(screen, nullptr, item, 31));
        CHECK(check(screen, nullptr, item, 999));
        opened[1] = 0;   // garage 2 not opened: not available, wherever the car is
        CHECK(!check(screen, nullptr, item, 3) && !check(screen, nullptr, item, 31));
        *(void**)0x00FE7228 = saved;
    }

    // Spool item lists past 1100 (SSIC SPOL): load through the patched 0x0059B6D0 from a
    // fake reader whose entry 0 is the list, then find UIDs through 0x0059B5A0.
    {
        typedef void(__fastcall * LoadFn)(void* self, void*, void* reader, int index);
        typedef unsigned(__fastcall * FindFn)(void* self, void*, unsigned lo, unsigned hi);
        static unsigned reader[0x5618 / 4];
        static unsigned list[1 + 1500 * 2];
        static unsigned char provider[0x2600];
        list[0] = 1500;
        for (unsigned i = 0; i < 1500; i++) {
            list[1 + 2 * i] = 0x1000 + i;
            list[2 + 2 * i] = 0x2D00AA00;
        }
        reader[0x44CC / 4] = (unsigned)(size_t)list;   // payload pointer of entry 0
        reader[0x55FC / 4] = 1;
        LoadFn load = (LoadFn)0x0059B6D0;
        FindFn find = (FindFn)0x0059B5A0;
        load(provider, nullptr, reader, 0);
        CHECK(*(unsigned*)(provider + 0x2260) == 1100);                        // inline part as vanilla sees it
        CHECK(find(provider, nullptr, 0x1000 + 1400, 0x2D00AA00) == 1400);      // past 1100: the side list
        CHECK(find(provider, nullptr, 0x1000 + 7, 0x2D00AA00) == 7);
        CHECK(find(provider, nullptr, 0x9999, 0x2D00AA00) == 0xFFFF);
        list[0] = 50;                                                           // a short list again
        load(provider, nullptr, reader, 0);
        CHECK(*(unsigned*)(provider + 0x2260) == 50);
        CHECK(find(provider, nullptr, 0x1000 + 1400, 0x2D00AA00) == 0xFFFF);    // side list dropped
        CHECK(find(provider, nullptr, 0x1000 + 49, 0x2D00AA00) == 49);
    }

    // HUD: ctor hook, and the garage card emblem hook (call + 3 NOPs over the itemState write).
    CHECK(*(BYTE*)0x006C6855 == 0xE8 && *(BYTE*)0x007118A5 == 0xE8);
    CHECK(*(BYTE*)0x007118AA == 0x90 && *(BYTE*)0x007118AC == 0x90 && *(BYTE*)0x007118AD == 0x74);
    CHECK(*(BYTE*)0x0070D72A == 0xE8 && *(int*)0x0070D72B != (int)(0x00ADA7F0 - 0x0070D72F));
    // The text length check of FUN_007215E0: a lone slot code keeps its length,
    // other strings of bytes up to 0x20 are empty as vanilla.
    CHECK(*(BYTE*)0x007216BE == 0xE9 && *(BYTE*)0x0072487A == 0xE9);
    CHECK(text_len("\x01") == 2 && text_len("\x0F") == 2 && text_len("A") == 2 && text_len("AB") == 3);
    CHECK(text_len("\x10") == 2 && text_len("\x11") == 2);
    CHECK(text_len(" ") == 0 && text_len("\x12") == 0 && text_len("\x09") == 0 && text_len("\x0B") == 0);
    CHECK(text_len("\x01\x02") == 0);
    CHECK(text_key("\x01") == 2 && text_key("A") == 2 && text_key(" ") == 0 && text_key("\x0C") == 0);
    CHECK(text_len("") == 0);
    // The maker list's name and badge string sets (0x6C6AC8, 0x6C6AE2) go to two plugin functions.
    {
        unsigned name_set = 0x006C6AC8 + 5 + *(int*)0x006C6AC9, badge_set = 0x006C6AE2 + 5 + *(int*)0x006C6AE3;
        CHECK(name_set != 0x00ACE110 && badge_set != 0x00ACE110 && name_set != badge_set);
    }
    lua_heap_test();
    // Hands: the arms' pose call goes through the plugin; the IK on a synthetic arm.
    CHECK(*(BYTE*)0x005676FF == 0xE8 && *(int*)0x00567700 != (int)(0x008609C0 - 0x00567704));
    // The occupant (chase views, AI drivers): his pose task and its two skin builds.
    CHECK(*(BYTE*)0x005668D3 == 0xE8 && *(int*)0x005668D4 != (int)(0x005650A0 - 0x005668D8));
    CHECK(*(BYTE*)0x005652FB == 0xE8 && *(int*)0x005652FC != (int)(0x00BCA840 - 0x00565300));
    CHECK(*(BYTE*)0x00565910 == 0xE8 && *(int*)0x00565911 != (int)(0x00BCA840 - 0x00565915));
    CHECK(mod_hands_selftest() == 0);
    // Draw queue: both pushes go through the plugin, the trap branch is gone, and a
    // full queue drops the entry (FUN_00BB5710's thiscall, through the patched site).
    {
        CHECK(memcmp((void*)0x00BB5A95, "\x90\x90\x90\x90\x90\x90", 6) == 0);
        CHECK(*(int*)0x00BB5BB6 == *(int*)0x00BB5B4D + (0x00BB5B51 - 0x00BB5BBA));
        typedef void(__fastcall * PushFn)(void* queue, void*, const unsigned* entry);
        PushFn push = (PushFn)(0x00BB5B51 + *(int*)0x00BB5B4D);
        CHECK((unsigned)(size_t)push != 0x00BB5710);
        static unsigned entries[3 * 8];
        struct { long count, capacity; void* block; unsigned* entries; } q = {0, 2, nullptr, entries};
        unsigned e[8] = {1, 2, 3, 4, 5, 6, 7, 8};
        long before = mod_render_dropped();
        for (int i = 0; i < 3; i++) { e[0] = 10 + i; push(&q, nullptr, e); }
        CHECK(q.count == 2 && entries[0] == 10 && entries[8] == 11 && entries[15] == 8 && entries[16] == 0);
        CHECK(mod_render_dropped() == before + 1);
    }
    printf("%s (%d failures); log: %s\\dsfforza.log\n", failures ? "FAILED" : "ok", failures, dsf_plugin_dir());
    return failures ? 1 : 0;
}
