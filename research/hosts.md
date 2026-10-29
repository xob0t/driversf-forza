# Hosts: which vanilla car a port is built on

Updated: 2026-10-29

A ported car is a clone of one vanilla vehicle, the host, with the Forza body,
cockpit, wheels and handling numbers put in. Everything the converter does
not rebuild stays the host's, so the host decides much of how the port
sounds and behaves. The first ports all used 266 (ASYM Desanne, a V6 saloon).
`src/dsfforza/build/host_select.py` picks a host per car and `port_car.py --host auto`
(the default) uses it. Evidence levels follow `dngvehicles.md`.

## What comes from the host

| Data | Where | Converter |
| --- | --- | --- |
| Engine sound | NONR/VSRT | replaced by the car's own Forza loops in a vanilla template bank (`engine-sound.md`); kept (or `--sound-host`) with `--no-forza-sound` or when that fails |
| Hierarchy: parts, doors, hinges, collision (PDL), lights, damage | NONR/UPVH | hubs, steering, gauges, seats moved; the rest kept |
| Handling base: steering, assists, tyre curves, cameras | NONR/VO3D | `handling_dsf.py` (default) keeps the host's VO3D and moves it by the fleet's rules (`handling.md`); `--handling forza` is the older `handling.py` mapping (about 33 parameters, grip, springs and dampers scaled by mass). Builds between #169 and #192 kept the host's VO3D unchanged (`handling.md`) |
| Paint sets | NONR/SPSR | replaced by the car's Forza factory colours (`paint.py`, `Combo_Colors`), each with the shading of the closest vanilla paint; all 573 cars in the main checkout's `out/build` (log line `paint: N factory colours`) |
| Category, flags | SSIC/VSCT record | copied |
| HUD badge, name, stat bars | Lua `vehicleStats` | copied by `register.lua`; stat bars generated (`handling_dsf.stat_bars`) |

## The engine sound (confirmed-static)

`VSRT` holds `VSVH` (0x9C bytes), `SBAK` and, on 19 vehicles, `GRBK`:

- `SBAK` is a Wwise SoundBank (`BKHD`, `DIDX`, `DATA`, ...). `BKHD`+0x0C is
  the bank ID, which differs per vehicle; the bank's own sections refer to
  it. No SBAK contains its vehicle's spool ID. All 150 SBAKs differ.
- `GRBK` is a granular engine bank (about 0x5D000 bytes). There are 10
  distinct ones among the 19; 266/268/270/281 (Desanne, Desanne taxi, Grand Caravan civ
  and taxi) share one, so the Desanne probably has a generic granular V6 rather than
  a recorded engine. The other 131 vehicles use a large sample bank
  (`SBAK` about 0x45000 to 0x53000) and no GRBK.
- `VSVH`+0 is the spool ID; the rest are hashes and floats the same across
  many vehicles (event IDs, presumably resolved in the bank or SSIC). The
  VSVHs of related vehicles (the three Trans Ams, Desanne and its taxi, the
  two Grand Caravans, stake and tanker truck) differ only at +0; F-150 vs
  F-350 and Scirocco civ vs rally differ in a few more fields.

So the VSRT is self-contained and keyed by the spool ID at VSVH+0, which
`clone_vehicle.py` already patches. A clone of the 370Z gets the 370Z's
sound. Copying another vanilla car's VSRT into a clone and patching VSVH+0 is
the same operation (`clone_vehicle.copy_sound`, `port_car.py --sound-host
N|auto`, `clone_vehicle.py --sound-host N`). The clone then shares the bank
ID with the sound host, just as every clone already shares it with its host
(266 and its clone were loaded together without trouble, sound not
checked). Untested in game: whether the swapped sound plays. VO3D p40/p42
need not match the sound host: the game normalises the car's own rpm and
maps it onto the bank's range at VSVH+94/+98 (p605/p606 are the speedometer,
not audio). The bank and object IDs being global, and giving a port the
Forza car's own engine: `research/engine-sound.md`.

NONR budget: VSRT is most of the NONR (Desanne 0x63C10 of 0x6C8D0; the
kind-0 limit is 0x6D000). A large sound on a host with a large UPVH can
overflow it; `port_car.py` checks every entry, and the M5 on 266 with the
MP4-12C sound came to 0x6BE90.

## The vanilla fleet as hosts

`uv run dsff host_select survey --tsv research/hosts.tsv` (cached in
`out/cache/host-survey.json`). Per vehicle: VSCT category, body class,
door count, wheels, hub wheelbase and track, length and height (exterior
MODL part 0 box), VO3D mass, gauge IDs, engine and year from `vehicleStats`,
granular sound, and why it can't be a host.

