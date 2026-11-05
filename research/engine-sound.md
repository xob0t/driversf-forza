# Engine sound: giving ports their own Forza engine

Updated: 2026-11-05 (pitch and crossfades, #461)

Can a ported car sound like its Forza self instead of borrowing a vanilla
DSF car's engine? Yes. Forza's engine loops can be swapped into a vanilla
DSF bank, keeping the bank's structure, and the bank still checks out
offline. `port_car`/`port_batch` now do this for every car by default
(section "In the build"). In the full roster (`out/build`,
2026-10-28) 571 of 573 cars have it; the Volt and the Leaf keep their
host's (Forza's bank for them is `NA.fsb`). Nobody has listened to it in
game yet (#202).

Evidence levels as in `dngvehicles.md`: confirmed-static (code or file
read), offline-checked (surveys of the vanilla fleet or the Forza files,
and what our tools produce and check), hypothesis. Nothing here is
confirmed-runtime yet.

Tools:
- `src/dsfforza/formats/wwise_bank.py`: Wwise v48 bank parser, rebuilder and IMA ADPCM codec.
- `src/dsfforza/convert/engine_sound.py`: Forza to DSF conversion and the archive step.
- `src/dsfforza/formats/forza_audio.py`: Forza FSB, ET and HT access.
- `tools/ghidra/DecompileRange.java`.

## DSF side

### Bank and loading (confirmed-static)

- `NONR/VSRT` holds `VSVH` (0x9C), `SBAK` (a Wwise bank) and on 19 cars `GRBK`.
- **Version:** `FUN_00c60e70` rejects any bank whose BKHD version isn't 0x30 (48). That is about Wwise 2010.x/2011.1; no SDK version string is linked. Every bank in `WWiseDefault.pck` is v48 too.
- **Loading:** `FUN_0084bf30` / `FUN_0084beb0` call `AK::SoundEngine::LoadBank(ptr, size)` from memory, asynchronously, in place in the spool buffer. The pointer must be 16-byte aligned and the size at least 0x18. The bank ID is read from BKHD.
- **Failure mode:** if the load fails, `FUN_0048f780` leaves the car silent.
- **No hard-coded bank contents:** the code never looks up object or media IDs inside a vehicle bank. It only posts the event IDs stored in VSVH and sets global RTPC, switch and state IDs.

### IDs are global (offline-checked)

Across the 150 vanilla banks:
- 3582 HIRC IDs and 617 media IDs; 3113 of the HIRC IDs appear in more than one bank.
- Yet an ID never has different content in two banks.
- There are only 46 distinct engine media sets. For example, the Stratos 217 and the 370Z 299 carry the 227 370Z's objects and media under their own bank IDs.

"Same content" holds byte for byte once three things are masked: references
to the bank's own ID (a Sound's file ID, a Play action's bank), a Sound's
media offset in its bank file, and an ActorMixer's children list (each bank
lists only its own children of a shared mixer). With those masked, 0 of the
fleet's shared IDs differ (offline-checked; `engine_sound._content`, the lint's rule for
banks that are not ours).

