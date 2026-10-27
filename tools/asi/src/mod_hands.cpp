// Driver's hands on each car's own steering rim (issue #99).
// See research/cockpit.md ("Hands on Forza's rim") and research/asi.md.
//
// Stock path (Driver.exe 1.04, confirmed-static): the in-car arms object
// (class vtable 0x00DE3D38, 0x3A4 bytes, two of them at [engine+0x2A8]+0x10,
// one per local player) evaluates its animation graph (+0xD4; skeleton at +0xD8,
// joint count at skeleton+0x24) with FUN_008609C0 into the global pose buffer
// 0x01214400: up to 64 joint transforms of 0x20 bytes (quaternion, translation),
// already composed with each joint's inverse bind (skin transforms). FUN_005676B0
// makes that call (0x005676FF); FUN_00566FD0 then counter-turns an arm that leaves
// the wheel (joints 2..23 by +0x350, 24..45 by +0x34C) and draws the model from
// the buffer. The grips are baked into the shared animations, sized for DSF's
// 0.176 m wheels.
//
// This module wraps that call. For a car whose VO3D carries its rim (p95 radius,
// p87 centre along the column from the steering marker; written by the converter,
// stock cars have neither), each hand that holds the wheel is moved onto the rim,
// and its arm follows through a two-bone IK (shoulder fixed, elbow in the arm's
// own bend plane, bone lengths kept). Everything is done on the pose buffer before
// the game reads it, so the cockpit and the chase views get the same hands.
#include "patch.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <intrin.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

struct Xf {   // joint transform: p -> rotate(q, p) + t
    float q[4];
    float t[4];
};

struct V3 {
    float x, y, z;
};