137 of 150 can be hosts. The 13 that can't: 10 with six wheels (trucks,
buses, rigs, and the transporter trailer 289), the two four-wheeled trailers
123 and 131 (no steering marker, no `_Interior` material; 289 lacks them
too), and 243 RUF RT12 (no hinge at the steering marker).

VSCT category byte (last byte of the record; data, one value per VEHC class
family): 0x00 security van, 0x01 fire truck, 0x02 cop, 0x03 ambulance,
0x04 taxi, 0x05 rig, 0x06 trailer, 0x07 truck, 0x08 tow truck, 0x09 limo,
0x0A bus, 0x0B pickup, 0x0C 70s muscle, 0x0D modern muscle, 0x0E van,
0x0F SUV, 0x10 saloon, 0x11 mini car, 0x12 super car, 0x13 sports car,
0x14 drift car, 0x15 rally and buggy, 0x16 classic, 0x17 off-roader (only
the 1988 Wrangler), 0x18 Tanner.

Doors: UPVH part records with roles 31/32 are the front doors (vertical
hinge at |x| about 0.9), 54/55 (52/53 on the Vista Cruiser) the rear doors.
Roles 33/34 share the front doors' hinge on every car, the Desanne
included, so they are not rear doors. 88 usable hosts have 2 doors, 44 have 4.

## What used to assume the Desanne (fixed)

| Assumption | Fleet | Fix |
| --- | --- | --- |
| Hubs found by value: y = -0.398, 0.6 < \|x\| < 1.0, 1.0 < \|z\| < 1.8 (`patch_hubs`) | hub y from -1.31 to 0.005, 44 hosts at y = 0 (Skyline R33, F-150); on any other host no hub moved | `forza2dsf.host_wheels` reads the hubs from the wheel table; `patch_hubs` moves the wheel table, every hinge pivot within 10 cm of a hub, and the wheel bodies |
| Placement with `HOST_HUB_Y` and axle midpoint 0.1155 | axle midpoint from -4.64 (bus) to 0.94 | from the host's wheel table (`ConvertOptions.host_axle_mid` = None) |
| Wheel geometry in full-entry MODL part 31 | 33 four-wheeled hosts split front and rear wheels into two parts (Corvette, GT, Lamborghinis, RUFs ...) | the parts the wheel records point at; the wheel goes into each. The other hosts get a rear copy of their wheel part when the Forza tyres differ (`forza2dsf.split_rear_wheel`, `wheels.md` "Rear wheel parts") |
| Exterior wheel in the last MODL part | right on all usable hosts, but by luck | the parts the wheel records' LOD links name |
| Exterior Chassis/Lights materials in the exterior DRSP (`dsf_materials.install_exterior`) | only 45 vehicles; 105 keep one or both `_LOD1` materials in the interior or full entry | falls back to the interior's, then the full entry's `*_LOD1` materials, used by UID like vanilla |
| Lint: kind 1/2 materials local, in the full entry, or used by the host | vanilla 229 and 251 failed their own lint | `upvh.py lint` accepts any of the car's three entries and every SSIC material/texture; all 150 vanilla cars pass |

The wheel bodies (UPVH bodies table, +0x30) hold the negated hub position
(Desanne record 33: hub (-0.848, -0.398, 1.583), body (0.825, 0.398,
-1.584)). The old code never moved them, so earlier ports had their wheel
bodies at the Desanne's hubs; they now move with the hub (untested in game).
The steering knuckle hinges next to the front hubs (Desanne hinges 3 and 5)
used to be snapped onto the hub; they now keep their offset.
On the Desanne the new `patch_hubs` moves the same 10 positions to the same
places as before plus the 4 bodies: the 370Z on 266 places identically to
the tested fix6 build (`out/hosts/png/desanne-regression.png`).

Things that were already per host: `cockpit.host_cockpit` (steering record,
hinge, marker, gauge slots from UPVH), `cockpit.host_interior` (materials,
needle part), `handling.hub_geometry`, the shared materials PAINT, GLASS,
TRIM and TYRE (SSIC defines them), the glass texture rectangles (SSIC
texture). Paint UV0 points at the shared paint atlas's paint block
(`dsf_materials.PAINT_TEXEL_UV`, the same for every host: `lights.md`,
"Paint"); the chassis atlas's plain cell is the Desanne's fixed texel
(`dsf_materials.DEFAULT_PLAIN`).

## Scoring (`host_select.py`)

Points add up; every host gets a list of reasons.

