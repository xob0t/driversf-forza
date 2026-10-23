# Engine sound: giving ports their own Forza engine

Updated: 2026-10-23

Can a ported car sound like its Forza self instead of borrowing a stock
DSF car's engine? Yes. Forza's engine loops can be swapped into a stock
DSF bank, keeping the bank's structure, and the bank still checks out
offline. `port_car`/`port_batch` now do this for every car by default
(section "In the build"). It has not been heard in game yet.

Evidence levels as in `dngvehicles.md`:
- **confirmed-static**: code or file read.
- **data**: seen across the stock fleet or Forza files.
- **offline**: produced and checked by our tools.
- **hypothesis**.

Tools:
- `tools/wwise_bank.py`: Wwise v48 bank parser, rebuilder and IMA ADPCM codec.
- `tools/engine_sound.py`: Forza to DSF conversion and the archive step.
- `tools/forza_audio.py`: Forza FSB, ET and HT access.
- `tools/ghidra/DecompileRange.java`.

## DSF side

### Bank and loading (confirmed-static)

- `NONR/VSRT` holds `VSVH` (0x9C), `SBAK` (a Wwise bank) and on 19 cars `GRBK`.
- **Version:** `FUN_00c60e70` rejects any bank whose BKHD version isn't 0x30 (48). That is about Wwise 2010.x/2011.1; no SDK version string is linked. Every bank in `WWiseDefault.pck` is v48 too.
- **Loading:** `FUN_0084bf30` / `FUN_0084beb0` call `AK::SoundEngine::LoadBank(ptr, size)` from memory, asynchronously, in place in the spool buffer. The pointer must be 16-byte aligned and the size at least 0x18. The bank ID is read from BKHD.
- **Failure mode:** if the load fails, `FUN_0048f780` leaves the car silent.
- **No hard-coded bank contents:** the code never looks up object or media IDs inside a vehicle bank. It only posts the event IDs stored in VSVH and sets global RTPC, switch and state IDs.

### IDs are global (data)

Across the 150 stock banks:
- 3582 HIRC IDs and 617 media IDs; 3113 of the HIRC IDs appear in more than one bank.
- Yet an ID never has different content in two banks.
- There are only 46 distinct engine media sets. For example, the Stratos 217 and the 370Z 299 carry the 227 370Z's objects and media under their own bank IDs.

"Same content" holds byte for byte once three things are masked: references
to the bank's own ID (a Sound's file ID, a Play action's bank), a Sound's
media offset in its bank file, and an ActorMixer's children list (each bank
lists only its own children of a shared mixer). With those masked, 0 of the
fleet's shared IDs differ (data; `engine_sound._content`, the lint's rule for
banks that are not ours).

