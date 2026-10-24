// State one module publishes for another. Each function has a definition in
// exactly one module and a safe answer when that module is off.
#pragma once

// mod_garage: the number of car cards the vehicles screen's car list fills at
// most for the group it shows (stock 20; 19 while a group of more than 20 cars
// is shown through a window, which keeps card 20's Header_Sprite_20 free).
int garage_card_limit();
