// dsfcars.asi core: exe check, logging, config, memory patching.
//
// Every module includes this header and nothing else from the core.
// Addresses are Driver.exe 1.04 VAs (image base 0x00400000, no ASLR).
// All functions are safe to call from a module's init (which runs inside
// DllMain, so: no LoadLibrary, no waiting on threads).
#pragma once

#include <stddef.h>
#include <stdint.h>

// ---- exe identity -------------------------------------------------------
// True when the running exe is Driver.exe 1.04 (checked once, before any
// module init). main.cpp skips every module when it is false, so modules
// don't need to call it; it's here for code that runs later (hooks).
bool dsf_exe_ok();

// ---- logging -------------------------------------------------------------
// Appends one line to <game>\plugins\dsfcars.log (truncated on start).
// printf-style; a trailing newline is added. Thread-safe.
void dsf_log(const char* fmt, ...);

// ---- config: <game>\plugins\dsfcars.ini ---------------------------------
// (The ini and the log are named after the plugin file: a copy loaded as
// plugins\dsfhostrun.asi reads dsfhostrun.ini and writes dsfhostrun.log.)
// [<module>] enabled=0 switches a whole module off (main.cpp checks it).
// Modules read their own per-patch switches with these.
int cfg_int(const char* section, const char* key, int def);
bool cfg_bool(const char* section, const char* key, bool def);
// Copies a string value (or def) into out; returns out.
const char* cfg_str(const char* section, const char* key, const char* def, char* out, size_t out_size);
// Full path of the ini / of the folder holding the asi (no trailing slash).
const char* dsf_ini_path();
const char* dsf_plugin_dir();

// ---- patching ------------------------------------------------------------
// Writes n bytes at va. When expect is given, the current n bytes must equal
// it, else nothing is written and false is returned (logged). Handles page
// protection and flushes the instruction cache.
bool patch_bytes(uint32_t va, const void* bytes, size_t n, const void* expect = nullptr);

// E9 rel32 jump at va to target, padded with NOPs up to len (len >= 5).
// expect (len bytes) as in patch_bytes.
bool patch_jmp(uint32_t va, void* target, size_t len = 5, const void* expect = nullptr);

// E8 rel32 call at va to target (5 bytes). expect (5 bytes) as in patch_bytes.
bool patch_call(uint32_t va, void* target, const void* expect = nullptr);

// True when the n bytes at va equal bytes.
bool bytes_equal(uint32_t va, const void* bytes, size_t n);
// bytes_equal that logs the current and the expected bytes when they differ.
bool check_bytes(uint32_t va, const void* bytes, size_t n);

// Executable, zeroed memory that lives for the whole process (never freed).
void* alloc_exec(size_t n);

// Shown in the log header; bump when patches change.
#define DSF_VERSION "0.1"

// What this plugin can do for car packages, as the car manager reads it from the DLL file
// without running the game (src/dsfforza/build/car_manager.py, plugin_capabilities): the
// marker, then space-separated names, NUL-terminated (main.cpp keeps it in .rdata). A .dsfcar
// manifest's "requires" names them (dsfcar.CAPABILITIES).
//   archive.packages  [archive] packages: cars from their own files beside dngvehicles.sp
//   archive.ids       the listed model ID is written into a package's data as it is read
//                     (mod_archive.cpp, pkg_set_ids), so car.sp is installed as it is
//   archive.redirect_chain  the redirect at 0x0085AC1E passes the names it doesn't list on
//                     through Driver.exe's CreateFileA import, where DSF Enhanced's [Files]
//                     Redirect hooks (worldlod reads it to know its map reaches the game)
//   lua.packages      ProfileSettings.GetVehicleUnlocked(8000000 + 2) and (8000000 + 0x4000 + id)
//                     tell register.lua whether the car packages, and each car's, are loaded
//                     (mod_garage.cpp magic_query); register.lua registers no package car
//                     without that answer (#511)
#define DSF_CAPABILITIES_MARKER "DSFCARS-CAPABILITIES: "
#define DSF_CAPABILITIES "archive.packages archive.ids archive.redirect_chain lua.packages"
extern const char dsf_capabilities[];
