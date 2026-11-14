// d3dlog.asi: a diagnostic plugin for test runs (issue #610). Logs what the game's Direct3D 9
// device returns: each change of Present's and TestCooperativeLevel's result, every Reset with its
// parameters and result, failed CreateTexture/VertexBuffer/IndexBuffer calls, and every 5 s the call
// counts. 15 s after d3d9.dll loads it makes a throwaway device on a hidden window to find d3d9.dll's
// functions (the game's device has another vtable copy, so patching the vtable caught nothing) and
// puts a 5-byte jmp at the start of each; a prologue that is already a jmp (dsf_enhanced's DLSS 5
// detours) is chained. Log: C:\Output\d3dlog\<stamp>.log in a test VM, else d3dlog.log beside Driver.exe.
// Build: tools\wsb\d3dlog\build.ps1. Use: put the .asi in an -Overlay folder's plugins\.
#define _CRT_SECURE_NO_WARNINGS
#include <windows.h>
#include <d3d9.h>
#include <stdio.h>

static FILE *g_log;
static CRITICAL_SECTION g_cs;
static DWORD g_t0;

static void Log(const char *fmt, ...) {
    if (!g_log) return;
    EnterCriticalSection(&g_cs);
    SYSTEMTIME st; GetLocalTime(&st);
    fprintf(g_log, "[%02d:%02d:%02d.%03d +%.1f] ", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds,
            (GetTickCount() - g_t0) / 1000.0);
    va_list a; va_start(a, fmt); vfprintf(g_log, fmt, a); va_end(a);
    fputc('\n', g_log); fflush(g_log);
    LeaveCriticalSection(&g_cs);
}

static const char *Name(HRESULT hr) {
    switch (hr) {
    case D3D_OK: return "D3D_OK";
    case D3DERR_DEVICELOST: return "D3DERR_DEVICELOST";
    case D3DERR_DEVICENOTRESET: return "D3DERR_DEVICENOTRESET";
    case D3DERR_DRIVERINTERNALERROR: return "D3DERR_DRIVERINTERNALERROR";
    case D3DERR_OUTOFVIDEOMEMORY: return "D3DERR_OUTOFVIDEOMEMORY";
    case E_OUTOFMEMORY: return "E_OUTOFMEMORY";
    case D3DERR_INVALIDCALL: return "D3DERR_INVALIDCALL";
    case D3DERR_NOTAVAILABLE: return "D3DERR_NOTAVAILABLE";
    case 0x88760874: return "D3DERR_DEVICEREMOVED";
    case 0x88760875: return "D3DERR_DEVICEHUNG";
    case 0x08760877: return "S_PRESENT_OCCLUDED";
    case 0x08760878: return "S_PRESENT_MODE_CHANGED";
    default: return "?";
    }
}

typedef HRESULT(STDMETHODCALLTYPE *TclFn)(IDirect3DDevice9 *);
typedef HRESULT(STDMETHODCALLTYPE *ResetFn)(IDirect3DDevice9 *, D3DPRESENT_PARAMETERS *);
typedef HRESULT(STDMETHODCALLTYPE *PresentFn)(IDirect3DDevice9 *, const RECT *, const RECT *, HWND, const RGNDATA *);
typedef HRESULT(STDMETHODCALLTYPE *CreateTexFn)(IDirect3DDevice9 *, UINT, UINT, UINT, DWORD, D3DFORMAT, D3DPOOL,
                                                IDirect3DTexture9 **, HANDLE *);
typedef HRESULT(STDMETHODCALLTYPE *CreateVbFn)(IDirect3DDevice9 *, UINT, DWORD, DWORD, D3DPOOL,
                                               IDirect3DVertexBuffer9 **, HANDLE *);
typedef HRESULT(STDMETHODCALLTYPE *CreateIbFn)(IDirect3DDevice9 *, UINT, DWORD, D3DFORMAT, D3DPOOL,
                                               IDirect3DIndexBuffer9 **, HANDLE *);

static TclFn oTcl; static ResetFn oReset; static PresentFn oPresent;
static CreateTexFn oTex; static CreateVbFn oVb; static CreateIbFn oIb;

static volatile LONG g_tclCalls, g_presents, g_texFails, g_vbFails, g_ibFails;
static HRESULT g_lastTcl = 1, g_lastPresent = 1;   // 1: no call yet

