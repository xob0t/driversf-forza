// HUD maker badge and model nameplate with 16-bit glyph codes.
// See research/gui-assets.md ("Unlimited badges and plates") and research/asi.md.
//
// Stock path (Driver.exe 1.04, confirmed-static): the vehicle-data manager's
// vtable slot 0x134 (FUN_006C63D0) calls Lua getVehicleStats(id), reads the
// fields and builds a 0x100-byte vehicle info record with FUN_006C40C0, which
// formats Manufacturer and Model as one-character strings ("%c") at +0x90 and
// +0x92 and stores the model id at +0x94. Five screens (driving HUD, Shift
// HUD, two more HUD panels, garage shop) then pass +0x90 / +0x92 to the text
// element's narrow SetText (vtable +0xBC) of an element drawn in
// vehicles_font. So a badge or plate is one byte: about 70 free codes.
//
// The font itself takes 16-bit codes (FNTD records are u16, the glyph lookup
// FUN_00729440 is a binary search on them) and several texture pages (u16
// page index at record +0x12). The element's text stays narrow (its buffer
// is char[capacity]; the plate elements hold only a few characters), so the
// code can't travel as text. This module instead:
//
//   1. hooks the record build (the call at 0x006C6855) to also read the
//      optional vehicleStats fields ManufacturerGlyph / ModelGlyph (16-bit
//      codes from 0x100, written by tools/gui_assets.py --wide-glyphs) while
//      the entry is still on the Lua stack, into a table by model id;
//   2. replaces the ten SetText(+0x90 / +0x92) calls. For a car with a code,
//      the element gets a slot code (1..15) of its own (gui_assets adds empty
//      glyph records for them), the slot's glyph record in the loaded
//      vehicles_font is overwritten with the 16-bit glyph's (position, size,
//      offsets, page), and the element's text becomes that one byte. Without
//      a code, the stock one-byte string.
//
// Cars without the new fields (every stock car, installs without the wide
// font) take the stock path unchanged.
#include "patch.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <stdio.h>
#include <string.h>

// ---- game addresses -------------------------------------------------------
static const uint32_t kLuaState = 0x0122B498;      // lua_State* of the game scripts
static const uint32_t kGetIntField = 0x005E8210;   // bool cdecl (L, key, int* out, idx): t[key] as int
static const uint32_t kInfoCtor = 0x006C40C0;      // vehicle info record ctor (thiscall, 18 stack args)
static const uint32_t kInfoCtorCall = 0x006C6855;  // its only call, in FUN_006C63D0
static const uint8_t kInfoCtorCallBytes[5] = {0xE8, 0x66, 0xD8, 0xFF, 0xFF};

static const uint32_t kEngine = 0x00FE7228;        // engine singleton; +0x724 = font manager
static const uint32_t kFontId = 0x6FFB8415;        // vehicles_font: FNTD +0x0C, font object +0x00
// Slot codes: control characters without tab, line feed, return (gui_assets.HUD_SLOT_CODES).
static const uint8_t kSlotCodes[] = {1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 14, 15};
static const int kSlots = (int)sizeof kSlotCodes;

static const int kMaxId = 4096;
static uint16_t g_badge[kMaxId];
static uint16_t g_plate[kMaxId];
static uint8_t g_emblem[kMaxId];      // vehicleStats GarageEmblem (1 = Forza logo on the garage card)
static uint32_t g_ctor = kInfoCtor;   // jumped through by ctor_stub
static int g_wide_hits = 0;           // first few uses are logged

typedef bool(__cdecl* GetIntField)(void* L, const char* key, int* out, int idx);
typedef void(__fastcall* SetTextA)(void* self, void* edx, const char* text);

static uint16_t glyph_field(void* L, const char* key)
{
    int v = 0;
    // idx -2: the vehicleStats entry is on top of the stack, the key goes above it.
    if (!((GetIntField)kGetIntField)(L, key, &v, -2)) return 0;
    return (v > 0 && v <= 0xFFFF) ? (uint16_t)v : 0;
}

// Runs right before FUN_006C40C0, with the entry of model `id` on the Lua stack.
static void __cdecl read_codes(int id)
{
    if (id < 0 || id >= kMaxId) return;
    void* L = *(void**)kLuaState;
    uint16_t badge = 0, plate = 0, emblem = 0;
    if (L) {
        badge = glyph_field(L, "ManufacturerGlyph");
        plate = glyph_field(L, "ModelGlyph");
        emblem = glyph_field(L, "GarageEmblem");
    }
    if ((badge || plate || emblem) && (g_badge[id] != badge || g_plate[id] != plate || g_emblem[id] != emblem))
        dsf_log("hud: model %d ManufacturerGlyph %u ModelGlyph %u GarageEmblem %u", id, badge, plate, emblem);
    g_badge[id] = badge;
    g_plate[id] = plate;
    g_emblem[id] = (uint8_t)(emblem == 1);
}

