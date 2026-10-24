// archive: lifts the 1100-entries-per-CHNK limit of the game's chunk reader,
// so dngvehicles.sp can hold more than 365 vehicles (stock 150 + 215 added).
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
#include <string.h>

#include "patch.h"

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

// Entry i of r. Indices past the side table fall back to the stock (inline,
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
        dsf_log("archive: reader %p entry %u has no side table (count %u); stock address used", r, i, r->count);
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
        dsf_log("archive: reader %p pointer %u has no side table (count %u); stock address used", r, i, r->count);
    return &r->ptrs[i];
}

// Makes room for count entries (count > kInline). Pointers of indices at or
// above old_ptr_count are zeroed, as the stock pointer-vector resize does.
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

// Payload plus name, aligned to the entry alignment (stock arithmetic,
// including the 8-bit wrap of name_len + 3).
uint32_t entry_span(const Entry* e)
{
    uint32_t a = 1u << (e->align_log2 & 31);
    uint32_t name = (uint8_t)(e->name_len + 3) & 0xFC;
    return align_up(name + e->size, a);
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
        pos += (int)E(r->parent, r->parent_index)->offset;
        r = r->parent;
    }
}

// 0x008FB620 (and its thunks 0x008FB9D0, destructor 0x008FBE80): close
void __fastcall h_close(Reader* r, void*)
{
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
    uint32_t sector = sector_size();
    Entry* e = E(r, i);
    uint32_t size = align_up(entry_span(e), sector);
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
// stock; a table of more than kInline entries goes through g_big_hdr and the
// side table.
uint32_t __fastcall h_poll(Reader* r, void*)
{
    uint8_t result = 1;
    switch (r->state) {
    case 0:
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
        // The stock pointer-vector resize zeroes the new slots.
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
// stock ones included, showed the lock icon (VM runs vm-20261024-235436 with
// 1121 SPOL entries against -234833 with 1098).
// The replacements keep lists longer than 1100 in a side table keyed by the
// provider object (the first 1100 also inline, as far as stock code would see
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
    dsf_log("archive: spool list at %p holds %u items (%u past the stock %u)", self, n, n - kSpolInline,
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
        dsf_log("archive: 0059B6D0/0059B5A0 are not the stock code; spool lists stay at %u items", kSpolInline);
        return;
    }
    if (!patch_jmp(0x0059B5A0, (void*)h_spol_find, sizeof find_expect, find_expect)) return;
    if (!patch_jmp(0x0059B6D0, (void*)h_spol_load, sizeof load_expect, load_expect)) {
        patch_bytes(0x0059B5A0, find_expect, sizeof find_expect);
        return;
    }
    dsf_log("archive: 0059B6D0/0059B5A0 spool item lists (SSIC SPOL) hold up to %u items (stock %u)", g_max_entries,
            kSpolInline);
}

}  // namespace

// All or nothing: the replacements only work together.
static void patch_readers()
{
    for (const Site& s : kSites) {
        if (!bytes_equal(s.va, s.expect, s.n)) {
            dsf_log("archive: %08X (%s) is not the stock code (another plugin?); CHNK readers stay at %u entries",
                    s.va, s.what, kInline);
            return;
        }
    }
    const int nsites = (int)(sizeof kSites / sizeof kSites[0]);
    for (int i = 0; i < nsites; i++) {
        const Site& s = kSites[i];
        if (!patch_jmp(s.va, s.target, 5, s.expect)) {
            for (int j = 0; j < i; j++) patch_bytes(kSites[j].va, kSites[j].expect, 5);
            dsf_log("archive: patching %08X failed after the check passed; the %d sites before it are undone, "
                    "no reader patches", s.va, i);
            return;
        }
        dsf_log("archive: %08X %s -> %p", s.va, s.what, s.target);
    }
    dsf_log("archive: CHNK readers hold up to %u entries (stock %u)", g_max_entries, kInline);
}

void mod_archive_init()
{
    int max_entries = cfg_int("archive", "max_entries", 16384);
    if (max_entries < (int)kInline) max_entries = kInline;
    if (max_entries > 1 << 20) max_entries = 1 << 20;
    g_max_entries = (uint32_t)max_entries;
    patch_readers();
    if (cfg_bool("archive", "spool_lists", true)) patch_spool_lists();

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
            dsf_log("archive: 0085A9AE file seek is not the stock code; archives must stay below 2 GiB");
    }
}
