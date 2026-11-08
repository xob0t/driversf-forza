// archive: lifts the 1100-entries-per-CHNK limit of the game's chunk reader,
// so dngvehicles.sp can hold more than 365 vehicles (vanilla 150 + 215 added).
// research/spool-limits.md, "Raising the root entry limit", has the evidence
// and the patch table.
//
// The chunk reader (constructor 0x008FBE30, 0x5618 bytes) keeps its entry
// table inline: 1100 records of 16 bytes at +0x08, the count at +0x44C8, 1100
// payload pointers at +0x44CC, their count at +0x55FC, then the file handle,
// parent and state fields. Readers live inline in other objects (the spool
// object at +0x14) and on the stack (34 functions reserve 0x5618 bytes), so
// the object can't grow. Instead, entries 1100 and up of a reader go to a side
// table owned by this module, keyed by the reader's address, and every
// function of the reader that touches an entry by index is replaced by one
// that knows about the side table. Entries 0..1099 stay where they were, so a
// reader with at most 1100 entries behaves exactly as before.
//
// Only the reader's own translation unit (0x008FB200..0x008FCA00) touches the
// table directly; every other caller goes through the accessors (Ghidra scans
// of 0x44C8/0x44CC/0x55FC, out/ghidra/archive1). The replaced functions are
// listed in kSites. The never-called writer functions (0x008FB500, 0x008FB940,
// 0x008FB9E0, 0x008FBB30, 0x008FBBD0, 0x008FBF30, 0x008FC2F0: no call, jump or
// data reference) are left alone.
//
// Second patch ([archive] large_files): the file layer's seek clamps positions
// of 2 GiB and up to 0, so an archive past 2 GiB reads the wrong data; one
// JGE -> JMP lets positions run to 4 GiB (see mod_archive_init).
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <intrin.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "patch.h"
#include "shared.h"

namespace {

const uint32_t kInline = 1100;  // entries the reader holds inline

#pragma pack(push, 1)
struct Entry {
    uint32_t tag;
    uint32_t offset;
    uint8_t version;
    uint8_t name_len;
    uint8_t align_log2;
    uint8_t flag;  // 1: payload pointer not owned (not freed on close)
    uint32_t size;
};
#pragma pack(pop)
static_assert(sizeof(Entry) == 16, "entry");

struct Reader {
    void* vtable;
    int mode;  // 0 memory, 1 file, 3 nested in parent
    Entry entries[kInline];
    uint32_t count;
    void* ptrs[kInline];
    uint32_t ptr_count;
    int file;
    Reader* parent;
    uint32_t parent_index;
    int refs;
    int state;  // async open state machine
    uint8_t flag5614;
    uint8_t ok;
    uint8_t pad[2];
};
static_assert(offsetof(Reader, entries) == 0x08, "entries");
static_assert(offsetof(Reader, count) == 0x44C8, "count");
static_assert(offsetof(Reader, ptrs) == 0x44CC, "ptrs");
static_assert(offsetof(Reader, ptr_count) == 0x55FC, "ptr_count");
static_assert(offsetof(Reader, file) == 0x5600, "file");
static_assert(offsetof(Reader, parent) == 0x5604, "parent");
static_assert(offsetof(Reader, parent_index) == 0x5608, "parent_index");
static_assert(offsetof(Reader, refs) == 0x560C, "refs");
static_assert(offsetof(Reader, state) == 0x5610, "state");
static_assert(offsetof(Reader, ok) == 0x5615, "ok");
static_assert(sizeof(Reader) == 0x5618, "size");

// ---- game functions --------------------------------------------------------
typedef int(__cdecl* FileSeek)(int file, int pos);
typedef void(__cdecl* FileRead)(int file, void* buf, uint32_t size);
typedef int(__cdecl* FileClose)(int file);
typedef uint32_t(__cdecl* SectorSize)();
typedef int(__cdecl* HeaderVersion)(const void* hdr);
typedef void*(__cdecl* GameMalloc)(size_t n);
typedef void(__cdecl* GameFree)(void* p);
// thiscall functions, called as fastcall with a dummy EDX
typedef void(__fastcall* ChainRead)(Reader* r, void*, void* buf, uint32_t size);
typedef void(__fastcall* ChainWait)(Reader* r, void*);
typedef uint8_t(__fastcall* ChainBusy)(Reader* r, void*);
typedef uint32_t(__fastcall* ChainSize)(Reader* r, void*);
typedef void(__fastcall* ResizeEntries)(void* vec, void*, uint32_t n, const Entry* fill);
typedef void(__fastcall* ResizePtrs)(void* vec, void*, uint32_t n, void* const* fill);
typedef void(__fastcall* ResizeBytes)(void* vec, void*, uint32_t n, const uint8_t* fill);

const FileSeek file_seek = (FileSeek)0x00857490;
const FileRead file_read = (FileRead)0x008573B0;
const FileClose file_close = (FileClose)0x008575D0;
const SectorSize sector_size = (SectorSize)0x00857620;
const HeaderVersion header_version = (HeaderVersion)0x008FB0E0;
const GameMalloc game_malloc = (GameMalloc)0x00C2905C;
const GameFree game_free = (GameFree)0x00C28F7F;
const ChainRead chain_read = (ChainRead)0x008FB260;
const ChainWait chain_wait = (ChainWait)0x008FB2A0;
const ChainBusy chain_busy = (ChainBusy)0x008FB2D0;
const ChainSize chain_size = (ChainSize)0x008FB230;
const ResizeEntries resize_entries = (ResizeEntries)0x008FC760;
const ResizePtrs resize_ptrs = (ResizePtrs)0x008FC7A0;
const ResizeBytes resize_bytes = (ResizeBytes)0x008FC7E0;

// The async open shares one header buffer: a byte vector of 0x4800 bytes at
// 0x01455FC4 (size at 0x0145A7C4); 0x01455FC0 is the reader using it.
Reader** const g_busy = (Reader**)0x01455FC0;
uint8_t* const g_hdr = (uint8_t*)0x01455FC4;
uint32_t* const g_hdr_size = (uint32_t*)0x0145A7C4;
const uint32_t kHdrCapacity = 0x4800;

const uint32_t kCHNK = 0x4B4E4843;

// ---- side table ------------------------------------------------------------
struct Ext {
    Reader* reader;
    uint32_t cap;  // entries kInline..kInline+cap-1
    Entry* entries;
    void** ptrs;
};
const int kMaxExt = 64;
Ext g_ext[kMaxExt];
volatile LONG g_ext_count = 0;  // used slots; 0 = fast path everywhere
SRWLOCK g_lock = SRWLOCK_INIT;
uint32_t g_max_entries = 16384;
volatile LONG g_warned_overflow = 0;

// Header buffer for big async opens (more than kInline entries).
uint8_t* g_big_hdr = nullptr;
uint32_t g_big_hdr_size = 0;
Reader* g_big_hdr_owner = nullptr;

Ext* ext_find_locked(Reader* r)
{
    for (int i = 0; i < kMaxExt; i++)
        if (g_ext[i].reader == r) return &g_ext[i];
    return nullptr;
}

// Entry i of r. Indices past the side table fall back to the vanilla (inline,
// overflowing) address, which is what the game did before.
Entry* E(Reader* r, uint32_t i)
{
    if (i < kInline || g_ext_count == 0) return &r->entries[i];
    Entry* e = nullptr;
    AcquireSRWLockShared(&g_lock);
    Ext* x = ext_find_locked(r);
    if (x && i - kInline < x->cap) e = &x->entries[i - kInline];
    ReleaseSRWLockShared(&g_lock);
    if (e) return e;
    if (InterlockedExchange(&g_warned_overflow, 1) == 0)
        dsf_log("archive: reader %p entry %u has no side table (count %u); vanilla address used", r, i, r->count);
    return &r->entries[i];
}

void** P(Reader* r, uint32_t i)
{
    if (i < kInline || g_ext_count == 0) return &r->ptrs[i];
    void** p = nullptr;
    AcquireSRWLockShared(&g_lock);
    Ext* x = ext_find_locked(r);
    if (x && i - kInline < x->cap) p = &x->ptrs[i - kInline];
    ReleaseSRWLockShared(&g_lock);
    if (p) return p;
    if (InterlockedExchange(&g_warned_overflow, 1) == 0)
        dsf_log("archive: reader %p pointer %u has no side table (count %u); vanilla address used", r, i, r->count);
    return &r->ptrs[i];
}

// Makes room for count entries (count > kInline). Pointers of indices at or
// above old_ptr_count are zeroed, as the vanilla pointer-vector resize does.
bool ext_ensure(Reader* r, uint32_t count, uint32_t old_ptr_count)
{
    uint32_t need = count - kInline;
    AcquireSRWLockExclusive(&g_lock);
    Ext* x = ext_find_locked(r);
    if (!x) {
        x = ext_find_locked(nullptr);
        if (!x) {
            ReleaseSRWLockExclusive(&g_lock);
            dsf_log("archive: side table full (%d readers with more than %u entries)", kMaxExt, kInline);
            return false;
        }
        x->reader = r;
        x->cap = 0;
        x->entries = nullptr;
        x->ptrs = nullptr;
        InterlockedIncrement(&g_ext_count);
    }
    if (x->cap < need) {
        Entry* e = (Entry*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, need * sizeof(Entry));
        void** p = (void**)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, need * sizeof(void*));
        if (!e || !p) {
            if (e) HeapFree(GetProcessHeap(), 0, e);
            if (p) HeapFree(GetProcessHeap(), 0, p);
            ReleaseSRWLockExclusive(&g_lock);
            dsf_log("archive: out of memory for %u entries", count);
            return false;
        }
        if (x->cap) {
            memcpy(e, x->entries, x->cap * sizeof(Entry));
            memcpy(p, x->ptrs, x->cap * sizeof(void*));
            HeapFree(GetProcessHeap(), 0, x->entries);
            HeapFree(GetProcessHeap(), 0, x->ptrs);
        }
        x->entries = e;
        x->ptrs = p;
        x->cap = need;
    }
    uint32_t from = old_ptr_count > kInline ? old_ptr_count - kInline : 0;
    for (uint32_t j = from; j < need; j++) x->ptrs[j] = nullptr;
    ReleaseSRWLockExclusive(&g_lock);
    return true;
}

void ext_drop(Reader* r)
{
    if (g_ext_count == 0) return;
    AcquireSRWLockExclusive(&g_lock);
    Ext* x = ext_find_locked(r);
    if (x) {
        HeapFree(GetProcessHeap(), 0, x->entries);
        HeapFree(GetProcessHeap(), 0, x->ptrs);
        x->reader = nullptr;
        x->cap = 0;
        x->entries = nullptr;
        x->ptrs = nullptr;
        InterlockedDecrement(&g_ext_count);
    }
    ReleaseSRWLockExclusive(&g_lock);
}

// Copies count records into r (inline part, then the side table).
void put_entries(Reader* r, const Entry* src, uint32_t count)
{
    uint32_t n = count < kInline ? count : kInline;
    memcpy(r->entries, src, n * sizeof(Entry));
    if (count > kInline) {
        AcquireSRWLockShared(&g_lock);
        Ext* x = ext_find_locked(r);
        if (x) memcpy(x->entries, src + kInline, (count - kInline) * sizeof(Entry));
        ReleaseSRWLockShared(&g_lock);
    }
}

// Driver.exe return addresses found on the stack above `from`, for the log.
void stack_callers(const void* from, char* out, size_t out_size)
{
    const uint32_t* sp = (const uint32_t*)from;
    size_t o = 0;
    int found = 0;
    out[0] = 0;
    for (int i = 0; i < 96 && found < 8; i++) {
        uint32_t v = sp[i];
        if (v < 0x00401000 || v >= 0x00DC9000) continue;
        const uint8_t* ret = (const uint8_t*)v;
        // A return address follows a call: E8 rel32, or FF /2 (2, 3 or 6 bytes).
        if (ret[-5] != 0xE8 && ret[-2] != 0xFF && ret[-3] != 0xFF && ret[-6] != 0xFF) continue;
        int n = _snprintf_s(out + o, out_size - o, _TRUNCATE, "%s%08X", found ? " " : "", v);
        if (n < 0) break;
        o += n;
        found++;
    }
}

void log_big(Reader* r, uint32_t count, const char* how, const void* stack)
{
    static volatile LONG logged = 0;
    if (InterlockedIncrement(&logged) <= 32) {
        char callers[128];
        stack_callers(stack, callers, sizeof callers);
        dsf_log("archive: reader %p %s CHNK with %u entries (%u in the side table); callers %s", r, how, count,
                count - kInline, callers);
    }
}

uint32_t align_up(uint32_t v, uint32_t a) { return (v + a - 1) & ~(a - 1); }

// Payload plus name, aligned to the entry alignment (vanilla arithmetic,
// including the 8-bit wrap of name_len + 3).
uint32_t entry_span(const Entry* e)
{
    uint32_t a = 1u << (e->align_log2 & 31);
    uint32_t name = (uint8_t)(e->name_len + 3) & 0xFC;
    return align_up(name + e->size, a);
}

// ---- car packages ([archive] packages, off by default) ----------------------
// research/car-packages.md. A car package is a CHNK file of its own holding one
// car's three VEHC entries (kinds 0, 1, 2: full, interior, exterior). A list
// file names the packages and their IDs, one "id path" per line (path relative
// to the game folder). The model ID inside the file is whatever its builder
// gave it; the plugin writes the listed one into the full VEHC as it serves the
// read (pkg_set_ids), and into the SSLP and VSCT records it adds.
//
// How the game finds a car (confirmed-static): the vehicle spool object's root
// reader (spool object +0x14) holds dngvehicles.sp's root table; the SSLP
// records {u16 id, u16 root index, u16 kind} map a model ID to a root index
// (0x005A0A80, 0x005A0B80, 0x005A0BD0 read them through the pointers at spool
// object +0x72F8/+0x72FC); a slot handler then reads the whole entry with the
// root reader's read entry(i) (SceneModelType2 0x00858870 -> 0x008FBE90) into
// the shared spool buffer and parses it in memory. So the packages need four
// things here, all in code this module already owns:
//   1. the root table of the vehicle archive gets three entries per package
//      after its own (h_poll, once the open completes);
//   2. read entry(i) and name(i) of those entries read from the package file
//      (with the plugin's own handle; for the slot's load on a thread-pool
//      thread, the poll busy until it is done) instead of the archive;
//   3. the SSLP the spool object uses gets three records per package (a copy
//      with the records appended; FUN_005A0B50 sets the two pointers);
//   4. the VSCT table gets the package's three records (FUN_0086C630 loads it
//      into [Engine+0x780]+0x2C; FUN_0086C5B0 looks a model ID up in it for
//      11 callers, so it is used at run time, not only by the stripped VEdit).
// A package's fourth entry, VSCT, holds its three 28-byte VSCT records (the
// host's, as clone_vehicle.register_clones makes them); the plugin writes the
// package's ID into them. It is required: two callers of FUN_0086C5B0
// (0x0080942B, 0x00890949) use the lookup's result without a NULL check.
// The vehicle archive's reader is the one FUN_005A11E0 opens (call 0x005A13C4)
// with a file name ending in dngvehicles.sp.
struct Pkg {
    HANDLE file;
    HANDLE raw;       // [archive] read_uncached: the same file without the system cache (0: none)
    uint16_t id;
    bool listed;      // its SSLP records are in (no ID clash, NONR fits)
    bool ids_logged;  // pkg_set_ids has logged this package's fields once
    Entry e[3];       // offsets relative to the package file
    uint32_t nonr[3];  // NONR size of each VEHC (the slot's buffer must hold it)
    uint8_t vsct[3][28];
    char path[MAX_PATH];
};
Pkg* g_pkgs = nullptr;
uint32_t g_pkg_count = 0;
Reader* g_veh_reader = nullptr;  // the vehicle archive's root reader
uint32_t g_pkg_first = 0;        // root index of the first package entry (0: none injected)
uint8_t* g_sslp = nullptr;       // the extended SSLP (header + records)
uint8_t* g_vsct = nullptr;       // the extended VSCT
// What register.lua asks through mod_garage's magic query (archive_packages_active,
// archive_package_loaded): the package sites are hooked, an injection or SSLP copy
// failed (no package reaches the game then), the vehicle SSLP has been extended.
bool g_pkg_sites = false;
bool g_pkg_failed = false;
bool g_sslp_done = false;
volatile LONG g_pkg_reads = 0;
uint32_t g_pkg_read_max_us = 0;  // slowest package read so far

// ---- read timing ([archive] read_log, read_uncached; off by default) --------
// read_log logs every package read with its thread, and the main loop's frame
// times (h_pump, below), so a read's cost on the frame can be seen.
// read_uncached reads package entries through a second handle opened with
// FILE_FLAG_NO_BUFFERING: every read then comes from the disk, as a first read
// after a reboot does, whatever the system cache holds (a test run copies the
// packages into its folder just before the game starts, so they are cached).
bool g_read_log = false;
bool g_read_uncached = false;
LARGE_INTEGER g_qpf = {};
LARGE_INTEGER g_qpc0 = {};
volatile DWORD g_main_tid = 0;    // the thread of the game window's message pump (h_pump)
volatile LONG g_frame_reads = 0;  // package reads since the last pump call
volatile LONG g_frame_read_us = 0;
volatile LONG g_frame_main_us = 0;  // of those, on the main thread
SRWLOCK g_bounce_lock = SRWLOCK_INIT;
uint8_t* g_bounce = nullptr;  // page-aligned buffer for the unbuffered reads
uint32_t g_bounce_size = 0;

uint32_t qpc_us(const LARGE_INTEGER& a, const LARGE_INTEGER& b)
{
    return g_qpf.QuadPart ? (uint32_t)((b.QuadPart - a.QuadPart) * 1000000 / g_qpf.QuadPart) : 0;
}

void* __fastcall h_ptr(Reader* r, void*, uint32_t i);

const uint32_t kVEHC = 0x43484556;
const uint32_t kNONR = 0x524E4F4E;
const uint32_t kVSCT = 0x54435356;

bool pread(HANDLE h, uint32_t pos, void* buf, uint32_t n, uint32_t* got)
{
    OVERLAPPED o = {};
    o.Offset = pos;
    DWORD read = 0;
    BOOL ok = ReadFile(h, buf, n, &read, &o);
    if (got) *got = read;
    return ok || GetLastError() == ERROR_HANDLE_EOF;
}

// The package of root entry i of r, and the entry's kind; nullptr when i isn't one.
Pkg* pkg_of(Reader* r, uint32_t i, uint32_t* kind)
{
    if (!g_pkg_first || r != g_veh_reader || i < g_pkg_first) return nullptr;
    uint32_t k = i - g_pkg_first;
    if (k / 3 >= g_pkg_count) return nullptr;
    if (kind) *kind = k % 3;
    return &g_pkgs[k / 3];
}

// Reads one package and checks it: a CHNK (version 3, not compressed) of four
// root entries, three VEHC (kinds 0, 1, 2) and a VSCT of three 28-byte records,
// each inside the file; each VEHC a CHNK whose NONR size is noted (the slot
// copies it into a fixed buffer). The VSCT is required: FUN_0086C5B0 looks a
// model ID up in it, and two of its callers (0x0080942B, 0x00890949) use the
// result without a NULL check, so a car without records would crash there.
// Logs why a package is refused.
bool pkg_open(Pkg* p)
{
    p->file = CreateFileA(p->path, GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL,
                          nullptr);
    if (p->file == INVALID_HANDLE_VALUE) {
        dsf_log("packages: %s: can't open (error %lu); left out", p->path, GetLastError());
        return false;
    }
    LARGE_INTEGER fsize = {};
    if (!GetFileSizeEx(p->file, &fsize) || fsize.QuadPart > 0xFFFFFFFFLL) {
        dsf_log("packages: %s: no size, or larger than 4 GiB; left out", p->path);
        return false;
    }
    uint64_t file_size = (uint64_t)fsize.QuadPart;
    uint32_t hdr[4];
    uint32_t got = 0;
    if (!pread(p->file, 0, hdr, sizeof hdr, &got) || got != sizeof hdr || hdr[0] != kCHNK || hdr[2] != 4 ||
        (hdr[3] & 0x7FFFFFFF) != 3 || (hdr[3] >> 31)) {
        dsf_log("packages: %s: not a CHNK of four entries (version 3, not compressed); left out", p->path);
        return false;
    }
    Entry all[4];
    if (!pread(p->file, 16, all, sizeof all, &got) || got != sizeof all) {
        dsf_log("packages: %s: entry table cut short; left out", p->path);
        return false;
    }
    for (int k = 0; k < 4; k++) {
        if ((uint64_t)all[k].offset + entry_span(&all[k]) > file_size) {
            dsf_log("packages: %s: entry %d (%#x + %#x) ends past the file (%#llx bytes); left out", p->path, k,
                    all[k].offset, entry_span(&all[k]), (unsigned long long)file_size);
            return false;
        }
    }
    memcpy(p->e, all, sizeof p->e);
    if (all[3].tag != kVSCT || all[3].size != sizeof p->vsct ||
        !pread(p->file, all[3].offset, p->vsct, sizeof p->vsct, &got) || got != sizeof p->vsct) {
        dsf_log("packages: %s: entry 3 is not a VSCT of three 28-byte records; left out", p->path);
        return false;
    }
    for (int k = 0; k < 3; k++) {
        Entry* e = &p->e[k];
        if (e->tag != kVEHC) {
            dsf_log("packages: %s: entry %d is not a VEHC; left out", p->path, k);
            return false;
        }
        // A VEHC is a CHNK; its NONR (if any) is copied into a fixed slot buffer.
        uint32_t sub[4];
        if (!pread(p->file, e->offset, sub, sizeof sub, &got) || got != sizeof sub || sub[0] != kCHNK ||
            16 + (uint64_t)sub[2] * sizeof(Entry) > e->size) {
            dsf_log("packages: %s: VEHC %d is not a CHNK; left out", p->path, k);
            return false;
        }
        uint32_t n = sub[2] * (uint32_t)sizeof(Entry);
        Entry* kids = (Entry*)HeapAlloc(GetProcessHeap(), 0, n ? n : 1);
        bool ok = kids && (n == 0 || (pread(p->file, e->offset + 16, kids, n, &got) && got == n));
        p->nonr[k] = 0;
        for (uint32_t j = 0; ok && j < sub[2]; j++) {
            if ((uint64_t)kids[j].offset + kids[j].size > e->size) ok = false;
            if (kids[j].tag == kNONR) p->nonr[k] = kids[j].size;
        }
        if (kids) HeapFree(GetProcessHeap(), 0, kids);
        if (!ok) {
            dsf_log("packages: %s: VEHC %d: entry table cut short, or an entry past its end; left out", p->path, k);
            return false;
        }
    }
    if (g_read_uncached) {
        p->raw = CreateFileA(p->path, GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                             FILE_ATTRIBUTE_NORMAL | FILE_FLAG_NO_BUFFERING, nullptr);
        if (p->raw == INVALID_HANDLE_VALUE) {
            dsf_log("packages: %s: no unbuffered handle (error %lu); its reads are cached", p->path, GetLastError());
            p->raw = nullptr;
        }
    }
    return true;
}

