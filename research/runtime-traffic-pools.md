# Live chapter traffic pools

DSF PC 1.04. This extends ambient selection without adding hundreds of models
to the native traffic spool list. `traffic.md` describes the original fixed
sets and their limits. The manager writes full per-chapter weighted choices
to `dsfcars/traffic/pools.bin`; the native AI file and its resident lists stay
unchanged. A pool is a set of eligible models, not a resident batch.

## Confirmed static evidence

Addresses are virtual addresses in Driver.exe. Decompiled functions and
disassembly used for this change are under `out/runtime-pool/native/` in the
`driversf-traffic-pools` worktree.

| Native code/data | Meaning and consequence |
| --- | --- |
| `00916870`, called at `0077ADCB` in `0077ADA0` | Main ambient weighted picker. Hook this call only; the second picker at `0077AF4B` retains attached-vehicle behavior. |
| `016DA940` | Pointer to native eight-byte model/float-weight pairs. The picker receives start, count and float total. |
| `00866510`, `00866320`, `0091689A..009168BB` | Seed a 72-byte RNG and draw in x87 extended precision. Multiply by total before rounding to float, then subtract float weights. |
| `[00FE7228]+758`, manager `+5CA840/+5CA844/+5CA9F0` | Active set hash, frequency and model override. Key full pools by active context; preserve overrides. |
| `[engine]+780`, `0086C5B0` | Vehicle-spec lookup. Package acceptance plus this lookup rejects invalid models; the native loaded query alone returns true for nonexistent models. |
| `[engine]+214`, `005A7860` | Traffic spooler and mission-model request. Native admission `005A6FD0` checks free capacity. Requests can queue and do not immediately mean resident. |
| `005A6EA0` | A model is resident if in traffic list `+330`, or in nontraffic list `+998` with a positive reference count. The implementation traverses each list once per pick. |
| `005A6D20` | Each loaded list has 50 nodes. Nontraffic storage is `+5B0..+998`, twenty bytes per node. The independent pending queue has fifty twenty-byte nodes at `+9AC..+D94`, header `+9A0`, live count `+9A8`. |
| `005A6FD0`, `005A6440`, `005A6480` | Admission accounts for queued requests, resident models and the single active load. Queued requests are distinct from the active loader, so several prefetch leases are valid. `005A6440` increments an existing queued load's reference count. |
| `005A6B00`, `005A7100`, `005A66E0` | RefCount checks resident, queued and active-load ownership, in that order. A zero-ref unloading resident node masks its queued reload. Release ignores the currently unloading ID; use its queued-release helper `005A66E0` for that ID before Release. The helper decrements exactly one queued reference and removes a zero-ref queue node. |
| `008D1380`, call at `008D1475` | Vehicle construction requests its own model reference. Transfer a prefetch lease by releasing exactly one reference after this succeeds. |
| `008D4B50`, call at `008D4F8A`, `005A78D0` | Vehicle destruction releases its own reference. The last release queues native unload; the mod never evicts a visible car's model. |
| `0074B7A0`, getter call `0074B8B6`, restore `0074BA20` | Replay captures model reference counts. Exclude the mod's transient lease from capture and cancel it before restore, so replay cannot recreate an ownerless lease. |
| `005A7360`, `0076FDF0` | Native reset and traffic-set change. Clear or cancel temporary ownership with these transitions. |
| `0077B2C6` | Native failed-spawn epilogue. When no eligible model is resident, return null before allocation, with the original stack cleanup. |

The hook reserves the native mission pot from prepared Lua data, one player
model and the interesting-vehicle manager's ten-model maximum at `007EB860`.
For each set/frequency, automatic added-model capacity is:

```
max(0, min(vehicle_slots - native_list_count - mission_pot_max - 1 - 10,
           50 - mission_pot_max - 1 - 10))
```

