// Driver's hands on each car's own steering rim (issues #99, #117).
// See research/cockpit.md ("Hands on Forza's rim", "Hands in the chase views") and
// research/asi.md.
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
// The driver in the chase and side views (and every AI driver) is another object: a
// full-body vehicle occupant (FUN_00567E60, pool of 0x90; vehicle +0x360, seat +0x364,
// its seat marker's matrix in the car at +0x00). FUN_00568590 queues him for a batched
// draw (FUN_00573450 -> FUN_0055ACE0: a slot of 0x280 bytes, the occupant at slot+0x144),
// and an engine task (FUN_005668B0 -> FUN_005650A0) blends his clips and builds the skin
// transforms on its own stack with FUN_00BCA840 (0x005652FB, again at 0x00565910 after
// the head look-at), then uploads them as shader constants. His pose frame is the seat's.
//
// This module wraps both. For a car whose VO3D carries its rim (p95 radius, p87 centre
// along the column from the steering marker; written by the converter, stock cars have
// neither), each hand that holds the wheel is moved onto the rim, and its arm follows
// through a two-bone IK (shoulder fixed, elbow in the arm's own bend plane, bone lengths
// kept). Everything is done on the skin transforms before the game reads them.
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

// The vehicle occupant (chase views, AI drivers): the draw task and its two skin builds.
static const uint32_t kBodyTaskCall = 0x005668D3;   // call FUN_005650A0 in the task FUN_005668B0
static const uint8_t kBodyTaskCallBytes[5] = {0xE8, 0xC8, 0xE7, 0xFF, 0xFF};
static const uint32_t kBodyTask = 0x005650A0;       // (ecx; slot, ...) 7 stack args, ret 0x1C
static const uint32_t kBodySkinCalls[2] = {0x005652FB, 0x00565910};   // call FUN_00BCA840
static const uint8_t kBodySkinCallBytes[2][5] = {{0xE8, 0x40, 0x55, 0x66, 0x00}, {0xE8, 0x2B, 0x4F, 0x66, 0x00}};
static const uint32_t kSkinBuild = 0x00BCA840;      // (thiscall skeleton; local, model out, skin out)

typedef void(__thiscall* EvalFn)(void* graph, void* pose, int count, int zero, float t);
typedef Xf*(__thiscall* ComposeFn)(const Xf* a, Xf* out, const Xf* b);
typedef void(__thiscall* BodyTaskFn)(void* self, void* slot, void* a2, void* a3, void* a4, void* a5, void* a6, void* a7);
typedef void(__thiscall* SkinBuildFn)(void* skel, Xf* local, Xf* model, Xf* skin);

// VO3D parameters the converter writes (cockpit.GRIP_PARAMS): unused by the game.
static const int kRadiusParam = 95;
static const int kAlongParam = 87;

// ---- settings --------------------------------------------------------------
static int g_debug = 0;
static float g_near = 0.04f;     // m: a fist this close to the grip circle or the rim is moved fully
static float g_far = 0.09f;      // m: from here on not at all (the hand is off the wheel)
static float g_max_move = 0.12f; // m: largest move of a hand

static int g_body = 1;           // [hands] chase=0 leaves the occupants alone