// Replaces `call FUN_006C40C0`: same registers and stack on entry, so after
// read_codes it tail-jumps into the ctor, which returns to the original caller.
static __declspec(naked) void ctor_stub()
{
    __asm {
        pushad
        push dword ptr [esp + 36]   // model id: first stack argument (pushad 32 + return address 4)
        call read_codes
        add esp, 4
        popad
        jmp dword ptr [g_ctor]
    }
}

#pragma pack(push, 1)
struct Glyph {          // FNTD glyph record, 20 bytes
    uint16_t code, x, y, w, h, x_offset, y_offset, advance, unknown, page;
};
#pragma pack(pop)

// Font object (FUN_00729550): +0x00 id, +0x04 glyph count, +0x20 records.
// The font manager (engine +0x724, FUN_00729880/FUN_00729840) holds up to
// ten font pointers at +0x08, their count at +0x30.
static Glyph* find_glyph(uint8_t* font, uint16_t code)
{
    int count = *(int*)(font + 4);
    Glyph* g = *(Glyph**)(font + 0x20);
    int lo = 0, hi = count - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        if (g[mid].code == code) return &g[mid];
        if (g[mid].code < code) lo = mid + 1; else hi = mid - 1;
    }
    return nullptr;
}

static uint8_t* vehicles_font()
{
    uint8_t* engine = *(uint8_t**)kEngine;
    if (!engine) return nullptr;
    uint8_t* mgr = *(uint8_t**)(engine + 0x724);
    if (!mgr) return nullptr;
    int n = *(int*)(mgr + 0x30);
    for (int i = 0; i < n && i < 10; i++) {
        uint8_t* font = ((uint8_t**)(mgr + 8))[i];
        if (font && *(uint32_t*)font == kFontId) return font;
    }
    return nullptr;
}

struct Slot {
    void* elem;
    uint16_t shown;   // 16-bit code the slot's record holds
    uint32_t used;    // last use, for reuse when every slot is taken (elements are re-created)
};
static Slot g_slots[kSlots];
static uint32_t g_clock = 0;

static void set_text(void* elem, const char* text, uint16_t code)
{
    void** vt = *(void***)elem;
    SetTextA set = (SetTextA)vt[0xBC / 4];
    if (code) {
        uint8_t* font = vehicles_font();
        Glyph* src = font ? find_glyph(font, code) : nullptr;
        int k = 0;
        while (k < kSlots && g_slots[k].elem && g_slots[k].elem != elem) k++;
        if (k == kSlots) {   // all taken: reuse the least recently set one
            k = 0;
            for (int i = 1; i < kSlots; i++)
                if (g_slots[i].used < g_slots[k].used) k = i;
        }
        Glyph* dst = font ? find_glyph(font, kSlotCodes[k]) : nullptr;
        if (src && dst) {
            bool changed = g_slots[k].elem != elem || g_slots[k].shown != code;
            g_slots[k].elem = elem;
            g_slots[k].shown = code;
            g_slots[k].used = ++g_clock;
            memcpy(&dst->x, &src->x, sizeof(Glyph) - sizeof(uint16_t));
            char slot[2] = {(char)kSlotCodes[k], 0};
            if (changed) set(elem, nullptr, "");   // text unchanged would skip the element's refresh
            set(elem, nullptr, slot);
            if (g_wide_hits < 16) {
                g_wide_hits++;
                dsf_log("hud: element %p slot %u shows U+%04X (page %u, %ux%u at %u,%u)", elem, kSlotCodes[k], code,
                        src->page, src->w, src->h, src->x, src->y);
            }
            return;
        }
        if (g_wide_hits < 16) {
            g_wide_hits++;
            dsf_log("hud: U+%04X not drawn (font %p, glyph %p, slot record %p): stock text", code, font, src, dst);
        }
    }
    set(elem, nullptr, text);
}

// thiscall-compatible (ecx = element, one stack argument, callee pops it).
// text points at record +0x90 (badge) or +0x92 (plate); the model id is at +0x94.
static void __fastcall hud_badge(void* elem, void*, const char* text)
{
    int id = *(const int*)(text + 4);
    set_text(elem, text, (id >= 0 && id < kMaxId) ? g_badge[id] : 0);
}

