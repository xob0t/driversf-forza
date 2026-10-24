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

typedef int* (__fastcall* SlotFn)(void* self, void* edx, int id);
typedef unsigned (__fastcall* TestFn)(void* self, void* edx, int id, unsigned bit);
typedef void (__fastcall* SetFn)(void* self, void* edx, int id);

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
#define DSF_MODULE(name) printf("init %s (changed sites)\n", #name); mod_##name##_init();
#include "../src/modules.h"
#undef DSF_MODULE
    CHECK(*(BYTE*)0x008FB340 == 0x8B && *(BYTE*)0x008FBE90 == 0x53);    // archive: no site replaced
    CHECK(*(BYTE*)0x0085A9B2 == 0xEB);                                 // large_files is its own patch
    CHECK(*(unsigned*)(0x006C7D90 + 1) == 0x3728);                     // garage object size stock
    CHECK(*(unsigned*)(0x006C54C2 + 2) == 0x20FC && *(unsigned*)0x00E18B14 == 0x006C6940);
    CHECK(*(unsigned*)(0x006C587C + 2) == 1000);                       // the list patch still applies
    CHECK(*(int*)0x0070D72B == (int)(0x00ADA7F0 - 0x0070D72F));        // hud: picture-slot calls stock
    CHECK(*(BYTE*)0x006C6855 == 0xE8 && *(int*)0x006C6856 != (int)(0x006C40C0 - 0x006C685A));  // text sites on
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
    CHECK(a == list + 0x38 / 4 && a[0] == 100);           // stock ID: stock list, first slot
    int* b = slot(list, nullptr, 500);
    CHECK(b && (b < list || b >= list + sizeof list / 4) && b[0] == 500);  // added ID: outside the block
    b[1] |= 2;
    CHECK(test(list, nullptr, 500, 1) == 1 && test(list, nullptr, 500, 0) == 0);
    CHECK(slot(list, nullptr, 500) == b);
    int* c = slot(list, nullptr, 310);                   // legacy entry already in the stock list wins
    CHECK(c != nullptr);
    list[0x38 / 4 + 2 * 5] = 320; list[0x38 / 4 + 2 * 5 + 1] = 1;
    CHECK(slot(list, nullptr, 320) == list + 0x38 / 4 + 10 && test(list, nullptr, 320, 0) == 1);
    CHECK(test(list, nullptr, 8000000, 0) == 1);      // save hooks active
    CHECK(test(list, nullptr, 8000000 + 1, 0) == 1);  // garage list active
    CHECK(test(list, nullptr, 8000000 + 0x1000 + 41, 0) == 1);
    CHECK(test(list, nullptr, 8000000 + 0x2000 + 999, 0) == 1);
    CHECK(test(list, nullptr, 8000000 + 0x2000 + 1000, 0) == 0);
    // Full stock list: a scratch slot instead of a NULL write.
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

    // Take-a-ride setter (FUN_0050A170, no side effects): stock bit, extended bit, dropped.
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

    // HUD: ctor hook, and the garage card emblem hook (call + 3 NOPs over the itemState write).
    CHECK(*(BYTE*)0x006C6855 == 0xE8 && *(BYTE*)0x007118A5 == 0xE8);
    CHECK(*(BYTE*)0x007118AA == 0x90 && *(BYTE*)0x007118AC == 0x90 && *(BYTE*)0x007118AD == 0x74);
    CHECK(*(BYTE*)0x0070D72A == 0xE8 && *(int*)0x0070D72B != (int)(0x00ADA7F0 - 0x0070D72F));
    printf("%s (%d failures); log: %s\\dsfforza.log\n", failures ? "FAILED" : "ok", failures, dsf_plugin_dir());
    return failures ? 1 : 0;
}