A numeric user streaming budget can lower that capacity. All resident package
models count toward it, including models owned by other systems. Native
admission remains authoritative. Each requested model has at most one temporary
prefetch lease. Both resident models and outstanding leases count against the
budget; a resident lease is counted once. The pending queue's fifty-node bound
also limits admission. These are capacity
reserves derived from native data, not fitted traffic-population targets.

## Selection and file contract

Use the existing manager's road-weight rules over the entire selected pool.
For each spawn, draw from those full weights with the native seeded RNG.
Request an unavailable added choice if there is room and that model has no
prefetch outstanding. Several different model loads can queue concurrently.
While it loads, draw among resident models in the same native/added group
using the next RNG result. If that group is cold, temporarily use the other
group. With both groups resident, the configured group shares remain intact.
Model shares during loading depend on residency; preview shares describe the
full configured pool, not a guarantee for each short stretch of road.

When all eligible added models fit in the budget, retain ordinary weighted
resident reuse. When they do not fit, prioritize fresh models: after one
successful primary birth of an added model, exclude it from further births
until native owners finish with it. Several served models can drain together.
The first model to finish loading cannot fill an entire road while peers load.
Ready prefetches still receive their first birth, so their temporary owners do
not prevent retirement indefinitely. Vanilla models fill loading gaps.

This is an explicit selection policy for the requested variety, not a claim
about vanilla's picker or a fitted engine constant. It trades short-term Forza
share for turnover when the entire pool cannot be resident. Existing cars keep
their references and disappear through native cleanup. The last release makes
room for another full-pool choice. There is no timer or forced deletion.

Each lease is released exactly once after construction succeeds or when its
context is cancelled. Replay capture excludes every owned lease. Reset drops
tracking because native reset owns the references; set changes and replay
restore cancel requests before calling their original functions. The unloading
reload exception above is exercised against actual native queue helpers.

The file starts with `DSFPOOL1`, then row, pair and package-ID counts. Package
IDs are u32. Rows are 36 bytes: eight u32 fields for set hash, frequency,
native start/count, native fingerprint, choice start/count and live capacity,
then float total. Pairs are twelve bytes: model ID, float weight and flags
for package model or native model added from another set. Reuse identical
pair blocks across rows. Reject invalid bounds, nonfinite/negative weights,
unknown flags and IDs outside the package base-ID domain below 1000.

FNV-1a over the original twelve-byte range and its eight-byte pairs detects
stale or externally changed native weights. A mismatch leaves that row native.
The separate binary marker `DSFCARS-TRAFFIC-POOLS: 1` lets the manager detect
support without changing car-package capabilities or rebuilding car assets.
Patch sites are checked as a group before installation; missing pool files
install no traffic hooks. The original native 40-entry selector buffer never
receives the large pool.

## Verification so far

The mapped-executable fixture runs the actual native RNG/picker for 10,000
full-pool seeds and 10,000 resident-filtered seeds. Another 10,000 seeds
check that residency fallback preserves the selected native/added group.
It exercises actual native request/release counts, last-release unload
queuing, replay capture and restore, native reset, the no-model spawn
epilogue, malformed files and atomic patch preflight. All pass in
`out/runtime-pool/build-v3.log`.

Python tests verify all 721 candidates in serialized rows and previews,
distinct pools for chapters sharing native ranges, unchanged original AI,
legacy-copy removal, plugin compatibility and existing share/disable rules.
`out/runtime-pool/python-runtime.log` records 36 targeted tests passing.
The final suite passed 783 tests with lint, formatting and ratchet checks in
`out/runtime-pool/tests-final-full.log`. The tracked branch's native test passes
in `out/runtime-pool/build-branch-final.log`; the installed combination with
the provider-map fix passes in `out/runtime-pool/build-v6.log`. The withdrawn Tk window check in
`out/runtime-pool/gui-smoke.log` confirms 721 entries for every chapter.

The 24-model prototype drove in `vm1-20261110-110943` without a recorded
fault. It made 25 prefetch requests and 24 native reference handoffs.
The full 721-model binary has 280 context rows and 109,917 deduplicated
weighted choices in 1,331,988 bytes. Every story chapter has 721 candidates;
the final 60-slot configuration reserves ten kind-0 slots for interesting
vehicles as well as ten nontraffic nodes. This is conservative when the
interesting vehicles are themselves Forza models, since they also count
against the added-model quota.