| Group | Points | Rule |
| --- | ---: | --- |
| Identity | 60 / 30 / 6 | same model (make and normalised model from `vehicleStats` Picture1, year within 4); same model family (first word of the DSF model: Skyline, Mustang, F150); same make. Aliases: Chrysler/Plymouth = Dodge, SRT = Dodge, Raptor = F-150 |
| Engine (the sound) | up to 30 | cylinders equal 12 (within two 5); layout V/I/flat/W 5; aspiration natural/turbo/supercharged/diesel 5 (turbo vs supercharged 2); peak power 8 x (1 - \|ln ratio\| / ln 2); both electric 30 |
| Body | up to 21 | class similarity x 15 (sedan, coupe, convertible, hatch, wagon, muscle, supercar, SUV, pickup, van; partial credit for coupe-muscle, sedan-wagon, SUV-pickup ...); same door count 6 |
| Drivetrain | 4 | FWD/RWD/AWD (4WD = AWD) |
| Size | up to 19 | wheelbase 6 (0 at 0.6 m off), track 3, length 3, height 3, VO3D mass 4 (0 at 1.6x) |
| Era | up to 5 | 0 at 30 years apart |
| Penalties | -3 / -8 / -20 | host lacks speed or tach gauge; a `_drift`/`_rally` variant whose civilian sibling exists; bus or truck for a car |

Excluded unless asked: categories 0x00 to 0x06, 0x08 and 0x18
(`port_car.py --allow-special-hosts`, `host_select.py --allow-special`),
and every host with a converter problem (always). `--sound` (and
`--sound-host auto`) ranks by identity, engine and era only, and excludes
nothing: `port_car.py` asks for the sound host with special entries allowed,
and a converter problem doesn't matter for the sound.

`host_select.same_vehicle` also says when the host is the Forza car itself:
the same model or a badge twin of the same group (Chevrolet Express / GMC
Savana), same body class, years within 4, wheelbase within 2.5% and length
within 4% (1% each for twins). The converter then keeps the host's interior
when Forza ships only a placeholder cockpit (`forza2dsf`, log line
`cockpit: placeholder Forza cockpit and the host is the same vehicle`): 3
cars in `out/build` (Express 2500 Cargo on the GMC Savana, F-350, Taurus
SHO).

Forza side, from the car's game's `gamedb.slt` `Data_Car`: `CylinderID`
(`List_Cylinders.Number`), `EngineConfigID` (V, W, I, rotary, flat,
electric), `AspirationTypeId`, `DriveTypeID`, `SimPeakPower` (100 W),
`CurbWeight` (100 kg), `Year`, `FamilyBodyID`, `Data_CarBody` wheelbase,
track and bounding box. `FamilyBodyID` names are string IDs; the classes
come from the cars in each: 2 to 5 hatchbacks and small cars, 6 SUVs and
pickups, 7 saloons, 8 and 9 roadsters and convertibles, 10 coupes and GTs,
11 mid-engined exotics, 12 to 22 race cars. FH1's family 1 is a catch-all
(Corolla, Camry, Focus ZX4, A4 Avant, RAV4, F-350, Express van, tour bus,
Kenworth truck); the first version mapped it to coupe, so the batch put the
Express van on a Corvette (`batch.md`). Now, in order:

1. bus or truck: make `BUS`/`KEN` (and other truck makes), curb weight from
   4.5 t, or 7 m long;
2. van, pickup, SUV: model-name prefixes and words (Express, Savana,
   Caravan; F-150/F-350, SVT Raptor, Ram, Syclone, El Camino, "SuperCab";
   RAV4, Explorer, Durango, Range Rover ...). Short codes only count for
   their make (BMW X5/X6, Audi Q5/Q7, Mercedes ML/GL, Mercedes Sprinter):
   `MAZ_MX5SL` contains "x5", Toyota's Sprinter is the AE86 coupe, and
   `SAL_S5SRaptor` is the Saleen supercar, not a pickup (it used to be one,
   which put it on a Corvette ZR1 for the wrong reason);
3. the family's class; family 1 gives sedan (wagon by name);
4. convertible by name, muscle for American V8 coupes by name.

The pristine bounding box can't separate them: its height is 2.0 m on the
Focus ZX4, 1.8 m on the Corolla and 1.66 m on the M5.

