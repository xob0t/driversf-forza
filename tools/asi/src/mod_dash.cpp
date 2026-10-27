// Digital dashboards: km/h digits for converted Forza displays.
// See research/cockpit.md ("Digital displays") and research/asi.md ("Digital dashboards").
//
// Stock (Driver.exe 1.04, confirmed-static): the vehicle manager's frame update
// (FUN_00888F30) calls FUN_008919A0 at 0x00888FE6 with ECX = the gauge input array,
// one 0x98-byte object per player ([[[0xFE7228]+0x78C]+4] + 0x2251C). Per object:
// +0x00 the seven needle inputs (id 1 rpm / 5000, id 2 clamp(mph, p606, p605) x
// 0.0125), +0x1C g_fDigiData (7 x vec4: the v offsets of the digital dials'
// vertices), +0x8C the vehicle. The game writes g_fDigiData ids 1 (rpm bar), 2 (mph
// digits), 6 and 7 (gear); ids 3..5 are never written.
//
// Forza's dash scenes show the speed in mph or in km/h, by the scene. The converter
// (src/dsfforza/convert/digital_dash.py) puts km/h digits on id 3 (units to
// thousands in comps 0..3, like the game's mph digits on id 2). This module calls
// the game's update, then writes id 3 from the clamped speed the game just wrote for
// the speedometer needle (id 2's input). Without the module those digits stay blank.
#include "patch.h"

#include <stdint.h>

static const uint32_t kUpdateCall = 0x00888FE6;  // call FUN_008919A0 (gauge inputs) in FUN_00888F30
static const uint8_t kUpdateCallBytes[5] = {0xE8, 0xB5, 0x89, 0x00, 0x00};
static const uint32_t kUpdate = 0x008919A0;
static const int kPlayers = 2;
static const int kObjectSize = 0x98;
static const int kDigiData = 0x1C;   // g_fDigiData in the object: vec4 per gauge id
static const int kVehicle = 0x8C;
static const int kKmhId = 3;          // the converter's km/h digits
static const float kMphInput = 0.0125f;  // id 2's input: mph x 0.0125 (DAT_00DDB93C)
static const float kKmhPerMph = 1.609344f;
static const float kDigitPitch = 23.0f / 256.0f;  // the game's digit cells: (digit + 1) x 23/256

typedef void(__fastcall* UpdateFn)(void* inputs, void* edx);

static void write_kmh(uint8_t* object)
{
    if (!*(void**)(object + kVehicle))
        return;
    float mph = *(float*)(object + 4) / kMphInput;
    if (!(mph >= 0.0f) || mph > 2000.0f)
        return;
    int kmh = (int)(mph * kKmhPerMph + 0.5f);
    float* cell = (float*)(object + kDigiData + 16 * (kKmhId - 1));
    int rest = kmh;
    for (int comp = 0; comp < 4; ++comp) {
        // units always shown; a leading zero is a blank cell (0), as the game's mph digits
        cell[comp] = (comp == 0 || rest > 0) ? (float)(rest % 10 + 1) * kDigitPitch : 0.0f;
        rest /= 10;
    }
}

static void __fastcall update_hook(void* inputs, void* edx)
{
    ((UpdateFn)kUpdate)(inputs, edx);
    uint8_t* base = (uint8_t*)inputs;
    if (!base)
        return;
    for (int p = 0; p < kPlayers; ++p)
        write_kmh(base + p * kObjectSize);
}

void mod_dash_init()
{
    if (!cfg_bool("dash", "kmh", true)) {
        dsf_log("dash: km/h digits off ([dash] kmh=0)");
        return;
    }
    if (patch_call(kUpdateCall, (void*)&update_hook, kUpdateCallBytes))
        dsf_log("dash: km/h digits on g_fDigiData id %d (call at %08X)", kKmhId, kUpdateCall);
}