static void __fastcall hud_plate(void* elem, void*, const char* text)
{
    int id = *(const int*)(text + 2);
    set_text(elem, text, (id >= 0 && id < kMaxId) ? g_plate[id] : 0);
}

// One `element->SetText(record + 0x90 or + 0x92)` call site:
//   mov r, [r + 0xBC] ; lea r, [esp + disp] ; push r ; (mov ecx, reg) ; call r
// becomes
//   lea eax, [esp + disp] ; push eax ; (mov ecx, reg) ; call hud_badge/plate ; nop...
// esp is the same at the lea as in the original, so disp is unchanged.
struct Site {
    const char* what;
    uint32_t va;
    uint8_t len;
    uint8_t orig[18];
    uint32_t disp;     // record + 0x90 / + 0x92, relative to esp
    uint8_t ecx_from;  // 0: ecx already holds the element; else the ModRM byte of `mov ecx, reg`
    bool plate;
    uint32_t resume;   // 0: inline; else the site is too short: jmp to a stub that ends with jmp resume
};

static const Site kSites[] = {
    {"driving HUD badge (vehicle_brand)", 0x006D4EA0, 16,
     {0x8B, 0x92, 0xBC, 0x00, 0x00, 0x00, 0x8D, 0x84, 0x24, 0x98, 0x00, 0x00, 0x00, 0x50, 0xFF, 0xD2},
     0x98, 0, false, 0},
    {"driving HUD plate (vehicle_model)", 0x006D4EB8, 16,
     {0x8B, 0x80, 0xBC, 0x00, 0x00, 0x00, 0x8D, 0x94, 0x24, 0x9A, 0x00, 0x00, 0x00, 0x52, 0xFF, 0xD0},
     0x9A, 0, true, 0},
    {"Shift HUD badge", 0x006CFBDD, 16,
     {0x8B, 0x80, 0xBC, 0x00, 0x00, 0x00, 0x8D, 0x94, 0x24, 0x9C, 0x00, 0x00, 0x00, 0x52, 0xFF, 0xD0},
     0x9C, 0, false, 0},
    {"Shift HUD plate", 0x006CFBF6, 16,
     {0x8B, 0x92, 0xBC, 0x00, 0x00, 0x00, 0x8D, 0x84, 0x24, 0x9E, 0x00, 0x00, 0x00, 0x50, 0xFF, 0xD2},
     0x9E, 0, true, 0},
    {"HUD panel 0x6DBA badge", 0x006DBADD, 16,
     {0x8B, 0x80, 0xBC, 0x00, 0x00, 0x00, 0x8D, 0x94, 0x24, 0x9C, 0x00, 0x00, 0x00, 0x52, 0xFF, 0xD0},
     0x9C, 0, false, 0},
    {"HUD panel 0x6DBA plate", 0x006DBAF6, 16,
     {0x8B, 0x92, 0xBC, 0x00, 0x00, 0x00, 0x8D, 0x84, 0x24, 0x9E, 0x00, 0x00, 0x00, 0x50, 0xFF, 0xD2},
     0x9E, 0, true, 0},
    {"garage shop badge (GarageShop_VehicleName)", 0x0070E361, 18,
     {0x8B, 0x90, 0xBC, 0x00, 0x00, 0x00, 0x8D, 0x8C, 0x24, 0xB0, 0x00, 0x00, 0x00, 0x51, 0x8B, 0xCF, 0xFF, 0xD2},
     0xB0, 0xCF /* mov ecx, edi */, false, 0},
    // 0x70E373: lea ecx, [esp+0xB2]; push ecx; jmp 0x70E3B9 (shared tail: mov ecx, ebx; call [vt+0xBC]),
    // which continues at 0x70E3C5.
    {"garage shop plate (GarageShop_ModelName)", 0x0070E373, 10,
     {0x8D, 0x8C, 0x24, 0xB2, 0x00, 0x00, 0x00, 0x51, 0xEB, 0x3C},
     0xB2, 0xCB /* mov ecx, ebx */, true, 0x0070E3C5},
    {"vehicle panel badge (vehicle_brand)", 0x00711687, 18,
     {0x8B, 0x92, 0xBC, 0x00, 0x00, 0x00, 0x8D, 0x84, 0x24, 0xDC, 0x00, 0x00, 0x00, 0x50, 0x8B, 0xCD, 0xFF, 0xD2},
     0xDC, 0xCD /* mov ecx, ebp */, false, 0},
    {"vehicle panel plate (vehicle_model)", 0x0071169F, 16,
     {0x8B, 0x80, 0xBC, 0x00, 0x00, 0x00, 0x8D, 0x94, 0x24, 0xDE, 0x00, 0x00, 0x00, 0x52, 0xFF, 0xD0},
     0xDE, 0, true, 0},
};