This is ordinary Wwise: objects and media are indexed by ID across all loaded banks. So a modified bank must not reuse its host's IDs. If the host car's bank loads first, its objects and media would be the ones played (hypothesis based on the SDK's behaviour; the stock data is consistent with it).

### Media (confirmed-static, data)

- Every embedded media in every stock vehicle bank is RIFF format 0x0002, Wwise IMA ADPCM:
  - mono, 32 kHz;
  - 0x24-byte blocks of 64 samples: s16 first sample, u8 step index, then 63 nibbles, low nibble first;
  - chunks are `fmt`, `JUNK`, `data`.
- The decoder in `wwise_bank.py` uses the (2n+1)·step/8 form and matches vgmstream sample for sample on stock media.
- Loops are whole-file (`Loop = 0`, infinite), so each loop must be a whole number of 64-sample blocks.

### 370Z bank (227), the template (confirmed-static)

Run `wwise_bank.py tree out/sound/stock/227/SBAK.bin --curves`. The bank holds 17 media (285 KB), a 37 KB HIRC and 177 objects.

- **Set A**, event at VSVH+04 → switch `audio_mode` (player/challenger) → state `cameratype` → layer containers: 8 loops at 921, 1728, 2085, 2729, 3526, 4429, 5274 and 6051 rpm.
- **Set B**, VSVH+0C: 8 different loops at 921, 1509, 2097, 2715, 3333, 4278, 5123 and 6051.
- Per Sound in a set:
  - a pitch RTPC on `vehicle_rpm` (0x9C4D0121) = 1200·log2(rpm / recording rpm), stored as 13 linearly interpolated points over 0..12000;
  - a layer crossfade on `vehicle_rpm`: triangles between neighbours, the first slot full below idle, the last full above the top.
- AI (mode 1) uses a 4-loop subset with wide crossfades.
- Both sets carry identical `vehicle_engineload` curves (params 6 and 11, -90 to 0 dB; names unresolved). So **there is no on-load/off-load pair**; the two sets are two microphone positions.
- The 17th media is a 0.2 s backfire pop (VSVH+70/74).
- Shared sounds (whine, tyres, wind, tone generators) reference media in other banks.
- Fleet: 93 of the 131 sample banks have 8+8 loops; the others have 7+7, 8+7, 7+8, 8+14 or 15+15.

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
On all 150 stock cars every non-zero value at +04, +08, +0C, +10, +14, +1C,
+24, +28, +2C, +34..+48, +54..+5C and +68..+78 is an Event of the car's own
bank, and no other field ever is (data; `engine_sound.EVENT_FIELDS`, checked
by the lint).

**Engine type, VSVH+4C (confirmed-static, data).** `0x550300` reads +4C,
subtracts 1 and indexes a 10-entry jump table; anything out of range
(including 0) gives v8:

| +4C | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | other |
|---|---|---|---|---|---|---|---|---|---|---|---|
| state | v6 | v8 | v10 | v12 | vr6 | h4 | h6 | il4 | il5 | il5 (not il6) | v8 |

Stock values (engine from `vehicleStats`): 2 on 67 V8s, 9 V12s, 4 I6s, 3
V10s and a W12; 1 on 9 V6s and 4 I6s; 8 on 11 I4s; 10 on 6 I6s (which
therefore get il5); 3, 5, 6, 9 on a few cars each; 0 on 23 (granular and
silent ones included). Reflections' own values are loose. The switch is
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

**rpm path:**
1. The game shapes the rpm: rpm + p596·(rpm²/p42 − rpm), with ±10% noise.
2. It normalises n = (rpm − p40)/(p42 − p40).
3. It sends `vehicle_rpm` = VSVH+94 + n·(VSVH+98 − VSVH+94), with ±5% jitter. This mapping is used only when +94 < 5000 and +98 < 15000.

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

## Forza side (confirmed-static; `tools/forza_audio.py`)

**Where the banks are.** Engine banks are shared per engine recording, not per car: `media/audio/cars/Engines/Soundbanks/LOD1/<code>_<name>_{EngAmb,Exh,Int}.fsb`.
- FH1: 943 banks, 207 MB.
- FM4 disc 1: 359 banks, 79 MB.
- FM4 packs: 68 more banks.

**Car to bank:**
1. `EngineTuning.zip/<car>_ET.xml` gives idle, redline and, per emission and upgrade level, an HT file.
2. `HarmonicTuning.zip/<x>_HT.xml` gives the bank and one `<Loop rpm_min rpm_sample rpm_max volume>` per subsong.
3. The mapping resolves for 146/175 FH1 cars, 277/312 FM4 disc 1 cars and 179/188 pack cars. The misses are mostly race cars with `NA.xml` at L0.

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

## Routes, ranked

1. **(a) Media swap into a host bank, with curve retune.** Done, offline, and in the build (below).
   - Keep a stock sample bank's HIRC. Put Forza EngAmb loops into set A and Exh loops into set B: 8 loops each, chosen to minimise the largest rpm step (at most about 1.4×).
   - Re-encode to IMA ADPCM with our own encoder (38 dB SNR re-encoding stock media; 0.25 s per loop).
   - Recompute the pitch curves for the Forza rpms, with one curve point placed on the recording rpm. Warp the crossfades and the other `vehicle_rpm` curves onto the new points.
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
3. **(d) Granular (GRBK).** The format is known, but grains would have to be cut per combustion cycle from the Forza loops, it only plays for the local player, and only 19 stock cars use it.
   - Research effort of days, and a lower payoff. For ports on granular hosts, use (a) with a sample-bank sound host. `engine_sound.py apply` drops the GRBK.
4. **(c) Wwise authoring tool of the matching version (2010.x/2011.1).** Not offered by the current Launcher; it would need Audiokinetic to supply a legacy build, a licence for modding use, and a re-created DSF project (state, switch and RTPC names are now known by FNV). Not needed given (a).

## In the build (offline)

`port_car.py` step 5 and every `port_batch.py` worker, on by default
(`--no-forza-sound` turns it off). Per car (`port_car.forza_sound_plan`,
`fit_forza_sound`, `tools/engine_sound.py`):

1. **Forza audio.** `<car>_ET.xml` → HT file per emission (L0) → FSB bank,
   decoded once into `out/cache/sound/<game>/<bank>/` (each worker decodes
   into its own temporary folder and renames it, so parallel workers sharing
   a bank don't race). Set A takes EngineAmbient, else EngineIntake, else
   Exhaust; set B takes Exhaust, else the other two (`SET_EMISSIONS`). Race
   and some road cars have `NA.xml` for an emission; the Veyron and the 250
   GTO have no Exh and Int, so both sets get EngAmb.
2. **Template.** `engine_sound.stock_index` (cached in
   `out/cache/sound/stock-banks.json`) lists the stock banks with the 370Z
   layout: two loop sets of 6 to 8 loops (Sounds with an rpm pitch curve)
   each, no GRBK, and no other embedded media under the two play events.
   122 of the 150 stock cars qualify. Out: the 19 granular cars, four banks
   without sample loop sets (123, 131, 289, 250), and five whose sets also
   hold a random container of 0.13 s idle grains pitched on the container
   (126, 129, 184: 7 grains; the Monaco 178 and 302: 16). Those grains would
   keep playing the template's engine at idle under the Forza loops; they
   are what the first survey counted as 8+14 and 15+15. No stock bank has
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
4. **Engine type.** VSVH+4C from the Forza cylinder count and layout
   (`engine_type`: 4 cylinders and 2-rotor rotaries il4, flat-4 h4, 5 il5,
   6 v6 (vr6 for the VR6, h6 flat), 8 v8, 10 v10, 12 v12, W16 v8; electric
   keeps the template's).
5. **Fresh IDs** (`fresh_ids`). New ID = (spool ID << 22) | 22 bits of
   FNV-1(`dsfforza:<template>:<old id>:<salt>`), the salt counting up while
   the value is 0, one of the template's IDs, already given, or in the
   reserved set: every ID the 150 stock vehicle banks define or reference
   plus every bank, HIRC object, media and streamed-file ID of
   `WWiseDefault.pck` (9897 IDs; the AKPK bank lookup table points at each
   bank). Deterministic, and two cars in one build (different spool IDs)
   can't collide. The bank gets the name `dsfforza_<game>_<media>` in STID.
   (`remerge` rewrites spool IDs but not bank IDs; the old spool IDs were
   unique, so the IDs stay unique.)
6. **Budget.** Each candidate (template × loop cap 1.1, 0.8, 0.6 s) is
   installed and the full entry checked against its NONR buffer (0x6D000);
   the first that fits stays. If none fits, or the car's Forza audio can't be
   resolved, the clone keeps its host's (or `--sound-host`'s) VSRT and the
   reason goes to the log and the report.
7. **Outputs.** `batch-report.json` has per car `sound`: Forza or not (and
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
  template's bank, as stock cars do). All 150 stock cars pass;
- for a `dsfforza` bank also the rpm range rule and both loop sets present.

Build-time checks on top: `same_structure` against the template under the
ID map, `Bank.validate`, and the event check above, before a candidate is
accepted.

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

## Proof of concept (offline)

```sh
python tools/engine_sound.py convert --car MAZ_RX7_97 --game fh1 --host 227 --out out/sound/MAZ_RX7_97
python tools/port_batch.py build --dsf out/stock/Install/dngvehicles.sp --car MAZ_RX7_97 --out out/sound/build \
    --jobs 1 --port "--mdxn-limit 0x2C0000 --damage --sound-host 227"
python tools/engine_sound.py apply out/sound/build/dngvehicles.sp out/sound/build/dngvehicles.forza-sound.sp \
    --spool 1 --sound out/sound/MAZ_RX7_97
```

Converted so far:
- `out/sound/MAZ_RX7_97`: FH1, onto the 370Z bank 227.
- `out/sound/NIS_370Z_10`: FH1, onto 227 itself.
- `out/sound/fm4_MAZ_RX7_97`: FM4, onto the CTS-V bank 147.

(These used the first ID scheme, plain FNV hashes; the build now uses the spool-ID namespace below.)

Each folder has `SBAK.bin`, `VSVH.bin`, `report.json`, `tree.txt` (RX7), `wav/` (every loop as the game will decode it), `sweep_host.wav` / `sweep_new.wav` (an offline render of idle → redline → idle through both sets' own pitch and crossfade curves) and `sweeps.png` (their spectrograms).

Checks (offline):
- `wwise_bank.py check` parses every object to its exact size and rebuilds byte-identically. Every Sound's offset and size point at its media, and the media are whole ADPCM blocks. This passes on all 150 stock banks and the three new ones.
- `engine_sound.same_structure` confirms the new bank is the host's hierarchy under the ID map: same objects, order, sizes and fields; only curve points, media and IDs differ. A corrupted child reference is caught.
- The sweeps show unbroken harmonics through every crossfade. The RX7 has 2 orders per revolution (rotary), the 370Z 3 (V6).

Test archive (offline): `out/sound/build/dngvehicles.forza-sound.sp`. It holds the RX7 as ID 1 on the Solstice GXP 278, with the Forza sound. The same build without the sound step is `dngvehicles.sp` (the RX7 with the stock 370Z sound), the A/B control. Both use the same `dngvehicles.lua`.
- NONR is 0x57D80 against the 0x6D000 limit.
- The VSVH has +0 = 1 and rpm range 800/8000, and its 19 event fields resolve in the new bank.
- SBAK is 16-byte aligned.
- `upvh.py lint` reports 0 problems.
- The batch build took 43 min at Idle priority; `apply` takes 3 s.

## Verifying in game (needs the user: no audio capture)

With a batch build (`out/testdrive8` in the worktree of PR "Forza engine
sound in the build"; IDs 1..40 in `tools/batches/testdrive.txt` order):

1. **Does it load?** Spawn a few ports in free roam or the test drive: ID 32
   (370Z, template 227, its own model's bank), 3 (RX-7: rotary on an I4
   template), 30 (LFA: V10) and 19 (Durango: only 5 Forza loops). Silence
   means the bank load failed (`FUN_0048f780`); a stock-sounding engine means
   the template's media won (an ID problem).
2. **Fresh IDs.** In one session spawn a port and its template's stock car
   (370Z 32 next to stock 227; RX-7 3 next to stock 274 Scirocco): each must
   sound like itself, whichever loads first.
3. **Follows rpm and load?** Idle, a slow full-throttle pull to the redline
   in second, lift, idle. Compare by ear with
   `out/testdrive8/sound/<media>/sweep_new.wav` (and `sweep_template.wav` for
   what the template sounds like); check interior and exterior cameras
   (emitters A and B), and AI traffic using the port (the AI subset of loops).
4. **Things to listen for:** clicks at loop seams (loops over 1.1 s are cut
   with a 60 ms crossfade), a chorus or phasing in crossfades between loops,
   levels against stock cars (set gains match the template's loudest loop by
   RMS), and the low rumble tone of VSVH+4C (rotary/I4 vs V6 vs V8).
5. Earlier single-car test: the port (ID 1) of `out/sound/build/dngvehicles.forza-sound.sp`. Silence means the bank load failed (`FUN_0048f780`). Also spawn the stock 370Z (227) in the same session: each must sound like itself, which tests the fresh IDs.
6. **Objective check (proposal):** add WASAPI loopback capture to the host harness (e.g. Python `soundcard` or ffmpeg). Record a scripted rev and compare spectrograms against `sweeps.png`: the rotary's 2nd-order harmonic structure versus the V6's 3rd order tells the swapped sound from the host's without anyone listening. Without audio capture, `tools/minidump.py` on the running game can confirm that the bank ID (0x27B29A12 for the RX7) is resident.
7. Then tune levels: the set gains are matched to the template's loudest loop by RMS, and Forza's per-loop HT volumes are kept.

## Open

- Everything in game (above).
- What ParamIDs 6 and 11 control, and whether `vehicle_gear_direction` is ever set (no setter was found).
- VSVH+4C is now set from the Forza engine; what the tone adds is not heard yet.
- Intake mixing (option above) and the right exhaust channel (ExhaustR, the right half of stereo subsongs) are not used.
- Templates come from the sound ranking; the template's backfire pop and gear events stay (the pop is a 0.2 s media of the template's engine).
