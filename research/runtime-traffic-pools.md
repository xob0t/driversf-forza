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