// Writes `lea eax,[esp+disp]; push eax; (mov ecx,reg); call target` at `at` (code
// that will run at address `va`); returns the number of bytes written.
static size_t emit_call(uint8_t* at, uint32_t va, const Site& s)
{
    size_t n = 0;
    at[n++] = 0x8D; at[n++] = 0x84; at[n++] = 0x24;   // lea eax, [esp + disp32]
    memcpy(at + n, &s.disp, 4); n += 4;
    at[n++] = 0x50;                                   // push eax
    if (s.ecx_from) { at[n++] = 0x8B; at[n++] = s.ecx_from; }
    void* target = s.plate ? (void*)hud_plate : (void*)hud_badge;
    at[n++] = 0xE8;
    int32_t rel = (int32_t)((uint32_t)target - (va + (uint32_t)n + 4));
    memcpy(at + n, &rel, 4); n += 4;
    return n;
}

static bool patch_site(const Site& s)
{
    uint8_t buf[32];
    memset(buf, 0x90, sizeof buf);
    if (!s.resume) {
        size_t n = emit_call(buf, s.va, s);
        if (n > s.len) {
            dsf_log("hud: %s: %u bytes do not fit in %u", s.what, (unsigned)n, s.len);
            return false;
        }
        return patch_bytes(s.va, buf, s.len, s.orig);
    }
    uint8_t* stub = (uint8_t*)alloc_exec(32);
    if (!stub) return false;
    size_t n = emit_call(stub, (uint32_t)stub, s);
    stub[n] = 0xE9;
    int32_t rel = (int32_t)(s.resume - ((uint32_t)stub + (uint32_t)n + 5));
    memcpy(stub + n + 1, &rel, 4);
    return patch_jmp(s.va, stub, s.len, s.orig);
}

// ---- garage card emblem -----------------------------------------------------
// research/gui-assets.md, "Garage card emblem". The "SF" disc at a garage car
// card's top right is the 3D sprite object Spr3D.unlocked<N> of card N
// (GUI_FrontEnd.fchunk; MmDk CObjectSprite3D, one resource slot at +0x104).
// All twenty share one sprite resource (Driver's CResSprite subclass: image
// plus four UV pairs at +0x30, here the SF rect of the card icon atlas
// 20125a7f761d9d2c, 512 x 256). The car list (FUN_007113A0) never touches it;
// the menu script shows it per itemState_N.
//
// For a car with vehicleStats GarageEmblem = 1 the card's sprite is pointed at
// a spare sprite resource made to look like the SF one: Header_Sprite_20, card
// 20's picture slot (free while the maker group has fewer than 20 cars), gets
// the SF resource's UVs and the texture kLogo (512 x 256, the Forza logo in
// the SF rect; gui_assets.py puts it in Fonts.fchunk so it is always loaded).
// The sprite sizes itself from the bound texture (FUN_00B42100), so texture
// size and UVs both have to match the SF resource's. Other cars get their
// original resource back. The spare's own UVs come back before anything else
// uses it: a group of 20 cars, or the challenge browser, which shares
// Header_Sprite_01..20 and even the card objects: its texture-slot lookups are
// wrapped and put every disc back to the SF resource. (Leaving the vehicles
// screen sets the SF resource back on the discs too; entering it refills the
// list, so this hook runs again.)
//
// Hook: in the car list loop, `mov edx,[ecx+0xF8]; mov [edx],edi` (the
// itemState_N write) at 0x007118A5 becomes a call to card_stub. There ebp is
// the card index, esi the vehicles screen, [esp+0x18] the model id and
// [esp+0x28] the number of cars in the group.
static const uint32_t kCardSite = 0x007118A5;
static const uint8_t kCardSiteBytes[8] = {0x8B, 0x91, 0xF8, 0x00, 0x00, 0x00, 0x89, 0x3A};
static const uint32_t kFindObject = 0x00ABED70;     // thiscall page->Find("Prefix.Name", -1)
static const uint32_t kSetResource = 0x00AB3800;    // thiscall object->SetResource(slot, entry, flag)
static const uint32_t kSpriteTexture = 0x00ADA7F0;  // fastcall: sprite resource -> texture slot (+0x30 u64 UID)
static const uint32_t kFindTexture = 0x00BAC620;    // thiscall (engine+0xC)->Find(lo, hi, info[6])
static const uint32_t kSetTexture = 0x006CC6F0;     // thiscall texture slot->Set(lo, hi): release, set, bind
static const uint32_t kVtSprite3D = 0x00E8CC54;     // MmDk::CObjectSprite3D
static const uint32_t kVtResSprite = 0x00E849AC;    // MmDk::CResSprite
static const uint32_t kVtGameSprite = 0x00E28570;   // Driver's CResSprite subclass (factory 0x006CACE0)
static const uint32_t kLogoLo = 0x7EEF12F0, kLogoHi = 0x2D2E5AF0;   // gui_assets.FORZA_LOGO_UID f012ef7ef05a2e2d
static const char* const kSpareName = "Header_Sprite_20";
static const int kCards = 20;
static const int kUvBytes = 0x20;                   // four {u, v} float pairs at resource +0x30