Doors: Forza has no door sections, and the side-glass names don't say which
pane is a door. `glassLM` is the M5's rear-door window but the door window
behind a vent pane on the 2-door Charger Daytona, Gallardo, MP4-12C and
300SL (the first version counted the Daytona as 4 doors and gave it the
four-door 2009 Charger). `_carbin_doors` now measures the left panes
(`glassLF/LM/LR`, not the lamp glass) from the wheelbase midpoint backwards,
merges the panes that touch from the front into the front door glass, and
says 2 doors if that ends 0.4 m or more behind the midpoint (every 2-door in
`out/cache`: 0.42 to 1.11 m), 4 if it ends by 0.2 m and a window at least
0.55 m long starts within 0.15 m behind it (every saloon and SUV there:
-0.04 to 0.18 m), and nothing otherwise; coupe, muscle, supercar,
convertible and race bodies never get 4 (the 1969 Charger R/T's quarter
window looks like a rear door). Without a carbin, or when it can't tell,
the body class decides (sedan, SUV, wagon 4; coupes 2; hatch, pickup, van
not scored); the M5 and the 2011 WRX STi, whose front glass ends at 0.38 and
0.28 m, get no answer and fall back to 4.

The thresholds came from the few dozen cars then in `out/cache`. Over the
whole roster (main checkout's `out/build`, 2026-10-29) the rule gives 2 doors
to 8 four-door cars: the 2004 and 2005 WRX STi, the S204, the 2005 Legacy B4,
the Carlton and the Rapide, whose rear-door pane starts where the front one
ends and is merged into it, and the 2008 and 2012 Range Rover, whose front
door glass alone ends 0.46 m behind the midpoint. Three picks move to a
2-door host (issue #208). Of the 55 saloons, 49 get 4 doors; of the 15 SUVs,
13.

DSF side, same pass: buses (category 0x0A) and trucks (0x07, including the
GMC C5500 that VEHC names "Van 07") are classes of their own; the 2009
Charger SRT8 ("Modern Muscle", 4 doors) counts as a saloon; Viper and
Corvette Z06/ZR1 as supercars, not muscle.

Check against the 48 vanilla/Forza pairs (`handling_dsf.PAIRS`, first kept in
`out/handling/pairs.py`): the top host is the paired vanilla car for 45
(rerun 2026-10-29 with the carbins in `out/cache`, offline-checked). The
other three are right too: the Forza Challenger SRT8 2009 goes to 173 (the
pair list had 172, the 1970 R/T), the Scirocco R to the civilian 274 instead of the rally 275, and the
RUF RT12 S can't use 243 (no steering hinge) and gets 240 (CTR3).

## The whole roster (`research/host-picks.tsv`)

`uv run dsff host_select roster --tsv research/host-picks.tsv` picks a
host for all 573 cars of `forza-roster.tsv` (FH1 first, as `port_car` does;
the carbin door rule where the car is in `out/cache`), with the runner-up,
the margin and flags: body similarity under 0.5, a door mismatch, score
under 45, cylinders more than 2 apart, wheelbase more than 0.6 m apart, or
a runner-up within 1 point whose body fits better. 87 vanilla cars are used.
The TSV was regenerated on 2026-10-29 with the whole roster extracted in
`out/cache`, so 48 more cars take their door count from the carbin (18 picks
changed); it now matches the hosts in the main checkout's `out/build` on all
573 cars (offline-checked against `batch-report.json`).

| Car class | Cars | Host classes | Most used hosts | Flagged |
| --- | ---: | --- | --- | ---: |
| coupe | 172 | coupe 139, convertible 15, supercar 8, muscle 8, sedan 2 | 227 370Z (26), 229 Skyline R33 (18), 125 Alfa 8C (13) | 6 |
| hatch | 91 | hatch 87, coupe 3, sedan 1 | 128 MiTo (27), 274 Scirocco (27), 127 Giulietta (14) | 4 |
| race | 86 | supercar 75, coupe 11 | 162 Corvette Z06 (42), 232 Zonda (12), 182 Viper (6) | 0 |
| supercar | 85 | supercar 75, coupe 9, convertible 1 | 232 Zonda (13), 162 Corvette Z06 (11), 163 Corvette ZR1 (9) | 5 |
| sedan | 55 | sedan 49, hatch 3, wagon 2, coupe 1 | 176 Charger SRT8 (11), 136 A4 (11), 124 Alfa 159 (7) | 4 |
| convertible | 30 | convertible 18, coupe 9, supercar 3 | 278 Solstice (7), 248 Cobra (5), 129 Spider Duetto (5) | 5 |
| muscle | 29 | muscle 29 | Camaros, Challenger R/T, Mustangs, GTO, Trans Am (3 each) | 0 |
| SUV | 15 | SUV 15 | 138 Q7 (7), 239 Range Rover Sport (4), 203 H3 (3) | 1 |
| pickup | 6 | pickup 6 | 251 F-150 (2), 164 El Camino, 181 Ram SRT10, 252 F-350, 199 Sierra | 1 |
| wagon, van, bus, truck | 1 each | sedan, van, bus, truck | 136 A4, 198 Savana, 246 school bus, 197 C5500 | 2 |

The batch's three errors are gone: CHE_Express2500Cargo_05 goes to 198 GMC
Savana (same-era V8 van), SAL_S5SRaptor_10 to 163 Corvette ZR1 as a
supercar (V8 supercharged, 2-door; 226 SLR and 189 Ford GT next), and
DOD_ChargerDaytona_69 to 175 Charger R/T 1969 (2 doors). The five cars
tested in game keep their hosts (227, 147, 229, 193, 251).

The 28 flagged picks, reviewed:

- Nothing closer exists in DSF: BUS_TOUR_12 (school bus, 8.3 m wheelbase on
  6.5), KEN_T440_12 (C5500, 7.0 on 4.9, the only 4-wheeled truck host),
  GMC_Syclone_92 (a compact pickup on the full-size Sierra), the W16
  Veyrons, the 12-cylinder 250 Testa Rossa, the rotary RX-8 and Furai (no
  rotary in DSF), the V12 TDI Q7 (on the DSF Q7, same model).
- Same or family model, other body class: CTS-V coupe on the CTS-V saloon,
  Volt, Corvette Grand Sport convertible, SLR Stirling Moss, Reventon
  Roadster: kept, identity outweighs the roof.
- Plausible but worth a look when ported: hatch class cars that are really
  coupes or mid-engined specials (Alfa Brera on the 159, Audi RS2 Avant on
  the TT RS, Clio V6 and 205 T16 on coupes), the Civic Si coupe on the Neon,
  and small coupes landing on convertibles by a fraction of a point
  (Integra R 2000, S2000, Starion; Elise on the Giulia TZ2).
- Wrong door count (#208): SUB_WRXSTi_04 and SUB_S204_06 on 274 Scirocco
  and LOT_Carlton_89 on 229 Skyline R33, four-door saloons the carbin rule
  counts as 2-door ("Scoring", the Doors paragraph).

## Test builds

`out/hosts/ported-hosts.sp` (`--mdxn-limit 0x2C0000`, host auto, all
defaults; lint clean, `chnk.py rebuild --verify` identical):

| ID | Car | Host | Why |
| ---: | --- | --- | --- |
| 304 | NIS_370Z_10 (FH1) | 227 Nissan 370Z | same model |
| 305 | BMW_M5_12 (FH1) | 147 Cadillac CTS-V | V8, 415 kW, RWD four-door saloon, wheelbase 2.89 (next: 206 Jaguar XF-R, 300 Bentley Arnage) |
| 306 | NIS_SkylineGTR_93 (FM4) | 229 Nissan Skyline R33 | same family, I6 turbo, AWD |
| 307 | FOR_MustangBOSS429_70 (FH1) | 193 Mustang Shelby GT500 1967 | same family, V8, muscle (next: 192 Mach 1, 191 Fastback) |
| 308 | FOR_SVTRaptor_12 (FH1) | 251 Ford F-150 SuperCrew | same family, V8 pickup |

229 is the only R33 and sits in the drift category, but its VO3D is a
normal car's (the 370Z drift variant 299 differs from 227 in tyre,
handbrake and drag terms; 229 doesn't), so it gets no penalty.

`out/hosts/ported-sound.sp` (`--host 266 --sound-host auto`): 304 NIS_370Z_10
on the Desanne with the 370Z's VSRT (227), 305 BMW_M5_12 on the Desanne
with the McLaren MP4-12C's (225, twin-turbo V8). Same hierarchy as the tested
fix6 build, so only the sound differs from it.

Offline renders in `out/hosts/png/`: `sides.png` (vanilla host left, port
right, wheel part on the UPVH hubs: all wheels in their arches), `eyes.png`
(head-camera views, all five cockpits in place), `desanne-regression.png`.

## Open

1. In game: the swapped sound in `ported-sound.sp` (does it play, does it
   follow rpm). Ports now get their own Forza engine banks by default
   (`engine-sound.md`); none has been checked in game yet (#202).
2. Whether moving the wheel bodies changes anything (they were left at the
   Desanne's hubs before).
3. Collision (PDL): with `--damage` (on in `port_batch`) each panel's
   collision boxes are moved and scaled onto the Forza panel; the root hull
   stays the host's shape unless `--damage hull`, which is untested in game
   (`damage.md`, #210).

The host's SPSR paint sets, open here before, are replaced by the car's
Forza factory colours (`paint.py`, table above).