// Reads a list file (one "id path" per line, # comments; paths relative to
// base, the game folder with a trailing backslash) and opens every package in
// it. A line whose ID is out of range or already listed, or whose package fails
// pkg_open, is left out (logged). Returns the number of packages kept.
uint32_t pkg_load_list(const char* base, const char* list)
{
    FILE* f = nullptr;
    if (fopen_s(&f, list, "r") || !f) {
        dsf_log("packages: no list %s; no car packages", list);
        return 0;
    }
    const uint32_t kMaxPkgs = 999;  // one per base model ID (1..999)
    if (!g_pkgs) g_pkgs = (Pkg*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, kMaxPkgs * sizeof(Pkg));
    char line[512];
    int lineno = 0;
    while (g_pkgs && fgets(line, sizeof line, f)) {
        lineno++;
        char* s = line;
        while (*s == ' ' || *s == '\t') s++;
        if (*s == '#' || *s == '\r' || *s == '\n' || !*s) continue;
        char* end = nullptr;
        long id = strtol(s, &end, 10);
        if (end == s) id = 0;
        while (*end == ' ' || *end == '\t') end++;
        size_t len = strcspn(end, "\r\n");
        end[len] = 0;
        while (len && (end[len - 1] == ' ' || end[len - 1] == '\t')) end[--len] = 0;
        if (id < 1 || id > 999 || !*end) {
            dsf_log("packages: %s line %d: '%s' is not \"id path\" with 1 <= id <= 999; left out", list, lineno, s);
            continue;
        }
        bool dup = false;
        for (uint32_t j = 0; j < g_pkg_count && !dup; j++) dup = g_pkgs[j].id == id;
        if (dup) {
            dsf_log("packages: %s line %d: ID %ld is listed twice; %s left out", list, lineno, id, end);
            continue;
        }
        if (g_pkg_count >= kMaxPkgs) {
            dsf_log("packages: more than %u packages; %s left out", kMaxPkgs, end);
            continue;
        }
        Pkg* p = &g_pkgs[g_pkg_count];
        memset(p, 0, sizeof *p);
        p->id = (uint16_t)id;
        _snprintf_s(p->path, sizeof p->path, _TRUNCATE, "%s%s", base, end);
        if (!pkg_open(p)) {
            if (p->file && p->file != INVALID_HANDLE_VALUE) CloseHandle(p->file);
            if (p->raw) CloseHandle(p->raw);
            memset(p, 0, sizeof *p);
            continue;
        }
        dsf_log("packages: %u %s: VEHC %u/%u/%u bytes, NONR %#x/%#x/%#x", p->id, end, p->e[0].size, p->e[1].size,
                p->e[2].size, p->nonr[0], p->nonr[1], p->nonr[2]);
        g_pkg_count++;
    }
    fclose(f);
    dsf_log("packages: %u car packages from %s", g_pkg_count, list);
    return g_pkg_count;
}

// The list [archive] packages_list names, relative to the game folder.
void pkg_load_config()
{
    char base[MAX_PATH];
    DWORD n = GetModuleFileNameA(nullptr, base, MAX_PATH);
    if (!n || n >= MAX_PATH) return;
    char* slash = strrchr(base, '\\');
    if (slash) slash[1] = 0;
    char rel[MAX_PATH], list[MAX_PATH];
    cfg_str("archive", "packages_list", "dsfcars\\cars\\cars.txt", rel, sizeof rel);
    _snprintf_s(list, sizeof list, _TRUNCATE, "%s%s", base, rel);
    pkg_load_list(base, list);
}