// `call FUN_00ADA7F0` sites that hand Header_Sprite_NN resources to their texture
// setters: the challenge browser's fill and picture update, the car list's.
static const uint32_t kSlotSites[] = {0x0070D72A, 0x0070D3BF, 0x00711537, 0x00711354};

typedef void*(__fastcall* FindObjectFn)(void* page, void*, const char* name, int index);
typedef void*(__fastcall* FindResFn)(void* reg, void*, const char* name, int kind, int index);
typedef int(__fastcall* SetResourceFn)(void* obj, void*, int slot, void* entry, int flag);
typedef uint8_t*(__fastcall* SpriteTextureFn)(void* res, void*);
typedef void(__fastcall* SetTextureFn)(void* slot, void*, uint32_t lo, uint32_t hi);
typedef bool(__fastcall* FindTextureFn)(void* mgr, void*, uint32_t lo, uint32_t hi, uint32_t* info);

static bool g_card_emblem = false;
static int g_emblem_logs = 0;
static int g_step = 0;                 // last step reached in card_emblem_body (logged if it faults)

// Card N's disc exists once per menu page that holds the cards: the maker list
// page and the card browser page (Enter) each have their own Spr3D.unlocked<N>.
static const int kPagesMax = 4;
struct Disc {
    uint8_t* obj;     // a Spr3D.unlocked<N> object
    void* orig;       // its own resource entry (the shared SF one)
};
struct Card {
    Disc disc[kPagesMax];
    int discs;
};
static Card g_cards[kCards];
static void* g_spare = nullptr;        // spare's registry entry while it is dressed as the Forza emblem
static uint8_t* g_spare_res = nullptr; // its resource object
static uint8_t g_spare_uv[kUvBytes];   // its own UVs, put back by spare_release
static int g_fill = 0, g_spare_fill = -1;

static uint32_t vtable_of(const void* p) { return p ? *(const uint32_t*)p : 0; }

static uint8_t* resource_of(void* entry) { return entry ? *(uint8_t**)((uint8_t*)entry + 0x1C) : nullptr; }

static bool is_sprite_resource(const void* res)
{
    return res && (vtable_of(res) == kVtResSprite || vtable_of(res) == kVtGameSprite);
}

// Texture slot of a sprite resource entry, or null.
static uint8_t* texture_slot(void* entry)
{
    uint8_t* res = resource_of(entry);
    return is_sprite_resource(res) ? ((SpriteTextureFn)kSpriteTexture)(res, nullptr) : nullptr;
}

static void log_resource(const char* what, void* entry)
{
    uint8_t* res = resource_of(entry);
    uint8_t* slot = texture_slot(entry);
    const float* uv = res ? (const float*)(res + 0x30) : nullptr;
    dsf_log("hud: emblem: %s entry %p kind %d res %p (vt %08X) tex %p uid %08X%08X size %ux%u "
            "uv %.4f,%.4f %.4f,%.4f %.4f,%.4f %.4f,%.4f",
            what, entry, entry ? *(int*)((uint8_t*)entry + 0x14) : -1, res, vtable_of(res), slot,
            slot ? *(uint32_t*)(slot + 0x34) : 0, slot ? *(uint32_t*)(slot + 0x30) : 0,
            slot ? *(uint32_t*)(slot + 0x0C) : 0, slot ? *(uint32_t*)(slot + 0x10) : 0,
            uv ? uv[0] : 0, uv ? uv[1] : 0, uv ? uv[2] : 0, uv ? uv[3] : 0,
            uv ? uv[4] : 0, uv ? uv[5] : 0, uv ? uv[6] : 0, uv ? uv[7] : 0);
}

