# Driver.exe: vehicle loading

Updated: 2026-10-21

Static findings from `Driver.exe` 1.04 (SHA-256 `C79987B0...6616`, fixed image
base 0x00400000, no ASLR). Addresses are VAs. Function names are Ghidra
defaults until a finding earns a real name.

Produced with `tools/ghidra/FindVehicleLookup.java` and
`tools/ghidra/ExploreAddresses.java`; rerun with, for example:

```powershell
.\tools\ghidra\run-headless.ps1 FindVehicleLookup.java out\ghidra\vehicle-lookup
.\tools\ghidra\run-headless.ps1 ExploreAddresses.java out\ghidra\x fn:005A6FD0 calls:0086C540
```

## Summary for adding vehicles

| Question | Answer | Evidence |
| --- | --- | --- |
| Is the vehicle count fixed in code? | No. It is the u16 at VSCT+6. | FUN_0086C540 |
| How are IDs resolved to archive entries? | Linear scan of SSLP records `{u16 id, u16 index, u16 kind}`, count at SSLP+2. Order doesn't matter. | FUN_005A0A80 |
| What is the ID used for in game? | It's the model ID: `TrafficSpooler.RequestMissionVehicle(188)` spools the Gran Torino, `GameVehicleResource.create{Model_Id = ...}` spawns it. | Felony Rework scripts |
| Is there a cap on spooled vehicles? | Yes, per kind, from SSLP header bytes +0x18..: 35 base, 2, 2, 0. | FUN_005A0AD0, FUN_005A6FD0 |
| Are vehicle IDs range-checked? | Not on the request path read so far (FUN_005A7860 → FUN_005A6FD0 → FUN_005A0A80). Other systems may still index tables by ID. | hypothesis |

## Unified spooling package loader (confirmed-static)

FUN_005A11E0 is a state machine for any `.sp` package, not just vehicles:

1. State 1 opens the spool file. On failure it logs
   `Couldn't Open Spool File '%s'`.
2. State 2 calls FUN_005A0DE0, then scans the root entries for `SSIC`; without
   one it logs `Package does not contain SSIC, therefore this package is not
   compatible with the Unified Spooling scheme.` FUN_0059F9C0 creates the
   handler for the package type.
3. State 3 polls the handler.

FUN_005A0DE0 records the sizes of the `MDXN` (entry version 3) and `NONR`
it finds. Correction (see `spool-limits.md`): it only measures SSIC's own
blocks (0x9CB800 and 0x13EAA0), not the largest vehicle entries. Vehicle
limits are covered in `spool-limits.md`.

The spool object's SSLP methods (vtable 0x00DE4D30):

| Function | Behaviour |
| --- | --- |
| FUN_005A0B50 | on SSLP load: payload at +0x72F4, records at payload+0x1C |
| FUN_005A0A80 | `contains(id)`: linear scan of `count` 6-byte records |
| FUN_005A0AD0 | `capacity(kind)`: byte at header +0x18 + kind |

## Vehicle request path (confirmed-static)

Lua "Requests a specific vehicle resource is loaded" (FUN_0066E650) reads a
UID with `luaL_checkinteger` (FUN_005F6BE0) and calls FUN_005A7860:

1. If the ID is already in the loaded list at +0x330 (u16 at node+8), done.
2. Else FUN_005A6FD0: refuse unless FUN_0059C620 → FUN_005A0A80 finds the ID
   in SSLP; refuse if used slots would exceed `capacity(0)`; otherwise queue
   the request (FUN_005A7E30).

## VSCT resource (confirmed-static)

VSCT's vtable is at 0x00E3967C. On load (FUN_0086C630) it stores the payload
pointer at `[Engine+0x780]+0x2C`.

- FUN_0086C540: `count = *(u16 *)(VSCT + 6)`
- FUN_0086C600: `record(i) = VSCT + 8 + i * 0x1C`

`VEdit.GetNumVehicles` and `VEdit.GetVehicleTypes` use these. Nothing else
calls them directly (ExploreAddresses `calls:` scan); other users may read
`+0x2C` directly.

Other resource vtables found from their `GetType` stubs:

| Tag | Vtable | Notes |
| --- | --- | --- |
| UPVH | 0x00E39630 | installed by FUN_008E3ED0, FUN_0088AA60 |
| VO3D | 0x00E39654 | |
| VSCT | 0x00E3967C | |
| SPSR | 0x00E395A4 | |
| VSRT | 0x00DDA040 | installed by FUN_0048F6F0 |

## Handling parameters (confirmed-static)

`VEdit.GetGameDefaults` (FUN_00626F00) walks 0x25F = 607 parameters, matching
the highest VO3D parameter ID in the archive (606):

- type table at 0x00F3E450 (static, in `.data`): 367 float, 3 bool,
  237 unused (type 3);
- default values at 0x0144FF0C, in `.bss`, so filled in at startup.
  `tools/game/dump-handling-defaults.ps1` reads them from a running game.

The executable has no parameter names. The loose `Vehicles\Configs\vehicNNN.VO3`
file (FUN_0088D020) holds the same binary as the chunk. Meanings, defaults and
the Forza mapping are in `handling.md`.

## VEdit is a stripped remote editor

The `VEdit` Lua functions format `$VEdit$...` messages into a local buffer
and return 0; the code that sent them to the external editor was removed
from the retail build. Calling them in game returns nothing. The table they
register is `{name, function, 0, help}`:

| Lua | Function |
| --- | --- |
| VEdit.GetNumVehicles | 0x00627110 |
| VEdit.GetVehicleTypes | 0x00627160 |
| VEdit.GetReadOnlyInformation | 0x006274A0 (spec manager at Engine+0x778: slot 0xC exists(id), slot 0x14 get(id)) |
| VEdit.GetGameDefaults | 0x00626F00 |
| load vehicle resource by UID | 0x0066E650 |
| unload vehicle resource by UID | 0x0066E690 |

FUN_0088D9A0 prints a vehicle spec (mass, wheelbase, track width, CoG
offset, top speeds, 0–60, quarter mile, horsepower, wheel radii, collision
box), which maps part of `CVehicleSpecification`.