// An unbuffered read (read_uncached) of n bytes at pos into buf: the 4 KiB
// pages around it go through the page-aligned bounce buffer, as
// FILE_FLAG_NO_BUFFERING needs aligned offsets, sizes and buffers (4 KiB is a
// multiple of every sector size Windows supports). Returns the bytes of
// [pos, pos + n) the file holds.
bool pread_raw(HANDLE h, uint32_t pos, void* buf, uint32_t n, uint32_t* got)
{
    const uint32_t kPage = 0x1000;
    uint32_t start = pos & ~(kPage - 1);
    uint32_t span = (pos - start + n + kPage - 1) & ~(kPage - 1);
    AcquireSRWLockExclusive(&g_bounce_lock);
    if (g_bounce_size < span) {
        if (g_bounce) VirtualFree(g_bounce, 0, MEM_RELEASE);
        g_bounce = (uint8_t*)VirtualAlloc(nullptr, span, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        g_bounce_size = g_bounce ? span : 0;
    }
    uint32_t read = 0;
    bool ok = g_bounce && pread(h, start, g_bounce, span, &read);
    uint32_t have = read > pos - start ? read - (pos - start) : 0;
    if (have > n) have = n;
    if (ok) memcpy(buf, g_bounce + (pos - start), have);
    ReleaseSRWLockExclusive(&g_bounce_lock);
    if (got) *got = have;
    return ok;
}

// Reads package entry e of p (offset relative to the package file) into buf:
// size bytes, the caller's sector-aligned read size; what the file doesn't
// hold is zeroed. Synchronous, on the plugin's own handle; timed for the log,
// with the thread it runs on (read_log: every read). Returns bytes read.
uint32_t pkg_read(Pkg* p, const Entry* e, void* buf, uint32_t size)
{
    LARGE_INTEGER t0, t1;
    QueryPerformanceCounter(&t0);
    uint32_t got = 0;
    bool ok = p->raw ? pread_raw(p->raw, e->offset, buf, size, &got) : pread(p->file, e->offset, buf, size, &got);
    DWORD err = ok ? 0 : GetLastError();
    if (!ok || got < entry_span(e))
        dsf_log("packages: %u: read at %#x came short: %u of %u bytes, entry %u (error %lu)", p->id, e->offset, got,
                size, entry_span(e), err);
    if (got < size) memset((uint8_t*)buf + got, 0, size - got);
    QueryPerformanceCounter(&t1);
    uint32_t us = qpc_us(t0, t1);
    if (us > g_pkg_read_max_us) g_pkg_read_max_us = us;
    LONG n = InterlockedIncrement(&g_pkg_reads);
    DWORD tid = GetCurrentThreadId();
    bool main = tid == g_main_tid;
    InterlockedIncrement(&g_frame_reads);
    InterlockedExchangeAdd(&g_frame_read_us, (LONG)us);
    if (main) InterlockedExchangeAdd(&g_frame_main_us, (LONG)us);
    if (g_read_log)
        dsf_log("pkg_read: id %u, %u bytes at %#x, %u.%03u ms, thread %lu (%s), at %u ms%s (read %ld)", p->id, got,
                e->offset, us / 1000, us % 1000, tid, main ? "main" : "not main", qpc_us(g_qpc0, t0) / 1000,
                p->raw ? ", unbuffered" : "", n);
    else if (n <= 64 || us > 50000)
        dsf_log("packages: %u: %u bytes at %#x read from its package in %u.%03u ms (read %ld, slowest %u.%03u ms)",
                p->id, got, e->offset, us / 1000, us % 1000, n, g_pkg_read_max_us / 1000, g_pkg_read_max_us % 1000);
    return got;
}

// The full VEHC's model-ID fields, a u32 each (clone_vehicle.ID_FIELDS, the four
// places a vanilla car's kind 0 entry holds its ID; research/car-packages.md,
// "Model ID in memory"): VSVH +0 (sound header), VO3D +0 (handling), SPID +0
// (shader params), UPVH +0x14 (hierarchy). The interior and exterior hold none.
constexpr uint32_t tag4(char a, char b, char c, char d)
{
    return (uint32_t)(uint8_t)a | (uint32_t)(uint8_t)b << 8 | (uint32_t)(uint8_t)c << 16 | (uint32_t)(uint8_t)d << 24;
}
struct IdField {
    uint32_t tag;
    uint32_t at;
    const char* name;
};
const IdField kIdFields[] = {
    {tag4('V', 'S', 'V', 'H'), 0x00, "VSVH"},
    {tag4('V', 'O', '3', 'D'), 0x00, "VO3D"},
    {tag4('S', 'P', 'I', 'D'), 0x00, "SPID"},
    {tag4('U', 'P', 'V', 'H'), 0x14, "UPVH"},
};
const int kIdFieldCount = (int)(sizeof kIdFields / sizeof kIdFields[0]);

struct IdScan {
    uint32_t at[8];       // buffer offsets of the fields written (the first 8)
    int field[8];         // their kIdFields index
    uint32_t count;       // fields written
    uint32_t old_id;      // the first field's value before
    bool mixed;           // the fields held different values
};

// Writes id into every model-ID field of the CHNK at buf + base (size bytes)
// and of the CHNKs nested in it. A child whose payload starts with "CHNK" (16
// bytes or more) is a nested CHNK, as dsfforza.formats.chnk reads them; the
// others are leaves. Offsets are relative to the CHNK they are in; every table
// and child is checked against the bytes it lies in.
void set_ids_in(uint8_t* buf, uint32_t base, uint32_t size, uint16_t id, IdScan* s, int depth)
{
    if (depth > 16 || size < 16 || *(uint32_t*)(buf + base) != kCHNK) return;
    uint32_t n = *(uint32_t*)(buf + base + 8);
    if (16 + (uint64_t)n * sizeof(Entry) > size) return;
    const Entry* kids = (const Entry*)(buf + base + 16);
    for (uint32_t j = 0; j < n; j++) {
        const Entry* k = &kids[j];
        if ((uint64_t)k->offset + k->size > size) continue;
        uint32_t at = base + k->offset;
        if (k->size >= 16 && *(uint32_t*)(buf + at) == kCHNK) {
            set_ids_in(buf, at, k->size, id, s, depth + 1);
            continue;
        }
        for (int f = 0; f < kIdFieldCount; f++) {
            if (k->tag != kIdFields[f].tag || kIdFields[f].at + 4 > k->size) continue;
            uint32_t* v = (uint32_t*)(buf + at + kIdFields[f].at);
            if (s->count == 0)
                s->old_id = *v;
            else if (*v != s->old_id)
                s->mixed = true;
            if (s->count < 8) {
                s->at[s->count] = at + kIdFields[f].at;
                s->field[s->count] = f;
            }
            s->count++;
            *v = id;
        }
    }
}

// After a package's entry is read into buf (size bytes, the entry at its
// start): the listed ID goes into the full VEHC's model-ID fields, whatever ID
// the file holds (its builder's; car.json's car.model_id). The game parses the
// buffer only after read entry returns (the slot's poll, state 3), so it sees
// the listed ID only. A file with the listed ID in it already (an install
// that patched it) is unchanged. Returns the number of fields written.
uint32_t pkg_set_ids(Pkg* p, uint32_t kind, uint8_t* buf, uint32_t size)
{
    if (kind != 0) return 0;
    IdScan s = {};
    set_ids_in(buf, 0, size, p->id, &s, 0);
    bool odd = s.count != (uint32_t)kIdFieldCount || s.mixed;
    if (!p->ids_logged || odd) {
        p->ids_logged = true;
        char where[160];
        size_t o = 0;
        where[0] = 0;
        for (uint32_t j = 0; j < s.count && j < 8; j++) {
            int n = _snprintf_s(where + o, sizeof where - o, _TRUNCATE, "%s%s+%#x", j ? " " : "",
                                kIdFields[s.field[j]].name, s.at[j]);
            if (n < 0) break;
            o += n;
        }
        if (!s.count)
            dsf_log("packages: %u: VEHC 0 read whole (%u bytes); no model-ID field found, nothing written", p->id,
                    size);
        else
            dsf_log("packages: %u: VEHC 0 read whole (%u bytes); model ID %u -> %u in %u fields (%s)%s", p->id, size,
                    s.old_id, p->id, s.count, where,
                    odd ? (s.mixed ? "; the fields held different IDs" : "; expected VSVH, VO3D, SPID and UPVH once")
                        : "");
    }
    return s.count;
}

// After the vehicle archive's root table is read: three entries per package
// after the archive's own. Their offsets stay relative to the package file;
// h_read_entry and h_name read them from there.
void pkg_inject(Reader* r)
{
    if (!g_pkg_count) return;
    uint32_t base = r->count;
    uint32_t count = base + 3 * g_pkg_count;
    if (count > g_max_entries || (count > kInline && !ext_ensure(r, count, r->ptr_count))) {
        dsf_log("packages: %u root entries would pass max_entries %u; no car packages", count, g_max_entries);
        g_pkg_failed = true;
        return;
    }
    for (uint32_t j = r->ptr_count; j < kInline && j < count; j++) r->ptrs[j] = nullptr;
    r->count = count;
    r->ptr_count = count;
    for (uint32_t j = 0; j < g_pkg_count; j++)
        for (uint32_t k = 0; k < 3; k++) {
            *E(r, base + 3 * j + k) = g_pkgs[j].e[k];
            *P(r, base + 3 * j + k) = nullptr;
        }
    g_pkg_first = base;
    dsf_log("packages: reader %p: root entries %u..%u are the %u car packages", r, base, count - 1, g_pkg_count);
}

// ---- vehicle spool slots ([archive] vehicle_slots) ---------------------------
// research/traffic.md, "The spool slot limit". Every vehicle model the game holds
// at once (the traffic set's 15 to 18, the interesting vehicles, the mission pots,
// the player's car) takes one kind-0 slot of the vehicle spool object. The count
// is data: SSLP header byte +0x18 + kind (35, 2, 2, 0 in the vanilla archive).
// FUN_005A1420 makes one slot handler per slot at package init, and
// TrafficSpooler.GetMaxSpooledVehicles (0x005A0AD0) returns the byte, which caps
// mission and interesting-vehicle requests (0x005A6FD0: refused at traffic +
// mission + pending + 1 >= max; the Lua loader then waits forever for the refused
// model, spoolingsystem.lua requestToSpoolVehicle). The traffic set's list is
// loaded without that check (0x005A7490), so a set that leaves no room hangs the
// chapter load. What else is sized by the slot count:
//   - the slot handler arrays: 64 pointers for all kinds (+0x5630, count +0x5730)
//     and 64 per kind (+0x5734 + 0x104 k, count at +0x100), written without a
//     bound; so kind 0 can have at most 64 - (the other kinds' slots) = 60;
//   - seven ID-list node pools, inline in the spool object, 64, 64, 48, 48, 48,
//     48 and 48 nodes of 16 bytes ({next free, free count, used} at +0x5B44,
//     +0x5F5C, +0x6374, +0x668C, +0x69A4, +0x6CBC, +0x6FD4, storage from +0x0C);
//     their allocator 0x007D7270 bumps the next-free pointer with no end check, so
//     a pool that runs out writes into the next one. They hold requested and
//     resident IDs of every kind. They are pointed at buffers of this module's
//     while they are still unused (each pool checked);
//   - graphics pool 9 "Vehicle Spooled" (GraphicsManager_Construct 0x00BAD960 from
//     the .data tables 0x00FD25D0 / 0x00FD2678, read once after DllMain): 35 slots
//     of 8 MiB / 35 SYS, 0x10000 / 35 range nodes and 35 KiB / 35 GPU, split
//     evenly; FUN_00BB5690 clamps a slot index to count - 1, so more spool slots
//     than graphics slots would share the last one (research/spool-limits.md,
//     "Graphics pool slots"). The table is scaled so each slot keeps its vanilla
//     size (patch_vehicle_pool, from init);
//   - one NONR buffer per slot (SSLP +0x08, 0x6D000 = 436 KiB, CRT heap,
//     allocated at package init by the slot's SceneModelType2) and the model's
//     data when it is resident: per slot, nothing shared to grow.
// The traffic spooler's own lists hold 50 traffic and 50 mission models
// (0x005A6D20) and the set selector copies at most 40 IDs (0x0076FDF0's stack
// buffer), so 60 slots leave room for a 40-model set, the largest mission pot
// request (18, spoolingsystem.lua numberOfVehicleAllowed) and the player's car:
// 40 + 17 + 1 pending + 1 < 60, no refusal.
const uint32_t kSlotHandlers = 64;          // +0x5630: slot handlers for all kinds
const uint32_t kVanillaVehicleSlots = 35;   // SSLP +0x18 in the vanilla archive
const uint32_t kDefaultVehicleSlots = 35;   // [archive] vehicle_slots default: no raise until #586 (at 50,
                                            // a provider's 45-node ID map probably overflows)
const uint32_t kPoolBytes = 0x4000;         // per relocated ID pool: 1024 nodes of 16 bytes
const uint32_t kIdPools[] = {0x5B44, 0x5F5C, 0x6374, 0x668C, 0x69A4, 0x6CBC, 0x6FD4};
uint32_t g_vehicle_slots = 0;               // [archive] vehicle_slots; 0 = vanilla, no patch
bool g_slots_done = false;
uint32_t g_slots_applied = 0;             // the kind-0 count apply_vehicle_slots set (0: not yet, or refused)
bool g_ai_copy_served = false;            // the traffic sets' AI copy was opened for the game ("file redirection")
bool g_sslp_hooked = false;               // the slot raise hooked 0x005A0B50 itself (no packages)

// Graphics pool 9's rows: {name, slots} at 0x00FD2618; {flag, SYS KiB, SYS nodes,
// GPU KiB, GPU nodes} at 0x00FD272C. Vanilla: 35; 0, 0x2000, 0x10000, 0x23, 0x400.
const uint32_t kPoolSlotsVa = 0x00FD261C;
const uint32_t kPoolSizesVa = 0x00FD2730;
const uint32_t kPoolSizesVanilla[4] = {0x2000, 0x10000, 0x23, 0x400};

uint32_t scale_up(uint32_t vanilla, uint32_t slots)
{
    return (uint32_t)(((uint64_t)vanilla * slots + kVanillaVehicleSlots - 1) / kVanillaVehicleSlots);
}

// From init (DllMain), before the game builds its graphics pools: graphics pool 9
// gets `slots` slots, each at least the vanilla size (GPU KiB stays a multiple of
// the slot count, which GraphicsManager_Construct asserts).
bool patch_vehicle_pool(uint32_t slots)
{
    const uint32_t vanilla_slots = kVanillaVehicleSlots;
    if (!bytes_equal(kPoolSlotsVa, &vanilla_slots, 4) || !bytes_equal(kPoolSizesVa, kPoolSizesVanilla, 16)) {
        check_bytes(kPoolSlotsVa, &vanilla_slots, 4);
        check_bytes(kPoolSizesVa, kPoolSizesVanilla, 16);
        dsf_log("slots: graphics pool 9 (00FD261C/00FD2730) is not the vanilla table; vehicle slots stay %u",
                kVanillaVehicleSlots);
        return false;
    }
    uint32_t sizes[4] = {scale_up(kPoolSizesVanilla[0], slots), scale_up(kPoolSizesVanilla[1], slots), slots,
                         scale_up(kPoolSizesVanilla[3], slots)};
    if (!patch_bytes(kPoolSizesVa, sizes, 16, kPoolSizesVanilla)) return false;
    if (!patch_bytes(kPoolSlotsVa, &slots, 4, &vanilla_slots)) {
        patch_bytes(kPoolSizesVa, kPoolSizesVanilla, 16);
        return false;
    }
    dsf_log("slots: graphics pool 9 Vehicle Spooled: %u slots, %u KiB SYS, %u nodes, %u KiB GPU, %u GPU nodes "
            "(vanilla 35, 8192 KiB, 65536, 35, 1024)", slots, sizes[0], sizes[1], sizes[2], sizes[3]);
    return true;
}

// ---- a scene-model provider's wrapper list (45 inline) ----------------------
// Every slot handler's scene model takes one wrapper from each resource provider
// at package init (SceneModelType2_InitializeResourceProviders 0x00858CD0 calls
// each provider's vtable +4). The provider class with vtable 0x00E18184
// (constructor 0x006AE790) keeps them in an inline array of 45 pointers at +0x04
// with the count at +0xB8 and no bound: acquire 0x006AE160 appends, release
// 0x006AE1B0 removes (count read through +0x04 as +0xB4), lookup 0x006ADF90 walks
// them (0x006AF970 on each), 0x006AE140 clears. The vanilla 35 + 2 + 2 vehicle
// slots take 39; with 45 the 46th append lands on the count (run
// vm1-20261105-222408: fault reading at 0x006AE19B, ECX 0x2D, from 0x00858E09 <-
// 0x0059FA87 <- 0x005A1496, the 46th slot). These four are replaced by versions
// that keep the list in this module (any length); the inline array keeps the first
// 45 and its count says how many of those there are, for any other reader.
const uint32_t kWrapInline = 45;
struct WrapList {
    uint8_t* owner;
    void** items;
    uint32_t count, cap;
};
WrapList g_wraps[8];  // one per provider object of the class (one in practice)
SRWLOCK g_wrap_lock = SRWLOCK_INIT;

WrapList* wraps_of(uint8_t* self)
{
    for (WrapList& w : g_wraps)
        if (w.owner == self) return &w;
    for (WrapList& w : g_wraps)
        if (!w.owner) {
            w.owner = self;
            return &w;
        }
    return nullptr;
}

void wraps_mirror(uint8_t* self, const WrapList* w)
{
    uint32_t n = w->count < kWrapInline ? w->count : kWrapInline;
    for (uint32_t i = 0; i < n; i++) ((void**)(self + 4))[i] = w->items[i];
    *(uint32_t*)(self + 0xB8) = n;
}

typedef void*(__cdecl* MakeObject)(void** out, uint32_t class_id);   // 0x0086A360
typedef void*(__cdecl* ClassInfo)(uint32_t class_id);                // 0x0086A400
typedef uint32_t(__cdecl* ClassOf)(void* obj);                       // 0x00869E30
typedef void(__cdecl* FreeObject)(uint32_t class_id, void* obj);     // 0x00869DA0
typedef void*(__fastcall* WrapMatch)(void* wrap, void*, uint32_t a, uint32_t b);  // 0x006AF970
typedef void(__fastcall* WrapRelease)(void* wrap, void*, int flag);              // vtable +0x14

void** __fastcall h_wrap_acquire(uint8_t* self, void*, void** out)
{
    void* obj = nullptr;
    ((MakeObject)0x0086A360)(&obj, 0x7C);
    ((ClassInfo)0x0086A400)(0x7C);
    AcquireSRWLockExclusive(&g_wrap_lock);
    WrapList* w = wraps_of(self);
    if (w && w->count == w->cap) {
        uint32_t cap = w->cap ? w->cap * 2 : 64;
        void** items = (void**)HeapAlloc(GetProcessHeap(), 0, cap * sizeof(void*));
        if (items) {
            if (w->items) {
                memcpy(items, w->items, w->count * sizeof(void*));
                HeapFree(GetProcessHeap(), 0, w->items);
            }
            w->items = items;
            w->cap = cap;
        }
    }
    if (w && w->count < w->cap) {
        w->items[w->count++] = obj;
        wraps_mirror(self, w);
        if (w->count == kWrapInline + 1) dsf_log("slots: provider %p holds more than %u wrappers", self, kWrapInline);
    } else {
        dsf_log("slots: provider %p: no room for wrapper %u (out of memory)", self, w ? w->count : 0);
    }
    ReleaseSRWLockExclusive(&g_wrap_lock);
    *out = obj;
    return out;
}

void __fastcall h_wrap_release(uint8_t* self, void*, void** wrap)
{
    void* obj = *wrap;
    AcquireSRWLockExclusive(&g_wrap_lock);
    WrapList* w = wraps_of(self);
    if (w) {
        for (uint32_t i = 0; i < w->count; i++)
            if (w->items[i] == obj) {
                memmove(w->items + i, w->items + i + 1, (w->count - i - 1) * sizeof(void*));
                w->count--;
                break;
            }
        wraps_mirror(self, w);
    }
    ReleaseSRWLockExclusive(&g_wrap_lock);
    uint32_t class_id = ((ClassOf)0x00869E30)(obj);
    WrapRelease release = *(WrapRelease*)(*(uint8_t**)obj + 0x14);
    if (class_id == 0x5B) {
        release(obj, nullptr, 1);
        return;
    }
    release(obj, nullptr, 0);
    ((FreeObject)0x00869DA0)(class_id, obj);
}

void** __fastcall h_wrap_lookup(uint8_t* self, void*, void** out, uint32_t a, uint32_t b)
{
    *out = nullptr;
    AcquireSRWLockShared(&g_wrap_lock);
    WrapList* w = nullptr;
    for (WrapList& x : g_wraps)
        if (x.owner == self) w = &x;
    uint32_t n = w ? w->count : 0;
    ReleaseSRWLockShared(&g_wrap_lock);
    for (uint32_t i = 0; i < n; i++) {
        void* found = ((WrapMatch)0x006AF970)(w->items[i], nullptr, a, b);
        if (found) {
            *out = found;
            break;
        }
    }
    return out;
}

void __fastcall h_wrap_clear(uint8_t* self)
{
    AcquireSRWLockExclusive(&g_wrap_lock);
    for (WrapList& w : g_wraps)
        if (w.owner == self) w.count = 0;
    ReleaseSRWLockExclusive(&g_wrap_lock);
    *(uint32_t*)(self + 0xB8) = 0;
    *(void**)(self + 0xC480) = (void*)0x01010101;
    *(void**)(self + 0xC484) = (void*)0x01010101;
}

// The four functions' first bytes (dsff xref dis).
const uint8_t kWrapAcquireExpect[] = {0x51, 0x56, 0x57, 0x8D, 0x44};             // 0x006AE160
const uint8_t kWrapReleaseExpect[] = {0x8B, 0x81, 0xB8, 0x00, 0x00, 0x00};       // 0x006AE1B0
const uint8_t kWrapLookupExpect[] = {0x8B, 0x81, 0xB8, 0x00, 0x00, 0x00};        // 0x006ADF90
const uint8_t kWrapClearExpect[] = {0xB8, 0x01, 0x01, 0x01, 0x01, 0xC7};         // 0x006AE140: mov eax, 0x01010101

bool patch_wrap_lists()
{
    if (!check_bytes(0x006AE160, kWrapAcquireExpect, 5) || !check_bytes(0x006AE1B0, kWrapReleaseExpect, 6) ||
        !check_bytes(0x006ADF90, kWrapLookupExpect, 6) || !check_bytes(0x006AE140, kWrapClearExpect, 6)) {
        dsf_log("slots: provider 00E18184's functions are not the vanilla code; vehicle slots stay 35");
        return false;
    }
    bool ok = patch_jmp(0x006AE160, (void*)h_wrap_acquire, 5, kWrapAcquireExpect) &&
              patch_jmp(0x006AE1B0, (void*)h_wrap_release, 6, kWrapReleaseExpect) &&
              patch_jmp(0x006ADF90, (void*)h_wrap_lookup, 6, kWrapLookupExpect) &&
              patch_jmp(0x006AE140, (void*)h_wrap_clear, 6, kWrapClearExpect);
    if (!ok) {
        patch_bytes(0x006AE160, kWrapAcquireExpect, 5);
        patch_bytes(0x006AE1B0, kWrapReleaseExpect, 6);
        patch_bytes(0x006ADF90, kWrapLookupExpect, 6);
        patch_bytes(0x006AE140, kWrapClearExpect, 6);
        dsf_log("slots: patching provider 00E18184's functions failed; undone");
        return false;
    }
    dsf_log("slots: provider 00E18184 wrapper list (45 inline) kept in the plugin: 006AE160, 006AE1B0, 006ADF90, "
            "006AE140");
    return true;
}

// ---- the generic providers' wrapper pools (45 nodes) --------------------------
// Two more provider classes (acquire 0x0086BEE0, vtable 0x00E395D4, pool at +0x18,
// class ID at +0x248; acquire 0x0086B710, pool at +0x04, class ID at +0x234) list
// their wrappers through 0x0086B6B0, the only appender of that pool layout: {u32
// next, u32 free count, u32 used} then 45 nodes of 12 bytes {next, prev, item} and
// the list head at +0x228. The next pointer is a bump pointer while the free count
// is 0 and the free list's head otherwise, with no end check; the 46th node lands
// on the list head and the class ID (run vm1-20261105-223116: fault reading at
// 0x0086A2AD with class ID 0x292E8970, from 0x0086BEF7 <- 0x00858E09). The
// replacement appends the same way, but before a bump that would reach the list
// head it moves the bump pointer to a heap chunk of this module's; freed nodes go
// back on the free list wherever they live, as before.
const uint32_t kNode45 = 12;
const uint32_t kChunkNodes = 256;
struct PoolChunk {
    int* pool;
    uint8_t* end;
};
PoolChunk g_pool_chunks[32];
SRWLOCK g_pool_lock = SRWLOCK_INIT;

// Moves a full node pool's bump pointer to a chunk of this module's. pool = {next,
// free count, used}; storage..head is its inline storage (head = the list head);
// what = the log name. Before the bump that would reach `end`. False when the pool is
// full and no chunk could be had: the caller must not append (the node would land on
// the list head, or past the chunk).
bool pool_room(int* pool, uint8_t* storage, uint8_t* head, const char* what)
{
    if (pool[1] != 0) return true;  // next is the free list's head: no bump
    uint8_t* cur = (uint8_t*)(uintptr_t)pool[0];
    bool inline_storage = cur >= storage && cur <= head;
    AcquireSRWLockExclusive(&g_pool_lock);
    PoolChunk* c = nullptr;
    for (PoolChunk& x : g_pool_chunks)
        if (x.pool == pool) c = &x;
    uint8_t* end = inline_storage ? head : (c ? c->end : nullptr);
    if (end && cur + kNode45 > end) {
        uint8_t* chunk = (uint8_t*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, kChunkNodes * kNode45);
        if (!c)
            for (PoolChunk& x : g_pool_chunks)
                if (!x.pool) {
                    c = &x;
                    c->pool = pool;
                    break;
                }
        if (chunk && c) {
            pool[0] = (int)(uintptr_t)chunk;
            c->end = chunk + kChunkNodes * kNode45;
            if (inline_storage)
                dsf_log("slots: %s %p passes its %u inline nodes; it goes on in the plugin's chunks", what, pool,
                        (uint32_t)((head - storage) / kNode45));
        } else {
            dsf_log("slots: %s %p: no chunk for node %d; the node is not added", what, pool, pool[2] + 1);
            ReleaseSRWLockExclusive(&g_pool_lock);
            return false;
        }
    }
    ReleaseSRWLockExclusive(&g_pool_lock);
    return true;
}

// The append of both pools: {next, prev, value} at the front of the list at head.
void pool_append(int* pool, int* head, int value)
{
    int* node = (int*)(uintptr_t)pool[0];
    if (pool[1] == 0) {
        node[2] = value;
        pool[0] += kNode45;
    } else {
        int next = node[0];
        node[2] = value;
        pool[1]--;
        pool[0] = next;
    }
    pool[2]++;
    node[0] = *head;
    node[1] = (int)(uintptr_t)head;
    ((int*)(uintptr_t)*head)[1] = (int)(uintptr_t)node;
    *head = (int)(uintptr_t)node;
}

void __fastcall h_pool45_append(int* pool, void*, int* item)
{
    if (!pool_room(pool, (uint8_t*)(pool + 3), (uint8_t*)(pool + 0x8A), "provider pool")) return;
    pool_append(pool, pool + 0x8A, *item);
}


const uint8_t kPool45AppendExpect[] = {0x83, 0x79, 0x04, 0x00, 0x8B, 0x01};  // 0x0086B6B0: cmp [ecx+4], 0; mov eax, [ecx]

bool patch_pool45()
{
    if (!check_bytes(0x0086B6B0, kPool45AppendExpect, 6)) {
        dsf_log("slots: 0086B6B0 is not the vanilla pool append; vehicle slots stay 35");
        return false;
    }
    if (!patch_jmp(0x0086B6B0, (void*)h_pool45_append, 6, kPool45AppendExpect)) return false;
    dsf_log("slots: 0086B6B0 provider pools (45 nodes) go on in the plugin's chunks when full");
    return true;
}

// A third provider class (acquire 0x0048F7D0, class 0x7A) has the same pool at +0x04
// but 46 nodes (storage from +0x10, the list head at +0x238) and appends inline rather
// than through 0x0086B6B0. One node is taken before the vehicle slots, so the 46th
// wrapper of the vehicle slots lands on the list head: with 45 slots the first kind-1
// slot (run vm1-20261107-211718, slots_trace: its bump pointer reaches +0x238 at the 45th
// kind-0 slot; then the fault at 0x0084BF49 of #566). The replacement is the function
// with the append through pool_room.
void** __fastcall h_acquire_7a(uint8_t* self, void*, void** out)
{
    void* obj = nullptr;
    ((MakeObject)0x0086A360)(&obj, 0x7A);
    ((ClassInfo)0x0086A400)(0x7A);
    *out = obj;
    int* pool = (int*)(self + 4);
    if (pool_room(pool, self + 0x10, self + 0x238, "provider pool (class 7A)"))
        pool_append(pool, (int*)(self + 0x238), (int)(uintptr_t)obj);
    return out;
}

// 0x0048F7D0: push ecx; push esi; push edi; lea eax, [esp+8]; push 0x7A
const uint8_t kAcquire7aExpect[] = {0x51, 0x56, 0x57, 0x8D, 0x44, 0x24, 0x08, 0x6A, 0x7A};

bool patch_acquire_7a()
{
    if (!check_bytes(0x0048F7D0, kAcquire7aExpect, 9)) {
        dsf_log("slots: 0048F7D0 is not the vanilla class-7A acquire; vehicle slots stay 35");
        return false;
    }
    if (!patch_jmp(0x0048F7D0, (void*)h_acquire_7a, 5, kAcquire7aExpect)) return false;
    dsf_log("slots: 0048F7D0 provider pool (class 7A, 46 nodes) goes on in the plugin's chunks when full");
    return true;
}

// ---- Wwise's default pool (AkInitSettings.uDefaultPoolSize) ---------------------------
// Each loaded bank's structures and the sound objects live in Wwise's default pool. The
// game sizes it at its sound init (FUN_0084A3B0: `mov [ebp+0x40], 0x599999` at 0x0084A64D,
// AkInitSettings +0x0C; the lower engine's 0x300000 is AkPlatformInitSettings +0x1C).
// With 60 slots and a 40-model set it fills: run vm1-20261108-021206, the pool at
// 5990 to 6048 KiB used (its blocks' 8-byte headers included) of 5731 from 30 s on,
// where 41 slots peaked at 4971 (vm1-20261108-020730). Like graphics pool 9 it gets each
// slot's vanilla share: ceil(0x599999 x slots / 35).
const uint32_t kAkDefaultPoolVa = 0x0084A650;
const uint32_t kAkDefaultPool = 0x599999;

bool patch_ak_default_pool(uint32_t slots)
{
    const uint8_t expect[] = {0xC7, 0x45, 0x40, 0x99, 0x99, 0x59, 0x00};
    if (!check_bytes(kAkDefaultPoolVa - 3, expect, 7)) {
        dsf_log("slots: 0084A64D is not the vanilla Wwise default pool size; vehicle slots stay 35");
        return false;
    }
    uint32_t size = scale_up(kAkDefaultPool, slots);
    if (!patch_bytes(kAkDefaultPoolVa, &size, 4, &kAkDefaultPool)) return false;
    dsf_log("slots: Wwise default pool %u KiB (vanilla %u)", size / 1024, kAkDefaultPool / 1024);
    return true;
}

// ---- the class factory's object pools -------------------------------------------
// MakeObject (0x0086A360) calls a class's creator from the table at [0x01449050]. Many
// creators take their object from one of 91 fixed object pools: the table at
// [0x01448BF8] (built by 0x00869990, 0x18 bytes each: {base, next, free count, object
// size, capacity, used}), made by 0x0086A070(id, capacity), allocated by 0x00869D50(id):
// the free list's head, or else `next` bumped by the size, with no end check. ClassOf
// (0x00869E30) and the free (0x00869DA0) find an object's pool from base and capacity,
// so a pool can't continue elsewhere: the room has to be there from its creation.
// The class-7A wrappers (creator 0x0048FB40: pool 0x16, 0x30 bytes, a sound bank wrapper
// of vtable 0x00E36D38 at +0x0C) come from pool 0x16, which their provider's constructor
// makes with 46 (push 0x2E; push 0x16; call 0x0086A070, in its constructor 0x0048F840
// and in its class creator 0x0048FB90, the one the provider table's provider comes from),
// one per node of its list. The 47th lands on the next object, the provider at table+0x68 (run
// vm1-20261107-215725: the fault of #566 with ESI = that provider). Its capacity gets
// the slots added over the vanilla 35, so it keeps vanilla's spare objects.
const uint32_t kPool16CapVa[] = {0x0048F85E, 0x0048FBB9};  // the push 0x2E immediates
const uint8_t kPool16Cap = 0x2E;
const uint32_t kObjPoolTable = 0x01448BF8;
const uint32_t kObjPools = 0x5B;

typedef void(__cdecl* PoolDestroy)(uint32_t id);              // 0x00869DF0: frees the storage, clears the entry
typedef void(__cdecl* PoolCreate)(uint32_t id, uint32_t cap);  // 0x0086A070

// Both creations get the raised capacity. Should the pool already exist when this module
// loads, it is made again with the game's own functions while nothing has taken an
// object yet (the provider's first wrapper comes with the first scene model, at package
// init).
bool patch_pool16(uint32_t slots)
{
    const uint8_t expect[] = {0x6A, kPool16Cap, 0x6A, 0x16};
    for (uint32_t va : kPool16CapVa)
        if (!check_bytes(va - 1, expect, 4)) {
            dsf_log("slots: %08X is not the vanilla pool 0x16 creation; vehicle slots stay 35", va - 1);
            return false;
        }
    uint32_t cap = kPool16Cap + (slots - kVanillaVehicleSlots);
    if (cap > 0x7F) cap = 0x7F;  // push imm8
    uint8_t* t = *(uint8_t**)kObjPoolTable;
    uint32_t* e = t ? (uint32_t*)(t + 0x16 * 0x18) : nullptr;
    if (e && e[4]) {
        if (e[4] != kPool16Cap || e[5] || e[2]) {
            dsf_log("slots: object pool 0x16 has %u objects, %u taken; vehicle slots stay 35", e[4], e[5]);
            return false;
        }
        ((PoolDestroy)0x00869DF0)(0x16);
        ((PoolCreate)0x0086A070)(0x16, cap);
        if (e[4] != cap || !e[0]) {
            ((PoolCreate)0x0086A070)(0x16, kPool16Cap);  // the vanilla pool back
            dsf_log("slots: making object pool 0x16 again failed; vehicle slots stay 35");
            return false;
        }
    }
    uint8_t b = (uint8_t)cap;
    if (!patch_bytes(kPool16CapVa[0], &b, 1, &kPool16Cap)) return false;
    if (!patch_bytes(kPool16CapVa[1], &b, 1, &kPool16Cap)) {
        patch_bytes(kPool16CapVa[0], &kPool16Cap, 1);
        return false;
    }
    dsf_log("slots: object pool 0x16 (class-7A wrappers): %u objects (vanilla 46)%s", cap,
            e && e[4] ? ", made again" : "");
    return true;
}

// The allocator as it is, plus a log line the first time a pool runs past its end (the
// object then overwrites whatever follows; vanilla does the same, silently).
typedef uint8_t*(__cdecl* PoolTableMake)();
bool g_obj_full_logged[kObjPools];

void* __cdecl h_obj_alloc(uint32_t id)
{
    uint8_t* t = *(uint8_t**)kObjPoolTable;
    if (!t) {
        t = ((PoolTableMake)0x00869990)();
        *(uint8_t**)kObjPoolTable = t;
    }
    uint32_t* e = (uint32_t*)(t + id * 0x18);
    uint8_t* obj = (uint8_t*)(uintptr_t)e[1];
    e[5]++;
    if (e[2]) {
        e[1] = *(uint32_t*)obj;
        e[2]--;
        return obj;
    }
    if (e[0] && (uint32_t)(uintptr_t)obj + e[3] > e[0] + e[4] * e[3] && id < kObjPools && !g_obj_full_logged[id]) {
        g_obj_full_logged[id] = true;
        dsf_log("slots: object pool %#x is full (%u objects of %u bytes): object %u goes past its end", id, e[4],
                e[3], e[5]);
    }
    e[1] = (uint32_t)(uintptr_t)obj + e[3];
    return obj;
}

const uint8_t kObjAllocExpect[] = {0xA1, 0xF8, 0x8B, 0x44, 0x01};  // 0x00869D50: mov eax, [0x01448BF8]

bool patch_obj_alloc()
{
    if (!check_bytes(0x00869D50, kObjAllocExpect, 5)) return false;
    return patch_jmp(0x00869D50, (void*)h_obj_alloc, 5, kObjAllocExpect);
}

// With slots_trace: the object pools within 2 of their capacity (each once).
bool g_obj_near_logged[kObjPools];

void trace_obj_pools(uint32_t slot)
{
    uint8_t* t = *(uint8_t**)kObjPoolTable;
    if (!t) return;
    for (uint32_t id = 0; id < kObjPools; id++) {
        uint32_t* e = (uint32_t*)(t + id * 0x18);
        uint32_t live = e[5];
        if (e[4] && live + 2 >= e[4] && !g_obj_near_logged[id]) {
            g_obj_near_logged[id] = true;
            dsf_log("slots_trace: after slot %u: object pool %#x has %u of %u (%u bytes each)", slot, id, live,
                    e[4], e[3]);
        }
    }
}

// ---- the MODL provider's records (157) ----------------------------------------------
// The provider at table+0x74 (vtable 0x00DDA2C8, class creator 0x00494240: 0x774 bytes)
// hands each scene model a MODL record (8 bytes: vtable 0x00DDA2B0, the resource at +4;
// its methods use nothing but that). Its fill (0x00494150) makes 157 records in place
// from +0x10 (push 0x9D; the count at +0x4F8) and stacks pointers to them at +0x4FC with
// the count at +0x770. Acquire (0x00493FD0) pops with no empty check and release
// (0x00494090) pushes with no full check. The records serve every spool object that uses
// the provider: at 60 slots they run out at the 63rd vehicle handler, and the pop reads
// the record count as a record (run vm1-20261108-015739: fault reading 0x9D at
// 0x00858E0B). The replacements hand out extra records once the stack is empty (made as
// the fill makes them, from the game's operator new 0x00C28780, since the record's
// deleting destructor frees with the game's operator delete) and keep them in this
// module when the stack is full, which is when every inline record is home.
const uint32_t kModlRecords = 0x9D;
const uint32_t kModlRecordVtable = 0x00DDA2B0;
typedef void*(__cdecl* GameNew)(size_t n);  // 0x00C28780
struct ModlExtra {
    uint8_t* owner;
    void** items;
    uint32_t count, cap;
};
ModlExtra g_modl_extra[4];
SRWLOCK g_modl_lock = SRWLOCK_INIT;
LONG g_modl_made = 0;

ModlExtra* modl_extra_of(uint8_t* self)
{
    for (ModlExtra& x : g_modl_extra)
        if (x.owner == self) return &x;
    for (ModlExtra& x : g_modl_extra)
        if (!x.owner) {
            x.owner = self;
            return &x;
        }
    return nullptr;
}

void** __fastcall h_modl_acquire(uint8_t* self, void*, void** out)
{
    int32_t* n = (int32_t*)(self + 0x770);
    if (*n > 0) {
        *out = ((void**)(self + 0x4FC))[*n - 1];
        (*n)--;
        return out;
    }
    void* rec = nullptr;
    AcquireSRWLockExclusive(&g_modl_lock);
    ModlExtra* x = modl_extra_of(self);
    if (x && x->count) rec = x->items[--x->count];
    ReleaseSRWLockExclusive(&g_modl_lock);
    if (!rec) {
        uint32_t* r = (uint32_t*)((GameNew)0x00C28780)(8);
        if (r) {
            r[0] = kModlRecordVtable;
            r[1] = 0;
            if (InterlockedIncrement(&g_modl_made) == 1)
                dsf_log("slots: MODL provider %p passes its %u records; more come from the heap", self, kModlRecords);
        }
        rec = r;
    }
    *out = rec;
    return out;
}

void __fastcall h_modl_release(uint8_t* self, void*, void** rec)
{
    int32_t* n = (int32_t*)(self + 0x770);
    if ((uint32_t)*n < kModlRecords) {
        ((void**)(self + 0x4FC))[*n] = *rec;
        (*n)++;
        return;
    }
    AcquireSRWLockExclusive(&g_modl_lock);
    ModlExtra* x = modl_extra_of(self);
    if (x && x->count == x->cap) {
        uint32_t cap = x->cap ? x->cap * 2 : 64;
        void** items = (void**)HeapAlloc(GetProcessHeap(), 0, cap * sizeof(void*));
        if (items) {
            if (x->items) {
                memcpy(items, x->items, x->count * sizeof(void*));
                HeapFree(GetProcessHeap(), 0, x->items);
            }
            x->items = items;
            x->cap = cap;
        }
    }
    if (x && x->count < x->cap) x->items[x->count++] = *rec;  // else the record is leaked, not lost
    ReleaseSRWLockExclusive(&g_modl_lock);
}

const uint8_t kModlStackExpect[] = {0x8B, 0x81, 0x70, 0x07, 0x00, 0x00};  // mov eax, [ecx+0x770]
const uint8_t kModlFillExpect[] = {0x68, 0x9D, 0x00, 0x00, 0x00};         // 0x0049415F: push 0x9D

bool patch_modl_records()
{
    if (!check_bytes(0x00493FD0, kModlStackExpect, 6) || !check_bytes(0x00494090, kModlStackExpect, 6) ||
        !check_bytes(0x0049415F, kModlFillExpect, 5)) {
        dsf_log("slots: the MODL provider's functions are not the vanilla code; vehicle slots stay 35");
        return false;
    }
    if (!patch_jmp(0x00493FD0, (void*)h_modl_acquire, 6, kModlStackExpect)) return false;
    if (!patch_jmp(0x00494090, (void*)h_modl_release, 6, kModlStackExpect)) {
        patch_bytes(0x00493FD0, kModlStackExpect, 6);
        return false;
    }
    dsf_log("slots: MODL provider records (157 inline): 00493FD0, 00494090 go on in the heap when used up");
    return true;
}

// ---- the sound manager's bank list (50 nodes) ---------------------------------
// FUN_00849870 (thiscall, one value; ret 4) appends a vehicle sound bank wrapper
// (vtable 0x00E36D38, class 9, FUN_0084C0A0) to the sound manager's list when the
// wrapper starts loading (FUN_0084BF30, the wrapper's vtable[0]): the 45-node pools'
// layout with 50 nodes, {next, free count, used} at +0x92DC, nodes from +0x92E8, the
// list head at +0x9540, and no end check. Its other users only unlink nodes and put
// them on the free list (0x008497D0 remove, 0x0084A16B the update's sweep of state-4
// wrappers), so nodes may live anywhere. The 51st lands on the list head.
uint32_t g_bank_peak = 0;

void __fastcall h_bank_append(uint8_t* self, void*, int item)
{
    int* pool = (int*)(self + 0x92DC);
    if (!pool_room(pool, self + 0x92E8, self + 0x9540, "sound bank list")) return;
    pool_append(pool, (int*)(self + 0x9540), item);
    if ((uint32_t)pool[2] > g_bank_peak) {
        g_bank_peak = (uint32_t)pool[2];
        if (g_bank_peak >= 20) dsf_log("slots: sound bank list %p holds %u (peak)", pool, g_bank_peak);
    }
}

const uint8_t kBankAppendExpect[] = {0x83, 0xB9, 0xE0, 0x92, 0x00, 0x00, 0x00};  // 0x00849870: cmp [ecx+0x92E0], 0

bool patch_bank_list()
{
    if (!check_bytes(0x00849870, kBankAppendExpect, 7)) {
        dsf_log("slots: 00849870 is not the vanilla sound bank append; vehicle slots stay 35");
        return false;
    }
    if (!patch_jmp(0x00849870, (void*)h_bank_append, 7, kBankAppendExpect)) return false;
    dsf_log("slots: 00849870 sound bank list (50 nodes) goes on in the plugin's chunks when full");
    return true;
}

// ---- slot creation trace ([archive] slots_trace; research, off by default) ---
// FUN_005A1420 makes each slot handler through FUN_0059F9C0 (thiscall: self = the
// provider table, Engine+0x15C; out, package type, extra provider, NONR size, index;
// callee cleans 0x14), whose type-0 range is self[0x17..0x1D] (#566). The range is
// filled during the first call. After it, the trace logs each provider's vtable and
// vtable[1] (its acquire); after every slot, the dwords of each provider's first 0x400
// bytes that changed (at most 8 per provider and slot).
bool g_slots_trace = false;
uint32_t g_trace_calls = 0;
bool g_trace_listed = false;
const uint32_t kTraceDwords = 0x100;
uint32_t g_trace_entry[7];
uint32_t g_trace_obj[7][kTraceDwords];

bool readable(const void* p, size_t n)
{
    MEMORY_BASIC_INFORMATION mbi;
    if (!p || !VirtualQuery(p, &mbi, sizeof(mbi)) || mbi.State != MEM_COMMIT) return false;
    if (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD)) return false;
    return (const uint8_t*)p + n <= (const uint8_t*)mbi.BaseAddress + mbi.RegionSize;
}