// Gives the spare its own UVs back (its texture is the next user's business:
// every user sets one). Cards still pointing at it are re-pointed by the next fill.
static void spare_release(const char* why)
{
    if (!g_spare_res) return;
    memcpy(g_spare_res + 0x30, g_spare_uv, kUvBytes);
    if (g_emblem_logs < 24) {
        g_emblem_logs++;
        dsf_log("hud: emblem: %s takes %s back (UVs restored)", why, kSpareName);
    }
    g_spare = nullptr;
    g_spare_res = nullptr;
    g_spare_fill = -1;
}

// Dresses the spare as the Forza emblem (once per fill), or returns null (logged).
static void* spare_entry(void* reg, void* sf_entry)
{
    void** vt = *(void***)reg;
    g_step = 40;
    void* entry = ((FindResFn)vt[0x14 / 4])(reg, nullptr, kSpareName, 1, -1);
    g_step = 41;
    if (entry && entry == g_spare && g_spare_fill == g_fill) return entry;
    uint8_t* res = resource_of(entry);
    uint8_t* sf = resource_of(sf_entry);
    uint8_t* slot = texture_slot(entry);
    static int logs = 0;
    bool log = logs++ < 4;
    if (!slot || !is_sprite_resource(sf)) {
        if (log) dsf_log("hud: emblem: no sprite resource %s (%p) or SF resource (%p); card emblems stay stock",
                         kSpareName, entry, sf_entry);
        return nullptr;
    }
    uint8_t* engine = *(uint8_t**)kEngine;
    uint32_t info[8] = {};
    g_step = 42;
    if (!engine || !((FindTextureFn)kFindTexture)(*(void**)(engine + 0xC), nullptr, kLogoLo, kLogoHi, info)) {
        if (log) dsf_log("hud: emblem: texture %08X%08X not loaded (Fonts.fchunk from gui_assets.py not installed?); "
                         "card emblems stay stock", kLogoHi, kLogoLo);
        return nullptr;
    }
    if (log) log_resource("spare before", entry);
    if (g_spare_res != res) {
        spare_release("another spare");
        memcpy(g_spare_uv, res + 0x30, kUvBytes);
    }
    memcpy(res + 0x30, sf + 0x30, kUvBytes);
    g_step = 43;
    // As the car list's picture update does (0x00711370): release the old reference, set, bind.
    ((SetTextureFn)kSetTexture)(slot, nullptr, kLogoLo, kLogoHi);
    g_step = 44;
    if (log) log_resource("spare after", entry);
    g_spare = entry;
    g_spare_res = res;
    g_spare_fill = g_fill;
    return entry;
}

