# Handling (VO3D) and generating it from Forza data

Updated: 2026-10-24

Static analysis of `Driver.exe` 1.04 and the stock `dngvehicles.sp`, plus
the FH1 and FM4 `gamedb.slt` databases, and in-game measurements with the
sandbox dyno (below). Evidence levels follow `dngvehicles.md`: confirmed-static
(code read), confirmed-runtime, data (correlation across the 150 stock
vehicles or the Forza pairs below), hypothesis.

The goal is not Forza-accurate physics. A ported car should drive like a
car Reflections would have made: DSF's own arcade grip and slides, assists,
steering and gearing, with the car placed where DSF's fleet would place it
(fast or slow, heavy or light, RWD/AWD/FWD). So the default mapping is
DSF-first (`src/dsfforza/build/handling_dsf.py`, below): the host's complete VO3D, changed
only where the stock fleet's own rules say the car's real specs require it.
The older Forza-derived mapping stays available (`port_car.py --handling
forza`).

Tools:

- `dsff handling_dsf loo|build` is the DSF-first mapping and its
  leave-one-out check; `port_car.py --handling dsf` (default) uses it.
- `dsff handling dump|build|apply` reads, derives and writes VO3D (the
  Forza mapping, `--handling forza`).
- `src/dsfforza/harness/dyno.py` turns the in-game dyno logs into 0-60, 0-100, quarter
  mile, braking and steer/handbrake response figures.
- `tools/ghidra/ParamRefs.java` lists every instruction whose memory operand
  falls in the parameter array and decompiles the heaviest users. Usage is
  `ParamRefs.java <out> 2960 35EC 6 60`.
- `tools/ghidra/ExploreAddresses.java` was used with
  `fn:0088D9A0 fn:0088D020 fn:0088BE60 fn:0088BF50 table:00E39654`.

## How the engine loads handling (confirmed-static)

`FUN_008908A0` builds a `CVehicleSpecification` from a UPVH payload:

1. `FUN_008914F0(&DAT_0144D5AC)` copies the global default block into the
   spec. The block's 607 parameters sit at `0x0144D5AC + 0x2960`, which is
   the `.bss` address `0x0144FF0C` noted in `exe-vehicles.md`.
2. `FUN_0086C2C0(UPVH+0x14)` looks up a loaded VO3D resource by the
   hierarchy ID, which equals the spool ID. If there is one, `FUN_0088BE60`
   applies it.
3. Without a VO3D resource it builds `Vehicles\Configs\vehicNNN.VO3`
   (`FUN_0088D020`) and `FUN_0088BF50` reads up to 0x1800 bytes of that file.
   The file holds the same binary as the chunk, starting at the tag
   (`"VO3D"` or `"DVO3"`). It is not a text format and it carries no names.
   A loose file could therefore supply handling for a vehicle without a VO3D
   chunk (hypothesis, untested).

`FUN_0088BE60` checks the tag and calls `FUN_0088BD60(tag + 0xC, count)`.
That loop reads 8-byte records. It masks the first dword with `0x5555FFFF`,
which strips the `0xAAAA` fill and leaves the ID. A record `0xAAAAFFFF` ends
the list early. IDs below 0x25F are stored with
`*(u32 *)(block + 0x2960 + id * 4) = value`, so the value is copied as raw
bits. That is how the three bool IDs (239, 594, 604) hold the integer 1
(the float `1.4e-45` in a dump). Larger IDs are skipped.

The parameter block sits at spec + 0x310, so code that holds the spec reads
parameter `i` at `spec + 0x2C70 + 4 * i`. Code that holds the block reads it
at `block + 0x2960 + 4 * i`. Both bases appear in the scan below.

The defaults come from `FUN_0088AC20`, which writes 365 of the 370 typed
parameters with constants from `.rdata`. Evaluating its assignments gives
every default in the appendix. IDs 223 to 226 and 264 get no default, so
they stay zero. The type table at `0x00F3E450` says 367 float, 3 bool and
237 unused.

The VO3D resource vtable is `0x00E39654`. Slot 0, `FUN_0086C430`, registers
the payload through `FUN_0086C3A0`, which I have not read. The map is
probably keyed by the payload's leading u16, the field `clone_vehicle.py`
already patches (hypothesis).

## What the parameters mean

Three sources:

1. Code. `ParamRefs.java` found 447 functions with a displacement in the
   window. Those inside `0x00860000..0x008FFFFF` with a consistent base are
   the vehicle code. Their decompilation, annotated with parameter IDs and
   `.rdata` constants, gave the formulas below.
2. The spec printer `FUN_0088D9A0`. It prints mass (p0) and simulates a
   standing start with `dv = (2 * F - 0.25 * p13 * |v| - p509 * v^2) / p0`.
   Speed in gear comes from `FUN_008C28A0`, `v = ratio[gear] * p361 * p360 * rpm * pi / 30`.
   It times 0 to 27.78 m/s (100 km/h, though the label says 0-60 mph) and the
   402.336 m quarter mile, and caps speed at `p160 * 0.447`.
3. Data. I parsed all 150 VO3Ds and UPVH wheel tables and joined them with
   the game's Lua `vehicleStats`. I also paired 48 stock vehicles with the
   same model in FH1 or FM4 (`out/handling/pairs.py`).

The pairs settle units. The DSF data was clearly built from real-world specs
of the same kind Forza uses. The 370Z's first two relative gear ratios
(0.264, 0.430) match the Forza gearbox exactly. So do the full gearboxes of
the 1969 Camaro, Chevelle, GTO Judge and Cobra. p361 matches
`1 / (final drive * 1:1 gear)` exactly on the Viper, CTS-V, Taurus SHO,
Gallardo and GT-R. p524 matches the Forza torque peak position exactly on 10
of 45 cars.

### Mass, inertia, geometry

| ID | Meaning | Unit | Evidence |
| ---: | --- | --- | --- |
| 0 | mass | kg | Printer "mass"; data: r = 0.99 with Lua weight, median error vs Forza 3% |
| 15, 16, 17 | inertia diagonal (x pitch, y yaw, z roll) | kg m² | `FUN_008B14F0` builds a vec from them, scaled by p26. Data: 2.30, 2.36 and 2.70 times the box inertia of Forza's `BlockDim` (interquartile ranges within ±10%) |
| 26 | inertia scale | x | `FUN_008B14F0` |
| 30, 32 | CoG y and z in model space | m | `FUN_008902C0` stores (0, -p30, -p32) at spec+0x35F0 and `FUN_0088E310` adds it to every hierarchy position. Data: p32 implies 0.53 median front weight, below 0.5 on the mid-engined cars. p30 sits a median 0.24 m above the hubs |
| 599 | equals p30 on most vehicles | m | data |
| 392 | wheelbase | m | data: equals the UPVH hub wheelbase on 140 of 140 four-wheel vehicles. Read by `FUN_008BF7B0` |
| 140, 141 | gravity, gravity in another state | m/s² | `FUN_0088D750` computes static sag as `-p140 * p0 / (25 * p1)`. Default -17 and no stock car overrides it |

### Engine and gearbox (`FUN_008C2420` copies them, `FUN_008C3CF0` runs them)

| ID | Meaning | Unit | Evidence |
| ---: | --- | --- | --- |
| 18 | peak engine torque | game Nm | Drive torque is `p18 * curve * throttle * 0.25`. Data: p18 ≈ 14.7 × Nm^0.621 (median ratio to real torque 1.38, IQR 1.22 to 1.56) |
| 40 | idle rpm, curve start, downshift point | rpm | Downshifts below it; 650 on most cars |
| 42 | redline, curve end | rpm | Upshift threshold base. Data: equals Forza `RedlineRPM` on half the pairs, median error 1.7% |
| 522, 524, 523 | torque curve | x peak | Piecewise linear over `x = (rpm - p40) / (p42 - p40)`: p522 at x = 0, 1.0 at x = p524, p523 at x = 1 |
| 183, 184, 185, 186 | shift thresholds | x p42 | Auto upshift when rpm > p183 * p42. The others are copied but their use isn't traced |
| 275 | reverse | x reference | Forced negative (a positive value becomes -0.25 x) |
| 276..281 | gears 1 to 6 | x reference | Speed per rpm relative to the reference gear. Only six slots exist |
| 282 | forward gear count | int (stored as float) | Index of top gear |
| 360 | driven wheel radius | m | Data: Forza tyre radius, median error 2% |
| 361 | overall ratio | 1 / ratio | Data: `1 / (final drive * reference gear)`, where the reference is the Forza gear nearest 1:1 |
| 160 | top speed limit | mph | Printer "limit topSpeed". Data: stock top gear at redline equals p160 (ratio median 1.01, min 0.99) |
| 187 | reverse speed limit | | `FUN_008B2DF0` multiplies by 0.6705 |
| 355, 356 | engine and driveline inertia | game | Integrated as `dt / p355`, `dt / p356` |
| 357, 358, 359 | engine friction | game | `((p359 * \|w\| + p358) * w + p357) * dt` |
| 364 | rev limit band | x | gearbox E[0x15] |
| 516 | shift time | s | Stored as `p516 * 25` frames |
| 569, 570 | clutch stiffness, clutch torque limit | | Limit is `p570 * p18 * 0.25` |
| 371 | front drive share | 0..1 | `FUN_008B2DF0`: front gets p371, the rear axles share the rest. Data: 1 on every FWD car, 0.15 to 0.3 on AWD, absent on RWD |

### Resistance

| ID | Meaning | Evidence |
| ---: | --- | --- |
| 13 | linear rolling resistance | Printer: `-0.25 * p13 * \|v\|`; also `FUN_008BA760`, `FUN_008BEE70` |
| 509 | aerodynamic drag | Printer: `-p509 * v²`. No relation to Forza's drag figures (designer values 0 to 0.06) |

### Suspension (`FUN_008BA900`, `FUN_0088D620`)

| ID | Meaning | Evidence |
| ---: | --- | --- |
| 1 | spring rate | Force = p1 x compression, times p505 on one axle, times `1 + p507 * compression / p3` |
| 2 | damper rate | Times p506 on one axle. Times p508 when extending |
| 3, 4 | travel and lower clamp | Compression is clamped to [p4, p3] and the force ramps 5x beyond p3 |
| 25, 188, 292, 469 | ride offset and droop clamps | `FUN_0088D620` |
| 505, 506, 507, 508 | axle scales, progression, rebound | as above |

Data: p1 and p2 run a median 1.4 and 1.7 per kg of mass. Their interquartile ranges span 43% and 49% of the median.

### Tyres (`FUN_008B8110`, `FUN_008B9900`, `FUN_008B2DF0`)

| ID | Meaning | Evidence |
| ---: | --- | --- |
| 367, 369 | longitudinal and lateral slip stiffness | p369 is multiplied by 4 everywhere |
| 368 | grip, the force limit scaled by load | Data: 0.65 x mass, interquartile range 0.60 to 0.67. The designers set grip from mass |
| 376, 377, 378 | friction falloff | `mu = p376 * p378 / (p376 + slip speed) + p377` |
| 517 | longitudinal stiffness scale on one axle | |
| 529, 530, 531 | same falloff when the handbrake is on | |
| 525 | surface grip sensitivity | `FUN_008B8110`: the force limit is scaled by `1 + (mu_surface - 1) * p525` (clamped 0..2), mu from the surface table (below). Default 1; 1.2 to 1.5 on five supercars (they lose more off road) |
| 526 | drive feedback on soft ground | on a soft surface (table +0x10 = 1) the wheel's drive-reaction term (wheel +0x148, summed over the wheels by `FUN_008BD170`) is scaled by `soft * p526 + (1 - soft)`. Default 1; 1.2 on the off-road, SUV and pickup cars. 0 leaves the driven wheels spinning without traction (section "Off road") |
| 519, 520, 521 | soft-ground digging force | only on soft surfaces and if p519 > 0: a force of `(p519/p521 - p519/(p521 + slip speed))` along the slip, p520 another scale. Defaults 0, 0.5, 8; 50 to 120, 0.25, 1 on the off-road cars, SUVs and pickups |
| 413, 414, 514, 515 | camber limit, combined-slip shaping | read in `FUN_008B8110`, not decoded |

