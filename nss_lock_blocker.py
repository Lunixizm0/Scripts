#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from threading import Lock

try:
    import frida
except ImportError:  # pragma: no cover
    sys.exit("frida is required: python -m pip install frida")

ROOT = Path(__file__).resolve().parent
DEFAULT_STOP_FILE = ROOT / "nss_lock_blocker.stop"


AGENT = r"""
'use strict';

var PID = Process.id;
var ABI = (Process.arch === 'ia32') ? 'stdcall' : 'default';
var user32 = null;
var kernel32 = null;
try { user32 = Process.getModuleByName('user32.dll'); } catch (e) {}
try { kernel32 = Process.getModuleByName('kernel32.dll'); } catch (e) {}

function exportOf(mod, name) {
    if (mod === null) return null;
    try { return mod.findExportByName(name); } catch (e) { return null; }
}
function nf(mod, name, ret, args) {
    var p = exportOf(mod, name);
    if (p === null) return null;
    try { return new NativeFunction(p, ret, args); } catch (e) { return null; }
}
var TerminateProcess = nf(kernel32, 'TerminateProcess', 'int', ['pointer', 'uint']);
function note(kind, obj) {
    obj.kind = kind;
    obj.pid = PID;
    obj.t = Date.now();
    try { send(obj); } catch (e) {}
}
function warn(msg) { note('warn', { msg: String(msg) }); }

function replaceApi(mod, name, ret, argTypes, impl) {
    var p = exportOf(mod, name);
    if (p === null) { warn('missing export ' + name); return null; }
    var original = null;
    try { original = new NativeFunction(p, ret, argTypes); } catch (e) { warn('NativeFunction ' + name + ': ' + e); return null; }
    try {
        var cb = new NativeCallback(impl, ret, argTypes, ABI);
        Interceptor.replace(p, cb);
        note('replaced', { api: name });
        return original;
    } catch (e) {
        warn('replace ' + name + ': ' + e);
        return null;
    }
}

function hookApi(mod, name, handlers) {
    var p = exportOf(mod, name);
    if (p === null) { return; }
    try {
        Interceptor.attach(p, handlers);
        note('hooked', { api: name });
    } catch (e) {
        warn('hook ' + name + ': ' + e);
    }
}

function hex(v) {
    try { return '0x' + v.toString(16); } catch (e) { return '?'; }
}

// 1) Keep the input desktop pinned to the real desktop.
//    SwitchDesktop("NSLock") is rewritten to SwitchDesktop("Default"), so the
//    lock desktop can never become visible. This also rescues the desktop if
//    the blocker is started while NSLock is already active.
var OpenInputDesktop = nf(user32, 'OpenInputDesktop', 'pointer', ['uint', 'int']);
var OpenDesktopW = nf(user32, 'OpenDesktopW', 'pointer', ['pointer', 'uint', 'int', 'uint']);
var GetUserObjectInformationW = nf(user32, 'GetUserObjectInformationW', 'int',
    ['pointer', 'int', 'pointer', 'uint', 'pointer']);
var switchDesktopOrig = nf(user32, 'SwitchDesktop', 'int', ['pointer']);
var desktopNameBuf = Memory.alloc(512);
var desktopNeedBuf = Memory.alloc(4);

function desktopName(h) {
    try {
        if (h === null || h.isNull() || GetUserObjectInformationW === null) return null;
        if (GetUserObjectInformationW(h, 2, desktopNameBuf, 512, desktopNeedBuf) !== 0) {
            return desktopNameBuf.readUtf16String() || null;
        }
    } catch (e) {}
    return null;
}

var inputDesktop = (OpenInputDesktop !== null) ? OpenInputDesktop(0, 0) : null;
var inputName = inputDesktop ? desktopName(inputDesktop) : null;
var defaultDesktop = inputDesktop;
if ((!inputName || inputName.toLowerCase() !== 'default') && OpenDesktopW !== null) {
    try {
        var d = OpenDesktopW(Memory.allocUtf16String('Default'), 0, 0, 0x0100);
        if (d !== null && !d.isNull()) {
            defaultDesktop = d;
            if (inputName && inputName.toLowerCase().indexOf('lock') >= 0 && switchDesktopOrig !== null) {
                try { switchDesktopOrig(d); } catch (e) {}
            }
            note('rescue', { from: inputName, to: 'Default' });
        }
    } catch (e) {}
}
note('desktop', {
    input: inputDesktop ? inputDesktop.toString() : null,
    inputName: inputName,
    pinned: defaultDesktop ? defaultDesktop.toString() : null,
    pinnedName: defaultDesktop ? desktopName(defaultDesktop) : null
});

var switchDesktopCalls = 0;
hookApi(user32, 'SwitchDesktop', {
    onEnter: function (args) {
        this.requested = args[0].toString();
        this.requestedName = desktopName(args[0]);
        args[0] = (defaultDesktop !== null && !defaultDesktop.isNull()) ? defaultDesktop : ptr(0);
    },
    onLeave: function (retval) {
        switchDesktopCalls++;
        note('block', {
            what: 'SwitchDesktop',
            requested: this.requested,
            requestedName: this.requestedName,
            forcedTo: (defaultDesktop !== null && !defaultDesktop.isNull()) ? defaultDesktop.toString() : '0x0',
            ret: retval.toInt32(),
            count: switchDesktopCalls
        });
    }
});

// 2) Never install low-level keyboard/mouse hooks. Fake the return handle.
var fakeHookSeq = 0x4E530001;

function isBlockedHook(id) { return id === 13 || id === 14; } // WH_KEYBOARD_LL / WH_MOUSE_LL

var realUnhook = nf(user32, 'UnhookWindowsHookEx', 'int', ['pointer']);

// The low-level hook is already installed by the time onLeave runs, so remove
// the real hook and hand the caller a fake non-null handle. The caller only
// uses this handle later to unhook, which is harmless.
function blockSetHook(api) {
    hookApi(user32, api, {
        onEnter: function (args) {
            this.idHook = args[0].toInt32();
        },
        onLeave: function (retval) {
            if (!isBlockedHook(this.idHook)) return;
            var real = retval;
            if (!real.isNull() && realUnhook !== null) {
                try { realUnhook(real); } catch (e) {}
            }
            var fake = ptr(fakeHookSeq++);
            retval.replace(fake);
            note('block', {
                what: api, idHook: this.idHook,
                name: this.idHook === 13 ? 'WH_KEYBOARD_LL' : 'WH_MOUSE_LL',
                fakeHandle: fake.toString()
            });
        }
    });
}
blockSetHook('SetWindowsHookExW');
blockSetHook('SetWindowsHookExA');

// 3) Swallow the other input-blocking APIs, just in case this build uses them.
hookApi(user32, 'BlockInput', {
    onEnter: function (args) {
        var requested = args[0].toInt32();
        if (requested !== 0) args[0] = ptr(0); // BlockInput(TRUE) -> BlockInput(FALSE)
        note('block', { what: 'BlockInput', requested: requested, forced: 0 });
    }
});
replaceApi(user32, 'LockWorkStation', 'int', [], function () {
    note('block', { what: 'LockWorkStation' });
    return 1;
});
hookApi(user32, 'ClipCursor', {
    onEnter: function (args) {
        note('block', { what: 'ClipCursor', requested: args[0].toString(), forced: 'NULL' });
        args[0] = ptr(0); // release any mouse confinement
    }
});

// 4) Hide/inhibit lock, show and blank windows.
var winCache = {};
var hiddenLogged = {};

function isBlockedWindow(cls, title) {
    var c = (cls || '').toLowerCase();
    var t = (title || '').toLowerCase();
    return c === 'nsmdisplayimageclass' ||   // lock image
           c === 'nsmclientshowclass' ||     // teacher screen "show"
           c === 'nsmblankwnd' ||            // "blank all"
           c === 'nsmremote32' ||
           c === 'nsmtouchborder' ||
           c === 'nsmcursorclass' ||
           t === 'lockimage';
}

function cacheWindow(hwnd, cls, title) {
    if (hwnd === null || hwnd.isNull()) return;
    var key = hwnd.toString();
    winCache[key] = { cls: cls || '', title: title || '' };
}

function safeStrW(p) {
    try { return (p === null || p.isNull()) ? '' : p.readUtf16String(); } catch (e) { return ''; }
}
function safeStrA(p) {
    try { return (p === null || p.isNull()) ? '' : p.readAnsiString(); } catch (e) { return ''; }
}

function hideIfLock(hwnd, why) {
    var key = hwnd.toString();
    var info = winCache[key];
    var cls = info ? info.cls : '';
    var title = info ? info.title : '';
    if (!isBlockedWindow(cls, title)) return false;
    try {
        var sw = user32 !== null ? user32.findExportByName('ShowWindow') : null;
        if (sw !== null) {
            var showWindow = new NativeFunction(sw, 'int', ['pointer', 'int']);
            showWindow(hwnd, 0); // SW_HIDE
        }
    } catch (e) {}
    if (!hiddenLogged[key]) {
        hiddenLogged[key] = true;
        note('block', { what: 'hide-blocked-window', hwnd: key, cls: cls, title: title, why: why });
    }
    return true;
}

hookApi(user32, 'CreateWindowExW', {
    onEnter: function (args) {
        this.cls = safeStrW(args[1]);
        this.title = safeStrW(args[2]);
        if (isBlockedWindow(this.cls, this.title)) {
            var style = args[3].toInt32() | 0;
            if (style & 0x10000000) { // WS_VISIBLE
                args[3] = ptr(style & ~0x10000000);
                note('block', { what: 'CreateWindowExW(strip WS_VISIBLE)', cls: this.cls, title: this.title });
            }
        }
    },
    onLeave: function (retval) {
        var h = retval;
        cacheWindow(h, this.cls, this.title);
        if (isBlockedWindow(this.cls, this.title) && !h.isNull()) {
            note('block', { what: 'blocked-window-created', hwnd: h.toString(), cls: this.cls, title: this.title });
            setTimeout(function () { hideIfLock(h, 'created'); }, 0);
        }
    }
});

hookApi(user32, 'CreateWindowExA', {
    onEnter: function (args) {
        this.cls = safeStrA(args[1]);
        this.title = safeStrA(args[2]);
        if (isBlockedWindow(this.cls, this.title)) {
            var style = args[3].toInt32() | 0;
            if (style & 0x10000000) { // WS_VISIBLE
                args[3] = ptr(style & ~0x10000000);
                note('block', { what: 'CreateWindowExA(strip WS_VISIBLE)', cls: this.cls, title: this.title });
            }
        }
    },
    onLeave: function (retval) {
        var h = retval;
        cacheWindow(h, this.cls, this.title);
        if (isBlockedWindow(this.cls, this.title) && !h.isNull()) {
            note('block', { what: 'blocked-window-created', hwnd: h.toString(), cls: this.cls, title: this.title });
            setTimeout(function () { hideIfLock(h, 'created'); }, 0);
        }
    }
});

hookApi(user32, 'ShowWindow', {
    onEnter: function (args) {
        var h = args[0];
        var info = winCache[h.toString()];
        var cls = info ? info.cls : '';
        var title = info ? info.title : '';
        if (isBlockedWindow(cls, title)) {
            var cmd = args[1].toInt32();
            if (cmd !== 0) {
                args[1] = ptr(0);
                note('block', { what: 'ShowWindow->HIDE', hwnd: h.toString(), cls: cls, title: title, requested: cmd });
            }
        }
    }
});

hookApi(user32, 'SetWindowPos', {
    onEnter: function (args) {
        var h = args[0];
        var info = winCache[h.toString()];
        var cls = info ? info.cls : '';
        var title = info ? info.title : '';
        if (isBlockedWindow(cls, title)) {
            var flags = args[6].toInt32() | 0;
            if (flags & 0x40) { // SWP_SHOWWINDOW
                args[6] = ptr(flags & ~0x40);
                note('block', { what: 'SetWindowPos(no-show)', hwnd: h.toString(), cls: cls, title: title });
            }
        }
    }
});

// The lock flow launches `client32.exe /L"nss_lock_image.jpg"` as a helper.
// Create it suspended (so it never shows a window or installs hooks) and then
// terminate it. This prevents helper-process accumulation across lock retries.
function hookLockHelper(api, readCmd) {
    hookApi(kernel32, api, {
        onEnter: function (args) {
            this.cmd = readCmd(args[1]);
            this.isLockHelper = !!(this.cmd && this.cmd.toLowerCase().indexOf('nss_lock_image') >= 0);
            this.pi = args[9];
            if (this.isLockHelper) {
                var flags = args[5].toInt32();
                args[5] = ptr(flags | 0x4); // CREATE_SUSPENDED
                note('block', {
                    what: api + '(suspend lock helper)',
                    cmd: this.cmd, flagsBefore: hex(flags)
                });
            }
        },
        onLeave: function (retval) {
            if (!this.isLockHelper) return;
            var ok = retval.toInt32() !== 0;
            note('block', { what: 'lock-helper-created', cmd: this.cmd, ok: ok });
            if (ok && this.pi !== null && !this.pi.isNull() && TerminateProcess !== null) {
                try {
                    var hp = this.pi.readPointer();
                    if (hp !== null && !hp.isNull()) {
                        TerminateProcess(hp, 0);
                        note('block', { what: 'lock-helper-terminated', hProcess: hp.toString() });
                    }
                } catch (e) {}
            }
        }
    });
}
hookLockHelper('CreateProcessW', safeStrW);
hookLockHelper('CreateProcessA', safeStrA);

// Poll windows on this process's current desktop and hide any lock image that
// was created before our hooks were in place (e.g. the /L helper process).
var enumPidBuf = Memory.alloc(4);
var enumClsBuf = Memory.alloc(1024);
var enumTxtBuf = Memory.alloc(1024);
var GetWindowThreadProcessId = nf(user32, 'GetWindowThreadProcessId', 'uint', ['pointer', 'pointer']);
var GetClassNameW = nf(user32, 'GetClassNameW', 'int', ['pointer', 'pointer', 'int']);
var GetWindowTextW = nf(user32, 'GetWindowTextW', 'int', ['pointer', 'pointer', 'int']);
var IsWindowVisible = nf(user32, 'IsWindowVisible', 'int', ['pointer']);
var EnumWindows = nf(user32, 'EnumWindows', 'int', ['pointer', 'pointer']);
var EnumChildWindows = nf(user32, 'EnumChildWindows', 'int', ['pointer', 'pointer']);

function inspectWindow(hwnd, why) {
    try {
        enumPidBuf.writeU32(0);
        if (GetWindowThreadProcessId !== null) GetWindowThreadProcessId(hwnd, enumPidBuf);
        if (enumPidBuf.readU32() !== PID) return;
        var cls = '';
        var title = '';
        if (GetClassNameW !== null) { GetClassNameW(hwnd, enumClsBuf, 512); cls = enumClsBuf.readUtf16String() || ''; }
        if (GetWindowTextW !== null) { GetWindowTextW(hwnd, enumTxtBuf, 512); title = enumTxtBuf.readUtf16String() || ''; }
        cacheWindow(hwnd, cls, title);
        if (!isBlockedWindow(cls, title)) return;
        if (IsWindowVisible !== null && IsWindowVisible(hwnd) !== 0) hideIfLock(hwnd, why);
    } catch (e) {}
}

var topEnumCb = new NativeCallback(function (hwnd, lparam) {
    inspectWindow(hwnd, 'poll-top');
    try { if (EnumChildWindows !== null) EnumChildWindows(hwnd, childEnumCb, ptr(0)); } catch (e) {}
    return 1;
}, 'int', ['pointer', 'pointer']);

var childEnumCb = new NativeCallback(function (hwnd, lparam) {
    inspectWindow(hwnd, 'poll-child');
    return 1;
}, 'int', ['pointer', 'pointer']);

function poll() {
    try { if (EnumWindows !== null) EnumWindows(topEnumCb, ptr(0)); } catch (e) {}
}

poll();
setInterval(poll, 150);

note('ready', { arch: Process.arch, pointerSize: Process.pointerSize });
"""