// Runs for every car the list files into card `index` (the last one wins).
static void card_emblem_body(uint8_t* screen, int id, int index, int cars)
{
    g_step = 1;
    if (index < 0 || index >= kCards || !screen) return;
    if (index == 0) g_fill++;
    // The spare is card 20's picture: a group of 20 cars needs it for that.
    if (cars >= kCards && g_spare) spare_release("a group of 20 cars");
    bool want = id >= 0 && id < kMaxId && g_emblem[id] && cars < kCards;
    Card& card = g_cards[index];
    if (!want && !card.discs && g_emblem_logs >= 8) return;   // nothing swapped on this card yet
    uint8_t* menu = *(uint8_t**)(screen + 4) ? *(uint8_t**)(*(uint8_t**)(screen + 4) + 8) : nullptr;
    void* reg = menu ? *(void**)(menu + 0x280) : nullptr;
    if (!menu || !reg) return;
    char name[32];
    _snprintf_s(name, sizeof name, _TRUNCATE, "Spr3D.unlocked%d", index + 1);
    // The menu's pages (FUN_00AB83C0 walks them): count at +0x1B0, array at +0x1B4.
    int pages = *(int*)(menu + 0x1B0);
    void** page_list = *(void***)(menu + 0x1B4);
    if (!page_list || pages <= 0 || pages > 256) return;
    uint8_t* found[kPagesMax];
    int n = 0;
    for (int i = 0; i < pages && n < kPagesMax; i++) {
        if (!page_list[i]) continue;
        g_step = 2;
        uint8_t* obj = (uint8_t*)((FindObjectFn)kFindObject)(page_list[i], nullptr, name, -1);
        g_step = 3;
        if (!obj || vtable_of(obj) != kVtSprite3D) continue;
        bool dup = false;
        for (int k = 0; k < n; k++) dup |= found[k] == obj;
        if (!dup) found[n++] = obj;
    }
    if (!n) {
        if (g_emblem_logs < 8) {
            g_emblem_logs++;
            dsf_log("hud: emblem: %s not found as a 3D sprite in %d pages", name, pages);
        }
        return;
    }
    // Keep what is known about objects still there; new objects start from their current resource.
    Disc next[kPagesMax];
    for (int k = 0; k < n; k++) {
        next[k].obj = found[k];
        next[k].orig = nullptr;
        for (int j = 0; j < card.discs; j++)
            if (card.disc[j].obj == found[k]) next[k].orig = card.disc[j].orig;
        void* current = *(void**)(found[k] + 0x104);
        if (!next[k].orig && current && current != g_spare) next[k].orig = current;
    }
    memcpy(card.disc, next, sizeof(Disc) * n);
    card.discs = n;
    g_step = 4;
    void* spare = want ? spare_entry(reg, card.disc[0].orig) : nullptr;
    g_step = 5;
    for (int k = 0; k < n; k++) {
        uint8_t* obj = card.disc[k].obj;
        void* current = *(void**)(obj + 0x104);
        if (g_emblem_logs < 8) {
            g_emblem_logs++;
            dsf_log("hud: emblem: card %d model %d GarageEmblem %d: %s %p (%d of %d pages)", index + 1, id, (int)want,
                    name, obj, k + 1, n);
            log_resource("card resource", current);
        }
        void* target = want ? spare : card.disc[k].orig;
        if (!target || target == current) continue;
        // Keep the object's own flag byte (+0x34, written by SetResource from its last argument).
        g_step = 6;
        ((SetResourceFn)kSetResource)(obj, nullptr, 0, target, *(uint8_t*)(obj + 0x34));
        g_step = 7;
        if (g_emblem_logs < 40) {
            g_emblem_logs++;
            dsf_log("hud: emblem: card %d (%p) model %d -> %s", index + 1, obj, id, want ? "Forza logo" : "stock");
        }
    }
}

// A fault in the game calls above must not take the garage down: log it once and stop.
static void __cdecl card_emblem(uint8_t* screen, int id, int index, int cars)
{
    if (!g_card_emblem) return;
    __try {
        card_emblem_body(screen, id, index, cars);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        g_card_emblem = false;
        dsf_log("hud: emblem: fault at step %d (card %d, model %d); card emblems off for this session",
                g_step, index + 1, id);
    }
}

// Replaces `mov edx,[ecx+0xF8]; mov [edx],edi` (+ 3 NOPs); flags are live (je follows).
static __declspec(naked) void card_stub()
{
    __asm {
        pushfd
        pushad
        push dword ptr [esp + 0x50] // cars in the group: [esp+0x28] at the site (+ ret 4 + pushfd 4 + pushad 32)
        push ebp                    // card index
        push dword ptr [esp + 0x48] // model id: [esp+0x18] at the site (+ 40 + two pushes)
        push esi                    // vehicles screen
        call card_emblem
        add esp, 16
        popad
        popfd
        mov edx, [ecx + 0xF8]
        mov [edx], edi
        ret
    }
}

// The challenge browser shows its cards with the same card objects (run
// host-20261023-094324: challenge cards wore the Forza logo left by the last
// car list): every disc goes back to its own resource and the spare is freed.
static void restore_all_cards(const char* why)
{
    int restored = 0;
    for (Card& c : g_cards) {
        for (int k = 0; k < c.discs; k++) {
            uint8_t* obj = c.disc[k].obj;
            void* orig = c.disc[k].orig;
            if (!obj || !orig || vtable_of(obj) != kVtSprite3D || *(void**)(obj + 0x104) == orig) continue;
            ((SetResourceFn)kSetResource)(obj, nullptr, 0, orig, *(uint8_t*)(obj + 0x34));
            restored++;
        }
    }
    static int logs = 0;
    if (restored && logs++ < 8) {
        dsf_log("hud: emblem: %s: %d card discs back to the SF resource", why, restored);
    }
}

// Called (ecx = resource) instead of FUN_00ADA7F0 where a screen is about to set a
// Header_Sprite_NN picture: the spare gets its own UVs back first; in the
// challenge browser the card discs go back to stock too.
static void slot_check(uint8_t* res, bool challenges)
{
    if (!g_card_emblem) return;
    __try {
        if (challenges) restore_all_cards("challenge browser");
        if (challenges || (res && res == g_spare_res)) spare_release(challenges ? "challenge browser" : "a picture slot user");
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        g_card_emblem = false;
        dsf_log("hud: emblem: fault while restoring for a picture slot user; card emblems off for this session");
    }
}