### Driver aids, steering, cameras

| IDs | Group | Reader |
| --- | --- | --- |
| 24, 166, 239, 365, 366, 393 | launch and burnout assist. p239 enables it, p166 is the throttle threshold, p24 the speed limit, p365 a ramp | `FUN_008BCBB0` |
| 298 | max steering angle, degrees | `FUN_008B2DF0`, `FUN_008AF860`, `FUN_008BD9B0` |
| 379..391, 470, 494, 495 | steering response. Values lerp by yaw rate x p391 and fade with speed between p383 and p384 | `FUN_008AF860` |
| 344..354, 372..375, 411, 412, 496, 510, 511 | stability or aligning-torque assist (speed², steering and slip terms) | `FUN_008BD9B0` |
| 300..305, 327..329, 337..342, 394..396, 535..537, 549..554 | camera mounts, a vec3 per camera index 0x10..0x1C. Measured in game: 535..537 the Bonnet camera, 327..329 the Bumper camera, 300..302 the chase camera (`research/cockpit.md`, "Cameras") | `FUN_0088DF80`, `FUN_0088E230` |
| 430, 431, 432 | camera 0x14 offset from hierarchy marker 0x2F | `FUN_0088DF80` |
| 571..582, 585..594 | unknown blocks of small offsets | `FUN_00874B00`, `FUN_0088F630` |
| 605, 606 | speedometer range in mph (`research/cockpit.md`), read with p40 and p42 by the gauge code, not audio | `FUN_00891670` |
| 596, 597 | engine audio: the rpm sent to Wwise is rpm + p596 x (rpm^2/p42 - rpm); p597 scales throttle under wheel slip (`research/engine-sound.md`) | `FUN_008B2370` |

The appendix lists every ID with stock statistics, the default and the
functions that read it. About 40 stock IDs have no reader in the vehicle
code range (310, 313, 410, 433..458, 513, 538..547, 559, 583, 584, 595, 598,
600, 601). AI, audio or effects code outside the range probably reads them.
p538 and p559 turned out to be the cockpit hands' anchor offset from the
steering marker, read by `FUN_005743E0` (`research/cockpit.md`, "Driver's hands").

## Forza side

`src/dsfforza/build/handling.py` reads the stock configuration of a car from
`media/db/gamedb.slt`, FH1 first and then FM4 (extract with
`dsff xiso extract <iso> media/db/gamedb.slt out/db/fh1_gamedb.slt`).
Joins and units, checked on the 370Z:

| Value | Source |
| --- | --- |
| mass, CoG height, front weight, inertia box | `List_UpgradeCarBodyWeight` (stock row of the car's `CarBodyID`): `Mass` kg, `CMHeight`, `CMBackFront`, `BlockDimX/Y/Z` m. `Data_Car.CurbWeight` is in units of 100 kg |
| wheelbase, track, bounding box | `Data_CarBody` (same values as `MAXData.xml`) |
| tyre radius | `Data_Car` widths, aspect ratios and rim inches |
| drive type | `List_UpgradeDrivetrain` → `Data_Drivetrain.DrivetypeID` (1 FWD, 2 RWD, 3 AWD); AWD split from `List_UpgradeDrivetrainDifferential.RearToqueSplit` |
| gearbox | `List_UpgradeDrivetrainTransmission`: `GearRatio0` is reverse, `NumGears` counts it, -1 marks unused slots |
| torque curve | `List_UpgradeEngine` → `List_UpgradeEngineCamshaft` (`RedlineRPM`, `TorqueCurveMaxRPM`) → `List_TorqueCurve`: samples evenly spaced from 0 rpm, in units of `TorqueScale` Nm. The last sample is a large negative value, which the tool drops |
| reference figures | `Data_Car.SimPeakTorque` (100 Nm), `SimPeakPower` (100 W), `Sim*AngVel` (rad/s), `SimTopSpeed` (m/s), `TopSpeed-mph` |

Stock turbos and other parts rescale the torque curve. Rather than model
each part, the tool scales the curve so its peak equals `SimPeakTorque`.
`physicsdefinition.bin` and `MAXData.xml` aren't needed.

## DSF-first mapping (default, src/dsfforza/build/handling_dsf.py)

Start from the host's complete VO3D (the stock car `host_select.py` picks:
same model, else same family, then engine, body, size, drivetrain, era) and
change as little as possible. Every parameter gets one of these rules:

| Rule | Value |
| --- | --- |
| H | the host's |
| M | host x (new mass / host mass) |
| F | host moved by the fleet model: a least-squares fit over the stock fleet (4-wheel cars with vehicleStats specs) of the parameter, in log when always positive, against log weight, log power, log torque, model year, FWD, AWD; new = host x exp(f(new specs) - f(host specs)), the change limited to +-22% (`F_LIMIT`; unclamped, the year term cut a 1969 Charger's tyre stiffness by 40%) |
| A | the fleet model's own prediction for the car's specs |
| Z | fits to DSF's values or real specs: Forza mass, 2.30/2.36/2.70 x box inertia, Forza redline, 50.8 + 0.477 x Forza top speed, Forza tyre radius |
| TP | peak torque p18 = exp(3.235) x Nm^0.121 x kW^0.450 (Forza curve peak torque and power, fitted on the 45 pairs without the Volt, F-350 and Q7) |
| CY | CoG height p30 = hub height + Forza CMHeight - drive tyre radius + 0.052 m |
| CZ | CoG fore/aft p32 from Forza's front weight |
| GX | gearing: the host's gear count and spacing; top gear at redline at the host's multiple of p160, kept inside the fleet's 1.00..1.07; first gear where Forza's first gear and final drive put it, the gears between on the host's log spacing |

Whole sets instead of single values (never blended parameter by parameter):

| Set | Parameters | Donor |
| --- | --- | --- |
| steering | 298, 379..391, 470 | the medoid of the host and the next two best-ranked hosts with the car's drivetrain and within 10 model years (was 15: "Rule change" under the Huayra feedback) (the one closest to the other two, fleet-normalised) |
| assists | 344, 351..354, 373..375, 411, 496, 510, 511 | medoid (p345 follows the weight distribution, rule W) |
| tyre falloff | 376, 377, 378 | medoid |
| suspension geometry | 3, 4, 25, 188, 292, 505..508 | medoid |
| engine dynamics | 356, 357, 359, 362, 364, 569 | medoid; then 355 (engine inertia) = the fleet's median p355/p18 x new p18 (rule R; log p355 vs log p18: r 0.78) and 358 (friction) = the drivetrain donor's x new p18 / donor p18 |
| drivetrain | 371 split, 497, 517, 519..521, 525, 526, 529..531 (handbrake), launch/burnout block 24, 166, 239, 365, 393 | the best-ranked host with the car's own drivetrain |

A set is copied as the donor has it: a parameter the donor leaves out (the
engine's default applies, `handling.EXE_DEFAULTS`) is left out of the port
too, or written as that default where the host sets it. Until 2026-10-24 it
was written as 0 ("Off road" below).

The drivetrain set follows the fleet: p530 (handbrake grip) is 0 on all 23
AWD and 16 FWD stock cars with specs and 0.15..0.5 on the RWD ones; the
combined-slip terms 519..521, 525, 526 appear together on 9 AWD cars; the
drive split p371 is 1 (FWD), 0 (RWD), 0.15..0.5 (AWD). The launch block
moved here after the FWD Mito, given a RWD donor's launch assist, lost 1.5 s
to 60 mph in game (run 20261022-083248). On a host of the car's own model
family (identity score 30 or more) the host's sets are kept; on a host of
the car's own model (identity 60, e.g. the Forza 370Z on stock 227) every
value is kept except the wheelbase and the CoG offset that follow the hubs,
since that VO3D is Reflections' version of the car.

Chosen rules (`RULES`): a rule other than H only where the leave-one-out
check below improves on the host's own value by at least 0.05 and 10%, or
where the in-game runs showed the host's value hurting:

| Params | Rule |
| --- | --- |
| 0 mass; 15, 16, 17 inertia; 42 redline; 160 top speed; 360 tyre radius | Z |
| 18 peak torque | TP |
| 30 CoG height (and 599, its copy) | CY |
| 32 CoG fore/aft | CZ |
| 1, 2 springs, dampers | M (LOO neutral; keeps the host's static sag and ride frequency) |
| 345 axle balance; 505 spring axle scale | W: 0.9448 - 0.8298 x fw and 3.035 - 3.812 x fw, fw the Forza front weight (clamped 0.42..0.58) |
| 509 aerodynamic drag | M (drag deceleration is 25 x p509 v^2 / p0: a light car on a heavy host otherwise stalls below its top speed; LOO 0.81 against 0.76 for H) |
| 368 grip | M (grip in g stays the host's) |
| 13 rolling resistance; 369 lateral stiffness | F |
| 366; 367 longitudinal stiffness; 516 shift time | A |
| 275..282, 361 gearing | GX |
| everything else | H, or the set donors above |

Mass, inertia, torque, redline, top speed and tyre radius replace the host's
values. They are the car's identity (how heavy, how strong, how fast), and in
the check they beat every host-relative alternative by a wide margin.

The HUD's stat bars (`vehicleStats` Speed, Strength, Drift, Reflections'
own summary of each car) are generated for ports (`stat_bars`, passed
through `ported.lua` to `register.lua`), fitted over the 137 stock cars that
have them: Speed = -0.6525 + 0.0101 x p160 + 0.0587 x ln(kW/kg), Strength =
-2.1427 + 0.3429 x ln(p0), Drift = 1.2138 + 0.2739 x ln(kW/kg) - 0.369 FWD
- 0.150 AWD, clamped to 0.05..1. Leave-one-out mean errors 0.042, 0.041 and
0.127 (bar spreads 0.24, 0.18, 0.28). The Speed bar is p160 (r 0.97), the
Strength bar the weight (r 0.82); the Drift bar is power-to-weight and
drivetrain, not the drift parameters, and it doesn't track the handbrake
rotation measured in game (the CTS-V and the 1969 Charger have Drift 1.0 and
rotate least; the Mito, Drift 0.26, rotates like the 370Z).

### Leave-one-out check (data)

`uv run dsff handling_dsf loo`: for each of the 48 stock cars that also
exist in Forza, build its VO3D from its Forza data as if DSF didn't have it:
host = the best-ranked host that is not of its own model (the 370Z goes on
228 GT-R, the GT-R on 240 RUF CTR3, the CTS-V on 206 Jaguar XF-R ...), fleet
fits without its model, set donors without it. Compare every parameter to
Reflections' actual VO3D for that car; error = |value - actual| / fleet
spread (MAD), clipped at 10; weights 1 for the feel, engine, gearing and
body groups, 0.5 for the unread blocks. Gearing is scored on what it does:
speed at redline in first and second gear, top gear at redline / p160, gear
count.

Figures from before the 10-year donor rule (with it the default's weighted
mean is 0.638 and steering 0.492: "Rule change" below).

| Group | DSF-first (default) | DSF-first, host sets | Forza mapping | Host unchanged |
| --- | ---: | ---: | ---: | ---: |
| tyres (grip, stiffness, falloff, handbrake) | 0.457 | 0.477 | 0.544 | 0.561 |
| steering | 0.501 | 0.600 | 0.600 | 0.600 |
| assists | 0.489 | 0.516 | 0.519 | 0.519 |
| suspension | 0.894 | 1.029 | 1.029 | 1.028 |
| engine | 0.534 | 0.521 | 0.574 | 0.577 |
| gearing | 1.329 | 1.329 | 1.669 | 1.411 |
| body (mass, inertia, CoG, drag) | 0.353 | 0.353 | 0.459 | 0.648 |
| drivetrain set | 0.474 | 0.474 | 0.535 | 0.554 |
| weighted mean, all parameters | 0.647 | 0.676 | 0.727 | 0.723 |

Per car the default beats the Forza mapping on 38 of 48 (median 0.629 vs
0.696). The engine rules give up a little in this check (355 at 0.85 against
0.73 for a plain medoid) for what the game showed: launches within a few
percent of the stock cars (below). Selected parameters (mean error; lower is better):

| Param | Host | DSF-first | Forza mapping |
| --- | ---: | ---: | ---: |
| 0 mass | 0.55 | 0.22 | 0.22 |
| 16 yaw inertia | 0.51 | 0.17 | 0.17 |
| 18 peak torque | 0.91 | 0.63 | 0.78 |
| 30 CoG height | 0.99 | 0.18 | 0.99 |
| 160 top speed | 0.39 | 0.27 | 0.27 |
| 355 engine inertia | 0.84 | 0.85 | 0.92 |
| 367 longitudinal stiffness | 0.59 | 0.40 | 0.61 |
| 368 grip | 0.67 | 0.39 | 0.39 |
| 369 lateral stiffness | 0.52 | 0.44 | 0.57 |
| 516 shift time | 0.49 | 0.38 | 0.49 |
| 530 handbrake grip | 0.67 | 0.43 | 0.67 |
| 298 steer angle | 0.37 | 0.26 | 0.37 |
| 376 tyre slip scale | 0.69 | 0.43 | 0.69 |
| 389 steering | 0.74 | 0.66 | 0.74 |
| first-gear speed at redline | 0.92 | 0.63 | 0.63 |
| second-gear speed at redline | 0.74 | 0.65 | 0.70 |
| gear count | 0.32 | 0.32 | 0.42 |

Torque in detail (|log error| over the pairs, leave one out): the torque-only
rule 14.7 x Nm^0.621 median 0.075, 75th percentile 0.175; torque and power
(TP) 0.060 / 0.102; adding weight, drivetrain or year doesn't help; fits on
the fleet's vehicleStats specs are worse (0.086 to 0.11), as vehicleStats
torque differs from Forza's on several cars.

What the check says:

- Most of the remaining error is the host: Reflections tuned each car's
  steering, assists and tyres by hand, and a similar stock car is a
  different hand-tuned set. A per-parameter consensus of the three best
  hosts scores lower still, but it mixes sets that were tuned together, so
  the default takes whole sets.
- DSF's gearing is not Forza's: DSF keeps fewer gears (median 4 to 5) and
  puts top gear at redline at or a little above p160.
- Unread or unnamed blocks (571..598 and so on) stay the host's.
- Siblings DSF tuned identically (LP670-4 SV and Murcielago LP640 share one
  VO3D) come out worse with any change; the identity score doesn't see
  them as one family.

### Drift and handbrake (data, confirmed-runtime)

46 then 68 handbrake flicks (0.4 s Space + Right at 45 to 80 mph) on 9 stock
cars and the ports give, by leave-one-car-out cross-validation, a rotation
model good to about 11 deg (a constant: 15.5): yaw inertia per kg (p16/p0),
CoG height and position (p30, p32), lateral stiffness and grip per kg, and
speed. Stock cars of one class rotate alike (sports cars 72 to 82 deg at
60 to 70 mph, the 370Z drift variant 109, the 1969 Charger 45 to 55, the
F-150 26 to 30). A "drift dial" that moves p369 so the port's drift index
(the speed-free part of the model) lands where the fleet puts a car of its
specs was tried both host-relative and absolute; in the leave-one-out check
it moves ports away from Reflections' values (index error 8.0 deg against
5.7 without it, p369 error 0.72 against 0.43), so it is off by default
(`build(drift="rel")` keeps it for experiments). The CoG height rule (CY)
fixes the largest drift-related error instead.

## In-game validation (confirmed-runtime)

Measured with the sandbox dyno (`run-series.ps1 -Dyno`, method in
`in-game-tests.md`, "Dyno"): standing starts on a 2 km straight with traffic
off, full throttle for 25 s, then full brake; and two scripted inputs from
about 70 mph: a 0.3 s steer pulse (Right) and a 0.4 s handbrake flick
(Space + Right). Two to four runs per car; acceleration figures repeat to
0.01 to 0.05 s, so differences of a few percent are real.

Stock DSF cars (Reflections' tuning, for reference): 0-60 mph is about two
thirds of the real figure (370Z 3.3 s vs 5.1 real, CTS-V 3.6 vs 4.3, Viper
ACR 2.6 vs 3.4, Gallardo LP570 3.0 vs 3.0, GT-R 4.2 vs 3.0: DSF's GT-R is
slow off the line but quick above 60), 60-0 mph takes 16 to 19 m (about
2 g), launches are traction-limited at 10 to 12 m/s^2 for 0-30 mph, and the
top speed reached in 25 s sits a few mph under p160 (`gameVehicle.topSpeed`
reads back p160, so the VO3D loads as built).

### Same model as host: the port is the stock car

Forza 370Z on stock 227 with the DSF-first mapping (ID 304; the stock
values kept, hubs and CoG follow the Forza wheelbase) against 227:

| | 0-60 s | 0-100 s | 1/4 mile s @ mph | 60-0 m | steer pulse peak yaw deg/s | handbrake: yaw change deg / peak slip deg |
| --- | ---: | ---: | --- | ---: | ---: | --- |
| 227 stock 370Z | 3.28 | 8.98 | 11.97 @ 111.2 | 16.7 | 33.3 | 74 / 15 |
| 304 DSF-first on 227 | 3.30 | 8.98 | 11.98 @ 111.2 | 16.4 | 33.8 | 82 / 21 |
| 304 Forza mapping on 227 (run 20261022-043801) | 3.42 | 10.74 | 12.39 @ 107.8 | 16.6 | | |

The handbrake spread on 227 itself is 65 to 83 deg over three runs, so 304
is within it. The Forza mapping on the same host was 20% slower to 100 mph
(p18 576 instead of Reflections' 650, upper gears compressed).

### A car DSF doesn't have (leave-one-out in game)

The same Forza cars built on the host the LOO check gives them when their
own model is excluded (`DSF_HANDLING_LOO=1 port_car.py --host N`), DSF-first
against the Forza mapping, compared with the stock car (runs
20261022-050855, -052515, -055824):

| Car (host) | Mapping | 0-60 s | 0-100 s | 1/4 mile s | top in 25 s mph | 60-0 m | steer peak yaw deg/s | handbrake yaw change / slip deg |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 370Z | stock 227 | 3.28 | 8.98 | 11.97 | 139 | 16.7 | 33.3 | 74 / 15 |
| 370Z (228 GT-R) | DSF-first, 305 | 3.34 | 9.48 | 12.10 | 133 | 16.5 | 33.3 | 91 / 29 |
| 370Z (228 GT-R) | Forza, 306 | 3.15 | 8.28 | 11.70 | 138 | 17.9 | 32.2 | 100 / 42 |
| GT-R | stock 228 | 4.21 | 8.63 | 12.03 | 150 | 18.5 | 34.3 | 68 / 11 |
| GT-R (240 RUF CTR3) | DSF-first, 307 | 5.05 | 10.29 | 12.92 | 140 | 16.4 | (58 mph) | (50 mph) |
| GT-R (240 RUF CTR3) | Forza, 308 | 6.11 | 13.84 | 14.15 | 126 | 129 | (50 mph) | (45 mph) |
| CTS-V | stock 147 | 3.58 | 9.94 | 12.38 | 131 | 18.4 | 33.3 | 71 / 13 |
| CTS-V (206 Jaguar XF-R) | DSF-first, 309 | 3.68 | 9.70 | 12.29 | 138 | 17.5 | 31.7 | 48 / 8 |
| CTS-V (206 Jaguar XF-R) | Forza, 310 | 3.24 | 7.82 | 11.57 | 144 | 17.4 | 31.5 | 75 / 20 |

- Acceleration: the DSF-first ports stay within 6% of the stock car to
  100 mph for the 370Z and the CTS-V, 19% slow for the GT-R; the Forza
  mapping is 8%, 60% and 21% off. The Forza GT-R on 240 also barely brakes
  (0.2 g, both runs), a defect the DSF-first build doesn't have.
- Steering response at speed is the same on every car, stock or ported
  (31 to 34 deg/s peak yaw rate, 1 to 2 deg slip): DSF's steering model
  dominates, and every host has it.
- Handbrake slides are where cars differ. The DSF-first 370Z rotates more
  than the stock 370Z (91 vs 74 deg) but less than the Forza one (100 deg,
  42 deg slip); the DSF-first CTS-V rotates less than the stock CTS-V (48 vs
  71 deg), the Forza one about as much (75) with more slip. These follow
  the host's and feel donor's handbrake and tyre-falloff sets; the GT-R
  ports met the input at lower speed (slower acceleration), so they aren't
  comparable.

### Round 2: more pairs and classes (runs 20261022-063701 to -102557)

Leave-one-out ports (built with `DSF_HANDLING_LOO=1 port_car.py --host N`,
the car's own model excluded from hosts, donors and fits) against the stock
car, with the final rules (run -102557, two acceleration runs, one steer
pulse, one handbrake flick per car), and earlier rounds where marked:

| Car (LOO host) | 0-60 s stock / port | 0-100 s stock / port | 1/4 mile s stock / port | 60-0 m stock / port | handbrake yaw change / slip deg, stock / port |
| --- | --- | --- | --- | --- | --- |
| 370Z (228 GT-R) | 3.26 / 3.10 | 8.94 / 8.43 | 11.96 / 11.74 | 16.5 / 17.1 | 78 / 21 ; 74 / 20 |
| GT-R (240 RUF CTR3) | 4.05 / 4.04 | 8.47 / 8.27 | 11.92 / 11.90 | 18.8 / 16.9 | 82 / 25 ; 44 / 7 |
| CTS-V (206 Jaguar XF-R) | 3.59 / 3.87 | 9.95 / 10.55 | 12.39 / 12.58 | 18.4 / 17.4 | 84 / 28 ; 78 / 18 |
| Charger R/T 1969 (176 Charger SRT8) | 4.29 / 4.39 | 12.70 / 13.97 | 13.21 / 13.37 | 19.8 / 18.8 | 69 / 15 ; 37 / 3 |
| Mito (127 Giulietta) | 4.51 / 8.67 | 13.83 / - | 13.28 / 16.17 | 19.3 / 19.8 | 76 / 15 ; 41 / 10 (37 mph) |
| MP4-12C (226 SLR), run -081346 | 2.61 / 2.56 | 6.83 / 7.10 | 10.94 / 11.05 | 16.5 / 16.4 | |
| Gallardo LP570 (216 Murcielago), run -065458, earlier rules | 2.99 / 2.91 | 7.15 / 6.49 | 11.17 / 10.74 | | 81 ; 104 |
| Viper ACR (212 Gallardo), run -065458, earlier rules | 2.59 / 2.70 | 7.55 / 7.63 | 11.40 / 11.31 | 17.4 / 15.0 | 78 ; 63 |
| F-350 (251 F-150), run -084303, earlier rules | 6.14 / 7.12 | - / 20.9 | 14.91 / 15.45 | 25.4 / 25.5 | 30 / 2 ; 28 / 3 |
| Scirocco R (274 Scirocco), run -092625, earlier rules | 3.68 / 4.91 | 9.20 / 14.59 | 12.18 / 13.57 | 20.1 / 19.9 | 77 ; 47 |

The 370Z, GT-R, CTS-V, MP4-12C, Gallardo, Viper and 1969 Charger ports
are within 8% of the stock car to 60 mph and within 3% over the quarter
mile; the GT-R, which launched 18% slow in round 1 on its RWD host, now
matches the stock car to 0.02 s. 0-100 mph is a poor yardstick for slow
cars: with p160 around 115 mph, 100 mph sits on the flat end of the curve,
and a 2 mph lower p160 adds seconds; the 1969 Charger, the F-350 and the
Mito are judged on 0-60 and the quarter mile.

Class check, stock cars for reference (run -063701, handbrake flick at 45 to
70 mph, yaw change / peak slip): sports and supercars 72 to 84 deg / 13 to
28 (370Z, GT-R, Gallardo, CTS-V), the 370Z drift variant 109 / 38, the
1969 Charger 45 to 69 / 6 to 15, the F-150 26 / 3, the Mito 74 / 12. The
steer pulse (0.3 s, about 70 mph) gives 30 to 36 deg/s peak yaw rate on
every car, the trucks 27 to 28. The ports land in their class: the F-350
port brakes (25.5 m) and turns (28 deg, 3 deg slip) like the F-150 and
F-350, the muscle car rotates less than the sports cars, the 370Z and CTS-V
ports slide like their stock cars (74 vs 78, 78 vs 84 deg).

What fixed what (diagnostic builds, one change at a time on a LOO port):

| Change | Effect | Run |
| --- | --- | --- |
| GT-R on 240: 228's engine inertia/friction (355, 356, 358) and shift time (516) | 0-30 mph 1.38 -> 0.73 s, 0-100 10.3 -> 7.9 s | -073627 |
| GT-R on 240: 228's drivetrain set, CoG height, tyre stiffness | no effect, or slower | -073627 |
| Mito, Scirocco: host CoG height | no effect | -081941 |
| Mito, Scirocco: host engine set, shift time, long. stiffness | 0-60 9.5 -> 7.4 s; 0-100 13.1 -> 12.1 s | -081941 |
| Mito: host assists | 0-60 9.5 -> 8.0 s; with the engine set 6.1 s | -091649, -083248 |
| Mito: every set from the host, identity values kept | 0-60 6.2 s (the Giulietta itself 6.4) | -095156 |
| Mito: the host's engine identity (p18 500, gears) kept | 0-60 4.0 s | -095156 |
| Engine friction 358 from the fleet ratio (R) instead of the donor | 370Z 0-100 8.5 -> 10.5 s, GT-R 8.3 -> 10.6 s | -100024 |
| Rule F unclamped vs +-22% | Charger tyre stiffness 4452 vs 6500 host (truth 7500) | -071526 |

### Round 3: what sets handbrake rotation and the FWD launch (runs 20261022-115227 to -134422)

Diagnostics, one parameter group copied from the stock car into a
leave-one-out port at a time (`out/dyno/make_specs.py`, `patch_vo3d.py`):

| Port (stock) | Group from the stock car | Result | Run |
| --- | --- | --- | --- |
| GT-R on 240 (228) | assists 344..354, 373..375, 411, 496, 510, 511 | handbrake 42 -> 83 deg (stock 80), 0-60 4.4 -> 3.4 s | -115227 |
| GT-R on 240 (228) | suspension, CoG, drivetrain set, tyres, inertia, steering | 40 to 51 deg: no effect | -115227 |
| GT-R on 240 (228) | 344..354 only | 41 -> 90 deg | -123844 |
| GT-R on 240 (228) | 510, 511 / 353 / 411, 496, 373..375 | no effect | -123844 |
| Charger R/T on 176 (175) | any single group, the unread blocks 571..598 | 30 to 41 deg (stock 51 to 69) | -121154, -125129 |
| Charger R/T on 176 (175) | every non-identity parameter at once | 37 -> 50 deg | -125129 |
| Mito on 127 (128) | assists | 0-60 8.6 -> 6.1 s | -123030 |
| Mito on 127 (128) | engine dynamics, clutch, shift time | 8.6 -> 6.6 s | -123030 |
| Mito on 127 (128) | torque curve and redline | 8.6 -> 7.7 s | -123030 |
| Mito on 127 (128) | gearing, p18, launch block, tyre stiffness and CoG | no effect or slower | -123030 |

Within 344..354 only p345 differed much (port 0.58, stock 0.51). FUN_008BD9B0
builds a two-way axle balance from it: p345 is the base, terms for slip
angle (p353, p354 above speed p411), speed^2 (p510, p511) and yaw rate are
added, the result is clamped to 0..1 and rate-limited by p344, and the two
outputs are fraction and 1 - fraction of an overall factor. Across the
fleet p345 is the weight distribution: p345 = 0.9448 - 0.8298 x front
weight (from p32), r = -0.93, residual sd 0.015 over 136 cars. On the pairs
the Forza front weight predicts it to 0.023 (rule W; LOO error 0.78 against
1.28 for the host's value and 1.08 for the set medoid). p505 (spring axle
scale) follows the weight distribution too (MAE 0.106 against 0.168).

For the Mito the slow launch was the donor's assist balance (a 1971-style
Fiat 500 Abarth's p345 0.39 against the Mito's 0.50) and its engine friction
(p358 0.0055 against 0.003): with rule W the port gets 0.46, and p358 now
comes from the drivetrain donor (the best-ranked host with the car's
drivetrain, here the Giulietta: 0.0033), as close as the medoid over the 48
pairs (1.199 vs 1.201).

The 1969 Charger's rotation is not in any one set: even every non-identity
value from the stock car brings it only from 37 to 50 deg, so the rest is
the port's body (its own wheelbase, hub positions and collision on the SRT8
host's hierarchy) or its identity values together.

Validation with the round-3 rules (leave-one-out hosts; stock / port; runs
-130038, -132131):

| Car (host) | 0-60 s | 0-100 s | 1/4 mile s | handbrake deg / slip |
| --- | --- | --- | --- | --- |
| 370Z (228) | 3.26 / 3.14 | 8.97 / 8.14 | 11.97 / 11.67 | 76 / 20 ; 95 / 41 |
| GT-R (240) | 4.12 / 3.41 | 8.52 / 7.45 | 11.95 / 11.41 | 81 / 21 ; 93 / 34 |
| Gallardo (216) | 3.02 / 3.39 | 7.19 / 7.27 | 11.20 / 11.29 | 80 / 28 ; 108 / 42 |
| R8 V10 (212) | 3.50 / 4.00 | 8.63 / 8.80 | 11.79 / 12.07 | 108 / 46 ; 115 / 41 |
| TT-RS (142) | 3.60 / 3.63 | 8.49 / 8.68 | 11.90 / 12.04 | 88 / 35 ; 87 / 32 |
| CTS-V (206) | 3.58 / 3.87 | 9.94 / 10.58 | 12.38 / 12.59 | 85 / 26 ; 48 / 6 |
| Charger R/T 1969 (176) | 4.31 / 4.43 | 12.72 / 14.02 | 13.21 / 13.39 | 51 / 8 ; 36 / 3 |
| F-350 (251) | 6.14 / 7.12 | - / 23.0 | 14.91 / 15.52 | 31 / 2 ; 29 / 3 |
| Mito (127) | 3.99 / 6.88 | 11.67 / - | 12.92 / 15.27 | 67 / 13 ; 70 / 9 |
| Scirocco R (274; stock is the rally car) | 3.72 / 5.09 | 9.24 / 14.97 | 12.21 / 13.73 | 75 / 18 ; 50 / 7 |

Final rules (drivetrain-donor friction added; run -134422, stock / port):

| Car (host) | 0-60 s | 0-100 s | 1/4 mile s | handbrake deg / slip |
| --- | --- | --- | --- | --- |
| Mito (127) | 3.99 / 4.95 | 11.67 / 15.55 | 12.92 / 13.76 | 67 / 12 ; 85 / 18 |
| 370Z (228) | 3.30 / 3.27 | 8.97 / 8.99 | 11.98 / 11.99 | 76 / 20 ; 121 / 79 |
| GT-R (240) | 4.03 / 3.50 | 8.47 / 7.61 | 11.89 / 11.51 | 73 / 16 ; 93 / 34 |
| CTS-V (206) | 3.58 / 3.86 | 9.94 / 10.58 | 12.38 / 12.58 | 83 / 27 ; 62 / 9 |
| Charger R/T 1969 (176) | 4.31 / 4.43 | 12.72 / 14.03 | 13.20 / 13.39 | 36 / 18 ; 36 / 3 |

The Mito's 0-60 went from 8.7 s (round 2) to 4.95 s against 4.0 for the
stock Mito; the 370Z port now accelerates exactly like the stock 370Z.

Handbrake rotation error against the stock car (mean |port - stock|):
before round 3 (run -102557 rules) 20 deg over the 370Z, GT-R, Gallardo,
Charger and F-350; with rule W 15 deg over the same five, and the GT-R no
longer rotates half as much. The rotation is very sensitive to p345 (about
7 deg per 0.01), so the remaining spread (the 370Z and the Gallardo now
rotate 20 to 28 deg more than stock, the CTS-V 37 less) is within what
p345's own scatter around the weight rule (sd 0.015) can produce.

### Round 4: closed-loop calibration and a better test (offline part)

Noise first. Over all sessions so far the same stock car's handbrake rotation
(throttle back on after the flick, entry speed whatever the car reached)
scatters by 6 to 21 deg (sd): the 370Z 17, the Mito 21, the 1969 Charger 13,
the CTS-V 10, the GT-R 6 (`out/dyno/stock-metrics.json`). Many port-vs-stock
differences in rounds 2 and 3 were inside that. The test is now:

- the throttle is lifted after the flick (`None` steps in `-DynoKeys`), so
  power-on oversteer doesn't add to the rotation;
- every car meets a manoeuvre at the same speed: a step time `@55` holds
  the throttle as long as that car took to reach 55 mph in its own run 1
  (the harness reads the run-1 samples from the log);
- a 1 s step steer at 45 mph gives the steady yaw rate and lateral g
  (`dyno.py`: `yaw_rate_steady`, `lat_acc_steady` 0.4 to 0.9 s into the
  input; `reverse_ratio` for lane changes);
- two handbrake runs per car, so each figure comes with its spread.

`src/dsfforza/build/handling_tune.py` closes the loop: per car, p18 (within +-25% of the
rule) toward a 0-60 target and p345 (within +-0.05 of rule W) toward a
rotation target, by secant steps from priors (d ln 0-60 / d ln p18 = -0.8;
-7 deg per +0.01 of p345), with dead bands at the noise (3%, 8 deg). Targets:
the stock car's own figures for cars DSF has (measured in the first session
of the run); for the others 0-60 from a fit of the measured stock cars on
power-to-weight and drivetrain, and the rotation as the median of the
measured stock cars among the car's best-ranked hosts. The 0-60 model is
weak (leave one out over 16 stock cars: median error 15%; Reflections' 0-60
is not a function of power-to-weight: the GT-R takes 4.1 s, the 370Z 3.3),
so for cars without a counterpart it only bounds the port (acts outside
+-18%). Results go to `out/cache/handling-tune.json` per car and host, and
`handling_dsf.apply` uses them (`DSF_HANDLING_NOTUNE=1` ignores them).

Coupled sets (offline): p345's offset from the weight rule (sd 0.015)
correlates only weakly with the other assist values (|r| 0.34 or less with
517, 510, 511), so inheriting p345 together with a donor's set carries no
information the formula lacks; the loop calibrates p345 against the
measured rotation instead.

### Round 4 in game: calibration loop (runs 20261022-184447 to -193345)

Stock baseline with the lifted test (run 20261022-184447). Handbrake rotation
in deg (sd over two flicks at about 46 mph): 370Z 227 52.4 (2.1), GT-R 228 58.6
(1.2), CTS-V 147 48.8 (3.7), Mito 128 87.9 (8.5), Giulietta 127 51.3 (1.1),
GT500 193 45.6 (0.0), F-150 251 33.5 (1.0), XF-R 206 66.5 (4.8), CTR3 240
63.2 (1.6), Charger 175 44.8 (0.9). The old throttle-on flick scattered 6 to
21 deg on the same cars. The 1 s step steer at 43 mph gives 55 to 61 deg/s on
cars and 43 deg/s on the F-150.

Three iterations of `handling_tune.py run` on `out/play/p4.sp` (port: stock
target, else the 0-60 model and the rotation of the best-ranked hosts):

| port (host) | iteration 1: 0-60, rotation | iteration 3: 0-60, rotation | target | cached p18, p345 |
| --- | --- | --- | --- | --- |
| 370Z (228) | 3.22, 63.4 | 3.22, 59.9 | 3.23, 52.4 (227) | 619, 0.521 |
| Mustang Boss 429 (193) | 4.40, 42.5 | 4.40, 45.4 | 3.86 model, 45.6 | 696, 0.464 |
| M5 (147) | 3.55, 56.4 | 3.54, 47.2 | 3.38 model, 58.6 | 840, 0.497 |
| GT-R (240) | 3.59, 58.5 | 3.96, 64.5 | 4.11, 58.6 (228) | 754, 0.505 |
| Focus RS500 (274) | 4.69, 45.6 | 3.58, 60.4 | 3.95 model, 58.6 | 810, 0.414 |
| Raptor (251) | 4.77, 37.0 | 4.74, 37.5 | 4.61 model, 44.8 | 723, 0.460 |
| CTS-V (206) | 3.86, 47.3 | 3.54, 45.1 | 3.60, 48.8 (147) | 930, 0.497 |
| Mito (127) | 4.99, 74.7 | 4.39, 85.1 | 4.63, 87.9 (128) | 425, 0.454 |

On a port, repeat runs with the same values scatter more than on the stock
cars. The 370Z read 63, 56 and 60 deg at nearly the same p345; the GT-R's 0-60
moved 3.6 to 5.0 s across p18 825, 696 and 767. So one session pins a port to
about 5 deg and 0.4 s. The cached values (`out/cache/handling-tune.json`) are the
loop's last proposals. Every port ends within the dead band of its target
except the Raptor (7 deg short) and the M5's last flick.

### Driving feedback: Pagani Huayra "can't exit a drift"

Round-3 build (out/testdrive ID 31): host 232 Zonda Cinque (RWD), drive set
and assists from the host, steering and engine sets from 224 McLaren F1
(1997), the medoid of the host and the two best-ranked RWD hosts within 15
years (232, 224, 163). The Veyron port (28, rated good) took every set from
its own host, 214.

What the F1's steering set changes (`FUN_008AF860`, params at
`spec + 0x2C70 + 4*id`): at speed the steering target is
`f*input*p298 + (1-f)*(p379*g*input + p380*slip)`, with
`f = min(1, |front slip|*p381)` and `slip` the body slip angle. p380 is the
automatic counter-steer, the share of the slip angle the front wheels follow
by themselves. The other supercars compared (232, 214, 225, 240, 163, 182)
have 0.6; the F1 has 0.4 (56 of the 150 stock cars have 0.4 or less). The
slew rate towards that line is `lerp(p389, p390, min(1, |slip|*p391))`.
(Correction, 2026-10-24: the F1, like 96 of the 150 stock cars, leaves p390
out, so the engine uses its default 3; the generator read the absence as 0
and wrote p390 0 into the Huayra, with which the rate reaches zero from about
11 deg of slip and the wheels stop moving towards the counter-steer line in a
big slide. The F1 itself never had that. See "Off road" below.) The Zonda
has p380 0.6, p390 2.5; the Veyron 0.6 and no p390 (default 3). Other differences from the host (mass
1430 kg against 1100, engine inertia p355 0.0157 against 0.009 from rule R,
p345 0.58 against 0.55, which steadies the car) don't stand out against
the fleet: the Veyron port runs p355 0.021 and drives well.

Rule change: set donors must be within 10 years of the car, not 15
(LOO weighted error 0.638 against 0.641 on the stock fleet; steering group
0.492 against 0.521; 6 to 12 years all score within 0.003). The Huayra now
takes steering, suspension and engine sets from 182 Viper ACR and assists
from 163: p380 0.6, p386/388 1.5, p390 2.5, p391 2, p353 0.6, p510/511
50/10. Eight of the 40 test-drive cars change donors (Supra, RX-7, M5 E28,
Golf R, 205 T16, Durango, 2000GT, Huayra); the GT-R, TT RS and Raptor are
family-hosted and keep their host's sets.

In game (confirmed-runtime; runs 20261022-173553 to -183057,
`out/huayra/huayra.sp` and `huayra2.sp`). `uv run dsff handling_tune recovery
--archive ... --lua ... --ids ...` drives a launch run, then pulls the
handbrake for 0.6 s with Right at 60 mph, steers Right 0.6 s more and
counter-steers (Left) 1.2 s: on the throttle (run 2), lifted (run 3), after a
1 s handbrake pull (run 4). `dyno.manoeuvre` reports `recover_s`, the time
from the input until slip stays under 5 deg and yaw rate under 10 deg/s for
0.25 s after a slide of at least 15 deg, and `slip_area`, the slip integrated
over up to 8 s (deg s). Lifted runs separate nothing: every car straightens
(slip area 14 to 69). The power-drift run reproduces the complaint:

| car | power-drift exit: recover_s / slip_area (each session) |
| --- | --- |
| 232 Zonda (stock host) | 2.6/53, 3.7/76, 2.7/58, 3.0/69 |
| 214 Veyron, 224 F1, 182 Viper (stock) | 3.1/48; 2.3 to 5.6/22 to 233; 3.8 to 3.9/100 |
| Huayra, round-3 build (testdrive ID 31) | never, 445 deg s, in all 4 sessions |
| same with p380 0.6 only | never (444, 449) |
| same with the host's engine inertia | 5.3/131, never (440) |
| same with the host's steering set | 4.2/83, 3.4/62, 4.1/85 |
| new rule (10-year donors) | 5.3/200, 3.3/84, never (204) |
| new rule + host's steering set | 2.7/81 |

The round-3 Huayra never comes out of a power drift within 8 s; every stock
car does. The steering set is the cause, and not p380 alone: with p390 0
(written by the generator, the F1 leaves it out) and p391 5 the wheels stop
turning towards the counter-steer line once the front slips more than about
11 deg; the same set also carried p381..p384 as 0 (defaults 2, 0.05, 10, 30). The new rule fixes the failure most
of the time; the host's own steering set brings it to the Zonda's figures.
So the Huayra also has a per-car entry in `out/cache/handling-tune.json`
(`PAG_Huayra_12@232`: the host's steering set, with a note), and
`out/huayra/testdrive-31-fix.json` (for `out/dyno/patch_vo3d.py`) moves ID 31
of the installed test drive to new rule + host steering.

Harness fixes found on the way (`dyno.lua`, `run-test.ps1`): the brake key
at the end of one schedule reached the next car before Driver.log told the
guest the run had ended, reversed it and started its run, so every later
schedule of that car went to the wrong run. A run now starts only on
forward motion from where the car last stood. The '@mph' step timing now
counts from the last standstill before the target speed, not from the first
motion, which could be that reversing. Sessions after 20261022-175640 have
every run aligned and the entry speeds within a few mph.

### Off road: absent parameters were read as 0 (driving feedback: Subaru WRX)

Feedback on testdrive11 (ID 6, SUB_WRXSTi_11 on 136 Audi A4): "just plain
not drivable on sand. it might as well be ice."

How DSF does surfaces (confirmed-static, `FUN_008B8110`, the tyre force).
Each wheel carries a surface index (wheel +0x3C) into a table of 32-byte
records at `0x00E3755C`; read from Driver.exe, 40 records. +0x00 is the
grip (1.0 for most; 0.7 for records 2, 3, 4 and 7; 0.5 for 20; 2.0 for 22;
0 for 17 and 28), +0x10 a soft-ground flag (1 on exactly records 2, 3, 4,
7), +0x08 0 to 0.25. Three groups of VO3D terms use them:

- the force limit (grip x load) is scaled by `1 + (grip - 1) * p525`,
  clamped to 0..2: a 0.7 surface gives 70% of the grip with the default
  p525 1, 64% and 55% on the supercars that set 1.2 and 1.5;
- on a soft surface the wheel's drive-reaction term (wheel +0x148, which
  `FUN_008BD170` sums over the wheels) is scaled by
  `soft * p526 + (1 - soft)`: the default 1 leaves it, the off-road cars,
  SUVs and pickups set 1.2, and 0 removes it;
- on a soft surface with p519 > 0 a force `p519/p521 - p519/(p521 + slip
  speed)` acts along the slip (p520 scales a second part): 50 to 120 on the
  same off-road cars (defaults p519 0, p520 0.5, p521 8).

The defaults are what `FUN_0088AC20` writes into the block before the
VO3D's records (appendix); a parameter a VO3D leaves out has that value.
`handling_dsf.value()` read a missing parameter as 0 unless `handling.PARAMS`
named it, and the set copies (steering, assists, drivetrain set) wrote that
0 into the port. The WRX took its drivetrain set from 196 Taurus SHO, which
sets none of 519..526, and got p520 0, p521 0, p525 0 and p526 0, plus
p381..p384 0 from the steering set (defaults 2, 0.05, 10, 30).

Over the 40 test-drive cars (master's generator against the fixed one, same
hosts; `out/offroad/survey40.py`) 20 cars had such values:

| Parameter (default) | Written as 0 on |
| --- | --- |
| p526 (1), p520 (0.5), p521 (8) | 2 Supra, 5 Silvia, 6 WRX, 14 M5 E28, 15 Golf R |
| p525 (1) | the same five and 18 205 T16 |
| p381 (2), p383 (10), p384 (30), p382 (0.05) | 19 cars: 2..7, 11, 13..19, 21, 23..25, 39 |
| p390 (3) | 2..5, 13, 18, 19, 21, 23, 25 |
| p411 (10), p352/p353/p354 (assists) | 16 Abarth, 17 Civic, 21 Range Rover, 39 Focus; 20 and 21 also changed assist donor |

The Huayra (31) had p381..p384 0 too, from its hand-made entry in
`out/cache/handling-tune.json` ("the host's steering set", values read the
same way); the entry now leaves them out. The p390 0 that the round-3
Huayra got from the F1's set (above) was the same artefact.

The fix (`handling.EXE_DEFAULTS`, all 365 defaults; `handling_dsf.value()`
returns them; `leave_out()`: a parameter the host doesn't set and the rule
or set donor leaves at the default stays out of the port's VO3D) changes no
value a stock car sets. With correct defaults the feel medoid sees the fleet
as the engine does, so a few set donors change (the WRX keeps the A4's
steering set instead of the Taurus's; the Abarth, Jeep, Range Rover and
300SL change assist or steering donors). Leave-one-out over the 48 pairs:
weighted mean 0.638 -> 0.623 (tyres 0.453 -> 0.433, assists 0.481 ->
0.455, drivetrain set 0.461 -> 0.434, steering 0.492 -> 0.521); the scores
now also compare absent values by their defaults.

In game (confirmed-runtime; dirt start of the off-road challenge, method in
`in-game-tests.md`, "Off-road test"; `uv run dsff dyno --dirt <run>`). Launch
times and deceleration are game time; "turn" is the yaw rate once
Up+Right acts on the dirt (0 when the car never turns there):

Before / after: run vm-20261024-104232 (the installed testdrive11 archive, keys `Up:2.5,Down:3;Up:1.2,Up+Right:2,Down:3`) and vm-20261024-113608 (the same archive with each port's VO3D as the fixed generator builds it, `out/offroad/patch_after.py`, equal to the rebuilt testdrive12; the stock 196 there has p526 0 as a check; keys as in `DIRT_KEYS`). Brake and turn figures of the same stock car scatter between sessions (A4 1.54 / 0.82 g: how long the brake acted depends on the VM's speed); the launch times repeat to 0.04 s.

| Car | 0-10 mph s | 0-20 mph s | brake g | turn yaw deg/s |
| --- | --- | --- | --- | --- |
| 136 Audi A4 (stock, WRX host) | 0.49 / 0.49 | 1.11 / 1.11 | 1.54 / 0.82 | 46 / 50 |
| 142 Audi Sport Quattro S1 (stock, rally) | 0.27 / 0.27 | 0.57 / 0.57 | 2.12 / 1.78 | 99 / 115 |
| 228 Nissan GT-R (stock, AWD) | 0.23 / 0.19 | 0.50 / 0.46 | 1.75 / 1.42 | 83 / 46 |
| 196 Ford Taurus SHO (stock, AWD; after: p526 = 0) | 0.35 / 12.10 | 0.73 / 18.66 | 1.52 / -0.01 | 51 / 0 |
| 6 Subaru WRX STi port | 9.75 / 0.34 | 15.93 / 0.72 | 0.03 / 1.47 | 0 / 62 |
| 15 VW Golf R port (AWD) | 11.05 / 0.26 | 16.77 / 0.61 | -0.00 / 1.40 | 0 / 63 |
| 2 Toyota Supra port (RWD) | 0.00 / 0.50 | 0.01 / - | -0.06 / 1.27 | 0 / 95 |
| 14 BMW M5 E28 port (RWD) | 9.21 / 0.46 | 14.82 / 1.00 | 0.05 / 1.44 | 0 / 68 |
| 33 Nissan GT-R port (control) | 0.22 / 0.23 | 0.49 / 0.53 | 1.55 / 1.48 | 74 / 85 |
| 38 Ford Raptor port (control) | 0.34 / 0.30 | 0.72 / 0.69 | 1.47 / 1.48 | 38 / 37 |

- Before the fix the ports with p526 0 can't drive on dirt: full throttle
  gives 0-10 mph in 9 to 11 s (the stock cars 0.2 to 0.5 s; the WRX again
  9.8 s in run -114701), the brake doesn't slow them (they roll on down the
  slope for 40 s), and steering doesn't turn them; the driven wheels spin
  (the HUD showed 24 mph at 8 mph, `img/offroad-wrx-dirt.jpg`). The Supra's
  launch assist threw it to 10 m/s in one update, after which it rolled the
  same way. The ports without the zeros (33 GT-R, 38 Raptor) drive like the
  stock cars.
- The stock Taurus SHO (196) with only p526 set to 0 does the same (0-10 mph
  in 12 s, no braking, no turn): p526 0 alone is the cause.
- After the fix the WRX launches like the stock AWD cars (0-20 mph 0.72 s;
  A4 1.11, GT-R 0.46 to 0.50, Sport Quattro S1 0.57), brakes at 1.47 g and
  turns at 62 deg/s; the Golf R, M5 E28 and Supra likewise.

### Remaining gaps

(Written after round 3; round 4's calibration loop since moved several of
these, see "Round 4 in game".)

1. FWD hatches: the Mito port took 4.95 s to 60 mph after round 3 (8.7 s in
   round 2, 4.39 s against a 4.63 s target after round 4's calibration)
   against 4.0 for the stock Mito; the rest of the gap is above 60 mph (its
   p160 is 2 mph lower and its torque curve is the Giulietta's). The
   Scirocco R is paired with DSF's Scirocco *rally* (275, extra traction
   shaping), so its port on the road Scirocco (274) is slower by design.
2. Handbrake rotation is very sensitive to the axle balance p345 (about
   7 deg per 0.01) and to everything around it. With p345 from the weight
   distribution the GT-R no longer rotates half as much as the stock car,
   but the 370Z port now spins further than the stock 370Z (121 vs 76 deg,
   with p345 0.505 against the stock 0.500: its other values from the GT-R
   host and the 8C donor add rotation) and the CTS-V rotates less (62 vs 83).
   Mean error over five cars: 15 deg with rule W, 20 before round 3 (see
   round 3). Round 4 calibrates p345 per car against the measured rotation
   instead (`src/dsfforza/build/handling_tune.py`).
3. The 1969 Charger's rotation (36 to 51 deg against 51 to 69 stock across
   sessions; the stock car itself varies that much) isn't in any one set.
4. p18: 6% median, 10% at the 75th percentile (torque and power rule).

## Forza mapping (port_car --handling forza, dsff handling build)

The tool starts from the host's VO3D, the clone made by `clone_vehicle.py`,
and replaces only what it can derive. Everything else stays the host's:
steering, assists, tyre friction curves, travel, cameras, AI, sound and
gravity.

| Params | Rule | Why |
| --- | --- | --- |
| 0 | Forza mass | units match (3% median error on the pairs) |
| 1, 2, 13, 355..359, 367..369 | host value x (new mass / host mass) | the fleet keeps these proportional to mass. Scaling keeps the host's ride frequency, grip in g and drivetrain inertia ratio |
| 15, 16, 17 | 2.30 / 2.36 / 2.70 x Forza box inertia | pair fit |
| 18 | 14.735 x peak Nm ^ 0.621, or `--torque-scale` x Nm | pair fit, compressive like the stock data |
| 42 | Forza redline | direct |
| 522, 523, 524 | Forza curve value at host idle x 0.92, at redline x 0.87, and the peak position, clamped to the stock 5 to 95% range | pair fits |
| 360, 361 | Forza drive-axle tyre radius; 1 / (final drive x gear nearest 1:1) | direct |
| 160 | min(50.83 + 0.477 x Forza top speed, 160) mph | pair fit, residual sd 8 mph |
| 275..282 | Forza gears relative to the reference, at most 6. Gears above second are compressed in log space until top gear at redline equals p160 | how the stock data is built |
| 371 | FWD 1, RWD 0, AWD 1 - rear split clamped to 0.15..0.3 | stock values |
| 392 | Forza wheelbase | equals the hub wheelbase that `forza2dsf` patches in |
| 32 | host axle midpoint + (front weight - 0.5) x wheelbase, front weight clamped to 0.42..0.58 | stock range |
| camera z (opt-in) | host value x wheelbase ratio (`--scale-cameras`) | size only |

p40 (idle) stays the host's. Nearly every stock car uses 650, and Forza's
`StallRPM` is 800 on every car.

### Validation

`out/handling/validate.py` rebuilds each of the 48 paired stock vehicles
from its own Forza counterpart. The fits come from the same pairs, so these
errors are optimistic:

| Param | Median error | 75th percentile |
| --- | ---: | ---: |
| mass | 3.1% | 8.2% |
| inertia x / y / z | 6.7% / 4.7% / 9.0% | 11.5% / 10.3% / 13.3% |
| peak torque p18 | 7.6% | 12.9% |
| redline | 1.7% | 7.1% |
| top speed p160 | 3.8% (5 mph) | 5.0% |
| tyre radius | 2.2% | 3.7% |
| gear count, drive split | exact on the median car | |
| torque curve p522 / p523 / p524 | 10% / 10% / 13% | 18% / 15% / 23% |
| CoG z | 0.058 m | |
| speed per rpm, first / top gear | 9% / 12% | |

The EV (Volt), the diesel F350 and the Q7 are the worst cases. DSF tuned
them by hand: the Volt has one gear and p18 = 2500.

## Confidence

- High: the layout and load path, defaults, mass, inertia, CoG, gearbox,
  wheel radius, rpm limits, torque curve, top speed limit, drive split and
  wheelbase. Code and data agree on these. In game: the VO3D loads as
  built (p160 reads back), a port on its own model's host drives like the
  stock car (confirmed-runtime).
- Medium: torque magnitude, gearing, grip and stiffness scaling, and the
  feel sets taken from similar hosts: the leave-one-out check and about
  ten cars in game (rounds 1 to 4) agree, with errors of a few percent to 20% in acceleration
  and about 30% in handbrake rotation.
- Low: what the steering, assist and unread parameters mean one by one;
  they are carried as whole sets from DSF cars instead.

## Open

1. The gaps under "In-game validation" ("Remaining gaps"): handbrake
   rotation, torque (the AWD launch on a RWD host was fixed in round 2).
2. `FUN_0086C3A0`: confirm the VO3D map key.
3. The loose `Vehicles\Configs\vehicNNN.VO3` fallback could be a way to ship
   handling without touching the archive.
4. Name the steering, assist and unread groups, if converted cars need them.

## Appendix: every parameter seen in stock VO3D or named here

Default is the value `FUN_0088AC20` writes. Type comes from `0x00F3E450`
(f float, b bool). Readers come from the displacement scan over
`0x0086xxxx..0x008Fxxxx`; a few may be false positives from other structs.

| ID | Name | Stock n | Stock min / median / max | Default | Type | Readers (FUN_00...) |
| ---: | --- | ---: | --- | ---: | --- | --- |
| 0 | mass | 145 | 550 / 1800 / 1.8e+04 | 1700 | f | 88D750, 88D9A0, 88F550, 89C330, 8AE650, 8AF6A0, 8B2DF0, 8B8110, 8BCAF0, 8D0E50, 8D93A0, 8D98E0, 8DECD0, 8E1F40, 8E9ED0 |
| 1 | spring_rate | 150 | 600 / 2650 / 2.2e+04 | 1000 | f | 88D620, 88D750, 88F550, 8BA900 |
| 2 | damper_rate | 150 | 500 / 3000 / 1.8e+04 | 770 | f | 8BA900 |
| 3 | suspension_travel | 134 | 0.18 / 0.28 / 0.6 | 0.3 | f | 88D620, 8AE9A0, 8B1A20, 8B2370, 8B9C40, 8BA900, 8BF3E0 |
| 4 | suspension_min | 28 | 0.02 / 0.05 / 0.08 | 0 | f | 88D620, 88D750, 88F550, 8B7E40, 8B8110, 8B9C40, 8BA900, 8BC190, 8BD170 |
| 13 | rolling_resistance | 147 | 2 / 10 / 40 | 15 | f | 88D9A0, 8B2DF0, 8BA760, 8BEE70, 8E1F40 |
| 14 | p14 | 30 | 0 / 0 / 5e-05 | 0.005 | f | 8BE990 |
| 15 | inertia_x | 149 | 1273 / 8530 / 7.848e+05 | 6200 | f | 89C330, 8AE650, 8B14F0 |
| 16 | inertia_y | 149 | 1378 / 1.003e+04 / 7.902e+05 | 1.25e+04 | f | 89C330, 8AE650, 8B14F0, 8B2DF0, 8DECD0 |
| 17 | inertia_z | 149 | 356.2 / 2101 / 8e+04 | 3200 | f | 89C330, 8AE650, 8B14F0 |
| 18 | engine_peak_torque | 144 | 0 / 700 / 4050 | 500 | f | 8C2420 |
| 24 | launch_speed_limit | 150 | 1.875 / 5 / 6 | 10 | f | 8BCBB0 |
| 25 | ride_offset | 67 | -0.13 / -0.02 / 0.1 | 0 | f | 88CFE0, 88D620, 8A7D10, 8D1380, 8D88E0 |
| 26 | inertia_scale | 0 |  | 1 | f | 8AE650, 8B14F0, 8BE410, 8BE990 |
| 30 | cog_y | 135 | -0.5 / -0.1 / 0.9 | 0.2 | f | 8902C0 |
| 32 | cog_z | 137 | -0.4 / 0.1 / 0.8 | 0 | f | 8902C0 |
| 38 |  | 147 | 0.5 / 1.39 / 4 | 0 | f | 874700, 87F730, 87F9C0 |
| 39 |  | 50 | 0.1 / 1 / 2.03 | 0 | f | 874700, 87F730, 87F9C0 |
| 40 | idle_rpm | 150 | 1 / 650 / 2211 | 2383 | f | 881DD0, 891670, 8B2370, 8C2420 |
| 42 | redline_rpm | 150 | 2000 / 6500 / 1e+04 | 1.3e+04 | f | 881DD0, 88D9A0, 891670, 8B2370, 8C2420 |
| 138 | p138 | 32 | 0.75 / 1.5 / 1.5 | 1 | f |  |
| 140 | gravity | 0 |  | -17 | f | 88D750, 88F550, 8AF6A0, 8B2DF0, 8BCAF0, 8BEE70 |
| 141 | gravity_air | 0 |  | -17 | f | 8BCAF0, 8BE410, 8CFAF0 |
| 160 | top_speed_limit | 143 | 79.89 / 122.2 / 169.8 | 160 | f | 8700F0, 88D9A0, 8B2DF0, 8BC4D0, 8BD9B0, 8CFAD0 |
| 166 | launch_throttle | 6 | 0.7 / 0.7 / 0.95 | 0.9 | f | 8AF740, 8BCBB0 |
| 183 | upshift_fraction | 140 | 0.85 / 0.95 / 0.99 | 0.9 | f | 8C2420 |
| 184 | shift_fraction_b | 147 | 0.75 / 0.85 / 0.85 | 0.7 | f | 8C2420 |
| 185 | shift_fraction_c | 25 | 0.75 / 0.8 / 0.8 | 0.85 | f | 8C2420 |
| 186 | shift_fraction_d | 98 | 0.85 / 0.99 / 0.99 | 0.95 | f | 8C2420 |
| 187 | reverse_speed_limit | 1 | 30 / 30 / 30 | 50 | f | 8B2DF0 |
| 188 | suspension_droop_max | 149 | 0.03 / 0.08 / 1.5 | 0.2 | f | 88D620 |
| 239 | launch_assist | 1 | 0 / 0 / 0 | 1 | b | 8BCBB0 |
| 275 | gear_reverse | 137 | -0.5 / -0.3165 / 1 | -0.35 | f | 8C2420 |
| 276 | gear_1 | 150 | 0.2 / 0.3269 / 1 | 0.38 | f | 8C2420 |
| 277 | gear_2 | 148 | 0.34 / 0.55 / 1 | 0.52 | f | 8C2420 |
| 278 | gear_3 | 147 | 0.51 / 0.76 / 1 | 0.66 | f | 8C2420 |
| 279 | gear_4 | 150 | 0.62 / 1 / 1.471 | 0.81 | f | 8C2420 |
| 280 | gear_5 | 149 | 0 / 1 / 1.8 | 0.95 | f | 8C2420 |
| 281 | gear_6 | 79 | 0 / 1.349 / 1.639 | 1 | f | 8C2420 |
| 282 | gear_count | 137 | 1 / 4 / 5 | 6 | f | 88D9A0, 8C2420 |
| 292 | suspension_droop_min | 114 | -0.5 / -0.1 / -0.02 | -1 | f | 88D620 |
| 298 | steer_max_angle | 120 | 0 / 40 / 55 | 35 | f | 8AE400, 8AF790, 8AF860, 8B2DF0, 8BD9B0, 8DC450, 8E1F40 |
| 300 | camera0_x | 0 |  | 0 | f | 88DF80 |
| 301 | camera0_y | 148 | 0.96 / 1.27 / 5.2 | 1.05 | f | 88DF80 |
| 302 | camera0_z | 150 | 4.7 / 5.88 / 18.5 | 6.7 | f | 88DF80 |
| 303 | camera1_x | 0 |  | 0 | f | 88E230 |
| 304 | camera1_y | 150 | 0 / 0.45 / 3.32 | 0.2 | f | 88E230 |
| 305 | camera1_z | 4 | -10 / -5.5 / 0 | -2 | f | 88E230 |
| 310 |  | 2 | 35 / 35 / 35 | 0 | f |  |
| 313 |  | 126 | -0.08 / -0.05 / 0 | -0.06 | f |  |
| 327 | camera2_x | 0 |  | 0 | f | 88DF80 |
| 328 | camera2_y | 0 |  | 0.48 | f | 88DF80 |
| 329 | camera2_z | 0 |  | -0.75 | f | 88DF80 |
| 337 | camera3_x | 0 |  | 0 | f | 88DF80 |
| 338 | camera3_y | 0 |  | 0.6 | f | 88DF80 |
| 339 | camera3_z | 0 |  | -2.2 | f | 88DF80 |
| 340 | camera4_x | 0 |  | 0 | f | 88E230 |
| 341 | camera4_y | 0 |  | 0 | f | 88E230 |
| 342 | camera4_z | 0 |  | 0 | f | 88E230 |
| 344 |  | 133 | 0.01 / 0.1 / 0.2 | 0.03 | f | 8B2DF0, 8BD9B0 |
| 345 |  | 149 | 0.33 / 0.5 / 1 | 0.485 | f | 8B2DF0, 8BD9B0 |
| 349 |  | 1 | -0.1 / -0.1 / -0.1 | 0 | f | 8BD9B0 |
| 350 |  | 3 | -0.1 / -0.1 / -0.1 | 0 | f | 8BD9B0 |
| 351 |  | 146 | -0.1 / -0.1 / -0.1 | 0 | f | 8BD9B0 |
| 352 |  | 150 | 0 / 0.5 / 0.7 | 0.2 | f | 8B2DF0, 8BD9B0 |
| 353 |  | 149 | 0 / 0.7 / 0.8 | 0.2 | f | 8BD9B0 |
| 354 |  | 148 | 0 / 0.5 / 1 | 0.75 | f | 8BD9B0 |
| 355 | engine_inertia | 150 | 0.0001 / 0.011 / 0.2 | 0.0035 | f | 8C2420 |
| 356 | driveline_inertia | 150 | 0.001 / 0.01 / 0.1 | 0.003 | f | 8C2420 |
| 357 | engine_friction_0 | 150 | 0 / 0 / 10 | 0.1 | f | 8C2420 |
| 358 | engine_friction_1 | 149 | 0.0001 / 0.008 / 0.12 | 0.0035 | f | 8C2420 |
| 359 | engine_friction_2 | 0 |  | 0 | f | 8C2420 |
| 360 | wheel_radius | 144 | 0.26 / 0.335 / 0.71 | 0.3 | f | 8C2420 |
| 361 | drive_ratio | 148 | 0.1074 / 0.2457 / 0.7438 | 0.25 | f | 8C2420 |
| 362 | p362 | 147 | 0.3 / 0.9 / 0.99 | 0.25 | f | 8C2420 |
| 364 | rev_limit_band | 5 | 0.12 / 0.5 / 0.99 | 0.2 | f | 8C2420 |
| 365 | launch_ramp | 37 | 50 / 50 / 800 | 100 | f | 8AF800, 8BCBB0 |
| 366 | p366 | 117 | 0 / 0.25 / 0.4 | 0.2 | f | 8AF800, 8B2DF0, 8BCBB0 |
| 367 | tyre_long_stiffness | 147 | 3500 / 8500 / 4e+04 | 9000 | f | 8B2370, 8B2DF0, 8B6F50, 8B8110, 8B9780, 8B9900, 8DECD0 |
| 368 | tyre_grip | 134 | 330 / 1105 / 7200 | 1080 | f | 8B2370, 8B2DF0, 8B6F50, 8B8110, 8B9780, 8B9900, 8C0DF0, 8DECD0, 8E1F40 |
| 369 | tyre_lat_stiffness | 146 | 3500 / 9500 / 4e+04 | 9000 | f | 8B2370, 8B2DF0, 8B8110, 8B9900, 8C0DF0 |
| 371 | front_drive_share | 40 | 0.15 / 0.3 / 1 | 0 | f | 8AE650, 8B2DF0, 8C0DF0 |
| 373 |  | 149 | 0 / 4e-05 / 0.0002 | 0.0001 | f | 8B2DF0, 8BD9B0 |
| 374 |  | 146 | 0 / 0.0001 / 0.0001 | 0.00015 | f | 8B2DF0, 8BD9B0 |
| 375 |  | 150 | 0 / 0 / 0.5 | 0.2 | f | 8B2DF0, 8BD9B0 |
| 376 | tyre_mu_slip_scale | 24 | 2 / 5 / 15 | 10 | f | 8B2DF0, 8B6F50, 8B8110, 8B9900 |
| 377 | tyre_mu_sliding | 0 |  | 0.9 | f | 88F630, 8B2DF0, 8B6F50, 8B8110, 8B9900, 8E1F40 |
| 378 | tyre_mu_peak_extra | 1 | 0.25 / 0.25 / 0.25 | 0.45 | f | 88F630, 8B2DF0, 8B6F50, 8B8110, 8B9900, 8E1F40 |
| 379 |  | 144 | 0 / 1.1 / 2 | 1 | f | 8AF860, 8B2DF0 |
| 380 |  | 150 | 0 / 0.6 / 0.8 | 1 | f | 8AF860, 8B2DF0 |
| 381 |  | 25 | 0 / 2.25 / 10 | 2 | f | 8AF860, 8B2DF0 |
| 382 |  | 22 | 0 / 0.05 / 0.2 | 0.05 | f | 8AF860 |
| 383 |  | 3 | 0 / 0 / 0 | 10 | f | 8AF860 |
| 384 |  | 3 | 0 / 0 / 0 | 30 | f | 8AF860 |
| 385 |  | 150 | 0 / 0.55 / 0.8 | 1.5 | f | 8AF860 |
| 386 |  | 150 | 0 / 2 / 2 | 3 | f | 8AF860 |
| 387 |  | 150 | 0 / 0.55 / 0.8 | 2.5 | f | 8AF860 |
| 388 |  | 140 | 0 / 2 / 2 | 1 | f | 8AF860 |
| 389 |  | 150 | 0 / 3 / 5 | 7 | f | 8AF860 |
| 390 |  | 54 | 0 / 2.5 / 5 | 3 | f | 8AF860 |
| 391 |  | 145 | 0 / 2 / 5 | 1 | f | 8AF860 |
| 392 | wheelbase | 150 | 1.84 / 2.794 / 10 | 2 | f | 8BF7B0 |
| 393 |  | 147 | 1.2 / 1.5 / 4 | 1 | f | 8B2DF0, 8BCBB0 |
| 394 | camera5_x | 0 |  | 0 | f | 88DF80 |
| 395 | camera5_y | 0 |  | 0.6 | f | 88DF80 |
| 396 | camera5_z | 0 |  | 0.2 | f | 88DF80 |
| 410 |  | 131 | 0.341 / 1.039 / 1.7 | 1 | f |  |
| 411 |  | 130 | 1 / 1 / 7 | 10 | f | 8BD9B0 |
| 412 |  | 2 | 0 / 0 / 0 | 0.1 | f | 8BD9B0 |
| 413 |  | 3 | 0.1 / 0.1 / 0.1 | 0.2 | f | 8B8110 |
| 414 |  | 4 | 5 / 5 / 5 | 0.25 | f | 8B8110 |
| 430 | camera_head_x | 1 | -0.02 / -0.02 / -0.02 | 0 | f | 88DF80 |
| 431 | camera_head_y | 149 | 0.21 / 0.49 / 1.64 | 0.3 | f | 88DF80 |
| 432 | camera_head_z | 146 | -5.64 / 0.11 / 0.71 | 0.19 | f | 88DF80 |
| 433 |  | 4 | 0.2 / 0.2 / 0.35 | 0 | f |  |
| 434 |  | 149 | -0.11 / 0.41 / 1.51 | 0 | f |  |
| 435 |  | 1 | -2 / -2 / -2 | -10 | f |  |
| 438 |  | 1 | 1 / 1 / 1 | 0.05 | f |  |
| 443 |  | 1 | 1 / 1 / 1 | 0.8 | f |  |
| 451 |  | 4 | -0.32 / -0.3 / -0.3 | 0 | f |  |
| 452 |  | 148 | 0.25 / 0.53 / 2.78 | 0.8 | f |  |
| 453 |  | 150 | -2.06 / 0.76 / 5.9 | 0.3 | f |  |
| 454 |  | 4 | -0.3 / -0.3 / -0.2 | 0 | f |  |
| 455 |  | 150 | -0.02 / 0.405 / 2.68 | 0 | f |  |
| 456 |  | 8 | 6 / 20 / 20 | 10 | f |  |
| 458 |  | 12 | 30 / 30 / 40 | 45 | f |  |
| 469 | p469 | 1 | 5.6e+04 / 5.6e+04 / 5.6e+04 | 1e+05 | f | 88D620 |
| 470 |  | 147 | 20 / 25 / 26 | 15 | f | 8AF860 |
| 492 |  | 145 | 0.01 / 0.3 / 0.7 | 0 | f | 874700, 87F730, 8831F0 |
| 493 |  | 64 | 0.05 / 0.15 / 0.5 | 0 | f | 874700, 87F730, 8831F0 |
| 496 |  | 134 | 0.0002 / 0.005 / 0.05 | 0 | f | 8BD9B0 |
| 497 |  | 120 | 1.15 / 1.5 / 1.95 | 1 | f | 8C0DF0 |
| 498 |  | 1 | 1 / 1 / 1 | 0 | f | 8BD170 |
| 502 |  | 150 | -5 / -2 / 0 | 2 | f | 874EA0, 8BF7B0 |
| 503 |  | 149 | 0.9 / 0.9 / 1 | 0.75 | f | 885E80 |
| 504 |  | 146 | 0.25 / 0.7 / 1 | 0.3 | f | 8858F0 |
| 505 | spring_axle_scale | 93 | 0.5 / 1.1 / 2 | 1 | f | 8BA900 |
| 506 | damper_axle_scale | 147 | 0.85 / 1.15 / 2 | 1.25 | f | 8BA900 |
| 507 | spring_progression | 1 | 0.1 / 0.1 / 0.1 | 0 | f | 8BA900 |
| 508 | damper_rebound_scale | 146 | 2.5 / 2.5 / 4 | 2 | f | 8BA900 |
| 509 | aero_drag | 104 | 0.004 / 0.02 / 0.5 | 0 | f | 88D9A0, 8BE410 |
| 510 |  | 110 | -15 / 7.5 / 80 | 0 | f | 8BD9B0 |
| 511 |  | 131 | 5 / 10 / 40 | 0 | f | 8BD9B0 |
| 512 |  | 23 | -1.571 / -1.571 / 3.14 | 0 | f | 8BEE70 |
| 513 |  | 18 | 3000 / 5000 / 5000 | 2000 | f |  |
| 516 | shift_time | 131 | 0 / 0.1 / 0.4 | 0.2 | f | 8C2420 |
| 517 | tyre_long_scale | 123 | 0.64 / 1.5 / 2 | 1 | f | 8B2370, 8B8110, 8B9900 |
| 519 |  | 16 | 50 / 90 / 120 | 0 | f | 8B8110 |
| 520 |  | 12 | 0 / 0.25 / 0.75 | 0.5 | f | 8B8110 |
| 521 |  | 16 | 1 / 1 / 1 | 8 | f | 8B8110 |
| 522 | torque_at_idle | 104 | 0.3 / 0.7 / 0.9 | 0.6 | f | 8C2420 |
| 523 | torque_at_redline | 136 | 0.4167 / 0.65 / 0.85 | 0.75 | f | 8C2420 |
| 524 | torque_peak_position | 139 | 0.2644 / 0.55 / 0.85 | 0.45 | f | 8C2420 |
| 525 |  | 5 | 1.2 / 1.2 / 1.5 | 1 | f | 8B8110 |
| 526 |  | 13 | 1.2 / 1.2 / 1.2 | 1 | f | 8B8110 |
| 527 |  | 150 | 0.15 / 0.25 / 1 | 0 | f | 8BD170 |
| 528 |  | 150 | 0.24 / 0.385 / 0.8 | 0 | f | 8BD170 |
| 529 | handbrake_mu_base | 2 | 0.925 / 0.9375 / 0.95 | 1 | f | 8B2DF0, 8B8110, 8B9900 |
| 530 | handbrake_mu_extra | 144 | 0 / 0.2 / 0.75 | 0.5 | f | 8B2DF0, 8B8110, 8B9900 |
| 531 | handbrake_mu_slip | 0 |  | 15 | f | 8B2DF0, 8B8110, 8B9900 |
| 532 |  | 145 | 0.03 / 0.03 / 0.1 | 0 | f | 8BE410 |
| 535 | camera6_x | 0 |  | 0 | f | 88DF80 |
| 536 | camera6_y | 140 | 0.65 / 0.935 / 4 | 0.9 | f | 88A790, 88DF80 |
| 537 | camera6_z | 137 | -0.09 / 0.78 / 3.71 | 0.8 | f | 88DF80 |
| 538 | hand_anchor_y (cockpit.md) | 71 | -0.1 / 0.02 / 0.1 | 0 | f |  |
| 540 |  | 1 | 90 / 90 / 90 | 100 | f | 88A790 |
| 542 |  | 83 | 0.125 / 0.175 / 0.275 | 0.2 | f |  |
| 544 |  | 2 | 60 / 60 / 60 | 85 | f |  |
| 546 |  | 125 | 0.72 / 1.04 / 4.5 | 0.9 | f |  |
| 547 |  | 132 | -4 / -1.3 / 2.25 | -0.8 | f |  |
| 549 | camera7_x | 0 |  | 0 | f | 88DF80 |
| 550 | camera7_y | 4 | 3.65 / 4.6 / 5.49 | 4.5 | f | 88DF80 |
| 551 | camera7_z | 3 | 10 / 20 / 22 | 14 | f | 88DF80 |
| 552 | camera8_x | 1 | -2 / -2 / -2 | 0 | f | 88E230 |
| 553 | camera8_y | 4 | 0.77 / 1.61 / 2.6 | 1 | f | 88E230 |
| 554 | camera8_z | 4 | -2 / -1 / 5 | -5 | f | 88E230 |
| 559 | hand_anchor_z (cockpit.md) | 40 | -0.3 / -0.05 / 0.1 | 0 | f |  |
| 561 |  | 144 | 0 / 0 / 1 | -1 | f |  |
| 568 |  | 1 | 0.75 / 0.75 / 0.75 | 1 | f | 8AF6A0 |
| 569 | clutch_stiffness | 73 | 0.15 / 0.5 / 10 | 1 | f | 8C2420 |
| 570 | clutch_torque | 0 |  | 4 | f | 8C2420 |
| 571 |  | 45 | 0 / 0 / 0.35 | 0.1 | f | 874B00 |
| 572 |  | 111 | 0 / 0.1 / 0.5 | 0.05 | f | 874B00 |
| 573 |  | 96 | 0 / 0.05 / 1 | 0.2 | f | 874B00 |
| 574 |  | 96 | 0 / 0.25 / 0.35 | 0.15 | f | 874B00 |
| 575 |  | 14 | -0.25 / 0.025 / 0.1 | 0.05 | f | 874B00 |
| 576 |  | 55 | -0.5 / 0 / 0.3 | -0.075 | f | 874B00 |
| 577 |  | 32 | 0 / 0 / 0.4 | 0.05 | f | 874B00 |
| 578 |  | 113 | 0 / 0.1 / 0.45 | 0.25 | f | 874B00 |
| 579 |  | 108 | 0 / 0.075 / 0.4 | 0.3 | f | 874B00 |
| 580 |  | 92 | -0.35 / -0.25 / 0 | -0.15 | f | 874B00 |
| 581 |  | 15 | -0.25 / -0.02 / 0.1 | 0.05 | f | 874B00 |
| 582 |  | 53 | -0.2 / 0 / 0.3 | -0.075 | f | 874B00 |
| 583 |  | 143 | 0.1 / 0.2 / 0.25 | 0 | f |  |
| 584 |  | 146 | 27 / 35 / 41 | 45 | f |  |
| 585 |  | 146 | -0.01 / 0.07 / 0.2 | 0 | f | 88F630 |
| 586 |  | 19 | -0.15 / 0.05 / 0.05 | 0.1 | f | 88F630 |
| 587 |  | 73 | -0.15 / -0.1 / 0.2 | 0.1 | f | 88F630 |
| 588 |  | 125 | 1 / 1 / 3 | 0 | f | 88F630 |
| 589 |  | 150 | -1 / 0.5 / 1 | 0 | f | 88F630 |
| 590 |  | 95 | -0.17 / -0.05 / 0.08 | 0 | f | 88F630 |
| 591 |  | 83 | 0.7 / 1 / 2 | 0 | f | 88F630 |
| 592 |  | 141 | -1 / -1 / 1.75 | 0 | f | 88F630 |
| 593 |  | 63 | -0.2 / -0.1 / 0.1 | 0 | f | 88F630 |
| 594 |  | 11 | 0 / 0 / 0 | 1 | b | 88F630 |
| 595 |  | 150 | 0.5 / 0.5 / 0.5 | 0 | f |  |
| 596 |  | 75 | 0 / 0 / 1 | 0.5 | f | 8B2370 |
| 597 |  | 2 | -1 / -1 / -1 | 1 | f | 8B2370 |
| 598 |  | 129 | -0.25 / 0.1 / 5 | 0 | f |  |
| 599 | cog_y_2 | 144 | -0.5 / -0.1 / 0.9 | 0 | f |  |
| 600 |  | 1 | 0.5 / 0.5 / 0.5 | 1 | f |  |
| 601 |  | 5 | 0 / 10 / 10 | 1.5 | f |  |
| 604 |  | 19 | 1.401e-45 / 1.401e-45 / 1.401e-45 | 0 | b | 86D9F0 |
| 605 |  | 46 | 88 / 121.5 / 220 | 255 | f | 891670 |
| 606 |  | 1 | 15 / 15 / 15 | 0 | f | 891670 |