void trace_snapshot(uint32_t* table, bool log, uint32_t slot)
{
    for (int i = 0; i < 7; i++) {
        uint32_t e = table[0x17 + i];
        static uint32_t obj[kTraceDwords];
        memset(obj, 0, sizeof(obj));
        if (readable((void*)(uintptr_t)e, sizeof(obj))) memcpy(obj, (void*)(uintptr_t)e, sizeof(obj));
        if (log) {
            uint32_t vt1 = readable((void*)(uintptr_t)(obj[0] + 4), 4) ? *(uint32_t*)(uintptr_t)(obj[0] + 4) : 0;
            dsf_log("slots_trace: provider %d at table+%#x: %08X vtable %08X acquire %08X", i, (0x17 + i) * 4, e,
                    obj[0], vt1);
        } else if (e != g_trace_entry[i]) {
            dsf_log("slots_trace: after slot %u: table entry %d %08X -> %08X", slot, i, g_trace_entry[i], e);
        } else {
            int shown = 0;
            for (uint32_t k = 0; k < kTraceDwords && shown < 8; k++)
                if (obj[k] != g_trace_obj[i][k]) {
                    dsf_log("slots_trace: after slot %u: provider %d +%#x %08X -> %08X", slot, i, k * 4,
                            g_trace_obj[i][k], obj[k]);
                    shown++;
                }
        }
        g_trace_entry[i] = e;
        memcpy(g_trace_obj[i], obj, sizeof(obj));
    }
}

typedef void*(__fastcall* MakeSlot)(void* self, void*, void* out, uint32_t type, uint32_t extra, uint32_t nonr,
                                    uint32_t index);
void* __fastcall h_trace_make_slot(void* self, void*, void* out, uint32_t type, uint32_t extra, uint32_t nonr,
                                   uint32_t index)
{
    uint32_t* table = (uint32_t*)self;
    void* r = ((MakeSlot)0x0059F9C0)(self, nullptr, out, type, extra, nonr, index);
    g_trace_calls++;
    if (type == 0) {
        dsf_log("slots_trace: slot %u (nonr %#x, index %u): scene model %p", g_trace_calls, nonr, index,
                *(void**)out);
        if (!g_trace_listed) {
            g_trace_listed = true;
            dsf_log("slots_trace: provider table %p", self);
            trace_snapshot(table, true, g_trace_calls);
        } else {
            trace_snapshot(table, false, g_trace_calls);
            trace_obj_pools(g_trace_calls);
        }
    }
    return r;
}

const uint8_t kMakeSlotCallExpect[] = {0xE8, 0x2A, 0xE5, 0xFF, 0xFF};  // 0x005A1491: call 0x0059F9C0

// The AI copy is opened before the SSLP is loaded (run vm1-20261108-025033: 'media\san_franciscoai.fchunk' opened 0.6 s before the SSLP load), so the redirect decides from
// slots_expected(); a refusal at the SSLP after the copy was served is logged as the error it is.
void slots_refused_late()
{
    if (g_ai_copy_served)
        dsf_log("slots: ERROR: the traffic sets' AI copy was already served for %u slots; a chapter whose set "
                "needs more than 35 may not finish loading (at the next start the copy is not used)",
                g_vehicle_slots);
}

// The vehicle spool object's SSLP header has just been set: raise its kind-0 slot
// byte and move its ID pools out of the object. obj = the spool object (the SSLP
// load's `self` - 4); its package type is at +0x7300 (vehicles: 0, FUN_005A1520).
void apply_vehicle_slots(uint8_t* obj)
{
    if (!g_vehicle_slots || g_slots_done || *(uint32_t*)(obj + 0x7300) != 0) return;
    g_slots_done = true;
    uint8_t* header = *(uint8_t**)(obj + 0x72F8);
    uint32_t others = header[0x19] + header[0x1A] + header[0x1B];
    uint32_t slots = g_vehicle_slots;
    if (slots + others > kSlotHandlers) slots = kSlotHandlers - others;
    if (header[0x18] != kVanillaVehicleSlots) {
        dsf_log("slots: the vehicle SSLP has %u kind-0 slots, not 35 (another mod's archive); left as it is",
                header[0x18]);
        slots_refused_late();
        return;
    }
    for (uint32_t off : kIdPools) {
        uint32_t* pool = (uint32_t*)(obj + off);
        if (pool[0] != (uint32_t)(uintptr_t)(obj + off + 0x0C) || pool[1] || pool[2]) {
            dsf_log("slots: spool ID pool +%#x is already in use; vehicle slots stay 35", off);
            slots_refused_late();
            return;
        }
    }
    for (uint32_t off : kIdPools) {
        void* buffer = HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, kPoolBytes);
        if (!buffer) {
            dsf_log("slots: out of memory for the spool ID pools; vehicle slots stay 35");
            return;  // the pools moved so far keep working: they only have more room
        }
        *(uint32_t*)(obj + off) = (uint32_t)(uintptr_t)buffer;
    }
    header[0x18] = (uint8_t)slots;
    g_slots_applied = slots;
    dsf_log("slots: vehicle spool object %p: %u kind-0 slots (vanilla 35; %u of %u handlers), ID pools of %u nodes",
            obj, slots, slots + others, kSlotHandlers, kPoolBytes / 16);
}

// FUN_005A0B50 (thiscall: self = spool object + 4; reader, entry index): SSLP
// loaded. The vanilla function points self+0x72F4 at the SSLP header and
// self+0x72F8 at its records. self is the spool object's second base (vtable
// 0x00DE4D48 at spool object +4, set by the constructor 0x005A10F0), and the root
// reader is embedded at spool object +0x14 (constructed in place: `lea ecx,
// [esi+0x14]` at 0x005A112F), so it is self+0x10 itself, not a pointer there.
// For the vehicle package a copy with three records per package goes there
// instead; a package whose ID (or +1000/+2000) the archive already has, or
// whose NONR is larger than the slot buffer the SSLP header gives (+0x08 +
// 4 * kind), is left out. Its root entries stay (nothing names them).
static void sslp_load_packages(uint8_t* self, Reader* reader, uint32_t index)
{
    uint8_t* p = (uint8_t*)h_ptr(reader, nullptr, index);
    *(uint8_t**)(self + 0x72F4) = p;
    *(uint8_t**)(self + 0x72F8) = p + 0x1C;
    if (!g_pkg_first || (Reader*)(self + 0x10) != g_veh_reader) return;
    uint32_t count = *(uint16_t*)(p + 2);
    const uint16_t* rec = (const uint16_t*)(p + 0x1C);
    uint32_t add = 0;
    for (uint32_t j = 0; j < g_pkg_count; j++) {
        Pkg* q = &g_pkgs[j];
        q->listed = false;
        bool clash = false;
        for (uint32_t i = 0; i < count && !clash; i++)
            for (int k = 0; k < 3; k++)
                if (rec[i * 3] == q->id + 1000 * k) clash = true;
        for (uint32_t i = 0; i < j && !clash; i++)
            if (g_pkgs[i].listed && g_pkgs[i].id == q->id) clash = true;
        if (clash) {
            dsf_log("packages: %u (%s) is already in the archive or another package; left out", q->id, q->path);
            continue;
        }
        bool fits = true;
        for (int k = 0; k < 3; k++)
            if (q->nonr[k] > *(uint32_t*)(p + 8 + 4 * k)) fits = false;
        if (!fits) {
            dsf_log("packages: %u: a NONR is larger than its slot buffer (SSLP %#x/%#x/%#x); left out", q->id,
                    *(uint32_t*)(p + 8), *(uint32_t*)(p + 12), *(uint32_t*)(p + 16));
            continue;
        }
        q->listed = true;
        add++;
    }
    uint8_t* sslp = nullptr;
    if (count + 3 * add > 0xFFFF)
        dsf_log("packages: %u SSLP records would pass 65535; no car packages", count + 3 * add);
    else if (!(sslp = (uint8_t*)HeapAlloc(GetProcessHeap(), 0, 0x1C + (count + 3 * add) * 6)))
        dsf_log("packages: out of memory for the SSLP copy; no car packages");
    g_sslp_done = true;
    if (!sslp) {
        // None listed, so the VSCT hook adds none either.
        for (uint32_t j = 0; j < g_pkg_count; j++) g_pkgs[j].listed = false;
        g_pkg_failed = true;
        return;
    }
    memcpy(sslp, p, 0x1C + count * 6);
    uint16_t* out = (uint16_t*)(sslp + 0x1C) + count * 3;
    for (uint32_t j = 0; j < g_pkg_count; j++) {
        if (!g_pkgs[j].listed) continue;
        for (uint16_t k = 0; k < 3; k++) {
            out[0] = (uint16_t)(g_pkgs[j].id + 1000 * k);
            out[1] = (uint16_t)(g_pkg_first + 3 * j + k);
            out[2] = k;
            out += 3;
        }
    }
    *(uint16_t*)(sslp + 2) = (uint16_t)(count + 3 * add);
    if (g_sslp) HeapFree(GetProcessHeap(), 0, g_sslp);
    g_sslp = sslp;
    *(uint8_t**)(self + 0x72F4) = sslp;
    *(uint8_t**)(self + 0x72F8) = sslp + 0x1C;
    dsf_log("packages: SSLP at %p: %u records + %u for %u car packages", self, count, 3 * add, add);
}

// The hook: the packages' SSLP copy (when there are packages), then the vehicle
// slot count ([archive] vehicle_slots) on whichever header the object now uses.
void __fastcall h_sslp_load(uint8_t* self, void*, Reader* reader, uint32_t index)
{
    sslp_load_packages(self, reader, index);
    apply_vehicle_slots(self - 4);
}

// The engine singleton, as FUN_0086C630 gets it (0x0086C644..0x0086C652): the
// global at 0x00FE7228, made by 0x0040A690 when still unset.
uint8_t* engine_ptr()
{
    uint8_t** engine = (uint8_t**)0x00FE7228;
    typedef uint8_t*(__cdecl * GetEngine)();
    if (!*engine) *engine = ((GetEngine)0x0040A690)();
    return *engine;
}

// FUN_0086C630 (thiscall: VSCT resource; reader, entry index): stores the
// payload at resource+4 and, when none is set yet, at [Engine+0x780]+0x2C (the
// table FUN_0086C550/FUN_0086C5B0 search: "VSCT" u16 version, u16 count, then
// 28-byte records whose first u16 is the model ID). FUN_0086C670 clears the
// global when it equals resource+4, so the copy goes to both places.
void __fastcall h_vsct_load(uint8_t* self, void*, Reader* reader, uint32_t index)
{
    uint8_t* p = (uint8_t*)h_ptr(reader, nullptr, index);
    *(uint8_t**)(self + 4) = p;
    uint32_t add = 0;
    for (uint32_t j = 0; j < g_pkg_count; j++)
        if (g_pkgs[j].listed) add++;
    uint32_t count = *(uint16_t*)(p + 6);
    if (add && *(uint32_t*)p == kVSCT && count + 3 * add <= 0xFFFF) {
        uint8_t* v = (uint8_t*)HeapAlloc(GetProcessHeap(), 0, 8 + (count + 3 * add) * 28);
        if (v) {
            memcpy(v, p, 8 + count * 28);
            uint8_t* out = v + 8 + count * 28;
            for (uint32_t j = 0; j < g_pkg_count; j++) {
                if (!g_pkgs[j].listed) continue;
                memcpy(out, g_pkgs[j].vsct, sizeof g_pkgs[j].vsct);
                for (int k = 0; k < 3; k++) *(uint16_t*)(out + 28 * k) = g_pkgs[j].id;
                out += sizeof g_pkgs[j].vsct;
            }
            *(uint16_t*)(v + 6) = (uint16_t)(count + 3 * add);
            // An earlier copy may still be the engine's table (FUN_0086C670
            // clears it only for its own resource); keep it then.
            uint8_t* holder = *(uint8_t**)(engine_ptr() + 0x780);
            if (g_vsct && *(uint8_t**)(holder + 0x2C) != g_vsct) HeapFree(GetProcessHeap(), 0, g_vsct);
            g_vsct = v;
            *(uint8_t**)(self + 4) = v;
            dsf_log("packages: VSCT: %u records + %u from car packages", count, 3 * add);
        }
    }
    uint8_t* holder = *(uint8_t**)(engine_ptr() + 0x780);
    if (!*(uint8_t**)(holder + 0x2C)) *(uint8_t**)(holder + 0x2C) = *(uint8_t**)(self + 4);
}

typedef uint32_t(__fastcall* ReaderOpen)(Reader* r, void*, const char* name, int async, int flag);
const ReaderOpen reader_open = (ReaderOpen)0x008FC530;

// Call 0x005A13C4 in FUN_005A11E0 (the unified spool loader, state 1): the
// package's root reader opens its file. Remembers the vehicle archive's reader.
uint32_t __fastcall h_spool_open(Reader* r, void*, const char* name, int async, int flag)
{
    uint32_t ok = reader_open(r, nullptr, name, async, flag);
    size_t n = strlen(name);
    const char kName[] = "dngvehicles.sp";
    if (n >= sizeof kName - 1 && _stricmp(name + n - (sizeof kName - 1), kName) == 0) {
        g_veh_reader = r;
        g_pkg_first = 0;
        dsf_log("packages: vehicle archive '%s' opens in reader %p (%s)", name, r, (ok & 0xFF) ? "ok" : "failed");
    }
    return ok;
}

// ---- replacements ----------------------------------------------------------
// 0x008FB340: tag of entry i
uint32_t __fastcall h_tag(Reader* r, void*, uint32_t i) { return E(r, i)->tag; }

// 0x008FB370: version byte of entry i
uint32_t __fastcall h_version(Reader* r, void*, uint32_t i) { return E(r, i)->version; }

// 0x008FB3A0: payload pointer of entry i
void* __fastcall h_ptr(Reader* r, void*, uint32_t i) { return *P(r, i); }

// 0x008FB3C0: size of entry i
uint32_t __fastcall h_size(Reader* r, void*, uint32_t i) { return E(r, i)->size; }

// 0x008FB570: set size, flag and alignment of entry i; clears its pointer
void __fastcall h_set_entry(Reader* r, void*, uint32_t i, uint32_t size, int flag, int align)
{
    Entry* e = E(r, i);
    e->size = size;
    e->flag = (uint8_t)flag;
    int v = align;
    uint8_t a = 0;
    if (v > 0xFFFF) { a = 16; v >>= 16; }
    if (v > 0xFF) { a += 8; v >>= 8; }
    if (v > 0xF) { a += 4; v >>= 4; }
    if (v > 3) { a += 2; v >>= 2; }
    if (v > 1) a += 1;
    e->align_log2 = a;
    *P(r, i) = nullptr;
}

// 0x008FB5E0: seek to pos inside this reader (nested readers add the parent
// entry's offset and defer to the parent)
void __fastcall h_seek(Reader* r, void*, int pos)
{
    for (;;) {
        if (r->mode == 1) {
            file_seek(r->file, pos);
            return;
        }
        if (r->mode != 3) return;
        if (pkg_of(r->parent, r->parent_index, nullptr)) {
            // A nested reader over a package entry would read the archive at the
            // package-relative offset. No vanilla path does this for a VEHC (the
            // slot reads it whole); log it if one ever does.
            static volatile LONG warned = 0;
            if (InterlockedExchange(&warned, 1) == 0)
                dsf_log("packages: nested reader %p over package entry %u: not supported", r, r->parent_index);
        }
        pos += (int)E(r->parent, r->parent_index)->offset;
        r = r->parent;
    }
}

// ---- asynchronous package reads ([archive] async_reads) -------------------
// The game reads a car's VEHC asynchronously: the slot (SceneModelType2) calls
// read entry from FUN_00858870 (the call at 0x008588B7), sets its state to 2,
// and from then on calls the root reader's poll (h_poll, 0x008FC0A0, from
// SceneModelType2_Poll at 0x008590CC) each time it is updated, until the poll
// answers 0; only then (state 3) does it parse the buffer. h_poll's state 0
// asks FUN_008FB2D0 whether the reader's file read is still running; nothing
// else calls FUN_008FB2D0 (its three calls are inside the vanilla poll, which
// h_poll replaces), and nothing outside the reader's own functions touches the
// reader's file (`xref disp 0x5600`, `xref call 0x8fb2a0`). So a package read
// for that caller runs on a thread-pool thread, and h_poll answers busy for its
// reader until it is done, as the game's own archive does. The data, the
// listed ID included (pkg_set_ids), are in the buffer before the poll says 0.
// Reads of package entries from any other caller stay synchronous.
// One read is pending at most: every package entry is read through the
// vehicle archive's root reader, into the shared spool buffer (one load at a
// time, owner word 0x016DA7D0). A read entry, a close of that reader, or a
// second read, waits for the pending one first, so it can't write into a
// buffer the game has moved on with.
const uint32_t kSlotReadReturn = 0x008588BC;  // after `call 0x8fbe90` in FUN_00858870
bool g_async_reads = true;
struct PendingRead {
    Reader* volatile reader;  // nullptr: none
    Pkg* pkg;
    Entry entry;
    uint8_t* buf;
    uint32_t size;
    uint32_t kind;
    uint32_t polls;  // busy answers given so far
    LARGE_INTEGER started;
    HANDLE event;  // manual reset; set when the read is done (the only completion signal)
};
PendingRead g_pending = {};
SRWLOCK g_pending_lock = SRWLOCK_INIT;
volatile LONG g_async_count = 0;

void CALLBACK pkg_read_work(PTP_CALLBACK_INSTANCE, void* ctx)
{
    PendingRead* q = (PendingRead*)ctx;
    pkg_read(q->pkg, &q->entry, q->buf, q->size);
    pkg_set_ids(q->pkg, q->kind, q->buf, q->entry.size);
    // The last touch of q: once the event is seen set, pkg_settle clears it and
    // pkg_read_async may reuse it for the next read.
    SetEvent(q->event);
}