static void __fastcall slot_check_cars(uint8_t* res) { slot_check(res, false); }
static void __fastcall slot_check_challenges(uint8_t* res) { slot_check(res, true); }

static __declspec(naked) void slot_stub_cars()
{
    __asm {
        pushad
        call slot_check_cars        // ecx unchanged: the resource
        popad
        mov eax, kSpriteTexture
        jmp eax
    }
}

static __declspec(naked) void slot_stub_challenges()
{
    __asm {
        pushad
        call slot_check_challenges  // ecx unchanged: the resource
        popad
        mov eax, kSpriteTexture
        jmp eax
    }
}

static void card_emblem_init()
{
    if (!cfg_bool("hud", "card_emblem", true)) {
        dsf_log("hud: card_emblem=0; garage card emblem stays stock");
        return;
    }
    // Every picture-slot site first: without them the spare's UVs could leak into another screen.
    for (uint32_t va : kSlotSites) {
        uint8_t expect[5] = {0xE8};
        int32_t rel = (int32_t)(kSpriteTexture - (va + 5));
        memcpy(expect + 1, &rel, 4);
        if (!bytes_equal(va, expect, 5)) {
            dsf_log("hud: call to FUN_00ADA7F0 at %08X differs; garage card emblem stays stock", va);
            return;
        }
    }
    if (!bytes_equal(kCardSite, kCardSiteBytes, sizeof kCardSiteBytes)) {
        dsf_log("hud: car list itemState write at %08X differs; garage card emblem stays stock", kCardSite);
        return;
    }
    const int nslots = (int)(sizeof kSlotSites / sizeof kSlotSites[0]);
    uint8_t orig[nslots][5];
    int done = 0;
    for (; done < nslots; done++) {   // the first two are the challenge browser's
        uint32_t va = kSlotSites[done];
        memcpy(orig[done], (const void*)va, 5);
        if (!patch_call(va, va < 0x00711000 ? (void*)slot_stub_challenges : (void*)slot_stub_cars)) break;
    }
    uint8_t code[8] = {0xE8, 0, 0, 0, 0, 0x90, 0x90, 0x90};
    int32_t rel = (int32_t)((uint32_t)(void*)card_stub - (kCardSite + 5));
    memcpy(code + 1, &rel, 4);
    if (done < nslots || !patch_bytes(kCardSite, code, sizeof code, kCardSiteBytes)) {
        for (int i = 0; i < done; i++) patch_bytes(kSlotSites[i], orig[i], 5);
        dsf_log("hud: writing the card emblem hooks failed (undone); garage card emblem stays stock");
        return;
    }
    g_card_emblem = true;
    dsf_log("hud: card emblem hook at %08X, picture-slot checks at %08X %08X %08X %08X "
            "(GarageEmblem = 1 -> %s with the SF UVs and texture %08X%08X)",
            kCardSite, kSlotSites[0], kSlotSites[1], kSlotSites[2], kSlotSites[3], kSpareName, kLogoHi, kLogoLo);
}

void mod_hud_init()
{
    if (!bytes_equal(kInfoCtorCall, kInfoCtorCallBytes, sizeof kInfoCtorCallBytes)) {
        dsf_log("hud: call to the vehicle info ctor at %08X differs; no HUD patches", kInfoCtorCall);
        return;
    }
    int sites = 0;
    for (const Site& s : kSites) {
        if (patch_site(s)) {
            sites++;
            dsf_log("hud: patched %s at %08X", s.what, s.va);
        } else {
            dsf_log("hud: skipped %s at %08X (stock narrow text stays)", s.what, s.va);
        }
    }
    if (!sites) {
        dsf_log("hud: no SetText site patched; leaving the ctor alone");
        return;
    }
    if (!patch_call(kInfoCtorCall, (void*)ctor_stub, kInfoCtorCallBytes)) {
        dsf_log("hud: ctor hook failed after %d sites; badges stay one-byte", sites);
        return;
    }
    dsf_log("hud: ctor hook at %08X; %d of %d text sites; ManufacturerGlyph/ModelGlyph active",
            kInfoCtorCall, sites, (int)(sizeof kSites / sizeof kSites[0]));
    card_emblem_init();
}