static V3 v3(const float* f) { return {f[0], f[1], f[2]}; }
static V3 add(V3 a, V3 b) { return {a.x + b.x, a.y + b.y, a.z + b.z}; }
static V3 sub(V3 a, V3 b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
static V3 mul(V3 a, float s) { return {a.x * s, a.y * s, a.z * s}; }
static float dot(V3 a, V3 b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
static V3 cross(V3 a, V3 b) { return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x}; }
static float len(V3 a) { return sqrtf(dot(a, a)); }
static V3 unit(V3 a)
{
    float l = len(a);
    return l > 1e-9f ? mul(a, 1.0f / l) : a;
}

// ---- game addresses -------------------------------------------------------
static const uint32_t kEvalCall = 0x005676FF;   // call FUN_008609C0 in FUN_005676B0
static const uint8_t kEvalCallBytes[5] = {0xE8, 0xBC, 0x92, 0x2F, 0x00};
static const uint32_t kEval = 0x008609C0;       // graph evaluate (thiscall graph; pose, 0x40, 0, t)
static const uint32_t kCompose = 0x00BC9E90;    // out = a o b (thiscall a; out, b)
static const uint32_t kPose = 0x01214400;       // 64 x Xf

typedef void(__thiscall* EvalFn)(void* graph, void* pose, int count, int zero, float t);
typedef Xf*(__thiscall* ComposeFn)(const Xf* a, Xf* out, const Xf* b);

// VO3D parameters the converter writes (cockpit.GRIP_PARAMS): unused by the game.
static const int kRadiusParam = 95;
static const int kAlongParam = 87;

// ---- settings --------------------------------------------------------------
static int g_debug = 0;
static float g_near = 0.04f;     // m: a fist this close to the grip circle or the rim is moved fully
static float g_far = 0.09f;      // m: from here on not at all (the hand is off the wheel)
static float g_max_move = 0.10f; // m: largest move of a hand

// The circle DSF's animations grip, in the pose (A-relative, the car's axes): the fist
// sits 0.177 m from a column 26.6 degrees up (Regal GNX probe, research/cockpit.md).
static const V3 kRigAxis = {0.0f, 0.448f, -0.894f};
static const float kRigRadius = 0.177f;

// ---- quaternion convention, checked against the game's compose once ---------
static int g_qmode = -1;   // 0: (x,y,z,w) active, 1: (x,y,z,w) conjugate, 2: unknown (module off)

static V3 rotate(const float* q, V3 v)
{
    Xf a = {{q[0], q[1], q[2], q[3]}, {0, 0, 0, 0}}, b = {{0, 0, 0, 1}, {v.x, v.y, v.z, 0}}, o;
    ((ComposeFn)kCompose)(&a, &o, &b);
    return {o.t[0], o.t[1], o.t[2]};
}

// Rotation by `angle` about unit `axis`, in the game's convention.
static void quat_axis(V3 axis, float angle, float* q)
{
    float s = sinf(angle * 0.5f) * (g_qmode == 1 ? -1.0f : 1.0f);
    q[0] = axis.x * s;
    q[1] = axis.y * s;
    q[2] = axis.z * s;
    q[3] = cosf(angle * 0.5f);
}

// The rotation that turns direction a onto direction b.
static void quat_from_to(V3 a, V3 b, float* q)
{
    a = unit(a);
    b = unit(b);
    V3 c = cross(a, b);
    float s = len(c), d = dot(a, b);
    if (s < 1e-6f) {
        q[0] = q[1] = q[2] = 0;
        q[3] = 1;
        return;
    }
    quat_axis(mul(c, 1.0f / s), atan2f(s, d), q);
}

static void quat_conj(const float* q, float* out)
{
    out[0] = -q[0];
    out[1] = -q[1];
    out[2] = -q[2];
    out[3] = q[3];
}

static bool near3(V3 a, V3 b) { return len(sub(a, b)) < 1e-3f; }

static void check_convention()
{
    const float h = 0.70710678f;
    float q[4] = {0, 0, h, h};   // 90 degrees about +z, if (x,y,z,w) active
    V3 r = rotate(q, {1, 0, 0});
    if (near3(r, {0, 1, 0})) g_qmode = 0;
    else if (near3(r, {0, -1, 0})) g_qmode = 1;
    else g_qmode = 2;
    dsf_log(g_qmode == 2 ? "hands: unknown quaternion convention; hands left alone" : "hands: quaternion convention ok");
    dsf_log("hands: quaternion convention %d (rotate x by 90 deg about z -> %.3f %.3f %.3f)", g_qmode, r.x, r.y, r.z);
}

// ---- skeleton --------------------------------------------------------------
// Skeleton (FUN_00BCA840, FUN_00BCAA80): +0x24 joint count; joint j's record at
// +0x54 + 0x70 j: +0x00 parent index, +0x04 name hash (FUN_00BC9340), +0x2C
// inverse bind (Xf).
static int parent_of(const uint8_t* skel, int j) { return j ? *(const int*)(skel + 0x54 + 0x70 * j) : -1; }
static uint32_t hash_of(const uint8_t* skel, int j) { return *(const uint32_t*)(skel + 0x58 + 0x70 * j); }
static const Xf* inverse_bind(const uint8_t* skel, int j) { return (const Xf*)(skel + 0x80 + 0x70 * j); }

static uint32_t name_hash(const char* s)   // FUN_00BC9340
{
    uint8_t b[4] = {0, 0, 0, 0};
    for (int i = 0; s[i]; i++) b[i & 3] = (uint8_t)(b[i & 3] + (uint8_t)s[i]);
    return b[0] | b[1] << 8 | b[2] << 16 | (uint32_t)b[3] << 24;
}

// One arm by joint name. The in-car arms skeleton (46 joints) hangs from the wheel:
// 0 root, 1 the wheel, 2 RightHand -> fingers and RightForearm (at the wrist) ->
// RightForearmRoll, RightArm (at the elbow) -> RightArmRoll, RightElbow,
// RightShoulder (at the shoulder); 24 LeftHand likewise (research/cockpit.md).
// A skeleton rooted at the hips (Shoulder -> Arm -> Forearm -> Hand) works too:
// each joint belongs to the nearest of the four named joints at or above it.
enum Group { kNone, kHand, kFore, kUpper, kShoulder };

struct Arm {
    int hand, fore, arm, shoulder;
    bool hanging;        // the hand is above the arm in the hierarchy (the in-car arms)
    uint8_t group[64];   // Group per joint
};

struct Rig {
    const uint8_t* skel;
    int n;
    int parent[64];
    V3 bind[64];   // joint positions in the bind pose (model space)
    Arm arm[2];    // [0] right: joints 2..23 (released by +0x350), [1] left: 24..45 (by +0x34C)
    bool ok;
};

static Rig g_rigs[4];

static bool above(const Rig& r, int j, int a)   // a is j or one of its ancestors
{
    for (int k = j, guard = 0; k >= 0 && guard < 64; k = r.parent[k], guard++)
        if (k == a) return true;
    return false;
}

static int joint_named(const Rig& r, const char* side, const char* part, const char* alt = nullptr)
{
    char name[40];
    const char* parts[2] = {part, alt};
    for (const char* p : parts) {
        if (!p) continue;
        sprintf_s(name, "%s%s", side, p);
        uint32_t h = name_hash(name);
        for (int j = 0; j < r.n; j++)
            if (hash_of(r.skel, j) == h) return j;
    }
    return -1;
}

static bool find_arm(Rig& r, const char* side, Arm* a)
{
    a->hand = joint_named(r, side, "Hand");
    a->fore = joint_named(r, side, "Forearm", "ForeArm");
    a->arm = joint_named(r, side, "Arm");
    a->shoulder = joint_named(r, side, "Shoulder");
    if (a->hand < 0 || a->fore < 0 || a->arm < 0 || a->shoulder < 0) return false;
    a->hanging = above(r, a->arm, a->hand);
    // The chain must be one line, either way up.
    bool line = a->hanging ? above(r, a->fore, a->hand) && above(r, a->arm, a->fore) && above(r, a->shoulder, a->arm)
                           : above(r, a->arm, a->shoulder) && above(r, a->fore, a->arm) && above(r, a->hand, a->fore);
    if (!line) return false;
    for (int j = 0; j < r.n; j++) {
        a->group[j] = kNone;
        for (int k = j, guard = 0; k >= 0 && guard < 64; k = r.parent[k], guard++) {
            if (k == a->hand) a->group[j] = kHand;
            else if (k == a->fore) a->group[j] = kFore;
            else if (k == a->arm) a->group[j] = kUpper;
            else if (k == a->shoulder) a->group[j] = kShoulder;
            else continue;
            break;
        }
    }
    return true;
}

static Rig* rig_for(const uint8_t* skel)
{
    for (Rig& r : g_rigs)
        if (r.skel == skel) return r.ok ? &r : nullptr;
    Rig* r = nullptr;
    for (Rig& s : g_rigs)
        if (!s.skel) { r = &s; break; }
    if (!r) r = &g_rigs[0];
    memset(r, 0, sizeof *r);
    r->skel = skel;
    r->n = *(const int*)(skel + 0x24);
    if (r->n < 46 || r->n > 64) {
        dsf_log("hands: skeleton %p has %d joints; hands left alone", skel, r->n);
        return nullptr;
    }
    for (int j = 0; j < r->n; j++) {
        r->parent[j] = parent_of(skel, j);
        if (r->parent[j] < -1 || r->parent[j] >= r->n) r->parent[j] = -1;
        const Xf* ib = inverse_bind(skel, j);
        float c[4];
        quat_conj(ib->q, c);
        r->bind[j] = mul(rotate(c, v3(ib->t)), -1.0f);
    }
    r->ok = find_arm(*r, "Right", &r->arm[0]) && find_arm(*r, "Left", &r->arm[1]) && r->arm[0].hand >= 2 &&
            r->arm[0].hand <= 23 && r->arm[1].hand >= 24 && r->arm[1].hand <= 45;
    dsf_log("hands: skeleton %p, %d joints: right hand/forearm/arm/shoulder %d/%d/%d/%d, left %d/%d/%d/%d%s%s", skel, r->n,
            r->arm[0].hand, r->arm[0].fore, r->arm[0].arm, r->arm[0].shoulder, r->arm[1].hand, r->arm[1].fore,
            r->arm[1].arm, r->arm[1].shoulder, r->arm[0].hanging ? " (hanging from the hands)" : "",
            r->ok ? "" : "; not the arms rig, hands left alone");
    return r->ok ? r : nullptr;
}

// ---- the car's rim -----------------------------------------------------------
static uint8_t* vehicle_spec(uint8_t* vehicle)
{
    if (!vehicle || *(int*)(vehicle + 0x544) != 3) return nullptr;   // FUN_008CE300
    uint8_t* a = *(uint8_t**)(vehicle + 0x53C);
    if (!a) return nullptr;
    uint8_t* b = *(uint8_t**)(a + 8);
    return b ? *(uint8_t**)(b + 0xAA4) : nullptr;
}

static float vo3d(const uint8_t* spec, int id) { return *(const float*)(spec + 0x2C70 + 4 * id); }

struct Rim {
    V3 centre, axis;
    float radius;
};

static bool car_rim(uint8_t* arms, Rim* rim)
{
    uint8_t* spec = vehicle_spec(*(uint8_t**)(arms + 0x348));
    if (!spec) return false;
    float r = vo3d(spec, kRadiusParam);
    if (!(r > 0.05f && r < 0.4f)) return false;   // stock cars: 0
    float along = vo3d(spec, kAlongParam);
    if (!(fabsf(along) < 0.5f)) return false;
    // In the pose: the origin is A = marker + (0, p538, p559) (FUN_005743E0), the axes
    // are the car's. The rim's centre is the marker + along x the column direction.
    V3 axis = unit(v3((const float*)(spec + 0x37F0)));
    V3 from_a = {0.0f, -vo3d(spec, 538), -vo3d(spec, 559)};
    rim->centre = add(from_a, mul(axis, along));
    rim->axis = axis;
    rim->radius = r;
    return true;
}

// ---- the hook -----------------------------------------------------------------
static DWORD g_last_log = 0;
static int g_samples = 0;

static V3 joint_pos(const Rig& rig, const Xf* pose, int j) { return add(rotate(pose[j].q, rig.bind[j]), v3(pose[j].t)); }

// pose[j] = T o pose[j] for T(x) = q(x) + t
static void apply(Xf* pose, int j, const float* q, V3 t)
{
    Xf T = {{q[0], q[1], q[2], q[3]}, {t.x, t.y, t.z, 0}}, o;
    ((ComposeFn)kCompose)(&T, &o, &pose[j]);
    pose[j] = o;
}

// The translation of T(x) = q(x - p) + p2: turn by q about p, then p goes to p2.
static V3 about(const float* q, V3 p, V3 p2) { return sub(p2, rotate(q, p)); }

static void fix_arm(const Rig& rig, Xf* pose, const Arm& a, const Rim& rim, float hold, int side, bool log)
{
    V3 W = joint_pos(rig, pose, a.hand);
    // Hanging arms: Forearm sits at the wrist, Arm at the elbow, Shoulder at the shoulder.
    // Hip-rooted: Arm at the shoulder, Forearm at the elbow.
    V3 E = joint_pos(rig, pose, a.hanging ? a.arm : a.fore);
    V3 S = joint_pos(rig, pose, a.hanging ? a.shoulder : a.arm);
    // The grip: the middle of the fist (the wrist and the finger joints). On DSF's own
    // wheels it sits on the rim: 0.177 m from the column, in the rim's plane (Regal GNX
    // probe, at rest and at full lock).
    V3 g = {0, 0, 0};
    int count = 0;
    for (int j = 0; j < rig.n; j++)
        if (a.group[j] == kHand) g = add(g, joint_pos(rig, pose, j)), count++;
    if (!count) return;
    g = mul(g, 1.0f / count);
    V3 v = sub(g, rim.centre);
    V3 plane = sub(v, mul(rim.axis, dot(v, rim.axis)));
    if (len(plane) < 0.02f) return;
    V3 target = add(rim.centre, mul(unit(plane), rim.radius));
    V3 delta = sub(target, g);
    float miss = len(delta);
    // A hand off the wheel (over the top while turning hand over hand, at the gear stick)
    // is left alone, and fades in as it comes back. On the wheel the fist is near the
    // circle the animations grip (kRigRadius about A, the pose origin) or near the rim.
    V3 rv = g;   // A is the pose origin
    V3 rp = sub(rv, mul(kRigAxis, dot(rv, kRigAxis)));
    float off_rig = len(sub(rv, mul(unit(rp), kRigRadius)));
    float off = off_rig < miss ? off_rig : miss;
    float w = hold * (off <= g_near ? 1.0f : off >= g_far ? 0.0f : (g_far - off) / (g_far - g_near));
    if (miss > g_max_move) delta = mul(delta, g_max_move / miss);
    delta = mul(delta, w);
    if (log)
        dsf_log("hands: %s grip %.1f cm from the column, %.1f cm off the rim (r %.1f cm); hold %.2f weight %.2f, moved %.1f cm",
                side ? "left" : "right", len(plane) * 100, miss * 100, rim.radius * 100, hold, w, len(delta) * 100);
    if (len(delta) < 1e-4f) return;
    // Two-bone IK: the shoulder stays, the wrist goes to W + delta, the elbow stays in
    // the plane of shoulder, elbow and wrist, and both bones keep their length.
    float l1 = len(sub(E, S)), l2 = len(sub(W, E));
    V3 sw = sub(add(W, delta), S);
    float d = len(sw);
    float dmin = fabsf(l1 - l2) + 1e-3f, dmax = l1 + l2 - 1e-3f;
    if (d < dmin) d = dmin;
    if (d > dmax) d = dmax;
    V3 u = unit(sw);
    V3 ws = unit(sub(W, S));
    V3 bend = sub(E, S);
    V3 pole = sub(bend, mul(ws, dot(bend, ws)));
    pole = sub(pole, mul(u, dot(pole, u)));
    if (len(pole) < 1e-4f) return;   // a straight arm: no bend plane
    pole = unit(pole);
    float x = (l1 * l1 - l2 * l2 + d * d) / (2 * d);
    float y = sqrtf(l1 * l1 - x * x > 0 ? l1 * l1 - x * x : 0);
    V3 E2 = add(S, add(mul(u, x), mul(pole, y)));
    V3 W2 = add(S, mul(u, d));   // W + delta, unless out of reach
    float q1[4], q2[4], none[4] = {0, 0, 0, 1};
    quat_from_to(sub(E, S), sub(E2, S), q1);    // upper arm: about the shoulder
    quat_from_to(sub(W, E), sub(W2, E2), q2);   // forearm: the old segment onto the new
    V3 t1 = about(q1, S, S);
    V3 t2 = a.hanging ? about(q2, W, W2) : about(q2, E, E2);   // the forearm joint's own end
    V3 t3 = sub(W2, W);
    for (int j = 0; j < rig.n; j++) {
        switch (a.group[j]) {
        case kHand: apply(pose, j, none, t3); break;   // the grip keeps its orientation
        case kFore: apply(pose, j, q2, t2); break;
        case kUpper: apply(pose, j, q1, t1); break;
        default: break;   // the shoulder and the rest of the body stay
        }
    }
}

static void probe(uint8_t* arms, uint32_t caller);

static void __fastcall eval_hook(void* graph, void*, void* pose, int count, int zero, float t)
{
    ((EvalFn)kEval)(graph, pose, count, zero, t);
    if (pose != (void*)kPose) return;
    uint8_t* arms = (uint8_t*)graph - 0xD4;
    if (!*(void**)(arms + 0x348)) return;
    if (g_debug) probe(arms, *(uint32_t*)((uint8_t*)_AddressOfReturnAddress() + 0xAC));
    Rim rim;
    if (g_qmode < 0) check_convention();   // here, not in DllMain: it calls the game's math
    if (g_qmode != 0 && g_qmode != 1) return;
    if (!car_rim(arms, &rim)) return;
    Rig* rig = rig_for(*(const uint8_t**)(arms + 0xD8));
    if (!rig) return;
    DWORD now = GetTickCount();
    bool log = g_debug && now - g_last_log >= 1500;
    if (log) g_last_log = now;
    Xf* xf = (Xf*)kPose;
    // +0x350 releases joints 2..23 from the wheel, +0x34C joints 24..45 (FUN_00566FD0).
    fix_arm(*rig, xf, rig->arm[0], rim, 1.0f - *(float*)(arms + 0x350), 0, log);
    fix_arm(*rig, xf, rig->arm[1], rim, 1.0f - *(float*)(arms + 0x34C), 1, log);
}

// ---- debug probe ---------------------------------------------------------------
static void log_floats(const char* tag, const float* f, int n)
{
    char line[2048];
    int k = _snprintf_s(line, sizeof line, _TRUNCATE, "hands: %s", tag);
    for (int i = 0; i < n && k < (int)sizeof line - 16; i++) k += _snprintf_s(line + k, sizeof line - k, _TRUNCATE, " %.5g", f[i]);
    dsf_log("%s", line);
}

static void* g_logged_skel[4];

static void probe(uint8_t* arms, uint32_t caller)
{
    uint8_t* skel = *(uint8_t**)(arms + 0xD8);
    if (!skel) return;
    int n = *(int*)(skel + 0x24);
    if (n <= 0 || n > 64) return;
    bool known = false;
    for (void* s : g_logged_skel) known |= s == skel;
    if (!known) {
        for (void*& s : g_logged_skel)
            if (!s) { s = skel; break; }
        dsf_log("hands: skeleton %p, %d joints (arms %p)", skel, n, arms);
        for (int j = 0; j < n; j++) {
            const uint8_t* rec = skel + 0x54 + 0x70 * j;
            char hex[0x70 * 3 + 1];
            for (int b = 0; b < 0x70; b++) sprintf_s(hex + 3 * b, 4, "%02X ", rec[b]);
            dsf_log("hands: joint %d parent %d rec %s", j, j ? *(int*)rec : -1, hex);
        }
    }
    static DWORD last = 0;
    DWORD now = GetTickCount();
    if (now - last < 1500 || g_samples >= 400) return;
    last = now;
    g_samples++;
    uint8_t* vehicle = *(uint8_t**)(arms + 0x348);
    uint8_t* spec = vehicle_spec(vehicle);
    int spool = -1;
    if (spec && *(uint8_t**)(spec + 0x4850)) spool = *(int*)(*(uint8_t**)(spec + 0x4850) + 0x14);
    dsf_log("hands: sample %d t=%lu arms %p caller %08X vehicle %p spec %p spool %d release %.3f %.3f", g_samples, now, arms,
            caller, vehicle, spec, spool, *(float*)(arms + 0x34C), *(float*)(arms + 0x350));
    log_floats("m4", (float*)(arms + 4), 16);
    log_floats("m84", (float*)(arms + 0x84), 16);
    log_floats("m360", (float*)(arms + 0x360), 16);
    if (spec) {
        float v[12];
        memcpy(v, spec + 0x37E0, 16);
        memcpy(v + 4, spec + 0x37F0, 16);
        v[8] = vo3d(spec, 538);
        v[9] = vo3d(spec, 559);
        v[10] = vo3d(spec, kRadiusParam);
        v[11] = vo3d(spec, kAlongParam);
        log_floats("marker", v, 12);
    }
    const Xf* pose = (const Xf*)kPose;
    char line[4096];
    for (int j0 = 0; j0 < n; j0 += 8) {
        int k = _snprintf_s(line, sizeof line, _TRUNCATE, "hands: pose %d", j0);
        for (int j = j0; j < n && j < j0 + 8; j++)
            for (int c = 0; c < 8; c++)
                k += _snprintf_s(line + k, sizeof line - k, _TRUNCATE, " %.5g", c < 4 ? pose[j].q[c] : pose[j].t[c - 4]);
        dsf_log("%s", line);
    }
}


void mod_hands_init()
{
    g_debug = cfg_int("hands", "debug", 0);
    g_near = cfg_int("hands", "near_mm", 40) / 1000.0f;
    g_far = cfg_int("hands", "far_mm", 90) / 1000.0f;
    g_max_move = cfg_int("hands", "max_move_mm", 100) / 1000.0f;
    if (g_far <= g_near) g_far = g_near + 0.01f;
    if (!bytes_equal(kCompose, "\x83\xEC\x30\x56\x57", 5)) {
        dsf_log("hands: FUN_00BC9E90 differs; hands left alone");
        return;
    }
    if (!patch_call(kEvalCall, (void*)eval_hook, kEvalCallBytes)) {
        dsf_log("hands: the arms' pose call at %08X differs; hands left alone", kEvalCall);
        return;
    }
    dsf_log("hands: pose hook at %08X (debug %d)", kEvalCall, g_debug);
}

// Offline check (tools/asi/test/offline.cpp, Driver.exe's image mapped, no game
// state): the quaternion convention against FUN_00BC9E90, and one hanging arm
// whose fist sits 0.177 m from a column moved onto rims 13% larger and 15% smaller.
// Returns the number of failed checks.
int mod_hands_selftest()
{
    int fails = 0;
    g_qmode = -1;
    check_convention();
    fails += g_qmode != 0;
    const float radii[2] = {0.20f, 0.15f};
    float near_was = g_near, far_was = g_far;
    g_near = 1.0f, g_far = 2.0f;   // the IK alone: every hand on the wheel
    for (float r : radii) {
        static uint8_t skel[0x80 + 0x70 * 64];
        memset(skel, 0, sizeof skel);
        Rig rig = {};
        rig.skel = skel;
        rig.n = 8;
        // 0 root, 1 hand (wrist), 2..3 fingers, 4 forearm (at the wrist), 5 arm (elbow), 6 shoulder, 7 roll
        const int parent[8] = {-1, 0, 1, 1, 1, 4, 5, 5};
        const V3 at[8] = {{0, 0, 0},       {0.15f, 0.08f, -0.02f}, {0.19f, 0.10f, 0.03f}, {0.18f, 0.11f, 0.01f},
                          {0.15f, 0.08f, -0.02f}, {0.22f, -0.02f, -0.25f}, {0.18f, 0.06f, -0.54f}, {0.20f, 0.02f, -0.40f}};
        for (int j = 0; j < 8; j++) rig.parent[j] = parent[j], rig.bind[j] = at[j];
        Arm& a = rig.arm[0];
        a = {1, 4, 5, 6, true, {}};
        const uint8_t group[8] = {kNone, kHand, kHand, kHand, kFore, kUpper, kShoulder, kUpper};
        memcpy(a.group, group, sizeof group);
        Xf pose[8];
        for (Xf& x : pose) x = {{0, 0, 0, 1}, {0, 0, 0, 0}};
        V3 g0 = mul(add(add(at[1], at[2]), at[3]), 1.0f / 3);
        Rim rim = {{0, 0, 0}, unit({0, -0.417f, 0.909f}), 0};
        V3 v = sub(g0, rim.centre);
        V3 plane = sub(v, mul(rim.axis, dot(v, rim.axis)));
        rim.centre = mul(rim.axis, dot(v, rim.axis));   // the rim in the fist's plane
        rim.radius = len(plane) * r / 0.177f;           // ... scaled as r is to DSF's 0.177 m
        fix_arm(rig, pose, a, rim, 1.0f, 0, false);
        V3 g = {0, 0, 0};
        for (int j = 1; j <= 3; j++) g = add(g, joint_pos(rig, pose, j));
        g = mul(g, 1.0f / 3);
        V3 gv = sub(g, rim.centre);
        float radial = len(sub(gv, mul(rim.axis, dot(gv, rim.axis)))), off = dot(gv, rim.axis);
        V3 S = joint_pos(rig, pose, 6), E = joint_pos(rig, pose, 5), W = joint_pos(rig, pose, 1);
        V3 R = joint_pos(rig, pose, 7);
        bool ok = fabsf(radial - rim.radius) < 1e-3f && fabsf(off) < 1e-3f && len(sub(S, at[6])) < 1e-4f &&
                  fabsf(len(sub(E, S)) - len(sub(at[5], at[6]))) < 1e-3f &&
                  fabsf(len(sub(W, E)) - len(sub(at[1], at[5]))) < 1e-3f &&
                  fabsf(len(sub(R, E)) - len(sub(at[7], at[5]))) < 1e-3f &&
                  len(sub(joint_pos(rig, pose, 4), W)) < 1e-3f;
        if (!ok)
            printf("hands selftest r %.2f: fist %.4f (rim %.4f, off %.4f), shoulder moved %.4f\n", r, radial, rim.radius, off,
                   len(sub(S, at[6])));
        fails += !ok;
    }
    g_near = near_was, g_far = far_was;
    // A fist far from the animations' circle and from the rim (a hand off the wheel) stays.
    {
        Rig rig = {};
        rig.n = 7;
        const int parent[7] = {-1, 0, 1, 1, 3, 4, 4};
        const V3 at[7] = {{0, 0, 0}, {0.05f, 0.30f, 0.0f}, {0.06f, 0.32f, 0.02f}, {0.05f, 0.30f, 0.0f},
                          {0.20f, 0.10f, -0.25f}, {0.18f, 0.06f, -0.54f}, {0.20f, 0.08f, -0.40f}};
        for (int j = 0; j < 7; j++) rig.parent[j] = parent[j], rig.bind[j] = at[j];
        Arm& a = rig.arm[0];
        a = {1, 3, 4, 5, true, {}};
        const uint8_t group[7] = {kNone, kHand, kHand, kFore, kUpper, kShoulder, kUpper};
        memcpy(a.group, group, sizeof group);
        Xf pose[7];
        for (Xf& x : pose) x = {{0, 0, 0, 1}, {0, 0, 0, 0}};
        Rim rim = {{0, 0, 0}, unit({0, -0.417f, 0.909f}), 0.20f};
        fix_arm(rig, pose, a, rim, 1.0f, 0, false);
        bool ok = len(sub(joint_pos(rig, pose, 1), at[1])) < 1e-5f;
        if (!ok) printf("hands selftest: a hand off the wheel moved\n");
        fails += !ok;
    }
    return fails;
}