// Clears the pending read of r, if any, once it is done (wait: blocks until it
// is). Returns false when r still has one running and wait is false.
bool pkg_settle(Reader* r, bool wait, const char* why)
{
    if (!g_pending.reader) return true;  // fast path; set and cleared under the lock
    AcquireSRWLockExclusive(&g_pending_lock);
    if (g_pending.reader != r) {
        ReleaseSRWLockExclusive(&g_pending_lock);
        return true;
    }
    if (WaitForSingleObject(g_pending.event, 0) != WAIT_OBJECT_0) {
        if (!wait) {
            g_pending.polls++;
            ReleaseSRWLockExclusive(&g_pending_lock);
            return false;
        }
        WaitForSingleObject(g_pending.event, INFINITE);
        if (g_read_log) dsf_log("pkg_read: %u waited for in %s", g_pending.pkg->id, why);
    }
    if (g_read_log) {
        LARGE_INTEGER now;
        QueryPerformanceCounter(&now);
        dsf_log("pkg_read: %u done, %u ms after it started, %u busy polls (%s)", g_pending.pkg->id,
                qpc_us(g_pending.started, now) / 1000, g_pending.polls, why);
    }
    g_pending.reader = nullptr;
    ReleaseSRWLockExclusive(&g_pending_lock);
    return true;
}

// Starts the read of package entry e into buf on a thread-pool thread; false
// when it can't (the caller then reads synchronously).
bool pkg_read_async(Reader* r, Pkg* p, uint32_t kind, const Entry* e, void* buf, uint32_t size)
{
    AcquireSRWLockExclusive(&g_pending_lock);
    if (g_pending.reader) {  // another reader's (none is known to exist): don't queue behind it
        ReleaseSRWLockExclusive(&g_pending_lock);
        return false;
    }
    if (!g_pending.event) g_pending.event = CreateEventA(nullptr, TRUE, FALSE, nullptr);
    bool ok = g_pending.event != nullptr;
    if (ok) {
        g_pending.pkg = p;
        g_pending.entry = *e;
        g_pending.buf = (uint8_t*)buf;
        g_pending.size = size;
        g_pending.kind = kind;
        g_pending.polls = 0;
        ResetEvent(g_pending.event);
        QueryPerformanceCounter(&g_pending.started);
        g_pending.reader = r;
        ok = TrySubmitThreadpoolCallback(pkg_read_work, &g_pending, nullptr) != FALSE;
        if (!ok) g_pending.reader = nullptr;
    }
    DWORD err = ok ? 0 : GetLastError();
    ReleaseSRWLockExclusive(&g_pending_lock);
    if (!ok) {
        static volatile LONG warned = 0;
        if (InterlockedExchange(&warned, 1) == 0)
            dsf_log("packages: no thread-pool work item (error %lu); reads are synchronous", err);
        return false;
    }
    if (InterlockedIncrement(&g_async_count) == 1)
        dsf_log("packages: entries are read on a thread-pool thread; the slot's poll waits for them");
    return true;
}

// 0x008FB620 (and its thunks 0x008FB9D0, destructor 0x008FBE80): close
void __fastcall h_close(Reader* r, void*)
{
    pkg_settle(r, true, "close");
    switch ((uint32_t)r->mode) {
    case 0:
        for (uint32_t i = 0; i < r->count; i++)
            if (E(r, i)->flag != 1) game_free(*P(r, i));
        break;
    case 1:
    case 2:
        file_close(r->file);
        r->file = -1;
        break;
    case 3:
        r->parent->refs--;
        r->parent = nullptr;
        break;
    default:
        break;
    }
    r->mode = 0;
    r->count = 0;
    r->ptr_count = 0;
    if (r == g_veh_reader) g_pkg_first = 0;
    ext_drop(r);
}

// 0x008FB6F0: size of entry i aligned to its alignment
uint32_t __fastcall h_aligned_size(Reader* r, void*, uint32_t i)
{
    Entry* e = E(r, i);
    return align_up(e->size, 1u << (e->align_log2 & 31));
}

// 0x008FB720: bytes to read for entry i (payload, name, both alignments)
uint32_t __fastcall h_read_size(Reader* r, void*, uint32_t i, uint32_t align)
{
    if (align == 0) align = sector_size();
    return align_up(entry_span(E(r, i)), align);
}

// 0x008FB770: name of entry i into buf (zero-terminated)
void __fastcall h_name(Reader* r, void*, uint32_t i, char* buf)
{
    Entry* e = E(r, i);
    uint32_t size = e->size;
    uint8_t len = e->name_len;
    if (Pkg* p = pkg_of(r, i, nullptr)) {
        uint32_t got = 0;
        pread(p->file, e->offset + size, buf, len, &got);
        buf[got] = 0;
        return;
    }
    if (r->mode == 0) {
        memcpy(buf, (const char*)*P(r, i) + size, len);
    } else if (r->mode == 1) {
        h_seek(r, nullptr, (int)(e->offset + size));
        chain_read(r, nullptr, buf, len);
        chain_wait(r, nullptr);
    }
    buf[len] = 0;
}

// 0x008FB880: attach data to entry i (copied when flag is 0, else borrowed)
void __fastcall h_set_data(Reader* r, void*, uint32_t i, void* data, uint32_t size, int flag, int align)
{
    void** slot = P(r, i);
    if (*slot) {
        game_free(*slot);
        *slot = nullptr;
        E(r, i)->name_len = 0;
    }
    if (flag == 0) {
        h_set_entry(r, nullptr, i, size, 0, align);
        if (data) {
            void* copy = game_malloc(size);
            memcpy(copy, data, size);
            *P(r, i) = copy;
        }
    } else {
        h_set_entry(r, nullptr, i, size, flag, align);
        *P(r, i) = data;
    }
}

// 0x008FBE90: read entry i into buf; store == 1 keeps buf as its pointer
void* __fastcall h_read_entry(Reader* r, void*, uint32_t i, void* buf, int store)
{
    pkg_settle(r, true, "read entry");
    uint32_t sector = sector_size();
    Entry* e = E(r, i);
    uint32_t size = align_up(entry_span(e), sector);
    uint32_t kind = 0;
    if (Pkg* p = pkg_of(r, i, &kind)) {
        // Whole: this is the only path to an entry's bytes (seek and chain
        // read are called only from the reader's own functions, which this
        // module replaces), so the ID written here is the one the slot parses.
        // For the slot's load (async_reads) the read runs on a thread-pool
        // thread and h_poll reports busy until it is done; for every other
        // caller, and if no work item can be queued, it is synchronous, so the
        // data is in buf when this returns.
        bool async = g_async_reads && _ReturnAddress() == (void*)(uintptr_t)kSlotReadReturn &&
                     pkg_read_async(r, p, kind, e, buf, size);
        if (!async) {
            pkg_read(p, e, buf, size);
            pkg_set_ids(p, kind, (uint8_t*)buf, e->size);
        }
        if (store == 1) *P(r, i) = buf;
        return buf;
    }
    h_seek(r, nullptr, (int)e->offset);
    if (r->mode == 1)
        file_read(r->file, buf, size);
    else if (r->mode == 3)
        chain_read(r->parent, nullptr, buf, size);
    if (store == 1) *P(r, i) = buf;
    return buf;
}

// 0x008FC0A0: async open state machine (1 read the first sector, 2 parse the
// header and read the whole entry table, 3 copy the table). Same steps as
// vanilla; a table of more than kInline entries goes through g_big_hdr and the
// side table.
uint32_t __fastcall h_poll(Reader* r, void*)
{
    uint8_t result = 1;
    switch (r->state) {
    case 0:
        if (!pkg_settle(r, false, "poll")) return 1;  // a package read still running (async_reads)
        return chain_busy(r, nullptr);
    case 1: {
        if (*g_busy) {
            h_poll(*g_busy, nullptr);
            return 1;
        }
        *g_busy = r;
        uint8_t zero = 0;
        uint32_t size = chain_size(r, nullptr);
        resize_bytes(g_hdr, nullptr, size > 0x800 ? 0x800 : size, &zero);
        h_seek(r, nullptr, 0);
        chain_read(r, nullptr, g_hdr, *g_hdr_size);
        r->state = 2;
        return 1;
    }
    case 2: {
        if (chain_busy(r, nullptr)) return 1;
        int version = header_version(g_hdr);
        uint32_t count = *(uint32_t*)(g_hdr + 8);
        bool valid = *(uint32_t*)g_hdr == kCHNK && version == 3;
        if (valid && count > kInline && (count > g_max_entries || !ext_ensure(r, count, r->ptr_count))) {
            dsf_log("archive: reader %p: CHNK with %u entries refused (max_entries %u)", r, count, g_max_entries);
            valid = false;
        }
        if (!valid) {
            result = 0;
            r->state = 0;
            *g_busy = nullptr;
            r->ok = 0;
            break;
        }
        r->flag5614 = (uint8_t)(((uint32_t)version >> 31) & 1);
        uint32_t need = (count * 16 + 0x80F) & 0xFFFFF800;
        if (count <= kInline) {
            ext_drop(r);
            Entry fill = {};
            resize_entries(r->entries, nullptr, count, &fill);
            void* nul = nullptr;
            resize_ptrs(r->ptrs, nullptr, count, &nul);
            g_big_hdr_owner = nullptr;
            if (need > *g_hdr_size) {
                uint8_t zero = 0;
                resize_bytes(g_hdr, nullptr, need, &zero);
                h_seek(r, nullptr, 0);
                chain_read(r, nullptr, g_hdr, *g_hdr_size);
            }
        } else {
            log_big(r, count, "opens", _AddressOfReturnAddress());
            if (g_big_hdr_size < need) {
                if (g_big_hdr) HeapFree(GetProcessHeap(), 0, g_big_hdr);
                g_big_hdr = (uint8_t*)HeapAlloc(GetProcessHeap(), 0, need);
                g_big_hdr_size = g_big_hdr ? need : 0;
            }
            if (!g_big_hdr) {
                // count and ptr_count are still the old ones and the side table
                // (just grown) still covers them, so a later close stays in bounds.
                dsf_log("archive: out of memory for a %u-byte CHNK header", need);
                result = 0;
                r->state = 0;
                *g_busy = nullptr;
                r->ok = 0;
                break;
            }
            for (uint32_t j = r->ptr_count; j < kInline; j++) r->ptrs[j] = nullptr;
            r->count = count;
            r->ptr_count = count;
            g_big_hdr_owner = r;
            h_seek(r, nullptr, 0);
            chain_read(r, nullptr, g_big_hdr, need);
        }
        r->state = 3;
        return 1;
    }
    case 3: {
        if (chain_busy(r, nullptr)) return 1;
        if (g_big_hdr_owner == r && r->count > kInline) {
            put_entries(r, (const Entry*)(g_big_hdr + 16), r->count);
        } else {
            memcpy(r->entries, g_hdr + 16, r->count * 16);
        }
        g_big_hdr_owner = nullptr;
        r->state = 0;
        *g_busy = nullptr;
        result = 0;
        r->ok = 1;
        if (r == g_veh_reader) pkg_inject(r);
        break;
    }
    default:
        return 1;
    }
    if (!r->ok) {
        if (r->file != -1) file_close(r->file);
        r->parent = nullptr;
        r->mode = 0;
        r->file = -1;
        return result;
    }
    if (r->parent) r->parent->refs++;
    return result;
}

// 0x008FC400: open a CHNK already in memory at p; flag as in h_set_data
uint32_t __fastcall h_load_mem(Reader* r, void*, uint32_t* p, int flag, int)
{
    if (p[0] != kCHNK) return 0;
    uint32_t n = p[2];
    if (n > kInline) {
        if (n > g_max_entries || !ext_ensure(r, n, r->ptr_count)) {
            dsf_log("archive: reader %p: in-memory CHNK with %u entries refused (max_entries %u)", r, n,
                    g_max_entries);
            return 0;
        }
        log_big(r, n, flag ? "parses (borrowed)" : "parses (copied)", _AddressOfReturnAddress());
        // The vanilla pointer-vector resize zeroes the new slots.
        for (uint32_t j = r->ptr_count; j < kInline; j++) r->ptrs[j] = nullptr;
        r->count = n;
        r->ptr_count = n;
    } else {
        ext_drop(r);
        Entry fill = {};
        resize_entries(r->entries, nullptr, n, &fill);
        void* nul = nullptr;
        resize_ptrs(r->ptrs, nullptr, n, &nul);
    }
    if (n) {
        put_entries(r, (const Entry*)(p + 4), n);
        for (uint32_t i = 0; i < n; i++) {
            Entry* e = E(r, i);
            uint32_t size = e->size;
            h_set_data(r, nullptr, i, (uint8_t*)p + e->offset, e->name_len + size, flag, 1 << (e->align_log2 & 31));
            E(r, i)->size = size;
        }
    }
    return 1;
}

// ---- patch sites -----------------------------------------------------------
struct Site {
    uint32_t va;
    void* target;
    uint8_t expect[14];
    uint8_t n;
    const char* what;
};

const Site kSites[] = {
    {0x008FB340, (void*)h_tag, {0x8B, 0x44, 0x24, 0x04, 0xC1, 0xE0, 0x04, 0x8B, 0x44, 0x08, 0x08, 0xC2, 0x04, 0x00}, 14, "entry tag"},
    {0x008FB370, (void*)h_version, {0x8B, 0x44, 0x24, 0x04, 0x40, 0xC1, 0xE0, 0x04, 0x8A, 0x04, 0x08, 0xC2, 0x04, 0x00}, 14, "entry version"},
    {0x008FB3A0, (void*)h_ptr, {0x8B, 0x44, 0x24, 0x04, 0x8B, 0x84, 0x81, 0xCC, 0x44, 0x00, 0x00, 0xC2, 0x04, 0x00}, 14, "entry pointer"},
    {0x008FB3C0, (void*)h_size, {0x8B, 0x44, 0x24, 0x04, 0xC1, 0xE0, 0x04, 0x8B, 0x44, 0x08, 0x14, 0xC2, 0x04, 0x00}, 14, "entry size"},
    {0x008FB570, (void*)h_set_entry, {0x56, 0x8B, 0x74, 0x24, 0x14, 0x57, 0x8B, 0x7C, 0x24, 0x0C}, 10, "set entry"},
    {0x008FB5E0, (void*)h_seek, {0x8B, 0x54, 0x24, 0x04, 0x8B, 0x41, 0x04, 0x83, 0xE8, 0x01}, 10, "seek"},
    {0x008FB620, (void*)h_close, {0x56, 0x8B, 0xF1, 0x8B, 0x46, 0x04, 0x83, 0xF8, 0x03}, 9, "close"},
    {0x008FB6F0, (void*)h_aligned_size, {0x8B, 0x44, 0x24, 0x04, 0xC1, 0xE0, 0x04, 0x03, 0xC1}, 9, "aligned size"},
    {0x008FB720, (void*)h_read_size, {0x56, 0x8B, 0x74, 0x24, 0x0C, 0x57, 0x8B, 0xF9}, 8, "read size"},
    {0x008FB770, (void*)h_name, {0x53, 0x56, 0x57, 0x8B, 0x7C, 0x24, 0x10, 0x8B, 0xF1}, 9, "entry name"},
    {0x008FB880, (void*)h_set_data, {0x53, 0x55, 0x8B, 0x6C, 0x24, 0x18, 0x56, 0x57}, 8, "set data"},
    {0x008FBE90, (void*)h_read_entry, {0x53, 0x55, 0x56, 0x57, 0x8B, 0xF9, 0xE8, 0x85, 0xB7, 0xF5, 0xFF}, 11, "read entry"},
    {0x008FC0A0, (void*)h_poll, {0x83, 0xEC, 0x1C, 0x53, 0x56, 0x8B, 0xF1, 0x8B, 0x86, 0x10, 0x56, 0x00, 0x00}, 13, "async open"},
    {0x008FC400, (void*)h_load_mem, {0x83, 0xEC, 0x18, 0x53, 0x8B, 0x5C, 0x24, 0x20, 0x81, 0x3B, 0x43, 0x48, 0x4E, 0x4B}, 14, "open in memory"},
};

// ---- spoolable item UID lists ([archive] spool_lists) -----------------------
// A spool package's SSIC/SPOL (u32 count, count x u64 item UID) is copied into
// an inline array of 1100 UIDs (provider object, constructor 0x0059B1E0: array
// at +0, count at +0x2260) by 0x0059B6D0, which loads nothing at all when the
// list is longer (CMP EDX,0x44C; JA). The index of a UID in that list is the
// item's spool ID: 0x0059B5A0 finds it (linear search, 0xFFFF when absent) for
// the four request/release functions 0x0059B0B0, 0x0059B0F0, 0x0059B420 and
// 0x0059B4D0, and nothing else reads the array (Ghidra scans of 0x2260 and of
// the find's callers, out/ghidra/spol*). gui.sp's garage pictures go through
// it: with more than 1100 pictures the list stayed empty and every garage card,
// vanilla ones included, showed the lock icon (VM runs vm-20261024-235436 with
// 1121 SPOL entries against -234833 with 1098).
// The replacements keep lists longer than 1100 in a side table keyed by the
// provider object (the first 1100 also inline, as far as vanilla code would see
// them), and the find searches the side list when the object has one. Lists
// of at most 1100 behave exactly as before.
const uint32_t kSpolInline = 1100;
const uint32_t kSpolCount = 0x2260;
struct SpolList {
    void* owner;
    uint32_t count;
    uint64_t* uids;
};
const int kMaxSpol = 16;
SpolList g_spol[kMaxSpol];
volatile LONG g_spol_used = 0;  // 0: no long list anywhere (fast path)
SRWLOCK g_spol_lock = SRWLOCK_INIT;

SpolList* spol_find_locked(void* owner)
{
    for (int i = 0; i < kMaxSpol; i++)
        if (g_spol[i].owner == owner) return &g_spol[i];
    return nullptr;
}

void spol_drop(void* self)
{
    if (!g_spol_used) return;
    AcquireSRWLockExclusive(&g_spol_lock);
    SpolList* s = spol_find_locked(self);
    if (s) {
        HeapFree(GetProcessHeap(), 0, s->uids);
        s->owner = nullptr;
        s->uids = nullptr;
        s->count = 0;
        InterlockedDecrement(&g_spol_used);
    }
    ReleaseSRWLockExclusive(&g_spol_lock);
}

// 0x0059B6D0, thiscall (reader, entry index): load the SPOL entry into self.
void __fastcall h_spol_load(uint8_t* self, void*, Reader* reader, uint32_t index)
{
    uint32_t* list = (uint32_t*)h_ptr(reader, nullptr, index);
    uint32_t n = list[0];
    const uint64_t* uids = (const uint64_t*)(list + 1);
    uint32_t inline_n = n <= kSpolInline ? n : kSpolInline;
    memcpy(self, uids, inline_n * sizeof(uint64_t));
    *(uint32_t*)(self + kSpolCount) = inline_n;
    if (n <= kSpolInline) {
        spol_drop(self);
        return;
    }
    if (n > g_max_entries) {
        dsf_log("archive: spool list of %u items at %p refused (max_entries %u); the first %u load", n, self,
                g_max_entries, kSpolInline);
        spol_drop(self);
        return;
    }
    uint64_t* copy = (uint64_t*)HeapAlloc(GetProcessHeap(), 0, n * sizeof(uint64_t));
    if (!copy) {
        spol_drop(self);
        return;
    }
    memcpy(copy, uids, n * sizeof(uint64_t));
    AcquireSRWLockExclusive(&g_spol_lock);
    SpolList* s = spol_find_locked(self);
    if (!s) {
        s = spol_find_locked(nullptr);
        if (s) InterlockedIncrement(&g_spol_used);
    }
    if (s) {
        if (s->uids) HeapFree(GetProcessHeap(), 0, s->uids);
        s->owner = self;
        s->count = n;
        s->uids = copy;
    }
    ReleaseSRWLockExclusive(&g_spol_lock);
    if (!s) {
        HeapFree(GetProcessHeap(), 0, copy);
        dsf_log("archive: no slot for the spool list of %u items at %p; the first %u load", n, self, kSpolInline);
        return;
    }
    dsf_log("archive: spool list at %p holds %u items (%u past the vanilla %u)", self, n, n - kSpolInline,
            kSpolInline);
}

// 0x0059B5A0, thiscall (UID low, UID high): the item's index in self's list, or 0xFFFF.
uint32_t __fastcall h_spol_find(uint8_t* self, void*, uint32_t lo, uint32_t hi)
{
    const uint64_t uid = ((uint64_t)hi << 32) | lo;
    if (g_spol_used) {
        AcquireSRWLockShared(&g_spol_lock);
        SpolList* s = spol_find_locked(self);
        if (s) {
            uint32_t found = 0xFFFF;
            for (uint32_t i = 0; i < s->count; i++) {
                if (s->uids[i] == uid) {
                    found = i;
                    break;
                }
            }
            ReleaseSRWLockShared(&g_spol_lock);
            return found;
        }
        ReleaseSRWLockShared(&g_spol_lock);
    }
    const uint64_t* uids = (const uint64_t*)self;
    uint32_t n = *(uint32_t*)(self + kSpolCount);
    for (uint32_t i = 0; i < n; i++)
        if (uids[i] == uid) return i;
    return 0xFFFF;
}

void patch_spool_lists()
{
    static const uint8_t load_expect[] = {0x8B, 0x44, 0x24, 0x08, 0x56};
    static const uint8_t find_expect[] = {0x8B, 0x91, 0x60, 0x22, 0x00, 0x00};
    static const uint8_t cap_expect[] = {0x81, 0xFA, 0x4C, 0x04, 0x00, 0x00, 0x77, 0x31};
    if (!bytes_equal(0x0059B6D0, load_expect, sizeof load_expect) ||
        !bytes_equal(0x0059B5A0, find_expect, sizeof find_expect) ||
        !bytes_equal(0x0059B6ED, cap_expect, sizeof cap_expect)) {
        dsf_log("archive: 0059B6D0/0059B5A0 are not the vanilla code; spool lists stay at %u items", kSpolInline);
        return;
    }
    if (!patch_jmp(0x0059B5A0, (void*)h_spol_find, sizeof find_expect, find_expect)) return;
    if (!patch_jmp(0x0059B6D0, (void*)h_spol_load, sizeof load_expect, load_expect)) {
        patch_bytes(0x0059B5A0, find_expect, sizeof find_expect);
        return;
    }
    dsf_log("archive: 0059B6D0/0059B5A0 spool item lists (SSIC SPOL) hold up to %u items (vanilla %u)", g_max_entries,
            kSpolInline);
}

}  // namespace