This is ordinary Wwise: objects and media are indexed by ID across all loaded banks. So a modified bank must not reuse its host's IDs. If the host car's bank loads first, its objects and media would be the ones played (hypothesis based on the SDK's behaviour; the vanilla data is consistent with it).

### Media (confirmed-static, offline-checked)

- Every embedded media in every vanilla vehicle bank is RIFF format 0x0002, Wwise IMA ADPCM:
  - mono, 32 kHz;
  - 0x24-byte blocks of 64 samples: s16 first sample, u8 step index, then 63 nibbles, low nibble first;
  - chunks are `fmt`, `JUNK`, `data`.
- The decoder in `wwise_bank.py` uses the (2n+1)·step/8 form and matches vgmstream sample for sample on vanilla media.
- Loops are whole-file (`Loop = 0`, infinite), so each loop must be a whole number of 64-sample blocks.

### 370Z bank (227), the template (confirmed-static)

Run `wwise_bank.py tree out/sound/vanilla/227/SBAK.bin --curves`. The bank holds 17 media (285 KB), a 37 KB HIRC and 177 objects.

- **Set A**, event at VSVH+04 → switch `audio_mode` (player/challenger) → state `cameratype` → layer containers: 8 loops at 921, 1728, 2085, 2729, 3526, 4429, 5274 and 6051 rpm.
- **Set B**, VSVH+0C: 8 different loops at 921, 1509, 2097, 2715, 3333, 4278, 5123 and 6051.
- Per Sound in a set:
  - a pitch RTPC on `vehicle_rpm` (0x9C4D0121) = 1200·log2(rpm / R), sampled at 13 fixed x (0, 94, 188, 375, 750, 1125, 1500, 2250, 3000, 4500, 6000, 9000, 12000) and joined linearly (Wwise curve shape 4). R is 902, 1696, 2058, 2704, 3458, 4414, 5220, 6042 for set A; the rpms listed above (921, 1728, ...) are where the straight segments cross 0 cents;
  - a layer crossfade on `vehicle_rpm`: Sine up from the previous loop's R, SineRecip down to the next one's, peaking at the loop's own R; the first slot full below its R, the last full above it. VSVH+94/+98 (902/6042) are the first and last R.
- AI (mode 1) uses a 4-loop subset with wide crossfades.
- Both sets carry identical `vehicle_engineload` curves (params 6 and 11, -90 to 0 dB; names unresolved). So **there is no on-load/off-load pair**; the two sets are two microphone positions.
- The 17th media is a 0.2 s backfire pop (VSVH+70/74).
- Shared sounds (whine, tyres, wind, tone generators) reference media in other banks.
- Fleet: of the 131 sample banks, 93 have 8+8 loops, 10 7+7, 10 8+7 and 9 7+8
  (`out/cache/sound/vanilla-banks.json`). Five more count 8+14 or 15+15 because
  their sets also hold idle grains, and four have no rpm loop sets ("In the
  build", 2.).

### VSVH (confirmed-static unless marked)

| Offset | Meaning |
|---|---|
| +00 | spool ID |
| +04 / +08 | play / stop of set A, on emitter A |
| +0C / +10 | play / stop of set B, on emitter B |
| +14..+2C | throttle-hysteresis events (threshold 0.1) |
| +3C / +40 | gear up / down |
| +4C | engine type → global switch `0x24B047F7` `sw_engine_type` (below) |
| +60..+74, +84..+90 | backfire / pop events and their timer |
| +94 / +98 | the bank's rpm range |

All event IDs live in the vehicle's own bank, so a VSVH must stay paired with its SBAK.
On all 150 vanilla cars every non-zero value at +04, +08, +0C, +10, +14, +1C,
+24, +28, +2C, +34..+48, +54..+5C and +68..+78 is an Event of the car's own
bank, and no other field ever is (offline-checked; `engine_sound.EVENT_FIELDS`, checked
by the lint).

**Engine type, VSVH+4C (confirmed-static, offline-checked).** `0x550300` reads +4C,
subtracts 1 and indexes a 10-entry jump table; anything out of range
(including 0) gives v8:

| +4C | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | other |
|---|---|---|---|---|---|---|---|---|---|---|---|
| state | v6 | v8 | v10 | v12 | vr6 | h4 | h6 | il4 | il5 | il5 (not il6) | v8 |

Vanilla values (engine from `vehicleStats` `Cylinders`; the 150 banks in
`out/cache/sound/vanilla-banks.json`): 2 on 86 cars (67 V8s, 9 V12s, 4 I6s,
3 V10s, a W12, a V6 and an I5); 1 on 14 (9 V6s, 4 I6s, a V8); 8 on 13 (11
I4s, an I2, an I5); 10 on 7 (6 I6s, which therefore get il5, and one
without an entry); 3 and 5 on three cars each, 6 on one; 0 on 23 (granular
and silent ones included). No vanilla car uses 4, 7 or 9. Reflections' own values are loose. The switch is
used only by four Switch containers in the `HUD` bank of `WWiseDefault.pck`
(none in any vehicle bank). Each picks one of three plugin-source Sounds
(tone generators) whose frequency follows `vehicle_rpm` linearly up to
200, 250 or 300 Hz at 12000 rpm, with volume on `vehicle_engineload`:

- 200 Hz (1 × crank frequency): v8, h4, il4;
- 250 Hz (1.25 ×): il5, v10;
- 300 Hz (1.5 ×): v6, vr6, h6, v12, il6.

That is half the firing frequency of a 4-, 5- and 6-cylinder engine: a
low rumble tone matched to the cylinder count. The granular engine has its
own copy of the table at `0x547B00` (index 0..9 = v8, h4, h6, il4, il5, il6,
v10, v12, v6, vr6). Effect in game not heard yet.

**rpm path** (confirmed-static; decompiles in `out/ghidra/snd10`, `snd3`, `snd-noise`):
1. `FUN_008B2370` (called once, at `0x0087324E`) fills the 200-byte sound record. +0x20 is
   `(rpm + p596·(rpm²/p42 − rpm)) · (1 + 0.1·noise)`. rpm is the engine's (`FUN_008C27D0` on
   vehicle+0xE0, i.e. vehicle+0x174); 0.1 is `DAT_00F3F27C`; the noise is `FUN_008BB7C0`, one
   octave of gradient noise over a table filled at run time. Driven wheels in contact then divide
   it by the mean of 1 + p597·(wheel speed − speed). p596 is read nowhere else (`handling.md`).
2. `FUN_00552920` (called once, at `0x00553B8B`) normalises n = (+0x20 − p40)/(p42 − p40) and
   sends `vehicle_rpm` = max(VSVH+94, VSVH+94 + n·(VSVH+98 − VSVH+94)) to both emitters
   (`FUN_0053DDC0` on +0xC4 and +0xFC). The value is multiplied by
   1 + (1 − rec+0xB0)·(2·rand − 1)·0.05 (`DAT_00DD6974`), where rec+0xB0 is `FUN_008BBDE0`'s 1.0, or 0.8
   (`DAT_00DD6870`) while vehicle+0xD84 is above 0.9: ±1% at most. The mapping is used only when
   +94 < 5000 and +98 < 15000 (integers here, floats `0x00DE2574`/`0x00DE37A8` in the state-3
   path `FUN_00554230`, which uses the same mapping without the floor).
3. The engine drives itself up to p42·(1 + p364), where its torque reaches 0 (`FUN_008C3CF0`,
   `research/handling.md`).

So a port's p40/p42 never have to match its sound host. VO3D p596 and p597 (slip scaling of throttle) are audio parameters; p605/p606 are not (they are the speedometer range, `research/cockpit.md`).

**Other RTPCs:**
- `vehicle_engineload`: throttle, rising over about 10 updates and dropping at once. It is pushed to 1 below 1.1·p40 and is 0 off-throttle.
- `vehicle_speed`, `vehicle_gear`, `vehicle_damage`, plus listener distance and approach.

**Emitters:**
- A sits at the engine marker (UPVH role 0x51); if that is behind the origin, 1 m forward with switch `rear`.
- B sits at the first exhaust marker (role 0x4D).
- In-car cameras place them around the listener.

So **set A = engine, set B = exhaust**.

### Granular cars (confirmed-static)

On the 19 granular cars, `GRBK` drives the game's own granular synth. It feeds Wwise through an Audio Input plugin, and only for local players; AI cars get no granular voice.

Layout (`FUN_00547800`):
- version 10..50, rpm values, engine type;
- two sets of {loops < 20, grains ≤ 600}, each grain {bytes, samples ≤ 22050, s16, u32 grain rpm, IMA ADPCM data, high nibble first}.

## Forza side (confirmed-static; `src/dsfforza/formats/forza_audio.py`)

**Where the banks are.** Engine banks are shared per engine recording, not per car: `media/audio/cars/Engines/Soundbanks/LOD1/<code>_<name>_{EngAmb,Exh,Int}.fsb`.
- FH1: 943 banks, 207 MB.
- FM4 disc 1: 359 banks, 79 MB.
- FM4 packs: 68 more banks.

**Car to bank:**
1. `EngineTuning.zip/<car>_ET.xml` gives idle, redline and, per emission and upgrade level, an HT file.
2. `HarmonicTuning.zip/<x>_HT.xml` gives the bank and one `<Loop rpm_min rpm_sample rpm_max volume>` per subsong.
3. The mapping resolves for 146/175 FH1 cars, 277/312 FM4 disc 1 cars and 179/188 pack cars. The misses are mostly race cars with `NA.xml` at L0. (Counted 2026-10-23, not rechecked. The build takes another emission when one is `NA.xml`, and since #37 the other game's ET file or the same model's, so 571 of 573 cars get a Forza sound.)

### FM4 DLC cars (confirmed-static; offline-checked 2026-11-04)

The 176 cars of the user's 15 FM4 DLC car packs (`research/forza-sources.md`, "FM4 DLC
packs"). The survey scripts were in the #457 PR's scratch folder and are not kept.

**Where their files are.**
- ET: all 176 `<car>_ET.xml` are loose in the car's own pack, in its inner zip
  `Media/DLCZips/<N>_pri_65.zip` under `media/audio/cars/Engines/EngineTuning/`. None is in
  disc 1's `enginetuning.zip`. The packs also re-ship 7 disc cars' ETs (MAS_GranSport_06,
  FOR_FocusSVT_03, ...), byte-identical to the disc's.
- HT: loose in the same inner zip under `HarmonicTuning/` (354 files over the 15 packs).
- Banks: loose package files `Media/DLCZips/<N>_pri_65/Media/audio/cars/Engines/Soundbanks/LOD1/*.fsb`
  (186 over the packs). The 29 that share a name with a disc bank are byte-identical to it,
  and no bank name has two contents across the packs.
- The inner zips use method 21 (LZX), like the disc zips. Disc 2's four packs hold no audio
  in their inner zips.
- FM4 disc 1 has the folders `Media/audio/cars/Engines/enginetuning/`, `harmonictuning/` and
  `Media/audio/cars/CarModelTuning/` next to its zips, each with a `dummy.txt` ("dummy for
  folder structure"). Those are the paths the pack's loose files sit under. Read statically,
  the game reads one tuning folder that the disc zip and the packs' loose files both go into.

**The packs rely on the disc.** At L0, 374 of the DLC cars' HT references resolve in their
own pack and 152 only on the disc. Of the banks, 220 come from the own pack and 303 from the
disc only. 59 cars take every bank from the disc and 80 mix both. No car names an HT or bank
that is only in another pack. So FM4 must load those from the disc, and the converter
does the same: a DLC car's sources are its pack, then the FM4 discs.

**Same name, different bytes.** The converter takes the car's own pack's copy (the pack
comes first), and a disc car never reads a pack. Four cars are affected, and each one's pack
carries the copy it names:
- `12T1_Bently_{Exh,Int}_HT.xml`: the ALMS pack's differs from disc 1's in its loop volumes
  (0 to 1 per loop; the disc's are 0.8 throughout) and some rpm_min/rpm_max, with the same 10
  rpm_sample points. BEN_PMContinental_11 uses them; no disc car's ET names them.
- `4N1_944_EngAmb_HT.xml`: the September Pennzoil pack's differs from the December IGN and
  Meguiar's packs' (two rpm_sample values and a DSP curve). Used by AH_Sprite_58,
  ALF_Spider_86 and MG_MGA_58.
- `4N3_Miata_MG_Int_HT.xml`: Meguiar's and Pennzoil differ in their DSP curves only, which
  the converter doesn't read. Used by MG_MGA_58 and AH_Sprite_58.

Which copy FM4 itself plays when every pack is installed (the `_pri_65` suffix looks like a
mount priority) is not confirmed. It would need the xex's file lookup (#495).

**Built offline** (`engine_sound.forza_engine(..., package=)` and `build_sound` on the 370Z
template, as `engine_sound convert --package` does): 174 of 176 cars get 8+8 loops, and
their banks pass the rebuild's validate, same_structure and lint checks. 173 have
EngineAmbient + Exhaust. BUG_VeyronSS_11 has `NA.xml` for Int and Exh at L0, so it gets
EngineAmbient on both sets. The two misses keep their host's sound, as the disc's Volt and
Leaf do:
- TES_RoadsterS_11 has `NA_*_HT.xml` (bank `NA.fsb`) for every emission;
- HON_CRZ_11 (a hybrid) has `audio_rpm_idle="0"`, like the disc's CHE_Volt_11 and NIS_Leaf_11.

Spot checks against the raw files agree on the ET's idle and redline, the HT's loops
(rpm_sample order, subsong) and the FSB sample counts of the decoded loops. Checked on
MAZ_55_787B_91 (all from its pack), BEN_PMContinental_11 (the pack's Bentley HTs),
ALF_Giulietta_11 (disc banks only), AH_3000_65 (mixed) and POR_959_87.

**Cache.** A bank read from a pack is cached under `out/cache/sound/fm4/dlc/<pack file
name>/`, so no pack's copy can stand in for a disc bank of the same name.

**Format.** FSB4 with XMA, mostly 36 kHz mono; some loops are 48 kHz stereo.
- Every subsong is one whole seamless loop of 0.6–3.4 s, and a sample's pitch is 1 at its `rpm_sample` (HT pitch is always 1).
- Each emission has 10–17 rpm points from idle to past redline.
- No limiter samples and **no off-load set**: Forza applies load only as DSP.
- Turbo, supercharger, gear whine, LFE and backfire come from shared FEV/FSB banks.
- vgmstream decodes XMA to exact header lengths.

**Examples (FH1):**
- The 370Z uses `6N1_350Z` EngAmb, `6N3_350Z` Int and `6N0_VQ35` Exh; FM4 is identical.
- The RX-7 FD uses the genuine 13B `2RT2_RX7` Exh (14 loops) with the RX-8 for EngAmb and a turbo five for Int.
- FM4's RX-7 is all RX-8.

## Pitch and crossfades (#461)

Ports sounded too low (the F50 GT, the LFA in v0.1.8) or off in the blend between loops.
There were two causes, both from the data.

**1. p596 (confirmed-static).** The bank gets rpm + p596·(rpm²/p42 − rpm) (above). The quadratic
leaves idle and the redline where they are and lowers everything between. With p596 = 0.5 the
middle of the range plays 0.75× (−5 semitones). Near idle the shaped rpm falls below p40,
and the floor at VSVH+94 holds the sound at the bank's idle up to about 2× idle.
- Vanilla: 0.5 on 75 cars (the exe default), 0 on 42, 0.25 on 27, 0.15 on 3, 0.75 on 2, 1.0 on
  1. Cars that share a bank (same media) have different p596 (0 and 1.0 on the bank of
  126/129/184). So p596 is Reflections' per-car tuning of how a vanilla bank follows that car's
  engine, not a property of the recordings.
- Ports copied the host's VO3D: p596 0.5 on 309 of the 717 Forza-sound cars of the v0.2.1 build,
  0.25 on 226, 0.75 or 1.0 on 16, 0 on 163.
- Forza's loops play at audio rpm / `rpm_sample`. The HT gives each loop the rpm at which its pitch
  is 1, and the HT pitch is always 1, so the pitch is linear in the rpm. With p596 = 0 the shaping is
  the identity, so a port's bank gets an rpm linear in its engine rpm:
  VSVH+94..+98 over p40..p42, and the ET's audio idle at idle and its audio redline at the redline.
- **Rule:** with the Forza bank, `port_car.fit_forza_sound` sets p596 = 0. A car that keeps its
  host's sound keeps the host's p596. Measured on the v0.2.1 roster (`vehicle_rpm` with the
  host's p596 against p596 0, idle..redline): 554 cars were 100 cents or more low somewhere,
  median 376, p95 875, worst 1980 (p596 1.0). The F50 GT (p596 0.5 from host 232) got 4917 rpm
  where 6400 was due at mid range; the LFA (host 182) had p596 0 in v0.2.1.

**2. Pitch curves of loops heard together (confirmed-static, offline-checked).**
- Vanilla: across the 122 template banks (1986 loop curves), every pitch curve is
  1200·log2(x/R) sampled on its set's one x grid and ends at x = 12000. Every crossfade peaks at its
  loop's R (1962 of 1986; the rest within 2%), and VSVH+94/+98 are the first and last R on most sets
  (201 and 160 of 254). Joined linearly, a curve is off 1200·log2(x/R) by an amount that depends only
  on the two x around it, not on R. So the two loops of a crossfade are off by the same amount and
  stay in tune with each other: on all 122 banks the largest difference between two loops heard
  at once is ≤ 1 cent (`engine_sound.pitch_error`, "detune"). The absolute error is a median 35 cents.
- Ports before: each loop's curve kept the template's x with the one point nearest its Forza R
  moved onto R. Neighbours then had different x, so in every crossfade they were out of tune
  with each other: detune median 53 cents, all 717 cars at least 28, max 112. That makes the
  chorus or phasing a player heard in the blend. The crossfades were warped from the 0-cent
  crossings (921, ...) instead of R (902, ...), so they peaked 1–2% off the Forza R.
- **Rule** (`engine_sound.retune`, `shared_grid`):
  - The crossfades are warped from the template loops' R (`recording_rpm`: each point gives
    R = x/2^(cents/1200); all of a vanilla curve's agree to 0.3%) to the Forza R, so they peak there.
  - Each layer's loop curves are built on shared x points over the `vehicle_rpm` range the game
    sends (`vehicle_rpm_span`: VSVH+94·0.99 up to the value at p42·(1 + p364)·1.1, times 1.01).
    The knots are the span's ends and the crossfade ends (the loops' R); more points go into the
    largest log steps while every loop heard there has one to spare. Leftover points sit below
    the range where the loop is heard. Each curve keeps its template's point count and ends at
    max(12000, the span's top). Before, a car whose span passes 12000 (35 cars) had its pitch
    held there: the F50 GT, 12000 at the redline, revs to about 16000.
  - Roster (v0.2.1's templates and loops, curves rebuilt; `pitch_error` over each car's span):
    detune median 53 → 0 cents (max 112 → 0), largest pitch error median 60 → 5.5 cents
    (max 636 → 12.6). In 4 cars (AUD_2_R8_06, SUZ_1_SX4_11, AUD_3_R18TDI_11,
    FOR_TransitSSV_11) a loop recorded below the car's idle is never heard; it keeps the template
    x with one point on R.

**VSVH+94/+98 = the ET's `audio_rpm_idle`/`audio_rpm_redline` (kept; inferred).** DSF maps the
engine's idle..redline onto a per-bank range, and the ET fields are named for that. How Forza
itself maps physics rpm onto audio rpm is not decoded. The ET range is per car, not per bank:
on 78 of 88 banks shared by cars with different redlines, the cars have different ET ranges.
Its idle equals the gamedb `StallRPM` on 322 of 571 cars (otherwise up to 1.2× higher). Its
redline over `RedlineRPM` has quantiles 0.92 (5%), 1.0 (median), 1.14 (95%). If Forza played
the loops at physics rpm instead, ports would be that much off at the redline. Every audio idle
and every stall rpm is covered by a loop, so the data doesn't tell the two apart. Deciding it
needs the xex (the code reading `audio_rpm_redline`).

**In game (confirmed-runtime).** `dsfforza.asi` with `[soundlog] log=1` (`mod_soundlog.cpp`)
logs, per engine sound, the engine rpm, the shaped rpm, the `vehicle_rpm` sent, VSVH+94/+98,
p40, p42 and p596. Setup: headless VM 1, `run-series.ps1 -Vm -Dyno -DynoKeys 'Up:25,Down:6'`.
"Before" is a master build (7fe208d) of the same three cars and "after" this branch,
`out/b461/{before,after}`. Runs vm1-20261105-122059 and -122358 (IDs 1, 2), -122756 and
-123124 (ID 3, vanilla 227). Each line below is the median of the logged updates near that
engine rpm, with "linear" the value p596 = 0 gives:

| Car (p596 before) | engine rpm | sent before | sent after | linear |
| --- | --- | --- | --- | --- |
| LFA (0, host 182) | idle 614 / redline 8633 | 800 / 8823 | 800 / 8889 | 800 / 8795 |
| F50 GT (0.5, host 232) | mid ~6000 | 5312 at 6203 | 6318 at 5772 | 6809 / 6343 |
| 2002 Turbo (1.0, host 126) | mid ~3500 | 2256 at 3528 | 4069 at 3612 | 4104 / 4199 |
| vanilla 227 (0) | idle 632 / mid 4260 | 902 / 3600 | | 902 / 3611 |

- The sent value equals max(VSVH+94, VSVH+94 + n·range) of the logged shaped rpm on every line
  (ratio 1.0000, 0.9999..1.0001). The jitter didn't act: rec+0xB0 stayed 1.
- Shaped rpm over the p596 shaping of the engine rpm: 0.94..1.04, the 0.1 noise at about half
  its bound.
- The F50 GT was 430 cents low at mid range before and the 2002 Turbo 1036 cents; after, both
  are within the noise of the linear value, as vanilla 227 is.
- This checks the rpm the bank gets, not what Wwise plays: the VMs' audio is muted, and nobody
  has listened.

## Routes, ranked

1. **(a) Media swap into a host bank, with curve retune.** Done, offline, and in the build (below).
   - Keep a vanilla sample bank's HIRC. Put Forza EngAmb loops into set A and Exh loops into set B: 8 loops each, chosen to minimise the largest rpm step (at most about 1.4×).
   - Re-encode to IMA ADPCM with our own encoder (38 dB SNR re-encoding vanilla media; 0.25 s per loop).
   - Recompute the pitch curves for the Forza rpms on x points shared by the loops of each crossfade layer; warp the crossfades and the other `vehicle_rpm` curves from the template's recording rpms onto the new ones ("Pitch and crossfades").
   - Set VSVH+94/98 to the Forza idle/redline, so bank rpm equals Forza rpm.
   - Give the bank, all 177 objects, the layers and the media fresh FNV IDs, and patch the VSVH event IDs to match.
   - Size: SBAK changes by -0x3700..+0x2F5C against its host (RX7 and 370Z +0x1C4B / +0x2F5C on 227, the FM4 RX7 -0x36E4 on 147), with a 1.1 s loop cap (`--max-seconds` trades size against seam crossfades).
   - Risks:
     - nothing heard in game yet;
     - the meaning of params 6 and 11;
     - Forza has no off-load loops (DSF's sets don't need one);
     - shared global layers (whine) stay the template's; the engine-type switch now follows the Forza engine.
   - Integrated into `port_car`/`port_batch`; what is left is in-game listening and tuning of levels.
2. **(b) Programmatic bank.** The parser handles every object type the vehicle banks use and rebuilds all 150 byte-identically. The practical version is to extend the template by cloning Sounds and layer entries, which would allow all 13–17 Forza points or a third (intake) set.
   - About 1–2 days; worth it only if (a)'s 8 loops per set sound too coarse.
3. **(d) Granular (GRBK).** The format is known, but grains would have to be cut per combustion cycle from the Forza loops, it only plays for the local player, and only 19 vanilla cars use it.
   - Research effort of days, and a lower payoff. For ports on granular hosts, use (a) with a sample-bank sound host. `engine_sound.py apply` drops the GRBK.
4. **(c) Wwise authoring tool of the matching version (2010.x/2011.1).** Not offered by the current Launcher; it would need Audiokinetic to supply a legacy build, a licence for modding use, and a re-created DSF project (state, switch and RTPC names are now known by FNV). Not needed given (a).

## In the build (offline-checked)

`port_car.py` step 5 and every `port_batch.py` worker, on by default
(`--no-forza-sound` turns it off). Per car (`port_car.forza_sound_plan`,
`fit_forza_sound`, `src/dsfforza/convert/engine_sound.py`):

1. **Forza audio.** `<car>_ET.xml` → HT file per emission (L0) → FSB bank,
   decoded once into `out/cache/sound/<game>/<bank>/` (each worker decodes
   into its own temporary folder and renames it, so parallel workers sharing
   a bank don't race). Set A takes EngineAmbient, else EngineIntake, else
   Exhaust; set B takes Exhaust, else the other two (`SET_EMISSIONS`). Race
   and some road cars have `NA.xml` for an emission; the Veyron and the 250
   GTO have no Exh and Int, so both sets get EngAmb.
2. **Template.** `engine_sound.vanilla_index` (cached in
   `out/cache/sound/vanilla-banks.json`) lists the vanilla banks with the 370Z
   layout: two loop sets of 6 to 8 loops (Sounds with an rpm pitch curve)
   each, no GRBK, and no other embedded media under the two play events.
   122 of the 150 vanilla cars qualify. Out: the 19 granular cars, four banks
   without sample loop sets (123, 131, 289, 250), and five whose sets also
   hold a random container of 0.13 s idle grains pitched on the container
   (126, 129, 184: 7 grains; the Monaco 178 and 302: 16). Those grains would
   keep playing the template's engine at idle under the Forza loops; they
   are what the first survey counted as 8+14 and 15+15. No vanilla bank has
   more than 8 rpm loops per set. They are tried in the order of
   `host_select.rank(mode="sound")` (identity, engine, era), an explicit
   `--sound-host` first, at most three, then the 370Z 227 as the proven
   fallback. So a V8 gets a V8's bank, with its backfire pop, gear events and
   shared layers. The visual host's own bank is used when it is the best
   engine match; a granular host's GRBK is dropped.
3. **Loops.** As many as the template set has slots (`pick_loops`); a car
   with fewer usable loops (the Durango: 5 Exh loops) gets the largest rpm
   gap split by a copy of its lower loop resampled to the gap's geometric
   middle, instead of duplicate loops at one rpm.
4. **Pitch.** Curves and crossfades as in "Pitch and crossfades"; the VO3D's p596 becomes 0
   with the Forza bank, and the bank is built for the car's p40, p42 and p364.
5. **Engine type.** VSVH+4C from the Forza cylinder count and layout
   (`engine_type`: 4 cylinders and 2-rotor rotaries il4, flat-4 h4, 5 il5,
   6 v6 (vr6 for the VR6, h6 flat), 8 v8, 10 v10, 12 v12, W16 v8; electric
   keeps the template's).
6. **Fresh IDs** (`fresh_ids`). New ID = (spool ID << 22) | 22 bits of
   FNV-1(`dsfforza:<template>:<old id>:<salt>`), the salt counting up while
   the value is 0, one of the template's IDs, already given, or in the
   reserved set: every ID the 150 vanilla vehicle banks define or reference
   plus every bank, HIRC object, media and streamed-file ID of
   `WWiseDefault.pck` (9897 IDs; the AKPK bank lookup table points at each
   bank). Deterministic, and two cars in one build (different spool IDs)
   can't collide. The bank gets the name `dsfforza_<game>_<media>` in STID.
   (`remerge` rewrites spool IDs but not bank IDs; the old spool IDs were
   unique, so the IDs stay unique.)
7. **Budget.** Each candidate (template × loop cap 1.1, 0.8, 0.6 s) is
   installed and the full entry checked against its NONR buffer (0x6D000);
   the first that fits stays. If none fits, or the car's Forza audio can't be
   resolved, the clone keeps its host's (or `--sound-host`'s) VSRT and the
   reason goes to the log and the report.
8. **Outputs.** `batch-report.json` has per car `sound`: Forza or not (and
   why not), template, bank ID, SBAK size, loop cap, idle/redline, engine
   type, and per set the emission, FSB bank, loops used of available, their
   rpms, and two pitch ranges: `pitch` (every loop while its crossfade is
   above -40 dB) and `pitch_main` (the loudest loop at each rpm). Previews in
   `<out>/sound/<media>/`: `sweep_new.wav` (the new bank, idle → redline →
   idle, both sets at full load), `sweep_template.wav` (the template bank as
   it would play on this car), `wav/` (every new loop as the game decodes
   it), `report.json`, `SBAK.bin`, `VSVH.bin`. A worker whose sound step
   crashes is retried once with `--no-forza-sound`.

**Lint** (`upvh.py lint`, run on every car by `port_car`/`port_batch`;
also `engine_sound.py lint ARCHIVE --id N`):
- VSVH is 0x9C bytes with the spool ID at +0;
- the SBAK parses (v48) and rebuilds byte-identically, every Sound's offset
  and size point at its media, media are whole ADPCM blocks;
- every non-zero `EVENT_FIELDS` value is an Event of the bank, its actions
  are in the bank, and sets A and B play something in the bank;
- IDs across the archive: a `dsfforza` bank's own IDs (bank, objects,
  layers, media) appear in no other vehicle's bank and in no reserved ID;
  any other bank may share IDs only with banks giving them the same content
  (the masked rule above: clones and `--sound-host` copies share their
  template's bank, as vanilla cars do). All 150 vanilla cars pass;
- for a `dsfforza` bank also the rpm range rule and both loop sets present.

Build-time checks on top: `same_structure` against the template under the
ID map, `Bank.validate`, and the event check above, before a candidate is
accepted.

### The 40-car test build (offline-checked)

`uv run dsff port_batch build --dsf out/vanilla/Install/dngvehicles.sp
--list tools/batches/testdrive.txt --gui --out out/testdrive8 --jobs 10`
(30 min; one car rebuilt with `--resume` after the idle-grain rule, below):

- All 40 cars got their Forza sound, none fell back; all 40 pass lint,
  including the archive-wide ID check.
- 28 different templates; on 18 cars the template is the visual host's own
  bank. The 1.1 s loop cap fits everywhere: NONR 0x4DBC0..0x5D570 (limit
  0x6D000), SBAK 0x4648C..0x54FE0.
- The lint did its job once: the M3 E30's first build used template 126,
  whose set B also holds seven 0.13 s idle grains; the "no media but the rpm
  loops" check failed it, which led to excluding such templates (5 banks).
- The loudest loop at each rpm is pitched 0.65..1.5× on most cars. Outliers
  come from gaps in Forza's own loop tables: 0.41× near idle on the cars
  using `8N3_PanozGTLM_EngAmb` (Cuda, Charger Daytona, S5S Raptor, Agera:
  its lowest loop is far above idle), 0.45× with `8N3_PanozGTLM_Exh` (300C,
  Jeep), 0.36× on the M5 (`5T3_TurboHeavyAmb`), up to 1.97× on the TT RS
  and Focus RS500 (`10N2_SRT10_EngAmb` jumps from 529 to 1633 rpm). Worth a
  listen first.
- Per car: `batch-report.json` → `ok[].sound`; previews in
  `out/testdrive8/sound/<media>/`.

### Option: mixing the intake loops

Not done; documented as an option. Forza plays three loop sets at once
(EngAmb, Int, Exh); DSF has two emitters, and the build drops Int unless a
car lacks EngAmb or Exh. In `testdrive.txt` the Int bank is often another
engine's or a generic turbo intake (`5T3_TurboHeavyInt` on the Supra, 205
T16, Huayra, Focus RS500; `5T3_Turbo` on the RX-7), so for turbo cars the
intake carries much of the character, at HT volumes of 0.8–1.0 like the
other emissions.

How it would work (`build_sound`, set A): for each chosen EngAmb loop at rpm
r take the Int loop with the nearest rpm r_i, resample it periodically by
r_i / r (so it plays at r), cut it to the EngAmb loop's length with a
crossfaded seam (as `seamless` does), and add it at its HT volume relative
to the EngAmb loop before encoding. About 30 lines and no extra bank size.
Costs: a second seam per loop and two independently periodic signals in one
loop (a beat at the seam). Worth trying as `--forza-sound-intake` once the
plain swap has been heard in game; comparing `sweep_new.wav` with and
without it offline is the first step.

## Proof of concept (offline-checked)

```sh
uv run dsff engine_sound convert --car MAZ_RX7_97 --game fh1 --host 227 --out out/sound/MAZ_RX7_97
uv run dsff port_batch build --dsf out/vanilla/Install/dngvehicles.sp --car MAZ_RX7_97 --out out/sound/build \
    --jobs 1 --port "--mdxn-limit 0x2C0000 --damage --sound-host 227"
uv run dsff engine_sound apply out/sound/build/dngvehicles.sp out/sound/build/dngvehicles.forza-sound.sp \
    --spool 1 --sound out/sound/MAZ_RX7_97
```

Converted so far:
- `out/sound/MAZ_RX7_97`: FH1, onto the 370Z bank 227.
- `out/sound/NIS_370Z_10`: FH1, onto 227 itself.
- `out/sound/fm4_MAZ_RX7_97`: FM4, onto the CTS-V bank 147.

(These used the first ID scheme, plain FNV hashes; the build now uses the spool-ID namespace below.)

Each folder has `SBAK.bin`, `VSVH.bin`, `report.json`, `tree.txt` (RX7), `wav/` (every loop as the game will decode it), `sweep_host.wav` / `sweep_new.wav` (an offline render of idle → redline → idle through both sets' own pitch and crossfade curves) and `sweeps.png` (their spectrograms).

Checks (offline-checked):
- `wwise_bank.py check` parses every object to its exact size and rebuilds byte-identically. Every Sound's offset and size point at its media, and the media are whole ADPCM blocks. This passes on all 150 vanilla banks and the three new ones.
- `engine_sound.same_structure` confirms the new bank is the host's hierarchy under the ID map: same objects, order, sizes and fields; only curve points, media and IDs differ. A corrupted child reference is caught.
- The sweeps show unbroken harmonics through every crossfade. The RX7 has 2 orders per revolution (rotary), the 370Z 3 (V6).

Test archive (offline-checked): `out/sound/build/dngvehicles.forza-sound.sp`. It holds the RX7 as ID 1 on the Solstice GXP 278, with the Forza sound. The same build without the sound step is `dngvehicles.sp` (the RX7 with the vanilla 370Z sound), the A/B control. Both use the same `dngvehicles.lua`.
- NONR is 0x57D80 against the 0x6D000 limit.
- The VSVH has +0 = 1 and rpm range 800/8000, and its 19 event fields resolve in the new bank.
- SBAK is 16-byte aligned.
- `upvh.py lint` reports 0 problems.
- The batch build took 43 min at Idle priority; `apply` takes 3 s.

## Verifying in game (needs the user: no audio capture)

With a build of `tools/batches/testdrive.txt` (IDs 1..40 in its order;
`out/testdrive8` was the first) or of the full roster, whose list starts
with the same 40 cars at the same IDs (checked on `out/build`). Tracked in
#202:

1. **Does it load?** Spawn a few ports in free roam or the test drive: ID 32
   (370Z, template 227, its own model's bank), 3 (RX-7: rotary on an I4
   template), 30 (LFA: V10) and 19 (Durango: only 5 Forza loops). Silence
   means the bank load failed (`FUN_0048f780`); a vanilla-sounding engine means
   the template's media won (an ID problem).
2. **Fresh IDs.** In one session spawn a port and its template's vanilla car
   (370Z 32 next to vanilla 227; RX-7 3 next to vanilla 274 Scirocco): each must
   sound like itself, whichever loads first.
3. **Follows rpm and load?** Idle, a slow full-throttle pull to the redline
   in second, lift, idle. Compare by ear with
   `out/testdrive8/sound/<media>/sweep_new.wav` (and `sweep_template.wav` for
   what the template sounds like); check interior and exterior cameras
   (emitters A and B), and AI traffic using the port (the AI subset of loops).
4. **Things to listen for:** clicks at loop seams (loops over 1.1 s are cut
   with a 60 ms crossfade), a chorus or phasing in crossfades between loops,
   levels against vanilla cars (set gains match the template's loudest loop by
   RMS), and the low rumble tone of VSVH+4C (rotary/I4 vs V6 vs V8).
5. Earlier single-car test: the port (ID 1) of `out/sound/build/dngvehicles.forza-sound.sp`. Silence means the bank load failed (`FUN_0048f780`). Also spawn the vanilla 370Z (227) in the same session: each must sound like itself, which tests the fresh IDs.
6. **Objective check (proposal):** add WASAPI loopback capture to the host harness (e.g. Python `soundcard` or ffmpeg). Record a scripted rev and compare spectrograms against `sweeps.png`: the rotary's 2nd-order harmonic structure versus the V6's 3rd order tells the swapped sound from the host's without anyone listening. Without audio capture, nothing in the repo can yet confirm that the bank ID (0x27B29A12 for the RX7) is resident: `src/dsfforza/formats/minidump.py` only reads a .dmp file written earlier (`minidump.py crash.dmp [--stack N]`) and can't inspect a running game; that would need a memory read of the running process.
7. Then tune levels: the set gains are matched to the template's loudest loop by RMS, and Forza's per-loop HT volumes are kept.

## Open

- Everything in game (above; #202). Nobody has listened to the #461 change; the plugin log
  checks the rpm the bank gets, not what Wwise plays.
- How Forza maps physics rpm onto audio rpm (`audio_rpm_idle`/`audio_rpm_redline`, "Pitch and
  crossfades"): not decoded.
- What ParamIDs 6 and 11 control, and whether `vehicle_gear_direction` is ever set (no setter was found).
- VSVH+4C is now set from the Forza engine; what the tone adds is not heard yet.
- Intake mixing (option above) and the right exhaust channel (ExhaustR, the right half of stereo subsongs) are not used.
- Templates come from the sound ranking; the template's backfire pop and gear events stay (the pop is a 0.2 s media of the template's engine).