The first full-roster run, `vm1-20261110-114332`, transferred 19 distinct
models but its driving probe stopped during chase view and the screen went
black. Rendering continued and the harness recorded no crash. This is not
accepted as a successful drive. Full-roster runtime validation remains open.

The updated group-preserving selector, `vm1-20261110-150100`, drove the
vanilla probe with 99 speed samples before the same black-screen stall.
Twenty distinct primary prefetches transferred and one additional package
model was constructed. A read-only spooler snapshot at 15:07:03 showed
18 traffic nodes and 28 nontraffic nodes, state zero and an empty pending
queue. The outstanding prefetch, model 801, had one resident reference.
Provider maps had crossed their 45 inline nodes. This correlation does not
establish whether the map extension, loading callbacks or another subsystem
caused the stall.

The attempted 45-slot control, `vm1-20261110-151125`, hit the same stall with
67 speed samples. Its custom pool-file override was skipped by the harness:
the overlay was suppressed for that path, but the harness only installs
known archive overrides and root files. The plugin logged its feature marker
but never logged pool parsing or hook installation. This was therefore a
native-selection control, not a 45-slot live-pool test. The stall reproduces
with no traffic hooks installed; it is not evidence against the live selector.

Two further straight-throttle controls also stopped: `vm1-20261110-151925`
with runtime pools explicitly off, and `vm1-20261110-152715` with package
loading off, no overlay and native 35 slots. No cause is established. This
also rules out attributing the stall solely to roster size or provider-map
growth. The measured route below avoids relying on an uncontrolled drive.

**Confirmed runtime turnover:** `vm2-20261110-153433` uses the existing
`drawdist.lua` traffic route, the full binary filtered to 24 accepted package
models, added IVs off and a deliberately small three-model streaming budget.
Initial primary models were 15, 10 and 3. The selector stopped new births of
15 at 15:35:17. Native cleanup released it at 15:36:20, then models 2 and 20
loaded and spawned, followed by 15 again. Five distinct primary models used
three resident slots, with six successful request-to-vehicle transfers and
no recorded crash. The test never forced a model reference to zero or removed
a visible vehicle. This verifies actual admission beyond the initial batch.

The route is the repository's existing controlled 1500 m movement at 20 m/s,
starting at its measured industrial straight. Its timing and test budget are
test stimuli, not production streaming rules. Production has no timer.
Full-roster route validation is recorded separately when complete.