static HRESULT STDMETHODCALLTYPE HkTcl(IDirect3DDevice9 *d) {
    HRESULT hr = oTcl(d);
    InterlockedIncrement(&g_tclCalls);
    if (hr != g_lastTcl) { Log("TestCooperativeLevel -> 0x%08X %s", (unsigned)hr, Name(hr)); g_lastTcl = hr; }
    return hr;
}

static HRESULT STDMETHODCALLTYPE HkReset(IDirect3DDevice9 *d, D3DPRESENT_PARAMETERS *pp) {
    Log("Reset called: %ux%u windowed %d swap %d ms %d", pp ? pp->BackBufferWidth : 0, pp ? pp->BackBufferHeight : 0,
        pp ? pp->Windowed : -1, pp ? pp->SwapEffect : -1, pp ? pp->MultiSampleType : -1);
    HRESULT hr = oReset(d, pp);
    Log("Reset -> 0x%08X %s", (unsigned)hr, Name(hr));
    return hr;
}

static HRESULT STDMETHODCALLTYPE HkPresent(IDirect3DDevice9 *d, const RECT *a, const RECT *b, HWND w, const RGNDATA *r) {
    HRESULT hr = oPresent(d, a, b, w, r);
    InterlockedIncrement(&g_presents);
    if (hr != g_lastPresent) { Log("Present -> 0x%08X %s", (unsigned)hr, Name(hr)); g_lastPresent = hr; }
    return hr;
}

static HRESULT STDMETHODCALLTYPE HkTex(IDirect3DDevice9 *d, UINT w, UINT h, UINT l, DWORD u, D3DFORMAT f, D3DPOOL p,
                                       IDirect3DTexture9 **t, HANDLE *s) {
    HRESULT hr = oTex(d, w, h, l, u, f, p, t, s);
    if (FAILED(hr) && InterlockedIncrement(&g_texFails) <= 50)
        Log("CreateTexture %ux%u levels %u usage 0x%X format %d pool %d -> 0x%08X %s", w, h, l, u, f, p, (unsigned)hr, Name(hr));
    return hr;
}

static HRESULT STDMETHODCALLTYPE HkVb(IDirect3DDevice9 *d, UINT len, DWORD u, DWORD fvf, D3DPOOL p,
                                      IDirect3DVertexBuffer9 **v, HANDLE *s) {
    HRESULT hr = oVb(d, len, u, fvf, p, v, s);
    if (FAILED(hr) && InterlockedIncrement(&g_vbFails) <= 50)
        Log("CreateVertexBuffer %u usage 0x%X pool %d -> 0x%08X %s", len, u, p, (unsigned)hr, Name(hr));
    return hr;
}

static HRESULT STDMETHODCALLTYPE HkIb(IDirect3DDevice9 *d, UINT len, DWORD u, D3DFORMAT f, D3DPOOL p,
                                      IDirect3DIndexBuffer9 **i, HANDLE *s) {
    HRESULT hr = oIb(d, len, u, f, p, i, s);
    if (FAILED(hr) && InterlockedIncrement(&g_ibFails) <= 50)
        Log("CreateIndexBuffer %u usage 0x%X pool %d -> 0x%08X %s", len, u, p, (unsigned)hr, Name(hr));
    return hr;
}