// All or nothing: the replacements only work together.
static bool patch_readers()
{
    for (const Site& s : kSites) {
        if (!bytes_equal(s.va, s.expect, s.n)) {
            dsf_log("archive: %08X (%s) is not the vanilla code (another plugin?); CHNK readers stay at %u entries",
                    s.va, s.what, kInline);
            return false;
        }
    }
    const int nsites = (int)(sizeof kSites / sizeof kSites[0]);
    for (int i = 0; i < nsites; i++) {
        const Site& s = kSites[i];
        if (!patch_jmp(s.va, s.target, 5, s.expect)) {
            for (int j = 0; j < i; j++) patch_bytes(kSites[j].va, kSites[j].expect, 5);
            dsf_log("archive: patching %08X failed after the check passed; the %d sites before it are undone, "
                    "no reader patches", s.va, i);
            return false;
        }
        dsf_log("archive: %08X %s -> %p", s.va, s.what, s.target);
    }
    dsf_log("archive: CHNK readers hold up to %u entries (vanilla %u)", g_max_entries, kInline);
    return true;
}

// [archive] packages=1: car packages beside dngvehicles.sp (see "car packages"
// above). Three sites besides the reader functions: the spool loader's open
// call (to know the vehicle archive's reader), the SSLP load and the VSCT load.
// All or none; none when no package is listed. Bytes from `dsff xref dis`:
//   0x005A13C4  E8 67 B1 35 00   call 0x008FC530 (push 0; push 1; push edi = name; lea ecx, [esi+8])
//   0x005A0B50  8B 44 24 08 56   mov eax, [esp+8]; push esi  (vtable 0x00DE4D48 slot 0)
//   0x0086C630  8B 44 24 08 56   the same prologue           (vtable 0x00E3967C)
static const uint8_t kOpenExpect[] = {0xE8, 0x67, 0xB1, 0x35, 0x00};
static const uint8_t kSslpExpect[] = {0x8B, 0x44, 0x24, 0x08, 0x56};
static const uint8_t kVsctExpect[] = {0x8B, 0x44, 0x24, 0x08, 0x56};

static bool patch_package_sites()
{
    if (!check_bytes(0x005A13C4, kOpenExpect, 5) || !check_bytes(0x005A0B50, kSslpExpect, 5) ||
        !check_bytes(0x0086C630, kVsctExpect, 5)) {
        dsf_log("packages: 005A13C4/005A0B50/0086C630 are not the vanilla code; no car packages");
        return false;
    }
    if (!patch_call(0x005A13C4, (void*)h_spool_open, kOpenExpect)) return false;
    if (!patch_jmp(0x005A0B50, (void*)h_sslp_load, 5, kSslpExpect)) {
        patch_bytes(0x005A13C4, kOpenExpect, 5);
        return false;
    }
    if (!patch_jmp(0x0086C630, (void*)h_vsct_load, 5, kVsctExpect)) {
        patch_bytes(0x005A0B50, kSslpExpect, 5);
        patch_bytes(0x005A13C4, kOpenExpect, 5);
        return false;
    }
    dsf_log("packages: 005A13C4 spool open, 005A0B50 SSLP load, 0086C630 VSCT load hooked; %u car packages",
            g_pkg_count);
    g_pkg_sites = true;
    return true;
}

// Drops every package (handles closed): no list, or the sites didn't patch.
static void pkg_drop_all()
{
    for (uint32_t j = 0; j < g_pkg_count; j++) {
        if (g_pkgs[j].file && g_pkgs[j].file != INVALID_HANDLE_VALUE) CloseHandle(g_pkgs[j].file);
        if (g_pkgs[j].raw) CloseHandle(g_pkgs[j].raw);
    }
    g_pkg_count = 0;
    g_pkg_first = 0;
    g_veh_reader = nullptr;
    g_pkg_sites = false;
}

static void patch_packages()
{
    pkg_load_config();
    if (g_pkg_count && !patch_package_sites()) pkg_drop_all();
}

// ---- file open log ([archive] open_log, off by default) --------------------
// FUN_00857370(name, flags), cdecl, the file layer's open (18 call sites):
// starts with `mov eax, [0x0143D1C4]` (A1 C4 D1 43 01), the slot table's
// singleton. A jump there to h_file_open, which calls the original through a
// trampoline (those 5 bytes, then a jump to 0x00857375), logs every name the
// game opens this way: phase 3 of research/car-packages.md needs to know
// whether gui.sp, Fonts.fchunk and global.fchunk all go through it.
typedef int(__cdecl* FileOpen)(const char* name, int flags);
static FileOpen g_file_open = nullptr;
static volatile LONG g_open_logged = 0;

static void log_open(const char* name, int flags, int result)
{
    __try {
        dsf_log("open: '%s' flags %#x -> %d", name ? name : "(null)", flags, result);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        dsf_log("open: name %p (unreadable) flags %#x -> %d", name, flags, result);
    }
}

static int __cdecl h_file_open(const char* name, int flags)
{
    int result = g_file_open(name, flags);
    if (InterlockedIncrement(&g_open_logged) <= 4000) log_open(name, flags, result);
    return result;
}

static void patch_open_log()
{
    static const uint8_t expect[] = {0xA1, 0xC4, 0xD1, 0x43, 0x01};
    if (!check_bytes(0x00857370, expect, sizeof expect)) {
        dsf_log("archive: 00857370 is not the vanilla code; no open log");
        return;
    }
    uint8_t* t = (uint8_t*)alloc_exec(16);
    if (!t) return;
    memcpy(t, expect, 5);
    t[5] = 0xE9;
    *(int32_t*)(t + 6) = (int32_t)(0x00857375 - (uint32_t)(uintptr_t)(t + 10));
    g_file_open = (FileOpen)(void*)t;
    if (patch_jmp(0x00857370, (void*)h_file_open, 5, expect))
        dsf_log("archive: 00857370 file open -> %p (open_log: every name is logged, up to 4000)", (void*)h_file_open);
}

// ---- main loop frame times ([archive] read_log, off by default) -----------
// FUN_0085DAD0 is the game window's message pump: PeekMessageA, GetMessageA,
// TranslateMessage and DispatchMessageA until the queue is empty (thiscall,
// two stack arguments, `ret 8`). Nothing calls it directly; it is slot 9 of the
// vtable at 0x00E37218 (0x00E3723C holds 0x0085DAD0). Windows hands a window's
// messages only to the thread that created it, so the thread that runs this
// pump is the window's thread, the game's main thread. The interval between
// two calls is one turn of the main loop, so a read on the main thread that
// stalls it lengthens that interval by the read's time.
// h_pump records the thread, a histogram of the intervals (0.1 ms bins), a
// summary every 10 s, every interval with a package read in it (with the one
// before, for comparison) and the first 200 intervals of 50 ms or more.
typedef uint32_t(__fastcall* Pump)(void* self, void*, void* a, void* b);
const uint32_t kPumpSlot = 0x00E3723C;
const uint32_t kPumpFn = 0x0085DAD0;
const uint32_t kBins = 2000;  // 0.1 ms each, up to 200 ms; longer ones in the last
uint32_t g_frame_bins[kBins];
uint32_t g_frame_count = 0;
uint32_t g_frame_max_us = 0;
LARGE_INTEGER g_frame_last = {};
LARGE_INTEGER g_frame_period = {};
uint32_t g_frame_prev_us = 0;
LONG g_frame_spikes = 0;
bool g_pump_other_logged = false;

uint32_t frame_quantile(uint32_t q_permille)
{
    uint64_t want = ((uint64_t)g_frame_count * q_permille + 999) / 1000, seen = 0;
    for (uint32_t b = 0; b < kBins; b++) {
        seen += g_frame_bins[b];
        if (seen >= want && want) return b * 100 + 50;  // the bin's centre, in us
    }
    return 0;
}

// Wwise's memory pools, sampled every frame and logged with each 10 s summary. AK::MemoryMgr is
// linked in: the pool table at *0x01D72390, 0x4C bytes per pool ({blocks, block size, ...} at +0,
// used bytes at +0x44, kept by Malloc 0x00C99A40 and Free 0x00C99BA0; the size it manages at
// +0x48, as GetPoolMemoryUsed 0x00C99B60 reads them), the pool count at 0x01D7238C. Release
// builds name no pool (GetPoolName returns null), so the summary gives each its size.
const uint32_t kAkPoolTable = 0x01D72390;
const uint32_t kAkPoolCount = 0x01D7238C;
const uint32_t kAkPoolsLogged = 16;
uint32_t g_ak_peak[kAkPoolsLogged];

void wwise_sample()
{
    uint8_t* table = *(uint8_t**)kAkPoolTable;
    int n = *(int*)kAkPoolCount;
    if (!table) return;
    for (int i = 0; i < n && i < (int)kAkPoolsLogged; i++) {
        uint32_t used = *(uint32_t*)(table + i * 0x4C + 0x44);
        if (used > g_ak_peak[i]) g_ak_peak[i] = used;
    }
}

void wwise_summary()
{
    uint8_t* table = *(uint8_t**)kAkPoolTable;
    int n = *(int*)kAkPoolCount;
    if (!table) return;
    char line[1024] = "";
    int len = 0;
    for (int i = 0; i < n && i < (int)kAkPoolsLogged && len < (int)sizeof(line) - 64; i++) {
        uint8_t* e = table + i * 0x4C;
        if (!*(uint32_t*)e) continue;  // a free entry
        len += _snprintf_s(line + len, sizeof(line) - len, _TRUNCATE, "%s%d: %u/%u/%u", len ? ", " : "", i,
                           *(uint32_t*)(e + 0x44) / 1024, g_ak_peak[i] / 1024, *(uint32_t*)(e + 0x48) / 1024);
    }
    dsf_log("wwise: pools (used/peak/size KiB) %s", line);
}

void frame_summary(const LARGE_INTEGER& now)
{
    uint32_t over25 = 0, over33 = 0, over50 = 0;
    for (uint32_t b = 250; b < kBins; b++) {
        over25 += g_frame_bins[b];
        if (b >= 333) over33 += g_frame_bins[b];
        if (b >= 500) over50 += g_frame_bins[b];
    }
    uint32_t p50 = frame_quantile(500), p95 = frame_quantile(950), p99 = frame_quantile(990);
    dsf_log("frames: %u in %u ms (at %u ms): median %u.%u ms, p95 %u.%u, p99 %u.%u, max %u.%03u; "
            "%u of 25 ms or more, %u of 33.3, %u of 50; %ld package reads so far",
            g_frame_count, qpc_us(g_frame_period, now) / 1000, qpc_us(g_qpc0, now) / 1000, p50 / 1000,
            p50 % 1000 / 100, p95 / 1000, p95 % 1000 / 100, p99 / 1000, p99 % 1000 / 100, g_frame_max_us / 1000,
            g_frame_max_us % 1000, over25, over33, over50, g_pkg_reads);
    wwise_summary();
    memset(g_frame_bins, 0, sizeof g_frame_bins);
    g_frame_count = 0;
    g_frame_max_us = 0;
    g_frame_period = now;
}

uint32_t __fastcall h_pump(void* self, void* edx, void* a, void* b)
{
    LARGE_INTEGER now;
    QueryPerformanceCounter(&now);
    DWORD tid = GetCurrentThreadId();
    if (!g_main_tid) {
        g_main_tid = tid;
        dsf_log("frames: main thread %lu (the window message pump, FUN_0085DAD0)", tid);
    } else if (tid != g_main_tid && !g_pump_other_logged) {
        g_pump_other_logged = true;
        dsf_log("frames: the pump also runs on thread %lu (main %lu)", tid, g_main_tid);
    }
    if (tid == g_main_tid) {
        wwise_sample();
        if (g_frame_last.QuadPart) {
            uint32_t us = qpc_us(g_frame_last, now);
            uint32_t bin = us / 100;
            g_frame_bins[bin < kBins ? bin : kBins - 1]++;
            g_frame_count++;
            if (us > g_frame_max_us) g_frame_max_us = us;
            LONG reads = InterlockedExchange(&g_frame_reads, 0);
            LONG read_us = InterlockedExchange(&g_frame_read_us, 0);
            LONG main_us = InterlockedExchange(&g_frame_main_us, 0);
            if (reads)
                dsf_log("frame: %u.%03u ms with %ld package reads (%ld.%03ld ms, %ld.%03ld ms of them on the main "
                        "thread); the frame before %u.%03u ms",
                        us / 1000, us % 1000, reads, read_us / 1000, read_us % 1000, main_us / 1000, main_us % 1000,
                        g_frame_prev_us / 1000, g_frame_prev_us % 1000);
            else if (us >= 50000 && g_frame_spikes++ < 200)
                dsf_log("frame: %u.%03u ms, no package read (at %u ms)", us / 1000, us % 1000,
                        qpc_us(g_qpc0, now) / 1000);
            g_frame_prev_us = us;
        } else {
            g_frame_period = now;
        }
        if (qpc_us(g_frame_period, now) >= 10000000) frame_summary(now);
        g_frame_last = now;
    }
    uint32_t r = ((Pump)(uintptr_t)kPumpFn)(self, edx, a, b);
    // The pump's own time (messages) belongs to the frame too; the next
    // interval starts at the call, so nothing is lost.
    return r;
}

void patch_frame_log()
{
    uint32_t want = kPumpFn;
    if (!check_bytes(kPumpSlot, &want, 4)) {
        dsf_log("archive: 00E3723C is not the message pump's vtable slot; no frame times");
        return;
    }
    void* target = (void*)h_pump;
    if (patch_bytes(kPumpSlot, &target, 4, &want))
        dsf_log("archive: 00E3723C message pump (FUN_0085DAD0) -> %p: frame times and the main thread are logged",
                target);
}

// ---- the slot count the game will have -----------------------------------------
// What apply_vehicle_slots will give, known before the game reads anything: 35 unless
// every patch of the slot raise landed at init (g_vehicle_slots) and the vehicle
// archive's SSLP header (Install\dngvehicles.sp: root -> SSIC -> NONR -> SSLP, as
// dsfforza.game.vehicle_limits reads it) has the vanilla 35 kind-0 slots. Once the SSLP
// is loaded, what was applied. apply_vehicle_slots' other check (the ID pools unused)
// can't fail before a vehicle has loaded.
int g_archive_kind0 = -1;  // the archive's kind-0 byte; -1 not read yet, 0 unreadable

static bool file_read_at(HANDLE f, uint32_t off, void* buf, DWORD n)
{
    LARGE_INTEGER at;
    at.QuadPart = off;
    DWORD got = 0;
    return SetFilePointerEx(f, at, nullptr, FILE_BEGIN) && ReadFile(f, buf, n, &got, nullptr) && got == n;
}

// The file offset of child `tag` of the CHNK at file offset `base` (0: none).
static uint32_t file_chnk_child(HANDLE f, uint32_t base, uint32_t tag)
{
    uint32_t hdr[4];
    if (!file_read_at(f, base, hdr, sizeof hdr) || hdr[0] != kCHNK || hdr[2] > 65536) return 0;
    uint32_t n = hdr[2];
    Entry* e = (Entry*)HeapAlloc(GetProcessHeap(), 0, n * sizeof(Entry) + 1);
    uint32_t found = 0;
    if (e && file_read_at(f, base + 16, e, n * sizeof(Entry)))
        for (uint32_t i = 0; i < n && !found; i++)
            if (e[i].tag == tag) found = base + e[i].offset;
    if (e) HeapFree(GetProcessHeap(), 0, e);
    return found;
}