**Full-roster eligibility confirmed, long drive still open:**
`vm2-20261110-154634` logged `validates 721 of 721 positive package choices`
from the actual package-acceptance and native VSCT queries. It constructed
18 distinct primary Forza models while resident package counts fell and new
requests were admitted as the player changed roads. The controlled route
stopped logging at t=27.84 and the screen went black, with C++ frames still
rendering. No crash was recorded. This does not pass the long-drive check.
The VM controls and unresolved pause are recorded on
[#589](https://github.com/xob0t/driversf-forza/issues/589#issuecomment-6767935567).

The owner requested installation for testing. The installed test build uses
this module plus PR #590's provider-map fix and retains the owner's 60 slots.
Its DLL SHA256 is `524975fb8d4a8c1da0cf363ae8fe4ae9c29acf41cf405e5ce6ee00d6b45c5028`.
`out/runtime-pool/installed-survey.json` verifies 721 positive package choices
in every one of 280 rows, 721 enabled cars, removal of the static AI copy and
slot marker, and all 16 original uninstall-manifest entries with matching
plugin/ini hashes. Before, chapters had 20 to 25 fixed Forza models; after,
all nine story chapters have 721 live candidates and 15 to 21 added-model
resident slots, with Chapter 7 at 18. The renderer and car assets were not
changed. The installer-helper backup is `out/runtime-pool/host-20261110-154922`.

Regenerate settings with the car manager after changing the native vehicle
slot limit. Full-roster host driving, replay transitions and mission/chapter
transitions remain to be verified before removing the PR's draft status.

**Car-management failure handling:** automatic `Manager.save()` refreshes use
`write(strict=False)`, log unavailable live-pool updates and independently
refresh IV settings when possible. Missing AI/scripts, an older plugin or
traffic-file IO failures cannot abort unrelated install/enable/remove work
or mask an install rollback's original error. Explicit CLI traffic edits
and GUI Apply remain strict, write files before saving settings, and restore
their prior settings on failure. `out/runtime-pool/review-targeted.log`
records 78 relevant tests passing; the final full-suite log includes these
regressions. Native code, installed DLL and pool membership are unchanged by
this review fix.

## Host feedback and concurrent turnover correction

The owner's host drive disproved the earlier selection policy's practical
variety. `out/runtime-pool/user-20261110-235909/dsfcars.log` records only 41
distinct constructed Forza models in about thirteen minutes. The first model,
668, became available at 16:35:11.118; the next at 16:35:11.418. During that
gap every added-group fallback could select 668. After filling the cache,
retiring only one model at a time restricted admission to that model's cleanup
latency. The owner observed an initially identical road population and a
small recurring selection with rare additions. Eligibility alone did not
verify the intended behavior.

The corrected module uses the concurrent requests and per-model retirement
policy described above. New native decompilations are in
`out/runtime-pool/queued-requests/`. Both the branch DLL and its combination
with PR #590 pass the mapped-executable tests in
`out/runtime-pool/build-branch-v8.log` and `out/runtime-pool/build-v8.log`.
New fixtures check multiple actual queued loads, duplicate-demand ownership,
in-flight budget accounting, cancellation during load and unload, concurrent
retirement, suppression of the first ready model's repeated births, and
replay capture/restore with two independent leases. All pass.

The final Python suite passed 783 tests in 136.587 seconds, including lint,
format and ratchet checks, process exit zero, in
`out/runtime-pool/tests-v7-full.log`. The GUI copy now explains vanilla filling
loading gaps. A subsequent targeted lint/format check passed after that copy
change.

`vm2-20261111-002000` queued models 15, 14 and 7 in the same update, before
any was ready. Each received one primary birth. With three added-model slots
and 24 accepted packages, the run made seven requests and seven handoffs for
seven distinct models, and recorded four native retirements. It later stopped
at route t=11.52 and became black. `vm1-20261111-002025` accepted 721/721
choices, queued eighteen initial models and constructed all eighteen without
reusing a served model. Four models retired together at 00:24:14.517, followed
by request 324. Its route stopped at t=0.96 and became black. Neither run
recorded a crash, but neither completed the route. These establish concurrent
admission and initial repeat suppression, not full-roster long-drive success.
The unresolved VM pause and earlier controls remain on #589.

The corrected combined test DLL was installed through `tools/install.ps1`
helpers with SHA256
`70cfc4d9a66f0b8aa139a971e5e4df15eb733dc215502ff7c2731fbc134d448c`.
Backup: `out/runtime-pool/host-20261111-002827`.
`out/runtime-pool/installed-v8.json` and `installed-v8-survey.json` verify the
DLL hash, unchanged INI, sixteen manifest entries and 721 positive candidates
in all 280 rows. This update changes only the installed DLL. Chapter budgets,
manager settings, assets and renderer stay as they were.

**GUI failure rollback review:** every explicit GUI traffic edit now supplies
its mutation to `save_traffic`, which snapshots the shared settings inside the
worker before mutating and restores them on `SystemExit`, `ValueError` or
`OSError`. This covers IV Apply, car traffic toggles, pin/unpin, chapter Apply
and reset, ambient off and the retained legacy shuffle handler. Strict write
errors still reach the worker's error log. Regression tests exercise every
handler with each failure type, deferred mutation, dictionary identity,
successful IV persistence, and the reported old-plugin failure followed by an
unrelated car save. The installed native DLL and pool data do not change.

## One-model roads report (2026-11-13): cause found 2026-11-14

The owner's host log (Epilogue free roam, 721 packages, 60 slots, a copy in
the coordinator's temp folder) ends with roads "almost all Mazda RX-7". The
RX-7 is ID 3. The pool requested about 45 distinct models a minute to the
end and never repeated its own requests.

**What the logs show.** In both host reports the model that filled the roads
is the player's car. A shift reads a car's interior and exterior entries
(`id + 1000`, `id + 2000`), which no traffic request does. Those unrequested
reads are ID 3 at 18:19:36 on 2026-11-13, and ID 668 at 16:35:20 in
`user-20261110-235909` (the earlier "initially identical road population"
was 668). ID 3 was served once at 18:19:17 and never retired: the player
drove it until the log ends.

**What `decide()` can and cannot do (confirmed-static).** A served added
model is marked retiring at its first construction (`retain`, call
`008D1475`). Fallback draws skip retiring models, and only an observed native
unload clears the flag. So the picker gives an added model one birth per
residency. `forget()` used to clear every retiring flag. That let each
resident served model take one more birth: a real defect, but bounded, and
not a flood. Now `forget()` keeps the flags. The native fixture's lifecycle
case reproduces the reuse with the old code (4 failures) and passes with the
fix.

**Other spawners (confirmed-static).** `00916870` has two callers: `0077ADCB`
(hooked) and the attached-vehicle draw `0077AF4B` (native weights, vanilla
models only). The spawn `0077ADA0` creates the picked model. (Corrected 2026-11-14:
construction `008D1380` requests the model of the spec that the spec manager
returns for it, and that can be another resident model; see "Cause" below.) Vehicles made outside the picker:

| Creator | Model source |
| --- | --- |
| `007EBB20` (call `007EBBED`) | interesting vehicles: region lists, at most 3 cars per model |
| `00801A80` via `008018B0` | a Lua-weighted list (`00685040` adds entries) |
| `0082FF20` (call `00830092`) | its own model `+0xC8`, or the player's selected car `[[engine+754]+78]` |
| `008D88E0` (request `008D8A9C`) | model change of a live vehicle (car swap, `008DA680`) |
| `0074E010`, `006547A0`, `007A6090`, `007BEE90`, `007F4C70`, `007EEA00`, `0081ABD0`, `008DA2F0` | not decoded |

So the copies of the player's model come from one of these, or nearby
traffic is drawn with the player's data. The second is untested: the
player's interior and exterior entries are the only resident kind-1 and
kind-2 entries.

**Runtime so far.** The diagnostic plugin (branch `diag/live-pool-spawners`)
logs the following:

- every added-model birth not made by the picker, with the return address of
  the vehicle-creation call `008C5C80`;
- the model's reference count and the player's model at that birth;
- model changes;
- a births-by-spawner summary every 30 s;
- vehicle spec pointers (`vehicle + 5B4`) shared by two models.

`vm1-20261113-200311` (probe ID 3, traffic on, full roster) recorded 292
births in its first 35 s: 279 from this picker through `0077AE6A`, 3
interesting vehicles (`007EBBF2`), 5 from the Lua list (`00801B58`), 5
outside `008C5C80`, and no spec alias. It then stalled on the pause menu
(#589) before the player drove, so the one-model case is not reproduced
yet. The host run with that plugin decides between a spawner and a drawing
fault.

### Cause (2026-11-14)

**The spawning path (confirmed-static, confirmed-runtime).** No spawner asks
for the player's model. Vehicle construction `008D1380` takes its spec from
the spec manager `[Engine+0x778]` (`get(id)`, vtable `00E395D0` +0x14 =
`0086B9E0`, called at `008D1405`), and requests the reference of *that
spec's* model (`008D144A`, `008D1475`). When the manager has no spec for the
ID, `get()` returns the first listed spec. Its map is sorted by ID, so that is
the lowest resident ID: the owner's RX-7, ID 3, in every census below. The
decode, the addresses and the fixture are in `traffic.md`, "Vehicle spec
manager map". Two things make the picker ask for an ID the manager lacks:

1. **A listed model that hasn't registered.** `decide()`'s readiness copied
   `005A6EA0`: listed and referenced. A spooler request lists the model at
   once, and its spec registers only after its UPVH loads. So for a model
   loading again (a native request: here an IV's), every pick gave an RX-7.
   And since the birth was model 3, `retain()` never marked the picked model
   served. The picker kept choosing it: one decision, repeated for the whole
   window. This is why model 3 is "0 picked, retiring" in the summaries.
2. **The map's 46th node.** The spec map holds 45 nodes inline and its
   append has no end check. Node 46 overwrites the list's sentinel and cuts
   the IDs below the new one out of the list, so their lookups miss for good.
   `vm1-20261114-132735`'s flood began in the 30 s after the ID maps reached
   46 nodes (13:31:27; the spec map tracked ED/EF exactly in every
   census below) and held for 4.5 minutes. The fixture's shipped-code
   reproduction gives the cut-off IDs the successor of the 46th instead of
   ID 3, so how the head became 3 in that run is not reproduced; no run with
   the shipped map passed 45 again.

**Runs** (VM, the owner's installed overlay as in the 2026-11-14 PR comment,
probe ID 3, traffic on, 60 slots; run folders under
`%LOCALAPPDATA%\Temp\dsfforza-wsb\runs\`; plugin `diag/live-pool-specs`
and `diag/live-pool-specs-fix`, which log every `get()` that returns another
model, with its caller, the spawner and `decide()`'s answer, and a census of
the spec map):

| Run | Build | What | Births (picker) | Model 3 births |
| --- | --- | --- | --- | --- |
| `vm1-20261114-132735` | before (409771a7) | the flood: stuck at a barrier | 2313 (1192) | 999, up to 128 of 143 per 30 s |
| `vm1-20261114-142148` | before + diag | standing 6 min | 1688 (1589) | 0; spec map peak 45, 0 fallbacks |
| `vm1-20261114-143150` | before + diag | driving 9 min | 1871 (1783) | 0; peak 45, 0 fallbacks |
| `vm1-20261114-151912` | before + diag | standing, `-AheadIds 4,5,6` | 1546 (1453) | 3: the ahead cars, refused by the spooler, built by the Lua spawner `006707E4` as model 3 |
| `vm2-20261114-151915` | spec map fix + diag | driving 9 min | 2645 (2405) | 71: 68 ambient, 3 IV, from 116 picked before its spec registered (15:28:27 to :48); 3 IV births from 159 at 15:26:49 |
| `vm2-20261114-153702` | both fixes + diag | driving 17 min | 3597 (3330) | 3, all IV (`007EBBF2` asking for 150 without a spec); 0 ambient fallbacks |
| `vm2-20261114-155816` | both fixes + diag | standing 11 min (0 mph) | 2004 (1885) | 3, all IV (236 without a spec); 0 ambient fallbacks |
| `vm2-20261114-161449` | both fixes + diag | driving, then the #584 Epilogue switch (`409E0517/0 validates 721 of 721`) | 2703 (2615) | 0; no fallback at all |

No run with either build passed 45 spec nodes (peaks 44 to 45), so the page
storage past 45 is verified by the native fixture only, not in game.

**Fix.** (1) `decide()` counts a listed model as spawnable only when `get(id)`
returns that model's own spec, the test construction applies
(`mod_traffic.cpp`, `spec_ready`). (2) The spec map continues past 45 in
page storage, as ED/EF do (`vehicle_maps.cpp`, the call at `0086BE67`).

**Open.** The IV spawner `007EBB20` builds its model from `get()` with no
check (`007EBB72`), and asked for models whose spec was gone or not yet
registered: 159 and 150 above, 3 births each. That is a native spawner and the
live pool doesn't decide it; whether vanilla IVs ever do it is not checked.

