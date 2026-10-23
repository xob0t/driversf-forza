# Engine sound: giving ports their own Forza engine

Updated: 2026-10-23

Can a ported car sound like its Forza self instead of borrowing a stock
DSF car's engine? Yes. Forza's engine loops can be swapped into a stock
DSF bank, keeping the bank's structure, and the bank still checks out
offline. It has not been heard in game yet.

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

1. **(a) Media swap into a host bank, with curve retune.** Done, offline.
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
     - shared global layers (whine, engine-type switch) stay the host's.
   - **Effort to ship:** about 1 day to integrate it into `port_car`/`port_batch` (`--engine-sound forza`, picking a sample-bank sound host when the visual host is granular), plus in-game tuning of levels.
2. **(b) Programmatic bank.** The parser handles every object type the vehicle banks use and rebuilds all 150 byte-identically. The practical version is to extend the template by cloning Sounds and layer entries, which would allow all 13–17 Forza points or a third (intake) set.
   - About 1–2 days; worth it only if (a)'s 8 loops per set sound too coarse.
3. **(d) Granular (GRBK).** The format is known, but grains would have to be cut per combustion cycle from the Forza loops, it only plays for the local player, and only 19 stock cars use it.
   - Research effort of days, and a lower payoff. For ports on granular hosts, use (a) with a sample-bank sound host. `engine_sound.py apply` drops the GRBK.
4. **(c) Wwise authoring tool of the matching version (2010.x/2011.1).** Not offered by the current Launcher; it would need Audiokinetic to supply a legacy build, a licence for modding use, and a re-created DSF project (state, switch and RTPC names are now known by FNV). Not needed given (a).

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

## Verifying in game (needs game runs)

1. **Does it load?** Spawn the port (ID 1) in the test drive. Silence means the bank load failed (`FUN_0048f780`). Also spawn the stock 370Z (227) in the same session: each must sound like itself, which tests the fresh IDs.
2. **Does it follow rpm and load?** The user drives: idle, a slow full-throttle pull to the redline in second, lift, idle. Compare by ear with `sweep_new.wav` and the `wav/` loops; check the interior and exterior cameras (emitters A and B).
3. **Objective check (proposal):** add WASAPI loopback capture to the host harness (e.g. Python `soundcard` or ffmpeg). Record a scripted rev and compare spectrograms against `sweeps.png`: the rotary's 2nd-order harmonic structure versus the V6's 3rd order tells the swapped sound from the host's without anyone listening. Without audio capture, `tools/minidump.py` on the running game can confirm that the bank ID (0x27B29A12 for the RX7) is resident.
4. Then tune levels: the set gains are matched to the host's loudest loop by RMS, and Forza's per-loop HT volumes are kept.

## Open

- What ParamIDs 6 and 11 control, and whether `vehicle_gear_direction` is ever set (no setter was found).
- VSVH+4C (engine type for global layers) is left as the host's. Setting it from Forza's cylinder count is a one-line change once its audible effect is known.
- Whether the right exhaust channel or the intake (Int) set should be mixed into set B or A.