// A 5-byte jmp at the function's start to our hook; the trampoline runs the moved prologue and
// jumps back. A prologue that is already a jmp rel32 (dsf_enhanced's DLSS5 detours) is followed.
static void *Detour(void *fn, void *hook, const char *what) {
    unsigned char *p = (unsigned char *)fn;
    unsigned char *tramp = (unsigned char *)VirtualAlloc(nullptr, 32, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    int moved = 0;
    if (p[0] == 0xE9) {                                   // already detoured: go where it went
        unsigned char *target = p + 5 + *(int *)(p + 1);
        tramp[0] = 0xE9; *(int *)(tramp + 1) = (int)(target - (tramp + 5));
        Log("%s: prologue already a jmp to %p; chained", what, target);
    } else if (p[0] == 0x8B && p[1] == 0xFF && p[2] == 0x55 && p[3] == 0x8B && p[4] == 0xEC) {   // mov edi,edi; push ebp; mov ebp,esp
        moved = 5;
        memcpy(tramp, p, 5);
        tramp[5] = 0xE9; *(int *)(tramp + 6) = (int)((p + 5) - (tramp + 10));
    } else {
        Log("%s: unknown prologue %02X %02X %02X %02X %02X %02X; not hooked", what, p[0], p[1], p[2], p[3], p[4], p[5]);
        return nullptr;
    }
    DWORD old;
    VirtualProtect(p, 5, PAGE_EXECUTE_READWRITE, &old);
    unsigned char jmp[5]; jmp[0] = 0xE9; *(int *)(jmp + 1) = (int)((unsigned char *)hook - (p + 5));
    memcpy(p, jmp, 5);
    VirtualProtect(p, 5, old, &old);
    FlushInstructionCache(GetCurrentProcess(), p, 5);
    Log("detoured %s at %p (moved %d bytes)", what, fn, moved);
    return tramp;
}

static DWORD WINAPI Worker(LPVOID) {
    // Wait for the game's own device (d3d9.dll loaded and its window up) before making ours.
    HMODULE d3d = nullptr;
    for (int i = 0; i < 600 && !(d3d = GetModuleHandleA("d3d9.dll")); ++i) Sleep(100);
    if (!d3d) { Log("d3d9.dll never loaded"); return 0; }
    Sleep(15000);
    typedef IDirect3D9 *(WINAPI * CreateFn)(UINT);
    CreateFn create = (CreateFn)GetProcAddress(d3d, "Direct3DCreate9");
    IDirect3D9 *d9 = create ? create(D3D_SDK_VERSION) : nullptr;
    if (!d9) { Log("Direct3DCreate9 failed"); return 0; }
    WNDCLASSA wc = {}; wc.lpfnWndProc = DefWindowProcA; wc.hInstance = GetModuleHandleA(nullptr); wc.lpszClassName = "d3dlog";
    RegisterClassA(&wc);
    HWND w = CreateWindowA("d3dlog", "", WS_OVERLAPPED, 0, 0, 64, 64, nullptr, nullptr, wc.hInstance, nullptr);
    D3DPRESENT_PARAMETERS pp = {}; pp.Windowed = TRUE; pp.SwapEffect = D3DSWAPEFFECT_DISCARD; pp.BackBufferFormat = D3DFMT_UNKNOWN;
    pp.BackBufferWidth = 64; pp.BackBufferHeight = 64; pp.hDeviceWindow = w;
    IDirect3DDevice9 *dev = nullptr;
    HRESULT hr = d9->CreateDevice(D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, w, D3DCREATE_SOFTWARE_VERTEXPROCESSING | D3DCREATE_FPU_PRESERVE, &pp, &dev);
    if (FAILED(hr)) { Log("dummy CreateDevice -> 0x%08X %s", (unsigned)hr, Name(hr)); d9->Release(); return 0; }
    void **vt = *(void ***)dev;
    oTcl = (TclFn)Detour(vt[3], (void *)HkTcl, "TestCooperativeLevel");
    oReset = (ResetFn)Detour(vt[16], (void *)HkReset, "Reset");
    oPresent = (PresentFn)Detour(vt[17], (void *)HkPresent, "Present");
    oTex = (CreateTexFn)Detour(vt[23], (void *)HkTex, "CreateTexture");
    oVb = (CreateVbFn)Detour(vt[26], (void *)HkVb, "CreateVertexBuffer");
    oIb = (CreateIbFn)Detour(vt[27], (void *)HkIb, "CreateIndexBuffer");
    dev->Release(); d9->Release(); DestroyWindow(w);
    Log("hooks in; logging changes of result and a summary every 5 s");
    for (;;) {
        Sleep(5000);
        Log("summary: presents %ld, TestCooperativeLevel calls %ld, failed CreateTexture %ld, VB %ld, IB %ld",
            g_presents, g_tclCalls, g_texFails, g_vbFails, g_ibFails);
    }
}

BOOL WINAPI DllMain(HINSTANCE, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        InitializeCriticalSection(&g_cs);
        g_t0 = GetTickCount();
        char path[MAX_PATH]; GetModuleFileNameA(nullptr, path, MAX_PATH);
        char *s = strrchr(path, '\\'); if (s) strcpy(s + 1, "d3dlog.log");
        if (GetFileAttributesA("C:\\Output") != INVALID_FILE_ATTRIBUTES) {   // a test VM: the host's share
            CreateDirectoryA("C:\\Output\\d3dlog", nullptr);
            SYSTEMTIME st; GetLocalTime(&st);
            sprintf(path, "C:\\Output\\d3dlog\\%04d%02d%02d-%02d%02d%02d.log", st.wYear, st.wMonth, st.wDay,
                    st.wHour, st.wMinute, st.wSecond);
        }
        g_log = fopen(path, "w");
        Log("d3dlog loaded");
        CreateThread(nullptr, 0, Worker, nullptr, 0, nullptr);
    }
    return TRUE;
}