// The circle DSF's animations grip. In the arms' pose (A-relative, the car's axes) the fist
// sits 0.177 m from a column 26.6 degrees up (Regal GNX probe, research/cockpit.md).
struct Circle {
    V3 centre, axis;
    float radius;
    float out;    // how far outside the rim's centre line the fist sits on a stock wheel
    float band;   // the radius varies by this either way (the occupants' animation sets)
};
static const Circle kArmsCircle = {{0.0f, 0.0f, 0.0f}, {0.0f, 0.448f, -0.894f}, 0.177f, 0.0f, 0.0f};
// In the occupant's pose (the seat marker's frame) his circle sits about the stock steering
// marker's place, (0, 0.469, 0.428) in 138 of 147 stock cars (research/cockpit.md, "The
// driver's seat follows the wheel"). His fist (the wrist and the finger joints of the
// full-body skeleton) runs 0.193 m from it, 1.7 cm outside a stock 0.176 m rim, in the car
// and sports sets; the truck set (tour bus) grips at a quarter to three, 0.206 m out (debug=1,
// runs vm1-20261027-164654, -165637; "Hands in the chase views").
static const Circle kBodyCircle = {{0.0f, 0.469f, 0.428f}, {0.0f, 0.448f, -0.894f}, 0.193f, 0.017f, 0.015f};

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
    Arm arm[2];    // [0] right, [1] left (arms: joints 2..23 released by +0x350, 24..45 by +0x34C)
    bool body;     // the occupant's full-body skeleton, not the in-car arms
    bool ok;
};

// The occupants are posed on the engine's task threads: rig_for runs under this lock,
// and a Rig doesn't change once it is filled in.
static Rig g_rigs[8];
static CRITICAL_SECTION g_lock;
static bool g_lock_ok = false;

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
    if (a->hand < 0 || a->fore < 0 || a->arm < 0) return false;
    a->hanging = above(r, a->arm, a->hand);
    // The chain must be one line, either way up. The hanging arms need the shoulder (the IK
    // turns about it); a hip-rooted arm turns about Arm, and the occupants' 16-joint
    // skeleton has no Shoulder joint.
    if (a->shoulder < 0 && a->hanging) return false;
    bool line = a->hanging ? above(r, a->fore, a->hand) && above(r, a->arm, a->fore) && above(r, a->shoulder, a->arm)
                           : (a->shoulder < 0 || above(r, a->arm, a->shoulder)) && above(r, a->fore, a->arm) &&
                                 above(r, a->hand, a->fore);
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

