// Draw queue: a full frame drops draws instead of ending the game.
// See research/spool-limits.md ("Draw queue: 16384 entries per frame").
//
// Vanilla (Driver.exe 1.04, confirmed-static): RenderQueueHolder_Construct
// (0x00BB6580) allocates one manager with two embedded draw queues of 0x4000
// entries (RenderQueueManager_Construct, 0x00BB5960), used in turn, one per
// frame. A queue is {u32 count, u32 capacity = 0x4000, block, entries}, 0x20
// bytes per entry. FUN_00BB5710 appends one entry: it bumps the count with a
// compare-exchange and copies the entry, with no bound.
//
// Its two callers check the bound first:
//   FUN_00BB5B70 skips the draw when count == capacity;
//   FUN_00BB5A50 (ShiftDrawMetadata_Enqueue 0x006BA880, FUN_00BB42E0) traps:
//     00BB5A92  3B 48 04           CMP ECX,[EAX+4]
//     00BB5A95  0F 84 BF 00 00 00  JZ 0x00BB5B5A   ; INT3
// so the 16385th draw of a frame ends the game with a breakpoint exception
// (0x80000003, fault offset 0x007B5B5A) and no error. The check is also not
// atomic: two threads that both see 16383 push entries 16384 and 16385 past the
// array, onto the other queue's header.
//
// A vanilla frame stays well under 16384. Full-detail traffic out to the cull
// distance (dsf_enhanced VehicleFullDetail=1) and a 500 m traffic radius bring
// a busy street near it; the user's game ended on a car spawn there (Hennessey
// Venom GT, 2026-10-28).
//
// This module:
//   1. replaces both calls to FUN_00BB5710 (0x00BB5B4C, 0x00BB5BB5) with a push
//      that takes a slot only while count < capacity (same compare-exchange),
//      and drops the draw otherwise;
//   2. turns FUN_00BB5A50's trap branch into NOPs, so a full queue reaches that
//      push and the draw is dropped, as FUN_00BB5B70 does.
// A frame over the limit then misses its last draws instead of ending the game.
// Drops and each new 1024 entries of the queue's high-water mark past 12288 are
// logged.
#include "patch.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <string.h>

static const uint32_t kPushCallChecked = 0x00BB5B4C;  // in FUN_00BB5A50
static const uint32_t kPushCallSkipped = 0x00BB5BB5;  // in FUN_00BB5B70
static const uint8_t kPushCallCheckedBytes[5] = {0xE8, 0xBF, 0xFB, 0xFF, 0xFF};  // call 0x00BB5710
static const uint8_t kPushCallSkippedBytes[5] = {0xE8, 0x56, 0xFB, 0xFF, 0xFF};  // call 0x00BB5710
static const uint32_t kTrapJump = 0x00BB5A95;  // jz 0x00BB5B5A (int3)
static const uint8_t kTrapJumpBytes[6] = {0x0F, 0x84, 0xBF, 0x00, 0x00, 0x00};
static const uint8_t kNops[6] = {0x90, 0x90, 0x90, 0x90, 0x90, 0x90};

static const long kMarkFrom = 12288;  // high-water marks logged from here on
static const long kMarkStep = 1024;

static volatile long g_dropped = 0;   // draws dropped since the start
static volatile long g_mark = 0;      // highest mark logged
static volatile long g_last_log = 0;  // GetTickCount of the last drop line

struct DrawQueue {
    volatile long count;
    long capacity;
    void* block;
    uint32_t* entries;  // 8 dwords each
};

static void log_drop(long capacity)
{
    long n = InterlockedIncrement(&g_dropped);
    long last = g_last_log;
    long now = (long)GetTickCount();
    // the first drop, then at most one line every 10 s
    if (n != 1 && (DWORD)(now - last) < 10000) return;
    if (InterlockedCompareExchange(&g_last_log, now, last) != last) return;
    dsf_log("render: draw queue full (%ld entries): %ld draws dropped so far", capacity, n);
}

// FUN_00BB5710's job, bounded: thiscall (ECX = queue, one stack argument).
static void __fastcall bounded_push(DrawQueue* q, void*, const uint32_t* entry)
{
    long index;
    for (;;) {
        index = q->count;
        if (index >= q->capacity) {
            log_drop(q->capacity);
            return;
        }
        if (InterlockedCompareExchange(&q->count, index + 1, index) == index) break;
    }
    memcpy(q->entries + index * 8, entry, 0x20);
    if (index + 1 >= kMarkFrom) {
        long mark = (index + 1) / kMarkStep * kMarkStep;
        long seen = g_mark;
        if (mark > seen && InterlockedCompareExchange(&g_mark, mark, seen) == seen)
            dsf_log("render: a frame's draw queue reached %ld of %ld entries", mark, q->capacity);
    }
}

// The offline test calls the push through the patched call site.
long mod_render_dropped() { return g_dropped; }

void mod_render_init()
{
    if (!cfg_bool("render", "drop_full", true)) {
        dsf_log("render: draw queue full stays fatal ([render] drop_full=0)");
        return;
    }
    // All or nothing: check every site before changing any.
    if (!check_bytes(kPushCallChecked, kPushCallCheckedBytes, 5) ||
        !check_bytes(kPushCallSkipped, kPushCallSkippedBytes, 5) || !check_bytes(kTrapJump, kTrapJumpBytes, 6)) {
        dsf_log("render: draw queue sites differ from Driver.exe 1.04, nothing patched");
        return;
    }
    patch_call(kPushCallChecked, (void*)&bounded_push, kPushCallCheckedBytes);
    patch_call(kPushCallSkipped, (void*)&bounded_push, kPushCallSkippedBytes);
    patch_bytes(kTrapJump, kNops, 6, kTrapJumpBytes);
    dsf_log("render: a full draw queue drops draws (calls at %08X, %08X; trap at %08X off)", kPushCallChecked,
            kPushCallSkipped, kTrapJump);
}