def _run(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return (p.stdout or "") + (p.stderr or "")
    except Exception:
        return ""


def tasklist_rows() -> list[dict]:
    rows: list[dict] = []
    out = _run(["tasklist", "/FO", "CSV", "/NH"])
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        rows.append({"name": parts[0], "pid": pid, "session": parts[2], "session_num": parts[3]})
    return rows


def auto_targets() -> list[tuple[int, str]]:
    targets: list[tuple[int, str]] = []
    for row in tasklist_rows():
        name = row["name"].lower()
        if name not in ("client32.exe", "studentui.exe"):
            continue
        if row["session"].lower() == "services" or row["session_num"] == "0":
            continue
        targets.append((row["pid"], row["name"]))
    targets.sort(key=lambda t: (0 if t[1].lower() == "studentui.exe" else 1, t[0]))
    return targets


def process_label(pid: int, name: str | None = None) -> str:
    if name:
        return f"{name}:{pid}"
    for row in tasklist_rows():
        if row["pid"] == pid:
            return f"{row['name']}:{pid}"
    return f"pid={pid}"


class Blocker:
    def __init__(self, base: Path) -> None:
        self.log_path = base.with_suffix(".log")
        self.jsonl_path = base.with_suffix(".jsonl")
        self.log = open(self.log_path, "w", encoding="utf-8", buffering=1)
        self.jsonl = open(self.jsonl_path, "w", encoding="utf-8", buffering=1)
        self.lock = Lock()
        self.names: dict[int, str] = {}
        self.blocks = 0
        self.started = time.time()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.summary()
        with contextlib.suppress(Exception):
            self.log.close()
        with contextlib.suppress(Exception):
            self.jsonl.close()

    def line(self, text: str) -> None:
        with self.lock:
            self.log.write(text + "\n")
            print(text, flush=True)

    def on_message(self, message: dict, data: bytes | None) -> None:
        if message.get("type") == "error":
            self.line(f"[agent error] {message.get('stack') or message.get('description')}")
            return
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return
        with self.lock:
            self.jsonl.write(json.dumps(payload, ensure_ascii=False) + "\n")
        kind = payload.get("kind")
        pid = payload.get("pid", 0)
        label = self.names.get(pid, f"pid={pid}")
        t = dt.datetime.fromtimestamp((payload.get("t") or 0) / 1000.0).strftime("%H:%M:%S.%f")[:-3]

        if kind == "block":
            self.blocks += 1
            what = payload.get("what", "?")
            extra = {k: v for k, v in payload.items() if k not in ("kind", "pid", "t", "what")}
            self.line(f"[{t}] [{label}] BLOCK {what} {json.dumps(extra, ensure_ascii=False)}")
        elif kind == "observe":
            what = payload.get("what", "?")
            extra = {k: v for k, v in payload.items() if k not in ("kind", "pid", "t", "what")}
            self.line(f"[{t}] [{label}] OBSERVE {what} {json.dumps(extra, ensure_ascii=False)}")
        elif kind == "ready":
            self.line(f"[{t}] [{label}] ready (blocker active)")
        elif kind == "warn":
            self.line(f"[{t}] [{label}] WARN {payload.get('msg')}")
        elif kind in ("replaced", "hooked"):
            self.line(f"[{t}] [{label}] {kind} {payload.get('api')}")

    def summary(self) -> None:
        elapsed = max(0.001, time.time() - self.started)
        self.line("")
        self.line("=" * 72)
        self.line(f"blocker log: {self.log_path}")
        self.line(f"duration: {elapsed:.1f}s  blocks: {self.blocks}")
        self.line("expected when the teacher locks: SwitchDesktop BLOCK, low-level hooks BLOCK, lock windows hidden")
        self.line("=" * 72)


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Block the NetSupport School workstation lock locally.")
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = until Ctrl+C / stop file)")
    ap.add_argument("--out", default=None, help="log base path without extension")
    ap.add_argument("--stop-file", default=str(DEFAULT_STOP_FILE), help="graceful stop file")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if args.out:
        base = Path(args.out)
        if base.suffix in (".log", ".jsonl"):
            base = base.with_suffix("")
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        base = ROOT / "logs" / f"nss_lock_blocker_{stamp}"
    base.parent.mkdir(parents=True, exist_ok=True)

    stop_file = Path(args.stop_file)
    with contextlib.suppress(FileNotFoundError):
        stop_file.unlink()

    blocker = Blocker(base)
    print(f"[block] log: {blocker.log_path}", flush=True)
    print(f"[block] jsonl: {blocker.jsonl_path}", flush=True)
    print("[block] targets: session-1 client32.exe / StudentUI.exe (auto-attach)", flush=True)
    if args.duration > 0:
        print(f"[block] duration: {args.duration:.0f}s", flush=True)
    else:
        print("[block] stop with Ctrl+C (or create the stop file)", flush=True)
    print("", flush=True)

    sessions: list[tuple[int, str, object, object]] = []
    attached: set[int] = set()
    last_failed: dict[int, float] = {}
    device = None

    def attach_pid(pid: int, name: str = "") -> bool:
        nonlocal device
        if pid in attached or device is None:
            return False
        now = time.time()
        if now - last_failed.get(pid, 0.0) < 5.0:
            return False
        label = blocker.names.get(pid) or process_label(pid, name or None)
        try:
            session = device.attach(pid)
            script = session.create_script(AGENT)
            script.on("message", blocker.on_message)
            script.load()
        except Exception as exc:
            last_failed[pid] = now
            print(f"[block] attach failed {label}: {type(exc).__name__}: {exc}", flush=True)
            return False
        blocker.names[pid] = label
        sessions.append((pid, label, session, script))
        attached.add(pid)
        print(f"[block] attached {label}", flush=True)
        return True

    try:
        device = frida.get_local_device()
        for pid, name in auto_targets():
            attach_pid(pid, name)

        deadline = time.time() + args.duration if args.duration > 0 else None
        last_scan = 0.0
        while True:
            if deadline is not None and time.time() >= deadline:
                print("[block] duration reached, stopping", flush=True)
                break
            if stop_file.exists():
                print(f"[block] stop file found ({stop_file}), stopping", flush=True)
                break
            if time.time() - last_scan >= 0.3:
                last_scan = time.time()
                for pid, name in auto_targets():
                    attach_pid(pid, name)
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n[block] Ctrl+C, stopping", flush=True)
    finally:
        blocker.close()
        def _force_exit() -> None:
            time.sleep(3)
            os._exit(0)

        threading.Thread(target=_force_exit, daemon=True).start()
        for pid, label, session, script in sessions:
            with contextlib.suppress(Exception):
                script.unload()
            with contextlib.suppress(Exception):
                session.detach()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
