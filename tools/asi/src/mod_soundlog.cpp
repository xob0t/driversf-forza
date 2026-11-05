// Engine sound rpm log (issue #461): what each car's engine sound is driven with, for the
// in-game check of research/engine-sound.md ("rpm path"). Off unless [soundlog] log=1; it
// only reads and logs, the game's calls go on unchanged.
//
// Driver.exe 1.04 (confirmed-static):
//   FUN_008B2370 (__thiscall, the vehicle; called once, at 0x0087324E) fills the 200-byte
//     sound record: +0x20 = (rpm + p596 * (rpm^2 / p42 - rpm)) * (1 + 0.1 * noise), rpm the
//     engine's (FUN_008C27D0 on vehicle+0xE0: vehicle+0x174), the VO3D parameters at
//     [vehicle+0x1270] + 0x2C70 + 4 * id (p40 +0x2D10, p42 +0x2D18, p596 +0x35C0).
//   FUN_00552920 (__thiscall, the vehicle's engine sound object; called once, at 0x00553B8B,
//     with that record) sends vehicle_rpm = VSVH+94 + n * (VSVH+98 - VSVH+94), at least
//     VSVH+94, n = (+0x20 - p40) / (p42 - p40), times the jitter, to both emitters
//     (FUN_0053DDC0 on +0xC4 and +0xFC) and keeps it at +0x288 while +0x88 is 0. Its VSVH is
//     at [sound+0xBC], the parameters at [[sound+0xB4]+0x5B4].
//
// One line per engine sound every [soundlog] every=N updates:
//   soundlog: spool S t MS rpm R shaped X vehicle_rpm V range LO..HI p40 A p42 B p596 C
// rpm is the engine rpm of the same update when the record matches the last one seen for
// those parameters, else -1.
#include "patch.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <stdint.h>

static const uint32_t kShapeCall = 0x0087324E;   // call FUN_008B2370 (in FUN_00873150)
static const uint8_t kShapeCallBytes[5] = {0xE8, 0x1D, 0xF1, 0x03, 0x00};
static const uint32_t kShape = 0x008B2370;
static const uint32_t kUpdateCall = 0x00553B8B;  // call FUN_00552920
static const uint8_t kUpdateCallBytes[5] = {0xE8, 0x90, 0xED, 0xFF, 0xFF};
static const uint32_t kUpdate = 0x00552920;

typedef void(__thiscall* ShapeFn)(void* vehicle, uint8_t* record);
typedef void(__thiscall* UpdateFn)(void* sound, uint8_t* record);

struct Last {
    const uint8_t* params;
    float rpm, shaped;
};
struct Count {
    const void* sound;
    unsigned n;
};
static Last g_last[32];
static Count g_count[32];
static int g_every = 15;

static float f32(const uint8_t* p, uint32_t off) { return *(const float*)(p + off); }

static void __fastcall shape_hook(void* vehicle, void*, uint8_t* record)
{
    ((ShapeFn)kShape)(vehicle, record);
    const uint8_t* params = *(const uint8_t**)((uint8_t*)vehicle + 0x1270);
    if (!params) return;
    int slot = -1;
    for (int i = 0; i < 32; i++) {
        if (g_last[i].params == params) { slot = i; break; }
        if (slot < 0 && !g_last[i].params) slot = i;
    }
    if (slot < 0) slot = (int)(((uintptr_t)params >> 4) & 31);
    g_last[slot] = {params, f32((uint8_t*)vehicle, 0x174), f32(record, 0x20)};
}

static void __fastcall update_hook(void* sound, void*, uint8_t* record)
{
    ((UpdateFn)kUpdate)(sound, record);
    int slot = -1;
    for (int i = 0; i < 32; i++) {
        if (g_count[i].sound == sound) { slot = i; break; }
        if (slot < 0 && !g_count[i].sound) slot = i;
    }
    if (slot < 0) slot = (int)(((uintptr_t)sound >> 4) & 31);
    if (g_count[slot].sound != sound) g_count[slot] = {sound, 0};
    if (g_count[slot].n++ % (unsigned)g_every) return;
    const uint8_t* s = (const uint8_t*)sound;
    const uint8_t* vsvh = *(const uint8_t* const*)(s + 0xBC);
    const uint8_t* owner = *(const uint8_t* const*)(s + 0xB4);
    const uint8_t* params = owner ? *(const uint8_t* const*)(owner + 0x5B4) : nullptr;
    if (!vsvh || !params) return;
    float shaped = f32(record, 0x20), rpm = -1.0f;
    for (int i = 0; i < 32; i++)
        if (g_last[i].params == params && g_last[i].shaped == shaped) rpm = g_last[i].rpm;
    int kept = *(const int*)(s + 0x88) == 0 && *(const int*)(s + 0x290) == 0;
    dsf_log("soundlog: spool %u t %lu rpm %.1f shaped %.1f vehicle_rpm %.1f range %u..%u p40 %.0f p42 %.0f p596 %.3f",
            *(const uint32_t*)vsvh, (unsigned long)GetTickCount(), rpm, shaped, kept ? f32(s, 0x288) : -1.0f,
            *(const uint32_t*)(vsvh + 0x94), *(const uint32_t*)(vsvh + 0x98), f32(params, 0x2D10),
            f32(params, 0x2D18), f32(params, 0x35C0));
}

void mod_soundlog_init()
{
    if (!cfg_int("soundlog", "log", 0)) return;
    g_every = cfg_int("soundlog", "every", 15);
    if (g_every < 1) g_every = 1;
    if (!patch_call(kShapeCall, (void*)shape_hook, kShapeCallBytes)) {
        dsf_log("soundlog: the sound record call at %08X differs; no log", kShapeCall);
        return;
    }
    if (!patch_call(kUpdateCall, (void*)update_hook, kUpdateCallBytes)) {
        patch_bytes(kShapeCall, kShapeCallBytes, 5);
        dsf_log("soundlog: the engine sound update call at %08X differs; no log", kUpdateCall);
        return;
    }
    dsf_log("soundlog: logging every %d engine sound updates", g_every);
}
