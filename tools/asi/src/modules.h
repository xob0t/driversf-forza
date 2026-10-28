// Module list. main.cpp runs each module's init in this order, once, from
// DllMain (after the exe check), unless dsfforza.ini has
// [<name>] enabled=0. A module is src/mod_<name>.cpp exporting
// `void mod_<name>_init()`.
//
// One line per module; add yours on its own line (keeps merges trivial).
// Suggested order: archive (vehicle archive limits), garage, hud.
DSF_MODULE(archive)
DSF_MODULE(garage)
DSF_MODULE(hud)
DSF_MODULE(lua)
DSF_MODULE(hands)
DSF_MODULE(dash)
DSF_MODULE(render)