// The kind-0 slot byte of an archive's SSLP header (0: no such file or chunk).
int archive_kind0_slots(const char* archive)
{
    HANDLE f = CreateFileA(archive, GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    if (f == INVALID_HANDLE_VALUE) return 0;
    uint8_t header[0x1C];
    uint32_t ssic = file_chnk_child(f, 0, tag4('S', 'S', 'I', 'C'));
    uint32_t nonr = ssic ? file_chnk_child(f, ssic, tag4('N', 'O', 'N', 'R')) : 0;
    uint32_t sslp = nonr ? file_chnk_child(f, nonr, tag4('S', 'S', 'L', 'P')) : 0;
    int kind0 = sslp && file_read_at(f, sslp, header, sizeof header) ? header[0x18] : 0;
    CloseHandle(f);
    return kind0;
}

uint32_t slots_expected()
{
    if (g_slots_done) return g_slots_applied ? g_slots_applied : kVanillaVehicleSlots;
    if (!g_vehicle_slots) return kVanillaVehicleSlots;
    if (g_archive_kind0 < 0) {
        char path[MAX_PATH];
        DWORD n = GetModuleFileNameA(nullptr, path, MAX_PATH);
        char* slash = n && n < MAX_PATH ? strrchr(path, '\\') : nullptr;
        g_archive_kind0 = 0;
        if (slash) {
            strcpy_s(slash + 1, MAX_PATH - (size_t)(slash + 1 - path), "Install\\dngvehicles.sp");
            g_archive_kind0 = archive_kind0_slots(path);
        }
        if (g_archive_kind0 != (int)kVanillaVehicleSlots)
            dsf_log("slots: the vehicle archive's SSLP has %d kind-0 slots (0: unreadable), not 35; the raise "
                    "won't apply", g_archive_kind0);
    }
    return g_archive_kind0 == (int)kVanillaVehicleSlots ? g_vehicle_slots : kVanillaVehicleSlots;
}

// ---- file redirection ([archive] packages: the manager's generated files) --
// The car manager writes copies of gui.sp, Fonts.fchunk and each language's
// global.fchunk with the enabled packages' pictures, glyphs and names, and a
// table of them (redirect_list, "game path|replacement" per line, both
// relative to the game folder). The game's own files are never written.
// Every open through the file layer (FUN_00857370 -> 0x00857270 -> 0x0085ACD0
// -> 0x0085AB80) resolves its device path first (0x0085B070: "TEXT:global.fchunk"
// becomes "Locale\<language>\global.fchunk") and then calls CreateFileA at
// 0x0085AC1E (`FF 15 60 92 DC 00`, call [0x00DC9260], the import of
// KERNEL32.CreateFileA; GetFileSizeEx follows). A call to h_create_file there
// (E8 rel32 + NOP) opens the replacement for a listed path, compared without
// case and without a leading ".\" or the game folder, so devices and languages
// need nothing of their own. A replacement that won't open falls back to the
// game's file (logged). Phase 1's open log showed gui.sp (twice), fonts.fchunk
// and TEXT:global.fchunk all going through FUN_00857370.
// Every other name goes on through Driver.exe's import (the slot the vanilla
// call reads, at the time of the call), not to KERNEL32 directly: DSF Enhanced's
// [Files] Redirect hooks that import for its own tables (worldlod's world
// archive, san_francisco.dngc, opens here too), so both plugins redirect in
// either load order. "archive.redirect_chain" in DSF_CAPABILITIES says so.
typedef HANDLE(WINAPI* CreateFileAFn)(LPCSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE);
static CreateFileAFn* const kCreateFileImport = (CreateFileAFn*)0x00DC9260;
// The car manager writes the traffic sets' AI copy (media\san_franciscoAI.fchunk ->
// dsfcars\traffic\san_franciscoAI.fchunk) for a slot count and records the count beside it
// (<replacement>.slots, src/dsfforza/build/traffic.py). Sets past what the game holds
// leave a mission pot no room and the chapter load never ends (research/traffic.md, "The
// spool slot limit"). So a replacement with a .slots record is used only while
// slots_expected() reaches it, and the AI copy without one only while the slots are raised
// at all; otherwise the game reads its own file (logged once).
const char kAiFile[] = "media\\san_franciscoai.fchunk";
struct Redirect {
    char from[MAX_PATH];  // normalised game-relative path
    char to[MAX_PATH];    // full path of the replacement
    uint32_t min_slots;   // vehicle slots it needs (0: any)
    bool refused_logged;
};
static Redirect* g_redirects = nullptr;
static uint32_t g_redirect_count = 0;
static char g_redirect_base[MAX_PATH];  // game folder, lower-case, trailing backslash
static volatile LONG g_redirected = 0;

// Lower-case, '/' -> '\', without a leading ".\" or the game folder.
static void redirect_key(const char* in, char* out, size_t n)
{
    char tmp[MAX_PATH];
    size_t i = 0;
    for (; in[i] && i + 1 < sizeof tmp; i++) {
        char c = in[i] == '/' ? '\\' : in[i];
        tmp[i] = (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
    }
    tmp[i] = 0;
    const char* s = tmp;
    size_t base = strlen(g_redirect_base);
    if (base && strncmp(s, g_redirect_base, base) == 0) s += base;
    while (s[0] == '.' && s[1] == '\\') s += 2;
    strncpy_s(out, n, s, _TRUNCATE);
}

// Reads the table (base: the game folder with a trailing backslash). A line whose
// replacement doesn't exist is left out (logged). Returns the number kept.
static uint32_t pkg_load_redirects(const char* base, const char* list)
{
    g_redirect_count = 0;
    g_redirect_base[0] = 0;  // nothing to strip while the folder itself is normalised
    redirect_key(base, g_redirect_base, sizeof g_redirect_base);
    FILE* f = nullptr;
    if (fopen_s(&f, list, "r") || !f) return 0;
    const uint32_t kMax = 64;
    if (!g_redirects) g_redirects = (Redirect*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, kMax * sizeof(Redirect));
    char line[2 * MAX_PATH + 8];
    while (g_redirects && g_redirect_count < kMax && fgets(line, sizeof line, f)) {
        line[strcspn(line, "\r\n")] = 0;
        if (!line[0] || line[0] == '#') continue;
        char* bar = strchr(line, '|');
        if (!bar || bar == line || !bar[1]) {
            dsf_log("redirect: '%s' is not \"game path|replacement\"; left out", line);
            continue;
        }
        *bar = 0;
        Redirect* r = &g_redirects[g_redirect_count];
        redirect_key(line, r->from, sizeof r->from);
        _snprintf_s(r->to, sizeof r->to, _TRUNCATE, "%s%s", base, bar + 1);
        if (GetFileAttributesA(r->to) == INVALID_FILE_ATTRIBUTES) {
            dsf_log("redirect: %s -> %s: no such file; left out", line, r->to);
            continue;
        }
        r->min_slots = 0;
        r->refused_logged = false;
        char rec[MAX_PATH + 8];
        _snprintf_s(rec, sizeof rec, _TRUNCATE, "%s.slots", r->to);
        FILE* sf = nullptr;
        if (!fopen_s(&sf, rec, "r") && sf) {
            unsigned v = 0;
            if (fscanf_s(sf, "%u", &v) == 1) r->min_slots = v;
            fclose(sf);
        }
        if (!r->min_slots && strcmp(r->from, kAiFile) == 0) r->min_slots = kVanillaVehicleSlots + 1;
        if (r->min_slots)
            dsf_log("redirect: %s -> %s (for %u vehicle slots)", line, bar + 1, r->min_slots);
        else
            dsf_log("redirect: %s -> %s", line, bar + 1);
        g_redirect_count++;
    }
    fclose(f);
    return g_redirect_count;
}

// The replacement for a path the file layer opens, or nullptr.
static const char* redirect_of(const char* name)
{
    if (!name || !g_redirect_count) return nullptr;
    char key[MAX_PATH];
    redirect_key(name, key, sizeof key);
    for (uint32_t i = 0; i < g_redirect_count; i++) {
        Redirect* r = &g_redirects[i];
        if (strcmp(key, r->from) != 0) continue;
        if (r->min_slots) {
            uint32_t have = slots_expected();
            if (have < r->min_slots) {
                if (!r->refused_logged) {
                    r->refused_logged = true;
                    dsf_log("redirect: %s not used: written for %u vehicle slots, the game has %u; '%s' as it is",
                            r->to, r->min_slots, have, name);
                }
                return nullptr;
            }
            if (strcmp(r->from, kAiFile) == 0) g_ai_copy_served = true;
        }
        return r->to;
    }
    return nullptr;
}

static HANDLE WINAPI h_create_file(LPCSTR name, DWORD access, DWORD share, LPSECURITY_ATTRIBUTES sa, DWORD disposition,
                                   DWORD flags, HANDLE templ)
{
    const char* to = redirect_of(name);
    if (to) {
        HANDLE h = CreateFileA(to, access, share, sa, disposition, flags, templ);
        if (h != INVALID_HANDLE_VALUE) {
            if (InterlockedIncrement(&g_redirected) <= 256) dsf_log("redirect: '%s' opened as %s", name, to);
            return h;
        }
        dsf_log("redirect: %s won't open (error %lu); '%s' as it is", to, GetLastError(), name);
    }
    return (*kCreateFileImport)(name, access, share, sa, disposition, flags, templ);
}

static const uint8_t kCreateFileExpect[] = {0xFF, 0x15, 0x60, 0x92, 0xDC, 0x00};  // call [0x00DC9260]

static bool patch_redirect_site()
{
    if (!check_bytes(0x0085AC1E, kCreateFileExpect, sizeof kCreateFileExpect)) {
        dsf_log("redirect: 0085AC1E is not the vanilla code; the game's own GUI files are used");
        return false;
    }
    uint8_t code[6] = {0xE8, 0, 0, 0, 0, 0x90};
    *(int32_t*)(code + 1) = (int32_t)((uint32_t)(uintptr_t)h_create_file - (0x0085AC1E + 5));
    if (!patch_bytes(0x0085AC1E, code, sizeof code, kCreateFileExpect)) return false;
    dsf_log("redirect: 0085AC1E CreateFileA -> %p; %u files replaced", (void*)h_create_file, g_redirect_count);
    return true;
}

// [archive] redirect_list, relative to the game folder; patched only when it lists a file.
static void patch_redirects()
{
    char base[MAX_PATH];
    DWORD n = GetModuleFileNameA(nullptr, base, MAX_PATH);
    if (!n || n >= MAX_PATH) return;
    char* slash = strrchr(base, '\\');
    if (slash) slash[1] = 0;
    char rel[MAX_PATH], list[MAX_PATH];
    cfg_str("archive", "redirect_list", "dsfcars\\generated\\redirect.txt", rel, sizeof rel);
    _snprintf_s(list, sizeof list, _TRUNCATE, "%s%s", base, rel);
    if (pkg_load_redirects(base, list)) patch_redirect_site();
}

void mod_archive_init()
{
    int max_entries = cfg_int("archive", "max_entries", 16384);
    if (max_entries < (int)kInline) max_entries = kInline;
    if (max_entries > 1 << 20) max_entries = 1 << 20;
    g_max_entries = (uint32_t)max_entries;
    QueryPerformanceFrequency(&g_qpf);
    QueryPerformanceCounter(&g_qpc0);
    g_read_log = cfg_bool("archive", "read_log", false);
    g_read_uncached = cfg_bool("archive", "read_uncached", false);
    g_async_reads = cfg_bool("archive", "async_reads", true);
    bool readers = patch_readers();
    if (cfg_bool("archive", "spool_lists", true)) patch_spool_lists();
    if (cfg_bool("archive", "packages", false)) {
        if (readers)
            patch_packages();
        else
            dsf_log("packages: need the reader patches; no car packages");
        patch_redirects();
    }
    if (cfg_bool("archive", "open_log", false)) patch_open_log();
    if (g_read_log) patch_frame_log();

    // Vehicle spool slots (see "vehicle spool slots"): 35 = vanilla, no patch; at most 60, what the slot
    // handler arrays hold (64 - the other kinds' 2 + 2). 50 (Chapter 7's set 36 models) passed Chapter 7 and a
    // mission start every time (runs vm1-20261108-022429, -022818), but crashed a free-roam session (#586), so
    // the default is 35 again; with 60 a 40-model set crashed in 2 of 4 runs at 0x00AB607F (#566).
    g_slots_trace = cfg_bool("archive", "slots_trace", false);
    int slots = cfg_int("archive", "vehicle_slots", (int)kDefaultVehicleSlots);
    if (slots > (int)kVanillaVehicleSlots && !patch_obj_alloc())
        dsf_log("slots: 00869D50 is not the vanilla object pool allocator; no overflow log");
    if (g_slots_trace && patch_call(0x005A1491, (void*)h_trace_make_slot, kMakeSlotCallExpect))
        dsf_log("slots_trace: 005A1491 traced");
    if (slots > (int)kVanillaVehicleSlots && cfg_bool("archive", "slots_fail", false)) {
        dsf_log("slots: slots_fail=1 (a test: as if a patch site weren't the vanilla code); vehicle slots stay 35");
        slots = (int)kVanillaVehicleSlots;
    }
    if (slots > (int)kVanillaVehicleSlots) {
        if (slots > (int)kSlotHandlers - 4) slots = (int)kSlotHandlers - 4;  // the other kinds' 2 + 2 slots
        bool hooked = g_pkg_sites;  // the packages' SSLP hook calls apply_vehicle_slots too
        if (!hooked && check_bytes(0x005A0B50, kSslpExpect, 5))
            hooked = g_sslp_hooked = patch_jmp(0x005A0B50, (void*)h_sslp_load, 5, kSslpExpect);
        if (!hooked)
            dsf_log("slots: 005A0B50 is not the vanilla SSLP load; vehicle slots stay 35");
        else if (!(patch_wrap_lists() && patch_pool45() && patch_acquire_7a() && patch_modl_records() && patch_bank_list())) {  // they only act past their inline room
            if (g_sslp_hooked) {
                patch_bytes(0x005A0B50, kSslpExpect, 5);
                g_sslp_hooked = false;
            }
        } else if (patch_pool16((uint32_t)slots) && patch_ak_default_pool((uint32_t)slots) &&
                   patch_vehicle_pool((uint32_t)slots))
            g_vehicle_slots = (uint32_t)slots;
        else if (g_sslp_hooked) {
            patch_bytes(0x005A0B50, kSslpExpect, 5);
            g_sslp_hooked = false;
        }
    }

    // Files past 2 GiB. The file layer's seek (0x0085A970) clamps a position
    // with the sign bit set to 0 (`CMP [ECX+0x30],0; JGE`), so every entry of
    // an archive that starts at 2 GiB or later is read from the start of the
    // file instead (a 2.6 GB archive hung on the loading screen: SSIC came back
    // as the root header). Everything else there is unsigned: GetFileSizeEx low
    // part rounded to 2 KiB at +0x28, OVERLAPPED.Offset = pos & ~0x7FF with
    // OffsetHigh 0. JGE -> JMP lets positions run to 4 GiB - 2 KiB; a position
    // past the end is still clamped to the file size just below.
    if (cfg_bool("archive", "large_files", true)) {
        static const uint8_t expect[] = {0x83, 0x79, 0x30, 0x00, 0x7D, 0x07};
        static const uint8_t jmp[] = {0xEB};
        if (bytes_equal(0x0085A9AE, expect, sizeof expect) && patch_bytes(0x0085A9B2, jmp, 1, expect + 4))
            dsf_log("archive: 0085A9B2 file seek takes positions up to 4 GiB (JGE -> JMP)");
        else
            dsf_log("archive: 0085A9AE file seek is not the vanilla code; archives must stay below 2 GiB");
    }
}

// ---- what Lua is told (shared.h; mod_garage's magic query) ------------------
// True when [archive] packages is on, its sites are hooked and at least one
// package was kept, and no injection or SSLP copy has failed since. register.lua
// registers the cars of packages.lua only then (#511).
bool archive_packages_active()
{
    bool active = g_pkg_sites && g_pkg_count && !g_pkg_failed;
    static bool logged = false;
    if (!logged) {
        logged = true;
        dsf_log("packages: Lua asks whether the car packages are loaded: %s (%u kept, hooked %d, failed %d, "
                "vehicle SSLP extended %d)", active ? "yes" : "no", g_pkg_count, g_pkg_sites, g_pkg_failed,
                g_sslp_done);
    }
    return active;
}

// True when the package listed for model `id` was kept at start and, once the
// vehicle SSLP is loaded, got its records there (no ID clash, NONR fits).
bool archive_package_loaded(int id)
{
    if (!(g_pkg_sites && g_pkg_count && !g_pkg_failed)) return false;
    for (uint32_t j = 0; j < g_pkg_count; j++)
        if (g_pkgs[j].id == id) return !g_sslp_done || g_pkgs[j].listed;
    return false;
}

// ---- offline selftest of the car packages (tools/asi/test/offline.cpp) ------
// Writes small packages into dir, loads a list of good and bad lines, injects
// them into fake vehicle readers (one below 1100 entries, one crossing it into
// the side table), reads entries and names back, runs the SSLP and VSCT hooks
// on fake tables, and patches the three sites (after a changed byte at the last
// one leaves all three vanilla). Returns the number of failed checks.
namespace {

int g_st_fail = 0;
#define ST_CHECK(c)                                                    \
    do {                                                               \
        if (!(c)) {                                                    \
            printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c);          \
            g_st_fail++;                                               \
        }                                                              \
    } while (0)

// Stands in for another plugin's hook in Driver.exe's CreateFileA import (DSF
// Enhanced's [Files] Redirect): remembers the name it was given.
char g_st_next_name[MAX_PATH];
HANDLE WINAPI st_next_create_file(LPCSTR name, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE)
{
    strncpy_s(g_st_next_name, sizeof g_st_next_name, name, _TRUNCATE);
    SetLastError(ERROR_FILE_NOT_FOUND);
    return INVALID_HANDLE_VALUE;
}

enum StFlaw { kGood, kNotChnk, kNoVsct, kCut, kBadKid };

// A package: CHNK header, 4 entries; VEHC k at 0x100 + 0x100 * k (0x80 bytes: a
// CHNK of one NONR of nonr bytes at +0x20, then the name "Pnnk"), VSCT at 0x400
// (3 records of 28 bytes; first u16 205, the host).
void st_write(const char* path, int tag_id, uint32_t nonr, StFlaw flaw)
{
    static uint8_t buf[0x500];
    memset(buf, 0, sizeof buf);
    uint32_t* h = (uint32_t*)buf;
    h[0] = flaw == kNotChnk ? 0x12345678 : kCHNK;
    h[1] = sizeof buf;
    h[2] = flaw == kNoVsct ? 3 : 4;
    h[3] = 3;
    Entry* e = (Entry*)(buf + 16);
    for (int k = 0; k < 3; k++) {
        e[k].tag = kVEHC;
        e[k].offset = 0x100 + 0x100 * k;
        e[k].version = 1;
        e[k].name_len = 4;
        e[k].align_log2 = 4;
        e[k].size = 0x80;
        uint32_t* v = (uint32_t*)(buf + e[k].offset);
        v[0] = kCHNK;
        v[1] = 0x80;
        v[2] = 1;
        v[3] = 3;
        Entry* kid = (Entry*)(v + 4);
        kid->tag = kNONR;
        kid->offset = 0x20;
        kid->size = flaw == kBadKid && k == 1 ? 0x100 : nonr;
        v[8] = 0xC0DE0000u | (uint32_t)(tag_id * 4 + k);  // marker at the NONR's start
        char* name = (char*)buf + e[k].offset + 0x80;
        name[0] = 'P';
        name[1] = (char)('0' + tag_id / 10 % 10);
        name[2] = (char)('0' + tag_id % 10);
        name[3] = (char)('0' + k);
    }
    if (flaw != kNoVsct) {
        e[3].tag = kVSCT;
        e[3].offset = 0x400;
        e[3].align_log2 = 4;
        e[3].size = 84;
        for (int k = 0; k < 3; k++) {
            *(uint16_t*)(buf + 0x400 + 28 * k) = 205;
            buf[0x400 + 28 * k + 0x1B] = (uint8_t)(0x10 + k);
        }
    }
    FILE* f = nullptr;
    if (fopen_s(&f, path, "wb") || !f) return;
    fwrite(buf, 1, flaw == kCut ? 0x250 : sizeof buf, f);
    fclose(f);
}

void st_write_list(const char* path, const char* text)
{
    FILE* f = nullptr;
    if (fopen_s(&f, path, "wb") || !f) return;
    fputs(text, f);
    fclose(f);
}

// A CHNK of n children at buf + at, each child `size` bytes after the table, in
// order; returns the child's offset (relative to this CHNK) for each.
uint32_t st_chnk(uint8_t* buf, uint32_t at, uint32_t n, const uint32_t* tags, const uint32_t* sizes, uint32_t* offs)
{
    uint32_t* h = (uint32_t*)(buf + at);
    h[0] = kCHNK;
    h[2] = n;
    h[3] = 3;
    Entry* e = (Entry*)(buf + at + 16);
    uint32_t pos = 16 + 16 * n;
    for (uint32_t j = 0; j < n; j++) {
        e[j].tag = tags[j];
        e[j].offset = pos;
        e[j].size = sizes[j];
        e[j].align_log2 = 4;
        offs[j] = pos;
        pos += (sizes[j] + 15) & ~15u;
    }
    h[1] = pos;
    return pos;
}

// A full VEHC as the roster's are (the 573 packages: NONR/VSRT/VSVH, NONR/VO3D,
// NONR/UPVH, NONR/SPSR/SPID): an MDXN leaf, then the NONR CHNK. Returns its size.
uint32_t st_vehc(uint8_t* buf, uint32_t id)
{
    memset(buf, 0, 0x400);
    uint32_t top_tags[2] = {tag4('M', 'D', 'X', 'N'), kNONR}, top_sizes[2] = {0x20, 0x200}, top[2];
    uint32_t size = st_chnk(buf, 0, 2, top_tags, top_sizes, top);
    uint32_t nonr_tags[4] = {tag4('V', 'S', 'R', 'T'), tag4('V', 'O', '3', 'D'), tag4('U', 'P', 'V', 'H'),
                             tag4('S', 'P', 'S', 'R')};
    uint32_t nonr_sizes[4] = {0x40, 0x10, 0x20, 0x40}, nonr[4];
    st_chnk(buf, top[1], 4, nonr_tags, nonr_sizes, nonr);
    uint32_t vsvh_tag = tag4('V', 'S', 'V', 'H'), vsvh_size = 0x10, spid_tag = tag4('S', 'P', 'I', 'D'), off;
    st_chnk(buf, top[1] + nonr[0], 1, &vsvh_tag, &vsvh_size, &off);
    *(uint32_t*)(buf + top[1] + nonr[0] + off) = id;
    *(uint32_t*)(buf + top[1] + nonr[1]) = id;
    *(uint32_t*)(buf + top[1] + nonr[2] + 0x14) = id;
    *(uint32_t*)(buf + top[1] + nonr[2]) = 0x1234;  // UPVH +0 is not the ID
    st_chnk(buf, top[1] + nonr[3], 1, &spid_tag, &vsvh_size, &off);
    *(uint32_t*)(buf + top[1] + nonr[3] + off) = id;
    *(uint32_t*)(buf + top[0]) = id;  // an MDXN word equal to the ID is not a field
    return size;
}

// pkg_set_ids on a VEHC like the roster's: the four fields get the listed ID,
// whatever the file had; nothing else changes; kinds 1 and 2 are left alone.
void st_set_ids()
{
    static uint8_t buf[0x400], before[0x400];
    Pkg p = {};
    p.id = 701;
    uint32_t size = st_vehc(buf, 1);
    memcpy(before, buf, sizeof buf);
    ST_CHECK(pkg_set_ids(&p, 0, buf, size) == 4);
    uint32_t changed = 0, wrong = 0;
    for (uint32_t i = 0; i + 4 <= sizeof buf; i += 4)
        if (*(uint32_t*)(buf + i) != *(uint32_t*)(before + i)) {
            changed++;
            if (*(uint32_t*)(buf + i) != 701 || *(uint32_t*)(before + i) != 1) wrong++;
        }
    ST_CHECK(changed == 4 && wrong == 0);
    ST_CHECK(*(uint32_t*)(buf + 0x30) == 1);  // the MDXN payload
    // A file that holds the listed ID already (an install that patched it): unchanged.
    memcpy(before, buf, sizeof buf);
    ST_CHECK(pkg_set_ids(&p, 0, buf, size) == 4 && memcmp(before, buf, sizeof buf) == 0);
    // Two packages with the same builder ID: each gets its own.
    Pkg q = {};
    q.id = 702;
    static uint8_t buf2[0x400];
    st_vehc(buf2, 1);
    ST_CHECK(pkg_set_ids(&q, 0, buf2, size) == 4);
    ST_CHECK(*(uint32_t*)(buf2 + 0x30) == 1 && memcmp(buf, buf2, sizeof buf) != 0);
    // The interior and exterior: nothing written.
    st_vehc(buf2, 1);
    memcpy(before, buf2, sizeof buf2);
    ST_CHECK(pkg_set_ids(&q, 1, buf2, size) == 0 && pkg_set_ids(&q, 2, buf2, size) == 0);
    ST_CHECK(memcmp(before, buf2, sizeof buf2) == 0);
    // A child past its CHNK, a table past the buffer: skipped, no write out of bounds.
    st_vehc(buf2, 1);
    Entry* top = (Entry*)(buf2 + 16);
    top[1].size = 0x10000;  // the NONR runs past the VEHC
    ST_CHECK(pkg_set_ids(&q, 0, buf2, size) == 0);
    st_vehc(buf2, 1);
    *(uint32_t*)(buf2 + 8) = 0x10000;  // more entries than the VEHC holds
    ST_CHECK(pkg_set_ids(&q, 0, buf2, size) == 0);
    // Fields that disagree are all written (and logged). VO3D: NONR at 0x50, VO3D at +0x90.
    st_vehc(buf2, 1);
    ST_CHECK(*(uint32_t*)(buf2 + 0x50 + 0x90) == 1);
    *(uint32_t*)(buf2 + 0x50 + 0x90) = 3;
    ST_CHECK(pkg_set_ids(&q, 0, buf2, size) == 4 && *(uint32_t*)(buf2 + 0x50 + 0x90) == 702);
}

// A fake vehicle spool object: self = spool object + 4 (the SSLP hook's this),
// the root reader embedded at self + 0x10, the SSLP pointers at +0x72F4/+0x72F8.
struct StSpool {
    uint8_t* self;
    Reader* root;
};

StSpool st_spool()
{
    StSpool s;
    s.self = (uint8_t*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, 0x7400);
    s.root = (Reader*)(s.self + 0x10);
    return s;
}

// Runs inject, reads, SSLP and VSCT on one fake archive of base root entries.
void st_archive(uint32_t base)
{
    StSpool sp = st_spool();
    Reader* r = sp.root;
    if (base > kInline) ext_ensure(r, base, 0);
    r->mode = 1;
    r->count = base;
    r->ptr_count = base;
    g_veh_reader = r;
    g_pkg_first = 0;
    g_pkg_failed = false;
    g_sslp_done = false;
    g_pkg_sites = true;  // as patch_package_sites leaves it
    pkg_inject(r);
    ST_CHECK(g_pkg_first == base && r->count == base + 3 * g_pkg_count && r->ptr_count == r->count);
    // Packages in list order: 700, 701, 706 (NONR too big), 266 (clash), 707.
    ST_CHECK(g_pkg_count == 5);
    uint32_t kind = 9;
    Pkg* p = pkg_of(r, base + 4, &kind);
    ST_CHECK(p == &g_pkgs[1] && kind == 1 && p->id == 701);
    ST_CHECK(pkg_of(r, base - 1, nullptr) == nullptr && pkg_of(r, base + 3 * g_pkg_count, nullptr) == nullptr);
    Reader other = {};
    ST_CHECK(pkg_of(&other, base + 4, nullptr) == nullptr);
    Entry* e = E(r, base + 4);
    ST_CHECK(e->tag == kVEHC && e->offset == 0x200 && e->size == 0x80);
    ST_CHECK(*P(r, base + 4) == nullptr);
    // read entry: the sector-aligned size, the tail zeroed
    static uint8_t buf[0x800];
    memset(buf, 0xAA, sizeof buf);
    uint32_t got = pkg_read(p, e, buf, 0x800);
    ST_CHECK(got == 0x300 && buf[0x7FF] == 0 && *(uint32_t*)buf == kCHNK && *(uint32_t*)(buf + 0x20) == (0xC0DE0000u | (1 * 4 + 1)));
    ST_CHECK(buf[0x80] == 'P' && buf[0x83] == '1');
    // These test VEHCs have no ID fields: nothing written.
    ST_CHECK(pkg_set_ids(p, 0, buf, e->size) == 0 && *(uint32_t*)(buf + 0x20) == (0xC0DE0000u | (1 * 4 + 1)));
    char name[260];
    h_name(r, nullptr, base + 4, name);
    ST_CHECK(strcmp(name, "P011") == 0);
    // A package entry read past the file's end: zeroed, logged, no overrun.
    Entry far_e = *e;
    far_e.offset = 0x480;
    memset(buf, 0xAA, sizeof buf);
    got = pkg_read(p, &far_e, buf, 0x800);
    ST_CHECK(got == 0x80 && buf[0x80] == 0 && buf[0x7FF] == 0);
    // The same read without the system cache (read_uncached): through the bounce buffer.
    p->raw = CreateFileA(p->path, GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                         FILE_ATTRIBUTE_NORMAL | FILE_FLAG_NO_BUFFERING, nullptr);
    ST_CHECK(p->raw != INVALID_HANDLE_VALUE);
    if (p->raw == INVALID_HANDLE_VALUE) p->raw = nullptr;
    memset(buf, 0xAA, sizeof buf);
    got = pkg_read(p, e, buf, 0x800);
    ST_CHECK(got == 0x300 && buf[0x7FF] == 0 && *(uint32_t*)(buf + 0x20) == (0xC0DE0000u | (1 * 4 + 1)));
    if (p->raw) CloseHandle(p->raw);
    p->raw = nullptr;
    // An asynchronous read: pending until the work item is done, then settled once.
    memset(buf, 0xAA, sizeof buf);
    ST_CHECK(pkg_read_async(r, p, 1, e, buf, 0x800));
    ST_CHECK(g_pending.reader == r);
    ST_CHECK(!pkg_read_async(&other, p, 1, e, buf, 0x800));  // one pending read at most
    bool settled = false;
    for (int t = 0; t < 2000 && !settled; t++) {
        settled = pkg_settle(r, false, "test");
        if (!settled) Sleep(1);
    }
    ST_CHECK(settled && g_pending.reader == nullptr);
    ST_CHECK(*(uint32_t*)buf == kCHNK && *(uint32_t*)(buf + 0x20) == (0xC0DE0000u | (1 * 4 + 1)) && buf[0x7FF] == 0);
    ST_CHECK(pkg_settle(r, false, "test"));  // nothing pending: done at once
    // A read entry (or a close) waits for the pending read first.
    memset(buf, 0xAA, sizeof buf);
    ST_CHECK(pkg_read_async(r, p, 1, e, buf, 0x800));
    ST_CHECK(pkg_settle(r, true, "test") && g_pending.reader == nullptr && buf[0x80] == 'P');

    // SSLP: 266 is in the archive (base, +1000, +2000); slot buffers 0x50/0x50/0x50.
    static uint8_t sslp[0x1C + 6 * 4];
    memset(sslp, 0, sizeof sslp);
    *(uint16_t*)(sslp + 2) = 4;
    for (int k = 0; k < 3; k++) *(uint32_t*)(sslp + 8 + 4 * k) = 0x50;
    uint16_t* rec = (uint16_t*)(sslp + 0x1C);
    const uint16_t vanilla[4][3] = {{266, 0, 0}, {1266, 1, 1}, {2266, 2, 2}, {5, 3, 0}};
    memcpy(rec, vanilla, sizeof vanilla);
    Reader* ssic = (Reader*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(Reader));
    ssic->count = 2;
    ssic->ptr_count = 2;
    ssic->ptrs[1] = sslp;
    // Before the SSLP load every kept package counts as loaded; IDs not kept never do.
    ST_CHECK(archive_packages_active() && archive_package_loaded(706) && archive_package_loaded(266));
    ST_CHECK(!archive_package_loaded(702) && !archive_package_loaded(703) && !archive_package_loaded(5));
    h_sslp_load(sp.self, nullptr, ssic, 1);
    uint8_t* out = *(uint8_t**)(sp.self + 0x72F4);
    ST_CHECK(out && out != sslp && *(uint8_t**)(sp.self + 0x72F8) == out + 0x1C);
    if (out) {
        ST_CHECK(*(uint16_t*)(out + 2) == 4 + 3 * 3);  // 700, 701, 707
        uint16_t* o = (uint16_t*)(out + 0x1C);
        ST_CHECK(memcmp(o, vanilla, sizeof vanilla) == 0);
        ST_CHECK(o[12] == 700 && o[13] == base + 0 && o[14] == 0);
        ST_CHECK(o[15] == 1700 && o[16] == base + 1 && o[17] == 1);
        ST_CHECK(o[18] == 2700 && o[19] == base + 2 && o[20] == 2);
        ST_CHECK(o[21] == 701 && o[22] == base + 3);
        ST_CHECK(o[30] == 707 && o[31] == base + 12 && o[36] == 2707 && o[37] == base + 14 && o[38] == 2);
    }
    ST_CHECK(g_pkgs[0].listed && g_pkgs[1].listed && !g_pkgs[2].listed && !g_pkgs[3].listed && g_pkgs[4].listed);
    // After it, only the listed ones (706: NONR too big, 266: in the archive).
    ST_CHECK(archive_packages_active() && archive_package_loaded(700) && archive_package_loaded(701) &&
             archive_package_loaded(707));
    ST_CHECK(!archive_package_loaded(706) && !archive_package_loaded(266));
    // Another spool object (gui.sp's): its SSLP is left alone.
    StSpool gui = st_spool();
    h_sslp_load(gui.self, nullptr, ssic, 1);
    ST_CHECK(*(uint8_t**)(gui.self + 0x72F4) == sslp);

    // VSCT: 2 vanilla records; the engine's table at [[0x00FE7228]+0x780]+0x2C.
    static uint8_t vsct[8 + 28 * 2];
    memset(vsct, 0, sizeof vsct);
    *(uint32_t*)vsct = kVSCT;
    *(uint16_t*)(vsct + 6) = 2;
    *(uint16_t*)(vsct + 8) = 205;
    *(uint16_t*)(vsct + 8 + 28) = 1205;
    ssic->ptrs[0] = vsct;
    static uint8_t engine[0x800], holder[0x40];
    memset(holder, 0, sizeof holder);
    *(uint8_t**)(engine + 0x780) = holder;
    void* saved = *(void**)0x00FE7228;
    *(void**)0x00FE7228 = engine;
    static uint8_t res[8];
    memset(res, 0, sizeof res);
    h_vsct_load(res, nullptr, ssic, 0);
    uint8_t* v = *(uint8_t**)(res + 4);
    ST_CHECK(v && v != vsct && *(uint8_t**)(holder + 0x2C) == v);
    if (v) {
        ST_CHECK(*(uint32_t*)v == kVSCT && *(uint16_t*)(v + 6) == 2 + 9);
        ST_CHECK(*(uint16_t*)(v + 8) == 205 && *(uint16_t*)(v + 8 + 28) == 1205);
        const uint16_t ids[9] = {700, 700, 700, 701, 701, 701, 707, 707, 707};
        bool ok = true;
        for (int i = 0; i < 9; i++) {
            const uint8_t* q = v + 8 + 28 * (2 + i);
            if (*(uint16_t*)q != ids[i] || q[0x1B] != 0x10 + i % 3) ok = false;
        }
        ST_CHECK(ok);
    }
    *(void**)0x00FE7228 = saved;
    r->mode = 5;  // no file or payloads to free (the game's close and free need its heap)
    h_close(r, nullptr);
    ST_CHECK(g_pkg_first == 0 && r->count == 0);
    // A root table the packages would take past max_entries: none reaches the game.
    StSpool full = st_spool();
    full.root->count = g_max_entries - 1;
    g_pkg_first = 0;
    pkg_inject(full.root);
    ST_CHECK(g_pkg_first == 0 && full.root->count == g_max_entries - 1);
    ST_CHECK(!archive_packages_active() && !archive_package_loaded(700));
    g_pkg_failed = false;
    g_sslp_done = false;
    g_pkg_sites = false;
}

}  // namespace

// apply_vehicle_slots on fake spool objects: the vehicle object gets 60 kind-0 slots
// and its seven ID pools move out; another package type, a used pool or a header that
// isn't the vanilla 35 is left alone.
static int slots_selftest()
{
    int failures = 0;
#define SL_CHECK(x) do { if (!(x)) { printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #x); failures++; } } while (0)
    const size_t kObj = 0x7400;
    uint8_t* obj = (uint8_t*)calloc(1, kObj);
    uint8_t header[0x1C] = {};
    auto reset = [&](uint32_t type, uint8_t kind0) {
        memset(obj, 0, kObj);
        for (uint32_t off : kIdPools) *(uint32_t*)(obj + off) = (uint32_t)(uintptr_t)(obj + off + 0x0C);
        *(uint32_t*)(obj + 0x7300) = type;
        memset(header, 0, sizeof header);
        header[0x18] = kind0;
        header[0x19] = 2;
        header[0x1A] = 2;
        *(uint8_t**)(obj + 0x72F8) = header;
        g_slots_done = false;
    };
    uint32_t saved = g_vehicle_slots;
    g_vehicle_slots = 60;
    reset(0, 35);
    apply_vehicle_slots(obj);
    SL_CHECK(header[0x18] == 60);
    bool moved = true;
    for (uint32_t off : kIdPools) moved &= *(uint32_t*)(obj + off) != (uint32_t)(uintptr_t)(obj + off + 0x0C);
    SL_CHECK(moved);
    reset(3, 35);  // another package type (not vehicles)
    apply_vehicle_slots(obj);
    SL_CHECK(header[0x18] == 35 && *(uint32_t*)(obj + kIdPools[0]) == (uint32_t)(uintptr_t)(obj + kIdPools[0] + 0x0C));
    reset(0, 40);  // another mod's archive
    apply_vehicle_slots(obj);
    SL_CHECK(header[0x18] == 40);
    reset(0, 35);
    *(uint32_t*)(obj + kIdPools[4] + 8) = 1;  // a pool already holds a node
    apply_vehicle_slots(obj);
    SL_CHECK(header[0x18] == 35 && *(uint32_t*)(obj + kIdPools[0]) == (uint32_t)(uintptr_t)(obj + kIdPools[0] + 0x0C));
    g_vehicle_slots = 70;  // capped by the 64 handlers
    reset(0, 35);
    apply_vehicle_slots(obj);
    SL_CHECK(header[0x18] == 60);

    // The MODL provider's stack: 157 records pop, a release onto the full stack goes to
    // this module's list and comes back once the stack is empty again.
    uint8_t* modl = (uint8_t*)calloc(1, 0x774);
    void* recs[kModlRecords + 1];
    for (uint32_t i = 0; i < kModlRecords; i++) ((void**)(modl + 0x4FC))[i] = (void*)(uintptr_t)(0x1000 + i * 8);
    *(int32_t*)(modl + 0x770) = (int32_t)kModlRecords;
    for (uint32_t i = 0; i < kModlRecords; i++) h_modl_acquire(modl, nullptr, &recs[i]);
    SL_CHECK(*(int32_t*)(modl + 0x770) == 0 && recs[0] == (void*)(uintptr_t)(0x1000 + (kModlRecords - 1) * 8));
    for (uint32_t i = 0; i < kModlRecords; i++) h_modl_release(modl, nullptr, &recs[i]);
    SL_CHECK(*(int32_t*)(modl + 0x770) == (int32_t)kModlRecords);
    void* extra = (void*)(uintptr_t)0x9000;
    h_modl_release(modl, nullptr, &extra);  // the stack is full: kept in this module
    SL_CHECK(*(int32_t*)(modl + 0x770) == (int32_t)kModlRecords);
    for (uint32_t i = 0; i < kModlRecords; i++) h_modl_acquire(modl, nullptr, &recs[i]);
    h_modl_acquire(modl, nullptr, &recs[kModlRecords]);
    SL_CHECK(recs[kModlRecords] == extra && *(int32_t*)(modl + 0x770) == 0);
    for (ModlExtra& x : g_modl_extra)
        if (x.owner == modl) x = ModlExtra{};
    free(modl);

    // The sound bank list: 60 appends to its 50 nodes keep the list head and link all 60.
    uint8_t* mgr = (uint8_t*)calloc(1, 0x9548);
    int* pool = (int*)(mgr + 0x92DC);
    int* head = (int*)(mgr + 0x9540);
    pool[0] = (int)(uintptr_t)(mgr + 0x92E8);
    head[0] = head[1] = (int)(uintptr_t)head;
    for (int i = 0; i < 60; i++) h_bank_append(mgr, nullptr, 0x100 + i);
    int linked = 0;
    for (int* n = (int*)(uintptr_t)head[0]; n != head && linked < 100; n = (int*)(uintptr_t)n[0]) linked++;
    SL_CHECK(pool[2] == 60 && linked == 60 && ((int*)(uintptr_t)head[0])[2] == 0x100 + 59);
    for (PoolChunk& c : g_pool_chunks)
        if (c.pool == pool) c = PoolChunk{};
    free(mgr);
#undef SL_CHECK
    free(obj);
    g_vehicle_slots = saved;
    g_slots_done = false;
    printf("archive slots selftest: %d failures\n", failures);
    return failures;
}

int mod_archive_pkg_selftest(const char* dir)
{
    // init's slot raise hooks the SSLP load when there are no packages; this test
    // installs the package sites itself, so it starts from the vanilla bytes.
    if (g_sslp_hooked) {
        patch_bytes(0x005A0B50, kSslpExpect, 5);
        g_sslp_hooked = false;
    }
    int slot_failures = slots_selftest();
    g_st_fail = 0;
    char d[MAX_PATH], path[MAX_PATH], list[MAX_PATH];
    _snprintf_s(d, sizeof d, _TRUNCATE, "%s\\pkgtest\\", dir);
    CreateDirectoryA(d, nullptr);
    struct {
        const char* name;
        int id;
        uint32_t nonr;
        StFlaw flaw;
    } files[] = {
        {"a.sp", 0, 0x40, kGood},       {"b.sp", 1, 0x40, kGood},     {"c.sp", 7, 0x40, kGood},
        {"big.sp", 6, 0x60, kGood},     {"clash.sp", 66, 0x40, kGood}, {"corrupt.sp", 3, 0x40, kNotChnk},
        {"novsct.sp", 4, 0x40, kNoVsct}, {"cut.sp", 5, 0x40, kCut},    {"badkid.sp", 8, 0x40, kBadKid},
    };
    for (auto& f : files) {
        _snprintf_s(path, sizeof path, _TRUNCATE, "%s%s", d, f.name);
        st_write(path, f.id, f.nonr, f.flaw);
    }
    _snprintf_s(list, sizeof list, _TRUNCATE, "%scars.txt", d);
    st_write_list(list,
                  "# test list\n"
                  "700 a.sp\n"
                  "  701\tb.sp  \r\n"
                  "\n"
                  "bad line\n"
                  "0 a.sp\n"
                  "1000 a.sp\n"
                  "700 c.sp\n"
                  "702 missing.sp\n"
                  "703 corrupt.sp\n"
                  "704 novsct.sp\n"
                  "705 cut.sp\n"
                  "708 badkid.sp\n"
                  "706 big.sp\n"
                  "266 clash.sp\n"
                  "707 c.sp\n");
    // The sites: a changed byte at the last one leaves all three vanilla.
    uint32_t n = pkg_load_list(d, list);
    ST_CHECK(n == 5);
    *(uint8_t*)0x0086C630 ^= 0xFF;
    ST_CHECK(!patch_package_sites());
    ST_CHECK(bytes_equal(0x005A13C4, kOpenExpect, 5) && bytes_equal(0x005A0B50, kSslpExpect, 5));
    *(uint8_t*)0x0086C630 ^= 0xFF;
    st_archive(453);
    st_archive(1098);  // the packages' entries cross 1100 into the side table
    st_set_ids();
    ST_CHECK(patch_package_sites());
    ST_CHECK(*(uint8_t*)0x005A13C4 == 0xE8 && !bytes_equal(0x005A13C4, kOpenExpect, 5));
    ST_CHECK(*(uint8_t*)0x005A0B50 == 0xE9 && *(uint8_t*)0x0086C630 == 0xE9);
    ST_CHECK(g_pkg_sites);
    pkg_drop_all();
    ST_CHECK(g_pkg_count == 0 && !g_pkg_sites && !archive_packages_active());
    // An empty or missing list: nothing loaded.
    st_write_list(list, "# nothing\n");
    ST_CHECK(pkg_load_list(d, list) == 0);
    _snprintf_s(list, sizeof list, _TRUNCATE, "%snone.txt", d);
    ST_CHECK(pkg_load_list(d, list) == 0);

    // File redirection: the table, the matching, the open, the site.
    char gen[MAX_PATH];
    _snprintf_s(gen, sizeof gen, _TRUNCATE, "%sgen", d);
    CreateDirectoryA(gen, nullptr);
    _snprintf_s(path, sizeof path, _TRUNCATE, "%sgen\\gui.sp", d);
    st_write_list(path, "GEN");
    _snprintf_s(list, sizeof list, _TRUNCATE, "%sredirect.txt", d);
    st_write_list(list,
                  "# test\n"
                  "Install\\GUI.sp|gen\\gui.sp\n"
                  "TEXT:x|gen\\missing.fchunk\n"
                  "no bar here\n"
                  "|gen\\gui.sp\n");
    ST_CHECK(pkg_load_redirects(d, list) == 1);
    const char* to = redirect_of("Install\\gui.sp");
    ST_CHECK(to && strcmp(to, path) == 0);
    ST_CHECK(redirect_of("install/GUI.SP") == to && redirect_of(".\\Install\\gui.sp") == to);
    char full[MAX_PATH];
    _snprintf_s(full, sizeof full, _TRUNCATE, "%sInstall\\gui.sp", d);
    ST_CHECK(redirect_of(full) == to);
    ST_CHECK(!redirect_of("Install\\gui.sp2") && !redirect_of("Media\\fonts.fchunk") && !redirect_of(nullptr));

    // The traffic sets' AI copy: used only while the game will have the slots it was
    // written for (its .slots record), and without a record only while they are raised.
    {
        // A vehicle archive: root CHNK -> SSIC (CHNK) -> NONR (CHNK) -> SSLP (0x1C bytes).
        uint8_t arc[3 * 32 + 0x1C] = {};
        auto chnk = [&](uint32_t at, uint32_t tag, uint32_t child_at, uint32_t child_size) {
            uint32_t* h = (uint32_t*)(arc + at);
            h[0] = kCHNK;
            h[2] = 1;
            Entry* e = (Entry*)(arc + at + 16);
            e->tag = tag;
            e->offset = child_at - at;
            e->size = child_size;
        };
        chnk(0, tag4('S', 'S', 'I', 'C'), 32, sizeof arc - 32);
        chnk(32, tag4('N', 'O', 'N', 'R'), 64, sizeof arc - 64);
        chnk(64, tag4('S', 'S', 'L', 'P'), 96, 0x1C);
        arc[96 + 0x18] = 35;
        char apath[MAX_PATH];
        _snprintf_s(apath, sizeof apath, _TRUNCATE, "%sdngvehicles.sp", d);
        FILE* af = nullptr;
        fopen_s(&af, apath, "wb");
        if (af) {
            fwrite(arc, 1, sizeof arc, af);
            fclose(af);
        }
        ST_CHECK(archive_kind0_slots(apath) == 35);
        arc[96 + 0x18] = 40;
        fopen_s(&af, apath, "wb");
        if (af) {
            fwrite(arc, 1, sizeof arc, af);
            fclose(af);
        }
        ST_CHECK(archive_kind0_slots(apath) == 40);
        _snprintf_s(path, sizeof path, _TRUNCATE, "%snone.sp", d);
        ST_CHECK(archive_kind0_slots(path) == 0);

        char ai[MAX_PATH], rec[MAX_PATH];
        _snprintf_s(ai, sizeof ai, _TRUNCATE, "%sgen\\san_franciscoAI.fchunk", d);
        _snprintf_s(rec, sizeof rec, _TRUNCATE, "%s.slots", ai);
        st_write_list(ai, "AIEX");
        st_write_list(list, "media\\san_franciscoAI.fchunk|gen\\san_franciscoAI.fchunk\n");
        const uint32_t saved_slots = g_vehicle_slots;
        const int saved_kind0 = g_archive_kind0;
        const bool saved_done = g_slots_done;
        g_slots_done = false;
        auto ai_used = [&](uint32_t raised, int kind0, const char* record) {
            if (record)
                st_write_list(rec, record);
            else
                DeleteFileA(rec);
            ST_CHECK(pkg_load_redirects(d, list) == 1);
            g_vehicle_slots = raised;
            g_archive_kind0 = kind0;
            return redirect_of("media\\san_franciscoai.fchunk") != nullptr;
        };
        ST_CHECK(ai_used(50, 35, "50\n"));           // written for 50, the game has 50
        ST_CHECK(ai_used(60, 35, "50\n"));           // more is fine
        ST_CHECK(!ai_used(41, 35, "50\n"));          // the ini lowered past what it was written for
        ST_CHECK(!ai_used(0, 35, "50\n"));           // the raise failed at init (any "stay 35")
        ST_CHECK(!ai_used(50, 40, "50\n"));          // another mod's archive: the raise won't apply
        ST_CHECK(!ai_used(50, 0, "50\n"));           // the archive can't be read
        ST_CHECK(ai_used(50, 35, nullptr));          // no record (an older manager): needs a raise
        ST_CHECK(!ai_used(0, 35, nullptr));
        // Once the SSLP is loaded: what was applied.
        ai_used(50, 35, "50\n");
        g_slots_done = true;
        g_slots_applied = 0;  // refused at the SSLP
        ST_CHECK(!redirect_of("media\\san_franciscoai.fchunk"));
        g_slots_applied = 50;
        ST_CHECK(redirect_of("media\\san_franciscoai.fchunk") != nullptr);
        g_slots_applied = 0;
        g_slots_done = saved_done;
        g_vehicle_slots = saved_slots;
        g_archive_kind0 = saved_kind0;
        g_ai_copy_served = false;
        DeleteFileA(rec);
        DeleteFileA(ai);
        DeleteFileA(apath);
        st_write_list(list, "Install\\GUI.sp|gen\\gui.sp\n");
        ST_CHECK(pkg_load_redirects(d, list) == 1);
    }
    // offline.exe maps Driver.exe without binding its imports: bind the one the
    // names not listed go through, as the loader does.
    const CreateFileAFn bound = *kCreateFileImport;
    *kCreateFileImport = CreateFileA;
    HANDLE h = h_create_file("Install\\gui.sp", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    char got3[4] = {};
    DWORD n3 = 0;
    ST_CHECK(h != INVALID_HANDLE_VALUE && ReadFile(h, got3, 3, &n3, nullptr) && n3 == 3 && memcmp(got3, "GEN", 3) == 0);
    if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
    ST_CHECK(h_create_file("Media\\none.fchunk", GENERIC_READ, 0, nullptr, OPEN_EXISTING, 0, nullptr) ==
             INVALID_HANDLE_VALUE);  // not listed: as it is
    // Another plugin hooked the import: a name not listed reaches its hook, a listed one doesn't.
    *kCreateFileImport = st_next_create_file;
    g_st_next_name[0] = 0;
    ST_CHECK(h_create_file("Install\\san_francisco.dngc", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0,
                           nullptr) == INVALID_HANDLE_VALUE);
    ST_CHECK(strcmp(g_st_next_name, "Install\\san_francisco.dngc") == 0);
    g_st_next_name[0] = 0;
    h = h_create_file("Install\\gui.sp", GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    ST_CHECK(h != INVALID_HANDLE_VALUE && g_st_next_name[0] == 0);
    if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
    *kCreateFileImport = bound;
    ST_CHECK(patch_redirect_site());
    ST_CHECK(*(uint8_t*)0x0085AC1E == 0xE8 && *(uint8_t*)0x0085AC23 == 0x90 &&
             *(int32_t*)0x0085AC1F == (int32_t)((uint32_t)(uintptr_t)h_create_file - 0x0085AC23));
    ST_CHECK(!patch_redirect_site());  // patched already: not the vanilla bytes
    g_redirect_count = 0;
    printf("archive packages selftest: %d failures\n", g_st_fail);
    return g_st_fail + slot_failures;
}
