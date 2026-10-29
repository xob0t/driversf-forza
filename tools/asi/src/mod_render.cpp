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
// Its callers into the frame's queue:
//   FUN_00BB5B70 skips the draw when count == capacity;
//   FUN_00BB5A50 (ShiftDrawMetadata_Enqueue 0x006BA880, FUN_00BB42E0) traps:
//     00BB5A92  3B 48 04           CMP ECX,[EAX+4]
//     00BB5A95  0F 84 BF 00 00 00  JZ 0x00BB5B5A   ; INT3
//   ShiftRenderCache_FinalizeEntry (0x00BB6DA0) replays a cached draw list into
//     the active queue ([0x019B6434]+4) at 0x00BB7301 with no check at all.
// Once the replay has pushed the count past 16384, the equality checks never
// fire again and every append writes past the array: entry 16384 lands on the
// other queue's header (it follows the entries), so a later frame finds a queue
// whose count equals its capacity (the trap: breakpoint exception 0x80000003 at
// offset 0x007B5B5A) or whose entries pointer is 0 (access violation at
// 0x00BB5737). The equality checks also race: two threads that both see 16383
// push 16384 and 16385. (The cache's own queues, the call at 0x00BB6FC3, take a
// size class at least as big as the list, 0x00FD2824.)
//
// A vanilla frame stays well under 16384. dsf_enhanced's worldlod world archive
// and full-detail traffic out to the cull distance brought the user's frames past
// it (2026-10-28: both crashes, the first on a car spawn out of a garage).
//
// This module:
//   1. replaces the three calls to FUN_00BB5710 into the frame's queue
//      (0x00BB5B4C, 0x00BB5BB5, 0x00BB7301) with a push that takes a slot only
//      while count < capacity (same compare-exchange), and drops the draw
//      otherwise;
//   2. turns FUN_00BB5A50's trap branch into NOPs, so a full queue reaches that
//      push and the draw is dropped, as FUN_00BB5B70 does.
// A frame over the limit then misses its last draws instead of ending the game.
// Drops and each new 1024 entries of the queue's high-water mark from 8192 up are
// logged.
#include "patch.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <string.h>

struct PushCall {
    uint32_t va;
    uint8_t bytes[5];  // call 0x00BB5710
};
static const PushCall kPushCalls[] = {
    {0x00BB5B4C, {0xE8, 0xBF, 0xFB, 0xFF, 0xFF}},  // FUN_00BB5A50
    {0x00BB5BB5, {0xE8, 0x56, 0xFB, 0xFF, 0xFF}},  // FUN_00BB5B70
    {0x00BB7301, {0xE8, 0x0A, 0xE4, 0xFF, 0xFF}},  // ShiftRenderCache_FinalizeEntry's replay
};
static const int kPushCallCount = sizeof kPushCalls / sizeof kPushCalls[0];
static const uint32_t kTrapJump = 0x00BB5A95;  // jz 0x00BB5B5A (int3)
static const uint8_t kTrapJumpBytes[6] = {0x0F, 0x84, 0xBF, 0x00, 0x00, 0x00};
static const uint8_t kNops[6] = {0x90, 0x90, 0x90, 0x90, 0x90, 0x90};

static const long kMarkFrom = 8192;  // high-water marks logged from here on
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
    // DSF Enhanced bounds FUN_00BB5710 itself and enlarges the queues (its [Limits]
    // DrawQueueEntries, dsf-enhanced PR #12). Its DllMain creates this event once that
    // guard is in; when it loaded first, it alone handles the queue. When this plugin
    // loads first, DSF Enhanced finds these call sites patched and still enlarges the
    // queues: bounded_push reads the capacity from the queue header, so it follows.
    char marker[64];
    wsprintfA(marker, "Local\\DSFEnhanced.DrawQueue.%lu", GetCurrentProcessId());
    if (HANDLE h = OpenEventA(SYNCHRONIZE, FALSE, marker)) {
        CloseHandle(h);
        dsf_log("render: DSF Enhanced's draw queue guard is active (%s); drop_full stands down", marker);
        return;
    }
    // All or nothing: check every site before changing any.
    bool same = check_bytes(kTrapJump, kTrapJumpBytes, 6);
    for (int i = 0; i < kPushCallCount; i++) same = same && check_bytes(kPushCalls[i].va, kPushCalls[i].bytes, 5);
    if (!same) {
        dsf_log("render: draw queue sites differ from Driver.exe 1.04, nothing patched");
        return;
    }
    // The trap goes last: with it still in place a full queue stays fatal, as in vanilla.
    int done = 0;
    while (done < kPushCallCount &&
           patch_call(kPushCalls[done].va, (void*)&bounded_push, kPushCalls[done].bytes))
        done++;
    if (done < kPushCallCount || !patch_bytes(kTrapJump, kNops, 6, kTrapJumpBytes)) {
        for (int i = 0; i < done; i++) patch_bytes(kPushCalls[i].va, kPushCalls[i].bytes, 5);
        dsf_log("render: a write failed, draw queue left vanilla");
        return;
    }
    dsf_log("render: a full draw queue drops draws (calls at %08X, %08X, %08X; trap at %08X off)",
            kPushCalls[0].va, kPushCalls[1].va, kPushCalls[2].va, kTrapJump);
}