static Rig* rig_for_locked(const uint8_t* skel, bool body)
{
    for (Rig& r : g_rigs)
        if (r.skel == skel && r.body == body) return r.ok ? &r : nullptr;
    Rig* r = nullptr;
    for (Rig& s : g_rigs)
        if (!s.skel) { r = &s; break; }
    if (!r) return nullptr;   // more skeletons than slots: leave the rest alone
    memset(r, 0, sizeof *r);
    r->skel = skel;   // find_arm reads the joint names through it
    r->n = *(const int*)(skel + 0x24);
    r->body = body;
    if (r->n < (body ? 8 : 46) || r->n > 64) {
        dsf_log("hands: %s skeleton %p has %d joints; hands left alone", body ? "occupant" : "arms", skel, r->n);
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
    bool arms = find_arm(*r, "Right", &r->arm[0]) && find_arm(*r, "Left", &r->arm[1]);
    // The in-car arms hang from the hands (joints 2..23 right, 24..45 left); the occupant's
    // body is rooted at the hips.
    r->ok = arms && (body ? !r->arm[0].hanging && !r->arm[1].hanging
                          : r->arm[0].hand >= 2 && r->arm[0].hand <= 23 && r->arm[1].hand >= 24 && r->arm[1].hand <= 45);
    dsf_log("hands: %s skeleton %p, %d joints: right hand/forearm/arm/shoulder %d/%d/%d/%d, left %d/%d/%d/%d%s%s",
            body ? "occupant" : "arms", skel, r->n, r->arm[0].hand, r->arm[0].fore, r->arm[0].arm, r->arm[0].shoulder,
            r->arm[1].hand, r->arm[1].fore, r->arm[1].arm, r->arm[1].shoulder,
            r->arm[0].hanging ? " (hanging from the hands)" : "", r->ok ? "" : "; not the expected rig, hands left alone");
    return r->ok ? r : nullptr;
}

static Rig* rig_for(const uint8_t* skel, bool body)
{
    if (!g_lock_ok) return nullptr;
    EnterCriticalSection(&g_lock);
    Rig* r = rig_for_locked(skel, body);
    LeaveCriticalSection(&g_lock);
    return r;
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

// The rim in the car's model space: the steering marker (spec + 0x37E0) + p87 along the
// column direction (spec + 0x37F0), radius p95. False for a car without it (stock).
static bool car_rim(uint8_t* vehicle, const uint8_t** spec_out, Rim* rim)
{
    const uint8_t* spec = vehicle_spec(vehicle);
    if (!spec) return false;
    float r = vo3d(spec, kRadiusParam);
    if (!(r > 0.05f && r < 0.4f)) return false;   // stock cars: 0
    float along = vo3d(spec, kAlongParam);
    if (!(fabsf(along) < 0.5f)) return false;
    V3 axis = unit(v3((const float*)(spec + 0x37F0)));
    rim->centre = add(v3((const float*)(spec + 0x37E0)), mul(axis, along));
    rim->axis = axis;
    rim->radius = r;
    if (spec_out) *spec_out = spec;
    return true;
}

// The arms' pose: the origin is A = marker + (0, p538, p559) (FUN_005743E0), the axes are
// the car's.
static bool arms_rim(uint8_t* arms, Rim* rim)
{
    const uint8_t* spec;
    if (!car_rim(*(uint8_t**)(arms + 0x348), &spec, rim)) return false;
    V3 a = add(v3((const float*)(spec + 0x37E0)), {0.0f, vo3d(spec, 538), vo3d(spec, 559)});
    rim->centre = sub(rim->centre, a);
    return true;
}

// p -> M p for a row-major 4x4 (rows 0..2 the axes, row 3 the translation), and back.
static V3 mat_point(const float* m, V3 p)
{
    return {p.x * m[0] + p.y * m[4] + p.z * m[8] + m[12], p.x * m[1] + p.y * m[5] + p.z * m[9] + m[13],
            p.x * m[2] + p.y * m[6] + p.z * m[10] + m[14]};
}

static bool mat_inverse_point(const float* m, V3 p, bool direction, V3* out)
{
    V3 r0 = {m[0], m[1], m[2]}, r1 = {m[4], m[5], m[6]}, r2 = {m[8], m[9], m[10]};
    float det = dot(r0, cross(r1, r2));
    if (!(fabsf(det) > 1e-6f)) return false;
    if (!direction) p = sub(p, {m[12], m[13], m[14]});
    // p = x r0 + y r1 + z r2 (Cramer)
    *out = {dot(p, cross(r1, r2)) / det, dot(r0, cross(p, r2)) / det, dot(r0, cross(r1, p)) / det};
    return true;
}

// The occupant's pose: his seat marker's frame (+0x00, FUN_00567E60 via FUN_00561BF0).
static bool body_rim(uint8_t* body, Rim* rim)
{
    Rim car;
    if (!car_rim(*(uint8_t**)(body + 0x360), nullptr, &car)) return false;
    const float* seat = (const float*)body;
    V3 c, a;
    if (!mat_inverse_point(seat, car.centre, false, &c) || !mat_inverse_point(seat, car.axis, true, &a)) return false;
    rim->centre = c;
    rim->axis = unit(a);
    rim->radius = car.radius;
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

// Moves one hand onto the rim (both in the pose's frame). `anim` is the circle the
// animations grip; `model`, when given, gets the same moves (the occupant's model-space
// transforms, which the skin transforms are built from).
static void fix_arm(const Rig& rig, Xf* pose, Xf* model, const Arm& a, const Rim& rim, const Circle& anim, float hold,
                    int side, bool log)
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
    // The hand keeps its angle about the circle the animations turn it on, now about the
    // rim's hub: so it turns with the wheel even where that circle's centre is off the
    // wheel's column (the occupant's seat, cockpit.seat_anchor). For the arms the circle's
    // centre, A, is on the column, and this is the nearest point of the rim.
    V3 v = sub(g, anim.centre);
    V3 plane = sub(v, mul(rim.axis, dot(v, rim.axis)));
    if (len(plane) < 0.02f) return;
    V3 target = add(rim.centre, mul(unit(plane), rim.radius + anim.out));
    V3 delta = sub(target, g);
    float miss = len(delta);
    V3 vr = sub(g, rim.centre);
    float from_column = len(sub(vr, mul(rim.axis, dot(vr, rim.axis))));
    // A hand off the wheel (over the top while turning hand over hand, at the gear stick)
    // is left alone, and fades in as it comes back. On the wheel the fist is near the
    // circle the animations grip or near the rim.
    V3 rv = sub(g, anim.centre);
    V3 rp = sub(rv, mul(anim.axis, dot(rv, anim.axis)));
    float radial = fabsf(len(rp) - anim.radius) - anim.band, along = dot(rv, anim.axis);
    if (radial < 0) radial = 0;
    float off_rig = sqrtf(radial * radial + along * along);
    float off = off_rig < miss ? off_rig : miss;
    float w = hold * (off <= g_near ? 1.0f : off >= g_far ? 0.0f : (g_far - off) / (g_far - g_near));
    if (miss > g_max_move) delta = mul(delta, g_max_move / miss);
    delta = mul(delta, w);
    if (log)
        dsf_log("hands: %s %s grip (%.3f %.3f %.3f) %.1f cm from the column, %.1f cm off the rim (r %.1f cm), %.1f cm off "
                "the animations' circle; hold %.2f weight %.2f, moved %.1f cm",
                rig.body ? "occupant" : "arms", side ? "left" : "right", g.x, g.y, g.z, from_column * 100, miss * 100,
                rim.radius * 100, off_rig * 100, hold, w, len(delta) * 100);
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
    Xf* bufs[2] = {pose, model};
    for (Xf* b : bufs) {
        if (!b) continue;
        for (int j = 0; j < rig.n; j++) {
            switch (a.group[j]) {
            case kHand: apply(b, j, none, t3); break;   // the grip keeps its orientation
            case kFore: apply(b, j, q2, t2); break;
            case kUpper: apply(b, j, q1, t1); break;
            default: break;   // the shoulder and the rest of the body stay
            }
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
    if (!arms_rim(arms, &rim)) return;
    Rig* rig = rig_for(*(const uint8_t**)(arms + 0xD8), false);
    if (!rig) return;
    DWORD now = GetTickCount();
    bool log = g_debug && now - g_last_log >= 1500;
    if (log) g_last_log = now;
    Xf* xf = (Xf*)kPose;
    // +0x350 releases joints 2..23 from the wheel, +0x34C joints 24..45 (FUN_00566FD0).
    fix_arm(*rig, xf, nullptr, rig->arm[0], rim, kArmsCircle, 1.0f - *(float*)(arms + 0x350), 0, log);
    fix_arm(*rig, xf, nullptr, rig->arm[1], rim, kArmsCircle, 1.0f - *(float*)(arms + 0x34C), 1, log);
}

// ---- the vehicle occupant (chase views, AI drivers) ------------------------------
// The engine task that poses one queued occupant (FUN_005650A0) runs on a worker thread;
// the slot it works on is kept per thread for the skin-build hook below.
static DWORD g_tls = TLS_OUT_OF_INDEXES;
static volatile LONG g_body_last_log = 0;

static void __fastcall body_task_hook(void* self, void*, void* slot, void* a2, void* a3, void* a4, void* a5, void* a6,
                                      void* a7)
{
    TlsSetValue(g_tls, slot);
    ((BodyTaskFn)kBodyTask)(self, slot, a2, a3, a4, a5, a6, a7);
    TlsSetValue(g_tls, nullptr);
}

static void body_probe(uint8_t* slot, uint8_t* body, const Rig* rig, const Xf* skin, const Rim* rim);

// The queue carries every character FUN_00568590 draws, pedestrians too (their +0x360 is
// no vehicle). Occupants come from the character manager's pool ([[0x00FE7228]+0x2A8]
// + 0x8B540, FUN_0055A250 -> FUN_0055B2D0): 0x90 items of 0x39C bytes from +8.
static const uint32_t kEngine = 0x00FE7228;
static const uint32_t kOccupantPool = 0x8B540, kOccupantSize = 0x39C, kOccupants = 0x90;

static bool is_occupant(const uint8_t* body)
{
    const uint8_t* engine = *(const uint8_t* const*)kEngine;
    if (!body || !engine) return false;
    const uint8_t* mgr = *(const uint8_t* const*)(engine + 0x2A8);
    if (!mgr) return false;
    const uint8_t* base = mgr + kOccupantPool + 8;
    if (body < base) return false;
    uint32_t off = (uint32_t)(body - base);
    return off < kOccupantSize * kOccupants && off % kOccupantSize == 0;
}

static void __fastcall body_skin_hook(void* skel, void*, Xf* local, Xf* model, Xf* skin)
{
    ((SkinBuildFn)kSkinBuild)(skel, local, model, skin);
    uint8_t* slot = (uint8_t*)TlsGetValue(g_tls);
    if (!slot) return;
    if (g_qmode < 0) {
        EnterCriticalSection(&g_lock);
        if (g_qmode < 0) check_convention();
        LeaveCriticalSection(&g_lock);
    }
    if (g_qmode != 0 && g_qmode != 1) return;
    // The slot (FUN_00573450): +0x144 the occupant. The occupant (FUN_00567E60): +0x00 his
    // seat marker's matrix in the car, +0x360 the vehicle, +0x364 the seat (0: the driver).
    uint8_t* body = *(uint8_t**)(slot + 0x144);
    if (!is_occupant(body)) {
        static volatile LONG others = 0;
        if (g_debug && InterlockedIncrement(&others) <= 3) dsf_log("hands: queued character %p is no vehicle occupant", body);
        return;
    }
    if (!*(uint8_t**)(body + 0x360) || *(int*)(body + 0x364) != 0) return;
    Rim rim;
    bool has_rim = body_rim(body, &rim);
    DWORD now = GetTickCount();
    bool log = false;
    if (g_debug) {
        LONG last = g_body_last_log;
        log = now - (DWORD)last >= 1500 && InterlockedCompareExchange(&g_body_last_log, (LONG)now, last) == last;
    }
    if (!has_rim && !log) return;
    Rig* rig = rig_for((const uint8_t*)skel, true);
    if (log) body_probe(slot, body, rig, skin, has_rim ? &rim : nullptr);
    if (!rig || !has_rim) return;
    fix_arm(*rig, skin, model, rig->arm[0], rim, kBodyCircle, 1.0f, 0, log);
    fix_arm(*rig, skin, model, rig->arm[1], rim, kBodyCircle, 1.0f, 1, log);
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

static V3 fist(const Rig& rig, const Xf* pose, const Arm& a)
{
    V3 g = {0, 0, 0};
    int count = 0;
    for (int j = 0; j < rig.n; j++)
        if (a.group[j] == kHand) g = add(g, joint_pos(rig, pose, j)), count++;
    return count ? mul(g, 1.0f / count) : g;
}

// One occupant every 1.5 s: the car, his seat matrix and world matrix, the rim in his
// frame, and both fists (the wrist and finger joints) with their elbows and shoulders.
static void body_probe(uint8_t* slot, uint8_t* body, const Rig* rig, const Xf* skin, const Rim* rim)
{
    uint8_t* vehicle = *(uint8_t**)(body + 0x360);
    const uint8_t* spec = vehicle_spec(vehicle);
    int spool = -1;
    if (spec && *(uint8_t* const*)(spec + 0x4850)) spool = *(int*)(*(uint8_t* const*)(spec + 0x4850) + 0x14);
    dsf_log("hands: occupant %p slot %p vehicle %p spool %d seat %d thread %lu rim %s", body, slot, vehicle, spool,
            *(int*)(body + 0x364), GetCurrentThreadId(), rim ? "yes" : "no");
    log_floats("occupant seat", (const float*)body, 16);
    log_floats("occupant m40", (const float*)(body + 0x40), 16);
    log_floats("occupant world", (const float*)(slot + 0x40), 16);
    if (spec) {
        float v[10];
        memcpy(v, spec + 0x37E0, 12);
        memcpy(v + 3, spec + 0x37F0, 12);
        v[6] = vo3d(spec, 538);
        v[7] = vo3d(spec, 559);
        v[8] = vo3d(spec, kRadiusParam);
        v[9] = vo3d(spec, kAlongParam);
        log_floats("occupant marker", v, 10);
    }
    if (rim) {
        float v[7] = {rim->centre.x, rim->centre.y, rim->centre.z, rim->axis.x, rim->axis.y, rim->axis.z, rim->radius};
        log_floats("occupant rim (seat frame)", v, 7);
    }
    if (!rig) return;
    for (int side = 0; side < 2; side++) {
        const Arm& a = rig->arm[side];
        V3 g = fist(*rig, skin, a), w = joint_pos(*rig, skin, a.hand), e = joint_pos(*rig, skin, a.fore),
           sh = joint_pos(*rig, skin, a.arm);
        float v[12] = {g.x, g.y, g.z, w.x, w.y, w.z, e.x, e.y, e.z, sh.x, sh.y, sh.z};
        log_floats(side ? "occupant left fist/wrist/elbow/shoulder" : "occupant right fist/wrist/elbow/shoulder", v, 12);
    }
}

void mod_hands_init()
{
    g_debug = cfg_int("hands", "debug", 0);
    g_near = cfg_int("hands", "near_mm", 40) / 1000.0f;
    g_far = cfg_int("hands", "far_mm", 90) / 1000.0f;
    g_max_move = cfg_int("hands", "max_move_mm", 120) / 1000.0f;
    g_body = cfg_int("hands", "chase", 1);
    if (g_far <= g_near) g_far = g_near + 0.01f;
    InitializeCriticalSection(&g_lock);
    g_lock_ok = true;
    if (!bytes_equal(kCompose, "\x83\xEC\x30\x56\x57", 5)) {
        dsf_log("hands: FUN_00BC9E90 differs; hands left alone");
        return;
    }
    if (!patch_call(kEvalCall, (void*)eval_hook, kEvalCallBytes)) {
        dsf_log("hands: the arms' pose call at %08X differs; hands left alone", kEvalCall);
        return;
    }
    dsf_log("hands: pose hook at %08X (debug %d)", kEvalCall, g_debug);
    // The occupant: all three sites or none.
    if (!g_body) {
        dsf_log("hands: chase=0; the occupants (chase views, AI drivers) are left alone");
        return;
    }
    g_tls = TlsAlloc();
    bool same = g_tls != TLS_OUT_OF_INDEXES && check_bytes(kBodyTaskCall, kBodyTaskCallBytes, 5) &&
                check_bytes(kBodySkinCalls[0], kBodySkinCallBytes[0], 5) &&
                check_bytes(kBodySkinCalls[1], kBodySkinCallBytes[1], 5);
    if (!same) {
        dsf_log("hands: the occupant's pose task differs; chase-view hands left alone");
        return;
    }
    // The skin builds first: without the task hook they find no slot and do nothing.
    bool ok = patch_call(kBodySkinCalls[0], (void*)body_skin_hook, kBodySkinCallBytes[0]) &&
              patch_call(kBodySkinCalls[1], (void*)body_skin_hook, kBodySkinCallBytes[1]) &&
              patch_call(kBodyTaskCall, (void*)body_task_hook, kBodyTaskCallBytes);
    if (!ok) {
        patch_bytes(kBodySkinCalls[0], kBodySkinCallBytes[0], 5);
        patch_bytes(kBodySkinCalls[1], kBodySkinCallBytes[1], 5);
        patch_bytes(kBodyTaskCall, kBodyTaskCallBytes, 5);
        dsf_log("hands: the occupant's hooks didn't all apply; undone");
        return;
    }
    dsf_log("hands: occupant hooks at %08X, %08X, %08X", kBodyTaskCall, kBodySkinCalls[0], kBodySkinCalls[1]);
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
        fix_arm(rig, pose, nullptr, a, rim, kArmsCircle, 1.0f, 0, false);
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
        fix_arm(rig, pose, nullptr, a, rim, kArmsCircle, 1.0f, 0, false);
        bool ok = len(sub(joint_pos(rig, pose, 1), at[1])) < 1e-5f;
        if (!ok) printf("hands selftest: a hand off the wheel moved\n");
        fails += !ok;
    }
    // The occupant: a hip-rooted arm in the seat's frame, its fist on the animations' circle
    // (kBodyCircle) 45 degrees right of the top, moved onto a rim with Forza's column
    // (15 degrees) and radius (0.15 and 0.20 m) found from a tilted seat matrix, its hub on
    // the circle's column or 4 cm below it (the seat at cockpit.seat_anchor). The fist keeps
    // its angle; the model-space transforms get the same moves.
    for (int k = 0; k < 4; k++) {
        float r = radii[k & 1], drop = k < 2 ? 0.0f : 0.04f;
        const Circle& c = kBodyCircle;
        V3 up = unit(sub({0, 1, 0}, mul(c.axis, c.axis.y)));
        V3 p = add(c.centre, add(mul({1, 0, 0}, c.radius * 0.7071f), mul(up, c.radius * 0.7071f)));
        Rig rig = {};
        rig.n = 8;
        rig.body = true;
        // 0 hips, 1 shoulder (clavicle), 2 arm (at the shoulder), 3 arm roll, 4 forearm (at the
        // elbow), 5 hand (at the wrist), 6..7 fingers
        const int parent[8] = {-1, 0, 1, 2, 2, 4, 5, 5};
        const V3 at[8] = {{0, 0.1f, -0.1f},  {0.05f, 0.55f, -0.05f}, {0.20f, 0.55f, 0.0f}, {0.22f, 0.45f, 0.11f},
                          {0.26f, 0.36f, 0.22f}, add(p, {-0.01f, -0.01f, -0.02f}), add(p, {0.02f, 0.01f, 0.01f}),
                          add(p, {-0.01f, 0.0f, 0.01f})};
        for (int j = 0; j < 8; j++) rig.parent[j] = parent[j], rig.bind[j] = at[j];
        Arm& a = rig.arm[0];
        a = {5, 4, 2, 1, false, {}};
        const uint8_t group[8] = {kNone, kShoulder, kUpper, kUpper, kFore, kHand, kHand, kHand};
        memcpy(a.group, group, sizeof group);
        Xf pose[8], model[8];
        for (Xf& x : pose) x = {{0, 0, 0, 1}, {0, 0, 0, 0}};
        memcpy(model, pose, sizeof pose);
        // The seat 5 degrees down at (0.37, 0.10, -0.40) in the car; the rim in the car.
        const float s5 = sinf(-0.0873f), c5 = cosf(-0.0873f);
        const float seat[16] = {1, 0, 0, 0, 0, c5, s5, 0, 0, -s5, c5, 0, 0.37f, 0.10f, -0.40f, 1};
        V3 axis_seat = unit({0, sinf(0.2618f), -cosf(0.2618f)});
        V3 hub = sub(add(c.centre, mul(c.axis, 0.01f)), mul(up, drop));
        V3 centre_car = mat_point(seat, hub);
        V3 axis_car = sub(mat_point(seat, axis_seat), mat_point(seat, {0, 0, 0}));
        Rim rim;
        bool ok = mat_inverse_point(seat, centre_car, false, &rim.centre) && mat_inverse_point(seat, axis_car, true, &rim.axis);
        rim.axis = unit(rim.axis);
        rim.radius = r;
        ok = ok && len(sub(rim.centre, hub)) < 1e-4f && len(sub(rim.axis, axis_seat)) < 1e-4f;
        fix_arm(rig, pose, model, a, rim, c, 1.0f, 0, false);
        V3 g = fist(rig, pose, a);
        V3 gv = sub(g, rim.centre);
        float radial = len(sub(gv, mul(rim.axis, dot(gv, rim.axis)))), off = dot(gv, rim.axis);
        V3 S = joint_pos(rig, pose, 2), E = joint_pos(rig, pose, 4), W = joint_pos(rig, pose, 5);
        V3 was = sub(p, c.centre), now = sub(gv, mul(rim.axis, off));
        was = sub(was, mul(rim.axis, dot(was, rim.axis)));
        ok = ok && dot(unit(was), unit(now)) > 0.9999f && fabsf(radial - (r + c.out)) < 1e-3f && fabsf(off) < 1e-3f && len(sub(S, at[2])) < 1e-4f &&
             len(sub(joint_pos(rig, pose, 1), at[1])) < 1e-5f && fabsf(len(sub(E, S)) - len(sub(at[4], at[2]))) < 1e-3f &&
             fabsf(len(sub(W, E)) - len(sub(at[5], at[4]))) < 1e-3f &&
             fabsf(len(sub(joint_pos(rig, pose, 3), S)) - len(sub(at[3], at[2]))) < 1e-3f;
        for (int j = 0; j < 8; j++) ok = ok && len(sub(joint_pos(rig, model, j), joint_pos(rig, pose, j))) < 1e-5f;
        if (!ok)
            printf("hands selftest occupant r %.2f drop %.2f: fist %.4f (off %.4f), shoulder moved %.4f\n", r, drop, radial,
                   off, len(sub(S, at[2])));
        fails += !ok;
    }
    // A full-body skeleton as the game keeps it (joint records of 0x70 bytes from +0x54:
    // parent, name hash, inverse bind at +0x2C), rooted at the hips: rig_for finds both
    // arms by name, not hanging.
    {
        static uint8_t skel[0x80 + 0x70 * 64];
        memset(skel, 0, sizeof skel);
        const char* names[15] = {"Hips",          "RightShoulder", "RightArm",       "RightArmRoll", "RightForeArm",
                                 "RightHand",     "RightHandIndex1", "LeftShoulder", "LeftArm",      "LeftArmRoll",
                                 "LeftForeArm",   "LeftHand",      "LeftHandIndex1", "Spine",        "Head"};
        const int parent[15] = {-1, 13, 1, 2, 2, 4, 5, 13, 7, 8, 8, 10, 11, 0, 13};
        *(int*)(skel + 0x24) = 15;
        for (int j = 0; j < 15; j++) {
            uint8_t* rec = skel + 0x54 + 0x70 * j;
            *(int*)rec = parent[j];
            *(uint32_t*)(rec + 4) = name_hash(names[j]);
            Xf* ib = (Xf*)(rec + 0x2C);
            *ib = {{0, 0, 0, 1}, {-0.01f * j, -0.02f * j, 0, 0}};
        }
        if (!g_lock_ok) InitializeCriticalSection(&g_lock), g_lock_ok = true;
        Rig* r = rig_for(skel, true);
        bool ok = r && r->arm[0].hand == 5 && r->arm[0].fore == 4 && r->arm[0].arm == 2 && r->arm[0].shoulder == 1 &&
                  r->arm[1].hand == 11 && !r->arm[0].hanging && r->arm[0].group[6] == kHand && r->arm[0].group[3] == kUpper &&
                  len(sub(r->bind[5], {0.05f, 0.10f, 0})) < 1e-5f;
        if (!ok) printf("hands selftest: the occupant skeleton's arms not found\n");
        fails += !ok;
        for (Rig& x : g_rigs)
            if (x.skel == skel) memset(&x, 0, sizeof x);
    }
    return fails;
}
