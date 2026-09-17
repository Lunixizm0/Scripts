#!/usr/bin/env bash
#   1) System update (update/full-upgrade/autoclean)
#   2) OpenSSH installation and activation
#   3) Wait for ssh-copy-id
#   4) Distribute the key to ALL users with a home dir (root included)
#   5) Make SSH key-only (also scans sshd_config.d drop-in conflicts)
#   6) ufw firewall (with optional extra ports)
#   7) unattended-upgrades
#   8) Time sync check
#   9) open-vm-tools + open-vm-tools-desktop
#   10) eta-register patch + register
#
# Usage:
#   sudo ./pardus-etap-vm-setup.sh              # real install
#   sudo ./pardus-etap-vm-setup.sh --dry-run    # show what would happen, change nothing
#   sudo ./pardus-etap-vm-setup.sh -h           # help
set -euo pipefail

#args
DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY_RUN=true ;;
        -h|--help)
            echo "Usage: sudo $0 [--dry-run]"
            echo "  --dry-run, -n   Show what would be done without changing anything"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 1
            ;;
    esac
done

# helpers
c_info()  { printf '\033[1;34m[INFO]\033[0m %s\n' "$1"; }
c_ok()    { printf '\033[1;32m[OK]\033[0m %s\n' "$1"; }
c_warn()  { printf '\033[1;33m[WARN]\033[0m %s\n' "$1"; }
c_err()   { printf '\033[1;31m[ERROR]\033[0m %s\n' "$1"; }

run_cmd() {
    local desc="$1"; shift
    if $DRY_RUN; then
        c_info "[DRY-RUN] ${desc}: $*"
        return 0
    fi
    "$@"
}

note_dry_run() { c_info "[DRY-RUN] $1"; }

# If another process holds the apt/dpkg lock wait for it
wait_for_apt_lock() {
    $DRY_RUN && return 0
    local waited=0
    local max_wait=120
    while true; do
        local locked=false
        local lock_holder=""

        # Method 1: check actual lock files via fuser
        if command -v fuser &>/dev/null; then
            if fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then
                locked=true
                lock_holder=$(fuser /var/lib/dpkg/lock-frontend 2>&1 | head -n1 || true)
            elif fuser /var/lib/dpkg/lock >/dev/null 2>&1; then
                locked=true
                lock_holder=$(fuser /var/lib/dpkg/lock 2>&1 | head -n1 || true)
            elif fuser /var/cache/apt/archives/lock >/dev/null 2>&1; then
                locked=true
                lock_holder=$(fuser /var/cache/apt/archives/lock 2>&1 | head -n1 || true)
            elif fuser /var/lib/apt/lists/lock >/dev/null 2>&1; then
                locked=true
                lock_holder=$(fuser /var/lib/apt/lists/lock 2>&1 | head -n1 || true)
            fi
        fi

        # Method 2: fallback to pgrep for apt/apt-get/dpkg 
        if ! $locked; then
            if pgrep -x apt-get >/dev/null 2>&1 || pgrep -x apt >/dev/null 2>&1 || pgrep -x dpkg >/dev/null 2>&1; then
                local pids
                pids=$(pgrep -x "apt-get" 2>/dev/null; pgrep -x "apt" 2>/dev/null; pgrep -x "dpkg" 2>/dev/null | head -n1)
                if [[ -n "$pids" ]]; then
                    # Check if any of those pids actually hold a lock (via /proc/locks or lsof)
                    if command -v lsof &>/dev/null; then
                        if lsof /var/lib/dpkg/lock* /var/cache/apt/archives/lock 2>/dev/null | grep -q "apt\|dpkg"; then
                            locked=true
                            lock_holder="pgrep:$pids"
                        fi
                    else
                        if ! timeout 2 apt-get check >/dev/null 2>&1; then
                            locked=true
                            lock_holder="pgrep:$pids"
                        fi
                    fi
                fi
            fi
        fi

        if ! $locked; then
            break
        fi

        [[ $waited -eq 0 ]] && c_warn "Another apt/dpkg process is holding the lock (holder:$lock_holder), waiting..."
        if (( waited > 0 && waited % 30 == 0 )); then
            c_info "Still waiting for apt lock... (${waited}s) holder:$lock_holder"
            pgrep -a -f 'apt|dpkg' 2>/dev/null | head -n 3 || true
        fi
        sleep 3
        waited=$((waited + 3))
        if (( waited >= max_wait )); then
            c_warn "apt/dpkg has been busy for ${waited}s (holder:$lock_holder)"
            # Check if lock files are actually free now - if so, it's a false positive (e.g. unattended-upgrades daemon)
            if command -v fuser &>/dev/null; then
                if ! fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 && ! fuser /var/lib/dpkg/lock >/dev/null 2>&1 && ! fuser /var/cache/apt/archives/lock >/dev/null 2>&1; then
                    c_info "Lock files are free, continuing despite pgrep (likely false positive from unattended-upgrades daemon)"
                    break
                fi
            fi
            # Try a quick apt-get check to see if apt is actually usable
            if timeout 3 apt-get check >/dev/null 2>&1; then
                c_info "apt-get check succeeded despite lock detection, continuing "
                break
            fi
            c_err "apt/dpkg still busy after ${max_wait}s. Holder:$lock_holder. Check: ps aux | grep -E 'apt|dpkg'; lsof /var/lib/dpkg/lock*"
            c_warn "Continuing anyway (may fail if lock really held) - use --dry-run to skip apt if needed"
            break
        fi
    done
}

# --- Idempotency helpers ---
STATE_DIR="/var/lib/pardus-etap-setup"
is_pkg_installed() { dpkg -s "$1" &>/dev/null; }
is_service_active() { systemctl is-active --quiet "$1" 2>/dev/null; }
is_service_enabled() { systemctl is-enabled --quiet "$1" 2>/dev/null; }
is_stage_done() { [[ -f "$STATE_DIR/$1.done" ]]; }
mark_stage_done() {
    if $DRY_RUN; then
        note_dry_run "Would mark stage $1 as done"
        return 0
    fi
    mkdir -p "$STATE_DIR"
    date -Iseconds > "$STATE_DIR/$1.done" 2>/dev/null || date > "$STATE_DIR/$1.done"
    c_info "Marked stage $1 as done"
}
is_sshd_hardened() {
    grep -qE '^\s*PasswordAuthentication\s+no' "$SSHD_CONFIG" 2>/dev/null && \
    grep -qE '^\s*PubkeyAuthentication\s+yes' "$SSHD_CONFIG" 2>/dev/null && \
    grep -qE '^\s*PermitRootLogin\s+prohibit-password' "$SSHD_CONFIG" 2>/dev/null
}
is_ufw_active() {
    command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q "Status: active"
}
is_time_synced() {
    if command -v timedatectl &>/dev/null; then
        [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo no)" == "yes" ]]
    else
        return 1
    fi
}

# Extracts the "type base64" part (excluding options and comment) from an authorized_keys line
normalize_key() {
    grep -oE '(ssh-rsa|ssh-ed25519|ssh-dss|ecdsa-sha2-[A-Za-z0-9]+|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-[A-Za-z0-9]+@openssh\.com)[[:space:]]+[A-Za-z0-9+/=]+' <<< "$1" | head -n1 || true
}

# Does the given (normalized) key already exist in another file?
key_already_in_file() {
    local norm="$1" file="$2" existing_norm existing_line
    [[ -f "$file" ]] || return 1
    while IFS= read -r existing_line; do
        existing_norm=$(normalize_key "$existing_line")
        if [[ -n "$existing_norm" && "$existing_norm" == "$norm" ]]; then
            return 0
        fi
    done < "$file"
    return 1
}

trap 'c_err "Unexpected error (line $LINENO, exit code $?). See log for details: ${LOGFILE:-unknown}"' ERR
trap 'c_warn "Script interrupted by user (Ctrl+C)."; exit 130' INT TERM

# --- ETA Register helpers (Stage 10) ---
ETA_SRC_DIR="/usr/share/pardus/eta-register/src"
ETA_CHECKS_FILE="$ETA_SRC_DIR/checks.py"
ETA_VMDETECT_FILE="$ETA_SRC_DIR/vm_detect.py"
AHENK_PACKAGE="ahenk"
AHENK_SERVICE="ahenk.service"
ETA_REGISTER_BIN="/usr/bin/eta-register"
ETA_REGISTER_FALLBACK="/usr/share/pardus/eta-register/eta-register"
ETA_STATUS="unknown"
AHENK_STATUS="skipped"

backup_file() {
    local f="$1"
    [[ -f "$f" ]] || return 0
    if grep -q "patched for VM setup" "$f" 2>/dev/null; then
        c_info "$f already patched, skipping backup"
        return 0
    fi
    if $DRY_RUN; then
        note_dry_run "Would backup $f to ${f}.bak.\$(date +%Y%m%d%H%M%S)"
        return 0
    fi
    local bak
    bak="${f}.bak.$(date +%Y%m%d%H%M%S)"
    cp -a "$f" "$bak"
    c_info "Backed up $f -> $bak"
}

patch_eta_register() {
    c_info "Stage 10: Patching eta-register (VM + vendor + user checks)"

    if [[ ! -d "$ETA_SRC_DIR" ]]; then
        c_warn "ETA source not found at $ETA_SRC_DIR, trying to install eta-register..."
        if $DRY_RUN; then
            note_dry_run "Would install eta-register package"
        else
            wait_for_apt_lock
            if ! apt-get install -y eta-register; then
                c_err "Failed to install eta-register, skipping Stage 10"
                ETA_STATUS="error-missing-eta"
                return 1
            fi
        fi
    fi

    if [[ ! -f "$ETA_CHECKS_FILE" || ! -f "$ETA_VMDETECT_FILE" ]]; then
        c_err "ETA source files still missing ($ETA_CHECKS_FILE / $ETA_VMDETECT_FILE), cannot patch"
        ETA_STATUS="error-missing-eta"
        return 1
    fi

    if $DRY_RUN; then
        note_dry_run "Would patch $ETA_CHECKS_FILE (is_vm -> False, is_correct_user -> True, check_touch_vendor -> True)"
        note_dry_run "Would patch $ETA_VMDETECT_FILE (detect_virt -> none, is_vm -> False)"
        return 0
    fi

    # Backup before patching
    backup_file "$ETA_CHECKS_FILE"
    backup_file "$ETA_VMDETECT_FILE"

    # Use python to do robust patching (permanent, idempotent)
    python3 << 'PYPATCH'
import re
import pathlib

checks_path = "/usr/share/pardus/eta-register/src/checks.py"
vm_path = "/usr/share/pardus/eta-register/src/vm_detect.py"

# --- patch checks.py ---
text = pathlib.Path(checks_path).read_text(encoding="utf-8", errors="ignore")
if "patched for VM setup" not in text:
    # 1) is_vm -> always False
    text = re.sub(
        r'def is_vm\(\):\s*\n\s+""".*?"""\s*\n\s+is_vm_status = vm_detect\.is_vm\(\)\s*\n\s+if is_vm_status:\s*\n\s+logger\.error\(.*?\)\s*\n\s+return is_vm_status',
        'def is_vm():\n    # patched for VM setup - VM check disabled\n    return False',
        text,
        flags=re.DOTALL
    )
    # fallback if pattern slightly different
    if 'return is_vm_status' in text:
        text = re.sub(
            r'def is_vm\(\):.*?return is_vm_status',
            'def is_vm():\n    # patched for VM setup - VM check disabled\n    return False',
            text,
            flags=re.DOTALL
        )

    # 2) is_correct_user -> always True
    text = re.sub(
        r'def is_correct_user\(\):.*?return is_correct, current_user',
        'def is_correct_user():\n    # patched for VM setup - user check disabled\n    current_user = get_current_user()\n    return True, current_user',
        text,
        flags=re.DOTALL
    )

    # 3) check_touch_vendor -> always True (replace whole function body until next def)
    # Find check_touch_vendor and replace until next top-level def
    def repl_vendor(m):
        header = m.group(1)
        return header + '    # patched for VM setup - vendor check disabled\n    return True\n'

    text = re.sub(
        r'(def check_touch_vendor\(allowed_vendors\):\s*\n\s+""".*?"""\s*\n)(.*?)(\n(?=def |\nclass |USB_IDS_PATHS))',
        repl_vendor,
        text,
        flags=re.DOTALL
    )
    # if still contains original logic (fallback: simple replace of early return False paths)
    if 'def check_touch_vendor' in text and 'patched for VM setup - vendor check disabled' not in text:
        text = re.sub(
            r'def check_touch_vendor\(allowed_vendors\):.*?return False',
            'def check_touch_vendor(allowed_vendors):\n    # patched for VM setup - vendor check disabled\n    return True',
            text,
            flags=re.DOTALL
        )

    pathlib.Path(checks_path).write_text(text, encoding="utf-8")
    print("patched checks.py")
else:
    print("checks.py already patched")

# --- patch vm_detect.py ---
text2 = pathlib.Path(vm_path).read_text(encoding="utf-8", errors="ignore")
if "patched for VM setup" not in text2:
    # detect_virt -> return "none"
    text2 = re.sub(
        r'def detect_virt\(\):.*?return "none"',
        'def detect_virt():\n    # patched for VM setup - VM check disabled\n    return "none"',
        text2,
        flags=re.DOTALL
    )
    # is_vm -> return False
    text2 = re.sub(
        r'def is_vm\(\):.*?return t != "none"',
        'def is_vm():\n    # patched for VM setup - VM check disabled\n    return False',
        text2,
        flags=re.DOTALL
    )
    # fallback for is_vm if pattern differs
    if 'return t != "none"' in text2:
        text2 = re.sub(
            r'def is_vm\(\):.*?return t != "none"',
            'def is_vm():\n    # patched for VM setup - VM check disabled\n    return False',
            text2,
            flags=re.DOTALL
        )
    pathlib.Path(vm_path).write_text(text2, encoding="utf-8")
    print("patched vm_detect.py")
else:
    print("vm_detect.py already patched")
PYPATCH

    local patch_rc=$?
    if [[ $patch_rc -ne 0 ]]; then
        c_err "Patching eta-register failed (exit $patch_rc)"
        return 1
    fi

    # Verify patch
    if grep -q "patched for VM setup" "$ETA_CHECKS_FILE" && grep -q "patched for VM setup" "$ETA_VMDETECT_FILE"; then
        c_ok "eta-register patched (VM, vendor, user checks disabled) - permanent"
    else
        c_warn "Patch marker not found after patching, check files manually"
        return 1
    fi
}

detect_eta_display() {
    # Robust display detection for VM desktop (etapadmin). Returns 0 if usable display found.
    # Sets DISPLAY/WAYLAND_DISPLAY. Multiple fallbacks for sudo/root vs user session.
    if [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
        # Verify display actually works (socket exists or wayland socket)
        if [[ -n "${DISPLAY:-}" && ! -S "/tmp/.X11-unix/${DISPLAY#:}" && "${DISPLAY}" != ":0" ]] || true; then
            c_info "Display detected from env: DISPLAY=${DISPLAY:-unset} WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-unset}"
            return 0
        elif [[ -n "${DISPLAY:-}" ]]; then
            c_info "Display detected from env: DISPLAY=${DISPLAY:-unset} WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-unset}"
            return 0
        elif [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
            c_info "Wayland display detected from env: WAYLAND_DISPLAY=${WAYLAND_DISPLAY}"
            return 0
        fi
    fi

    local user_to_check="etapadmin"
    if ! id "$user_to_check" &>/dev/null; then
        user_to_check="${SUDO_USER:-root}"
    fi
    local discovered=""

    # Method 0: direct env from user session - try sudo/runuser/su
    if id "$user_to_check" &>/dev/null; then
        for cmd in "sudo -u $user_to_check env" "runuser -u $user_to_check -- env" "su $user_to_check -c env"; do
            if command -v sudo &>/dev/null || command -v runuser &>/dev/null || command -v su &>/dev/null; then
                local env_out
                # shellcheck disable=SC2086
                env_out=$($cmd 2>/dev/null | grep -E '^(DISPLAY|WAYLAND_DISPLAY)=' || true)
                if [[ -n "$env_out" ]]; then
                    local env_disp
                    env_disp=$(echo "$env_out" | grep '^DISPLAY=' | cut -d= -f2- | head -n1)
                    if [[ -n "$env_disp" ]]; then
                        export DISPLAY="$env_disp"
                        c_info "Discovered DISPLAY=$DISPLAY via $cmd"
                        # also capture WAYLAND if present
                        local env_way
                        env_way=$(echo "$env_out" | grep '^WAYLAND_DISPLAY=' | cut -d= -f2- | head -n1 || true)
                        [[ -n "$env_way" ]] && export WAYLAND_DISPLAY="$env_way"
                        return 0
                    fi
                    local env_wayland
                    env_wayland=$(echo "$env_out" | grep '^WAYLAND_DISPLAY=' | cut -d= -f2- | head -n1 || true)
                    if [[ -n "$env_wayland" ]]; then
                        export WAYLAND_DISPLAY="$env_wayland"
                        c_info "Discovered WAYLAND_DISPLAY=$WAYLAND_DISPLAY via $cmd"
                        return 0
                    fi
                fi
            fi
        done
    fi

    # Method 1: loginctl (seat/session)
    if command -v loginctl &>/dev/null; then
        local session
        session=$(loginctl list-sessions --no-legend 2>/dev/null | awk -v u="$user_to_check" '$3==u {print $1; exit}' || true)
        # fallback: any active graphical session
        if [[ -z "$session" ]]; then
            session=$(loginctl list-sessions --no-legend 2>/dev/null | awk '$5=="seat0" {print $1; exit}' || true)
        fi
        if [[ -n "$session" ]]; then
            local sess_disp
            sess_disp=$(loginctl show-session "$session" -p Display 2>/dev/null | cut -d= -f2 || true)
            if [[ -n "$sess_disp" ]]; then
                export DISPLAY="$sess_disp"
                c_info "Discovered DISPLAY=$DISPLAY via loginctl session $session"
                return 0
            fi
            # Try Type wayland?
            local sess_type
            sess_type=$(loginctl show-session "$session" -p Type 2>/dev/null | cut -d= -f2 || true)
            if [[ "$sess_type" == "wayland" ]]; then
                # try to find wayland socket
                local uid
                uid=$(id -u "$user_to_check" 2>/dev/null || echo "")
                if [[ -n "$uid" && -S "/run/user/$uid/wayland-0" ]]; then
                    export WAYLAND_DISPLAY="wayland-0"
                    c_info "Discovered WAYLAND_DISPLAY=$WAYLAND_DISPLAY via loginctl"
                    return 0
                fi
            fi
        fi
    fi

    # Method 2: who / w
    if command -v who &>/dev/null; then
        local who_disp
        who_disp=$(who 2>/dev/null | grep -E "$user_to_check.*\(:[0-9]" | grep -oE ":[0-9]+(\.[0-9]+)?" | head -n1 || true)
        if [[ -n "$who_disp" ]]; then
            export DISPLAY="$who_disp"
            c_info "Discovered DISPLAY=$DISPLAY via who"
            return 0
        fi
    fi

    # Method 3: scan /proc/*/environ for DISPLAY
    if command -v pgrep &>/dev/null; then
        # First try specific desktop processes
        for pattern in "cinnamon-session" "gnome-session" "plasmashell" "cinnamon"; do
            local pid
            pid=$(pgrep -u "$user_to_check" -f "$pattern" 2>/dev/null | head -n1 || true)
            if [[ -n "$pid" && -f "/proc/$pid/environ" ]]; then
                local env_disp
                env_disp=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep '^DISPLAY=' | cut -d= -f2- | head -n1 || true)
                if [[ -n "$env_disp" ]]; then
                    export DISPLAY="$env_disp"
                    c_info "Discovered DISPLAY=$DISPLAY from $pattern ($pid)"
                    return 0
                fi
            fi
        done
        # Fallback: scan all user pids
        for pid in $(pgrep -u "$user_to_check" 2>/dev/null | head -n 50); do
            if [[ -f "/proc/$pid/environ" ]]; then
                local env_disp
                env_disp=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep '^DISPLAY=' | cut -d= -f2- | head -n1 || true)
                if [[ -n "$env_disp" ]]; then
                    discovered="$env_disp"
                    break
                fi
                local env_wayland
                env_wayland=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep '^WAYLAND_DISPLAY=' | cut -d= -f2- | head -n1 || true)
                if [[ -n "$env_wayland" ]]; then
                    discovered="wayland:$env_wayland"
                    export WAYLAND_DISPLAY="$env_wayland"
                    break
                fi
            fi
        done
        if [[ -n "$discovered" && "$discovered" != wayland:* ]]; then
            export DISPLAY="$discovered"
            c_info "Discovered DISPLAY=$DISPLAY from $user_to_check session (scan)"
            return 0
        elif [[ "$discovered" == wayland:* ]]; then
            c_info "Discovered WAYLAND_DISPLAY=$WAYLAND_DISPLAY from $user_to_check session (scan)"
            return 0
        fi
    fi

    # Method 4: check common sockets
    if [[ -S /tmp/.X11-unix/X0 ]]; then
        export DISPLAY=":0"
        c_warn "No DISPLAY in env, but /tmp/.X11-unix/X0 exists, trying DISPLAY=:0"
        # Verify XAUTHORITY will be set later; still return success
        return 0
    fi
    if [[ -S /tmp/.X11-unix/X1 ]]; then
        export DISPLAY=":1"
        c_warn "Trying DISPLAY=:1"
        return 0
    fi
    # Wayland socket
    local uid_wl
    uid_wl=$(id -u "$user_to_check" 2>/dev/null || echo "")
    if [[ -n "$uid_wl" && -S "/run/user/$uid_wl/wayland-0" ]]; then
        export WAYLAND_DISPLAY="wayland-0"
        export XDG_RUNTIME_DIR="/run/user/$uid_wl"
        c_info "Discovered wayland socket /run/user/$uid_wl/wayland-0"
        return 0
    fi

    c_warn "No graphical display detected (DISPLAY and WAYLAND_DISPLAY empty, user=$user_to_check)"
    c_warn "Script is designed to run directly inside VM desktop session (etapadmin)."
    c_warn "If you are on console, run from desktop terminal or set DISPLAY manually (e.g. DISPLAY=:0 sudo -u etapadmin $0)"
    # Debug help
    c_info "Debug: who=$(who 2>&1 | head -n1 || true) loginctl=$(loginctl list-sessions --no-legend 2>&1 | head -n1 || true) X0=$(stat -c '%A %n' /tmp/.X11-unix/X0 2>&1 | head -n1 || echo 'X0 missing')"
    return 1
}

check_eta_registration_status() {
    # Sets global ETA_STATUS and echoes it. Returns 0 on known status, 1 on error.
    # Requires patched checks.py (VM/vendor off) but works even unpatched for registered check.
    if $DRY_RUN; then
        note_dry_run "Would check ETA registration via BACKEND_URL (board/check?mac=...)" >&2
        ETA_STATUS="registered"
        echo "$ETA_STATUS"
        return 0
    fi

    if [[ ! -f "$ETA_CHECKS_FILE" ]]; then
        c_err "Cannot check registration: $ETA_CHECKS_FILE missing"
        ETA_STATUS="error-missing-eta"
        echo "$ETA_STATUS"
        return 1
    fi

    local result
    result=$(python3 << 'PYCHECK' 2>&1
import sys
sys.path.insert(0, "/usr/share/pardus/eta-register/src")
import os
os.chdir("/usr/share/pardus/eta-register/src")
try:
    from checks import get_device_check_url, interpret_device_status, ConnectionError
    import requests, urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    import time
    url = get_device_check_url()
    if not url:
        print("error-no-mac")
        sys.exit(0)
    # 3 retries with 5s delay (lighter than dispatcher 30*20s)
    last_err = None
    for attempt in range(3):
        try:
            from config import SECURE_HEADER
            resp = requests.get(url, headers=SECURE_HEADER, timeout=10, verify=False)
            try:
                body = resp.json()
            except ValueError:
                print("error-server-issue")
                sys.exit(0)
            try:
                res = interpret_device_status(resp.status_code, body)
                if res.get("registered"):
                    print("registered")
                else:
                    print("not-registered")
                sys.exit(0)
            except ConnectionError:
                # interpret treats non-200 without registered key as connection error
                print("error-no-connection")
                sys.exit(0)
        except requests.RequestException as e:
            last_err = e
            if attempt < 2:
                time.sleep(5)
                continue
            print("error-no-connection")
            sys.exit(0)
        except Exception as e:
            print("error-no-connection")
            sys.exit(0)
    print("error-no-connection")
except Exception as e:
    # fallback if imports fail (e.g. etainfo missing)
    print(f"error-no-connection")
PYCHECK
)
    # python may have printed with extra logs, take last line
    result=$(echo "$result" | tail -n1 | tr -d '\r' | xargs)
    ETA_STATUS="$result"
    echo "$ETA_STATUS"
    case "$ETA_STATUS" in
        registered|not-registered) return 0 ;;
        *) return 1 ;;
    esac
}

launch_eta_register_once() {
    c_info "Launching eta-register for registration (single launch, patch applied)..."
    if $DRY_RUN; then
        note_dry_run "Would launch $ETA_REGISTER_BIN (or fallback $ETA_REGISTER_FALLBACK) as etapadmin with DISPLAY=${DISPLAY:-:0}"
        return 0
    fi

    local eta_bin="$ETA_REGISTER_BIN"
    if [[ ! -x "$eta_bin" && -x "$ETA_REGISTER_FALLBACK" ]]; then
        eta_bin="$ETA_REGISTER_FALLBACK"
    fi
    if [[ ! -x "$eta_bin" ]]; then
        c_err "eta-register binary not found ($ETA_REGISTER_BIN nor $ETA_REGISTER_FALLBACK)"
        return 1
    fi

    local launch_user="etapadmin"
    if ! id "$launch_user" &>/dev/null; then
        c_warn "etapadmin user not found, will launch as $SUDO_USER or root"
        launch_user="${SUDO_USER:-root}"
        if ! id "$launch_user" &>/dev/null; then
            launch_user="root"
        fi
    fi

    local launch_disp="${DISPLAY:-:0}"
    local launch_wayland="${WAYLAND_DISPLAY:-}"
    local launch_xauth="${XAUTHORITY:-}"
    if [[ -z "$launch_xauth" ]]; then
        if [[ -f "/home/$launch_user/.Xauthority" ]]; then
            launch_xauth="/home/$launch_user/.Xauthority"
        elif [[ -f "$HOME/.Xauthority" ]]; then
            launch_xauth="$HOME/.Xauthority"
        fi
    fi
    local xdg_runtime
    xdg_runtime="/run/user/$(id -u "$launch_user" 2>/dev/null || echo 0)"
    local dbus_addr
    dbus_addr="unix:path=$xdg_runtime/bus"

    c_info "Launching as $launch_user with DISPLAY=$launch_disp WAYLAND_DISPLAY=${launch_wayland:-unset} XAUTHORITY=${launch_xauth:-unset}"

    # Build env prefix for sudo
    local env_args=()
    env_args+=(DISPLAY="$launch_disp")
    [[ -n "$launch_wayland" ]] && env_args+=(WAYLAND_DISPLAY="$launch_wayland")
    [[ -n "$launch_xauth" && -f "$launch_xauth" ]] && env_args+=(XAUTHORITY="$launch_xauth")
    [[ -d "$xdg_runtime" ]] && env_args+=(XDG_RUNTIME_DIR="$xdg_runtime")
    [[ -S "$xdg_runtime/bus" ]] && env_args+=(DBUS_SESSION_BUS_ADDRESS="$dbus_addr")

    local rc=0
    if [[ "$launch_user" == "root" ]]; then
        env "${env_args[@]}" "$eta_bin" || rc=$?
    else
        # Use sudo -u to preserve desktop session; fallback to runuser
        if command -v sudo &>/dev/null; then
            sudo -u "$launch_user" env "${env_args[@]}" "$eta_bin" || rc=$?
        elif command -v runuser &>/dev/null; then
            runuser -u "$launch_user" -- env "${env_args[@]}" "$eta_bin" || rc=$?
        else
            su "$launch_user" -c "env ${env_args[*]} $eta_bin" || rc=$?
        fi
    fi

    if [[ $rc -eq 0 ]]; then
        c_ok "eta-register exited cleanly (user completed registration flow, may have triggered installer)"
    else
        c_warn "eta-register exited with code $rc (user may have closed window)"
    fi
    return $rc
}

install_and_enable_ahenk() {
    c_info "Installing and enabling ahenk (LiderAhenk agent)..."
    if $DRY_RUN; then
        note_dry_run "Would install $AHENK_PACKAGE and enable $AHENK_SERVICE"
        AHENK_STATUS="would-install"
        return 0
    fi

    wait_for_apt_lock
    if dpkg -l 2>/dev/null | grep -q "^ii.* $AHENK_PACKAGE "; then
        c_ok "$AHENK_PACKAGE already installed"
        AHENK_STATUS="already-installed"
    else
        if ! apt-get install -y "$AHENK_PACKAGE"; then
            c_err "Failed to install $AHENK_PACKAGE (check apt sources and network)"
            AHENK_STATUS="install-failed"
            return 1
        fi
        c_ok "$AHENK_PACKAGE installed"
        AHENK_STATUS="installed"
    fi

    if systemctl enable --now "$AHENK_SERVICE" 2>&1 | tee -a "$LOGFILE"; then
        c_ok "$AHENK_SERVICE enabled and started"
        AHENK_STATUS="${AHENK_STATUS}+active"
    else
        c_warn "systemctl enable --now $AHENK_SERVICE failed, trying enable only"
        systemctl enable "$AHENK_SERVICE" || true
        systemctl restart "$AHENK_SERVICE" || true
        if systemctl is-active --quiet "$AHENK_SERVICE"; then
            c_ok "$AHENK_SERVICE is active after retry"
            AHENK_STATUS="${AHENK_STATUS}+active"
        else
            c_warn "$AHENK_SERVICE not active, check: systemctl status $AHENK_SERVICE"
            AHENK_STATUS="${AHENK_STATUS}+inactive"
        fi
    fi

    # Verify
    if dpkg -l 2>/dev/null | grep -q "^ii.* $AHENK_PACKAGE "; then
        c_ok "Verified: $AHENK_PACKAGE installed"
    else
        c_err "Verification failed: $AHENK_PACKAGE not installed"
        AHENK_STATUS="verify-failed"
        return 1
    fi
}

# preflight
if [[ $EUID -ne 0 ]]; then
    c_err "This script must be run as root (use sudo)"
    exit 1
fi

if ! command -v apt-get &>/dev/null; then
    c_err "apt-get not found This script only works on Debian-based systems (especially Pardus)"
    exit 1
fi

if ! command -v systemctl &>/dev/null; then
    c_err "systemctl not found This script requires systemd"
    exit 1
fi

LOGFILE="/var/log/pardus-etap-setup.log"
touch "$LOGFILE" 2>/dev/null || LOGFILE="/tmp/pardus-etap-setup.log"
exec > >(tee -a "$LOGFILE") 2>&1
c_info "Log file: $LOGFILE"
if ! $DRY_RUN; then
    mkdir -p "$STATE_DIR" 2>/dev/null || true
    c_info "State dir: $STATE_DIR"
fi

if $DRY_RUN; then
    c_warn "DRY-RUN mode active: NO system changes will be made"
fi

APT_OPTS=(-o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold")
SSHD_CONFIG="/etc/ssh/sshd_config"
SSHD_CONFIG_BACKUP="/etc/ssh/sshd_config.bak.$(date +%Y%m%d%H%M%S)"

# system update
c_info "Stage 1: System update"
if ! $DRY_RUN && is_stage_done stage1; then
    # Check if still up-to-date (no upgrades) - if so skip
    if timeout 10 apt-get -s full-upgrade 2>/dev/null | grep -qE "0 upgraded, 0 newly installed"; then
        c_ok "Stage 1 already done (system up-to-date), skipping"
    else
        c_info "Stage 1 marker exists but upgrades pending, re-running"
        wait_for_apt_lock
        run_cmd "apt-get update" apt-get update -y
        wait_for_apt_lock
        run_cmd "apt-get full-upgrade" apt-get "${APT_OPTS[@]}" full-upgrade -y
        wait_for_apt_lock
        run_cmd "apt-get autoclean" apt-get autoclean -y
        c_ok "System update complete"
        mark_stage_done stage1
    fi
elif $DRY_RUN && is_stage_done stage1; then
    c_info "[DRY-RUN] Stage 1 would be skipped (already done, idempotent)"
else
    wait_for_apt_lock
    run_cmd "apt-get update" apt-get update -y
    wait_for_apt_lock
    run_cmd "apt-get full-upgrade" apt-get "${APT_OPTS[@]}" full-upgrade -y
    wait_for_apt_lock
    run_cmd "apt-get autoclean" apt-get autoclean -y
    c_ok "System update complete"
    mark_stage_done stage1
fi

# openssh install and activation
c_info "Stage 2: OpenSSH installation"
if ! $DRY_RUN && is_pkg_installed openssh-server && is_pkg_installed inotify-tools && { is_service_active ssh || is_service_active sshd; }; then
    c_ok "Stage 2 already done (openssh-server/inotify-tools installed and ssh active), skipping"
    SSH_SERVICE="ssh"
    if ! systemctl list-unit-files --type=service 2>/dev/null | grep -qE '^ssh\.service'; then
        if systemctl list-unit-files --type=service 2>/dev/null | grep -qE '^sshd\.service'; then
            SSH_SERVICE="sshd"
        fi
    fi
    c_info "SSH service name in use: $SSH_SERVICE (already active)"
    mark_stage_done stage2
elif $DRY_RUN && is_pkg_installed openssh-server 2>/dev/null; then
    c_info "[DRY-RUN] Stage 2 would be skipped (already installed, idempotent)"
    SSH_SERVICE="ssh"
    c_info "SSH service name in use: $SSH_SERVICE"
else
    wait_for_apt_lock
    run_cmd "Installing openssh-server and inotify-tools" apt-get install -y openssh-server inotify-tools

    SSH_SERVICE="ssh"
    if ! $DRY_RUN; then
        if ! systemctl list-unit-files --type=service 2>/dev/null | grep -qE '^ssh\.service'; then
            if systemctl list-unit-files --type=service 2>/dev/null | grep -qE '^sshd\.service'; then
                SSH_SERVICE="sshd"
            fi
        fi
    fi
    c_info "SSH service name in use: $SSH_SERVICE"

    run_cmd "Enabling the SSH service" systemctl enable --now "$SSH_SERVICE"
    c_ok "SSH service active (or would be activated in dry-run)"
    mark_stage_done stage2
fi

c_info "IPv4 addresses of this machine:"
ip -4 -o addr show scope global 2>/dev/null | awk '{print "  - " $4}' || true

FIRST_IP=$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1 || true)
if [[ -z "$FIRST_IP" ]]; then
    c_warn "No IPv4 address found IPv6 addresses:"
    ip -6 -o addr show scope global 2>/dev/null | awk '{print "  - " $4}' || true
    FIRST_IP="<VM_IP_ADDRESS>"
    c_warn "Replace <VM_IP_ADDRESS> in the examples below with the real address"
fi

# wait for keys
c_info "Stage 3: Waiting for SSH key"
SKIP_SSH_KEY_STAGES=false
if ! $DRY_RUN && is_stage_done stage3 && is_stage_done stage4; then
    if [[ -s /home/etapadmin/.ssh/authorized_keys ]] && grep -qE 'ssh-(rsa|ed25519|dss)|ecdsa' /home/etapadmin/.ssh/authorized_keys 2>/dev/null; then
        # Check distribution: count users with authorized_keys
        _distributed=true
        while IFS=: read -r uname _ uid gid _ uhome ushell; do
            if [[ "$uname" != "root" && "$uid" -lt 1000 ]]; then continue; fi
            if [[ ! -d "$uhome" ]]; then continue; fi
            if [[ "$uname" != "root" && "$ushell" =~ (nologin|false)$ ]]; then continue; fi
            if [[ ! -s "$uhome/.ssh/authorized_keys" ]]; then _distributed=false; break; fi
        done < <(getent passwd)
        if $_distributed; then
            c_ok "Stages 3/4 already done (keys exist and distributed to all users), skipping"
            SKIP_SSH_KEY_STAGES=true
        fi
    fi
elif $DRY_RUN && is_stage_done stage3 && is_stage_done stage4; then
    c_info "[DRY-RUN] Stages 3/4 would be skipped (already done)"
    SKIP_SSH_KEY_STAGES=true
fi
if [[ "$SKIP_SSH_KEY_STAGES" == "true" ]]; then
    # Load PUB_KEYS from existing file for later stages if needed (no-op)
    c_info "Skipping SSH key wait and distribution"
else
DEFAULT_USER="${SUDO_USER:-}"

TARGET_USER=""
while true; do
    read -rp "Target username the public key should be added for [${DEFAULT_USER}]: " INPUT_USER
    TARGET_USER="${INPUT_USER:-$DEFAULT_USER}"
    if [[ -z "$TARGET_USER" ]]; then
        c_err "Username cannot be empty"
        continue
    fi
    if ! id "$TARGET_USER" &>/dev/null; then
        c_err "User not found: $TARGET_USER"
        continue
    fi
    break
done

TARGET_HOME=$(getent passwd "$TARGET_USER" | head -n1 | cut -d: -f6)
TARGET_SHELL=$(getent passwd "$TARGET_USER" | head -n1 | cut -d: -f7)

if [[ -z "$TARGET_HOME" || ! -d "$TARGET_HOME" && "$DRY_RUN" == "false" ]]; then
    c_warn "Home directory for $TARGET_USER ($TARGET_HOME) does not exist yet, it will be created."
fi
if [[ "$TARGET_SHELL" =~ (nologin|false)$ ]]; then
    c_warn "$TARGET_USER's shell is '$TARGET_SHELL' - this user normally cannot log in interactively over SSH"
fi

TARGET_SSH_DIR="$TARGET_HOME/.ssh"
TARGET_AUTH_KEYS="$TARGET_SSH_DIR/authorized_keys"

if [[ -L "$TARGET_SSH_DIR" || -L "$TARGET_AUTH_KEYS" ]]; then
    c_warn "$TARGET_SSH_DIR or $TARGET_AUTH_KEYS is a symlink."
fi

if $DRY_RUN; then
    note_dry_run "mkdir -p $TARGET_SSH_DIR ; chmod 700 ; chown $TARGET_USER"
    note_dry_run "touch $TARGET_AUTH_KEYS ; chmod 600 ; chown $TARGET_USER"
else
    mkdir -p "$TARGET_SSH_DIR"
    chown "$TARGET_USER:$TARGET_USER" "$TARGET_SSH_DIR"
    chmod 700 "$TARGET_SSH_DIR"
    touch "$TARGET_AUTH_KEYS"
    chown "$TARGET_USER:$TARGET_USER" "$TARGET_AUTH_KEYS"
    chmod 600 "$TARGET_AUTH_KEYS"
fi

cat <<EOF

RUN THE FOLLOWING COMMAND FROM YOUR OWN MACHINE

  Linux / macOS:
    ssh-copy-id ${TARGET_USER}@${FIRST_IP}

  If ssh-copy-id is not available (Linux/macOS alternative):
    cat ~/.ssh/id_ed25519.pub | ssh ${TARGET_USER}@${FIRST_IP} \\
        "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"

  Windows (PowerShell):
    type \$env:USERPROFILE\\.ssh\\id_ed25519.pub | ssh ${TARGET_USER}@${FIRST_IP} \`
        "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"

    (If you use an RSA key, replace id_ed25519.pub with id_rsa.pub.)

EOF

extract_pub_keys() {
    # $1: path to authorized_keys to writes raw lines into the PUB_KEYS array
    PUB_KEYS=()
    [[ -f "$1" ]] || return 0
    local line norm
    while IFS= read -r line; do
        [[ -z "${line// /}" ]] && continue
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        norm=$(normalize_key "$line")
        [[ -n "$norm" ]] && PUB_KEYS+=("$line")
    done < "$1"
}

wait_for_key_change() {
    c_info "Waiting... (watching $TARGET_SSH_DIR, Ctrl+C to cancel)"
    inotifywait -e create -e modify -e close_write -e moved_to --quiet "$TARGET_SSH_DIR" >/dev/null 2>&1 || true
    # Short wait + retry so we dont read a partially-written file
    local tries=0
    sleep 1
    while [[ ! -s "$TARGET_AUTH_KEYS" ]] && (( tries < 5 )); do
        sleep 1
        tries=$((tries + 1))
    done
}

PUB_KEYS=()
if $DRY_RUN; then
    note_dry_run "Would watch $TARGET_SSH_DIR with inotifywait; no real key is waited for in dry-run"
    PUB_KEYS=("ssh-ed25519 AAAA...SAMPLE-DRY-RUN-KEY ${TARGET_USER}@example")
else
    extract_pub_keys "$TARGET_AUTH_KEYS"
    if [[ ${#PUB_KEYS[@]} -gt 0 ]]; then
        c_warn "$TARGET_AUTH_KEYS already contains ${#PUB_KEYS[@]} valid key"
        read -rp "Wait for a new key as well? (y/N): " WAIT_NEW
        if [[ "${WAIT_NEW,,}" == "y" || "${WAIT_NEW,,}" == "yes" ]]; then
            wait_for_key_change
            extract_pub_keys "$TARGET_AUTH_KEYS"
        else
            c_info "Using the existing key(s), skipping the wait"
        fi
    else
        wait_for_key_change
        extract_pub_keys "$TARGET_AUTH_KEYS"
    fi
fi

if [[ ${#PUB_KEYS[@]} -eq 0 ]]; then
    c_err "No valid public key found ($TARGET_AUTH_KEYS is empty or in an unrecognized format)"
    exit 1
fi
c_ok "${#PUB_KEYS[@]} key(s) will be used"
    mark_stage_done stage3
fi

# distrubate the keys
if [[ "${SKIP_SSH_KEY_STAGES:-false}" == "true" ]]; then
    c_info "Stage 4: Key distribution (already done, skipped)"
else
c_info "Stage 4: Key distributio"

while IFS=: read -r uname _ uid gid _ uhome ushell; do
    if [[ "$uname" != "root" && "$uid" -lt 1000 ]]; then
        continue
    fi
    if [[ ! -d "$uhome" ]]; then
        continue
    fi
    if [[ "$uname" != "root" && "$ushell" =~ (nologin|false)$ ]]; then
        continue
    fi

    USSH_DIR="$uhome/.ssh"
    UAUTH_KEYS="$USSH_DIR/authorized_keys"

    if [[ -L "$USSH_DIR" ]]; then
        c_warn "$uname: $USSH_DIR is a symlink, skipping."
        continue
    fi

    if $DRY_RUN; then
        for key_line in "${PUB_KEYS[@]}"; do
            note_dry_run "$uname: key would be added (if not already present) -> $UAUTH_KEYS"
        done
        continue
    fi

    mkdir -p "$USSH_DIR"
    touch "$UAUTH_KEYS"

    ADDED=0
    for key_line in "${PUB_KEYS[@]}"; do
        norm=$(normalize_key "$key_line")
        [[ -z "$norm" ]] && continue
        if key_already_in_file "$norm" "$UAUTH_KEYS"; then
            continue
        fi
        echo "$key_line" >> "$UAUTH_KEYS"
        ADDED=$((ADDED + 1))
    done

    if [[ $ADDED -gt 0 ]]; then
        c_ok "$uname: $ADDED key(s) added"
    else
        c_info "$uname: no new keys to add"
    fi

    chown -R "$uname:$gid" "$USSH_DIR"
    chmod 700 "$USSH_DIR"
    chmod 600 "$UAUTH_KEYS"
done < <(getent passwd)

c_ok "Key distribution complete (or simulated in dry-run)"
    mark_stage_done stage4
fi

# make ssh key only
c_info "Stage 5: SSH hardening"
if ! $DRY_RUN && is_stage_done stage5 && is_sshd_hardened; then
    c_ok "Stage 5 already done (SSH already hardened), skipping"
elif $DRY_RUN && is_stage_done stage5; then
    c_info "[DRY-RUN] Stage 5 would be skipped (already done)"
else

set_sshd_option() {
    local key="$1" value="$2"
    if $DRY_RUN; then
        note_dry_run "sshd_config: would set ${key} to ${value}"
        return 0
    fi
    if grep -qE "^\s*#?\s*${key}\s+" "$SSHD_CONFIG"; then
        sed -i "s|^\s*#\?\s*${key}\s\+.*|${key} ${value}|" "$SSHD_CONFIG"
    else
        echo "${key} ${value}" >> "$SSHD_CONFIG"
    fi
}

if $DRY_RUN; then
    note_dry_run "sshd_config would be backed up to: $SSHD_CONFIG_BACKUP"
else
    cp "$SSHD_CONFIG" "$SSHD_CONFIG_BACKUP"
    c_info "sshd_config backed up: $SSHD_CONFIG_BACKUP"
fi

set_sshd_option "PubkeyAuthentication" "yes"
set_sshd_option "PasswordAuthentication" "no"
set_sshd_option "ChallengeResponseAuthentication" "no"
set_sshd_option "PermitRootLogin" "prohibit-password"
set_sshd_option "UsePAM" "yes"

# On Debian 11+/Ubuntu 22.04+, sshd_config.d/*.conf drop-in files are read
# BEFORE the main sshd_config via Include and sshd uses the FIRST value it
# finds for each keyword a conflicting setting in a drop-in file can
# silently override ours So we scan and neutralize those too.
SSHD_DROPIN_DIR="/etc/ssh/sshd_config.d"
if $DRY_RUN; then
    note_dry_run "Would scan *.conf files under $SSHD_DROPIN_DIR for conflicting settings"
else
    if [[ -d "$SSHD_DROPIN_DIR" ]]; then
        for f in "$SSHD_DROPIN_DIR"/*.conf; do
            [[ -e "$f" ]] || continue
            for key in PasswordAuthentication PubkeyAuthentication ChallengeResponseAuthentication PermitRootLogin; do
                if grep -qiE "^\s*${key}\s+" "$f"; then
                    c_warn "$f contains a '$key' setting it could have overridden our main sshd_config setting"
                    cp "$f" "${f}.bak.$(date +%Y%m%d%H%M%S)"
                    sed -i -E "s|^([[:space:]]*)(${key}[[:space:]]+.*)|\1# [disabled by pardus-vm-setup] \2|I" "$f"
                    c_info "Commented out the '$key' line in $f"
                fi
            done
        done
    fi
fi

if $DRY_RUN; then
    note_dry_run "Would run sshd -t and restart the '$SSH_SERVICE' service"
else
    c_info "Testing the sshd config..."
    if ! sshd -t; then
        c_err "sshd_config is invalid. Restoring backup: $SSHD_CONFIG_BACKUP"
        cp "$SSHD_CONFIG_BACKUP" "$SSHD_CONFIG"
        exit 1
    fi
    systemctl restart "$SSH_SERVICE"
    c_ok "SSH now accepts key-only authentication"
    c_warn "Before closing this terminal test that you can log in with your key from a NEW terminal"
fi
    mark_stage_done stage5
fi

# ufw setup
c_info "Stage 6: ufw firewall"
if ! $DRY_RUN && is_stage_done stage6 && is_ufw_active; then
    c_ok "Stage 6 already done (ufw active), skipping"
elif $DRY_RUN && is_stage_done stage6; then
    c_info "[DRY-RUN] Stage 6 would be skipped (already done)"
else
wait_for_apt_lock
run_cmd "Installing ufw" apt-get install -y ufw

read -rp "Any extra ports to open besides SSH? (comma-separated, leave empty for none, e.g. 80,443,5222): " EXTRA_PORTS

if $DRY_RUN; then
    note_dry_run "Would set ufw default deny incoming / allow outgoing"
    note_dry_run "Would add ufw allow OpenSSH"
    [[ -n "$EXTRA_PORTS" ]] && note_dry_run "Would open extra ports: $EXTRA_PORTS"
    note_dry_run "Would run ufw --force enable"
else
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    ufw allow OpenSSH >/dev/null
    if [[ -n "$EXTRA_PORTS" ]]; then
        IFS=',' read -ra PORT_ARR <<< "$EXTRA_PORTS"
        for p in "${PORT_ARR[@]}"; do
            p_trimmed="${p// /}"
            [[ -z "$p_trimmed" ]] && continue
            if ufw allow "$p_trimmed" >/dev/null 2>&1; then
                c_ok "Port opened: $p_trimmed"
            else
                c_warn "Could not open port, check the format: $p_trimmed"
            fi
        done
    fi
    ufw --force enable
    c_ok "ufw is active. Status:"
    ufw status verbose
fi
    mark_stage_done stage6
fi

# auto updates
c_info "Stage 7: unattended-upgrades"
if ! $DRY_RUN && is_stage_done stage7 && is_pkg_installed unattended-upgrades && is_service_enabled unattended-upgrades; then
    c_ok "Stage 7 already done (unattended-upgrades installed and enabled), skipping"
elif $DRY_RUN && is_stage_done stage7; then
    c_info "[DRY-RUN] Stage 7 would be skipped (already done)"
else
wait_for_apt_lock
run_cmd "Installing unattended-upgrades" apt-get install -y unattended-upgrades apt-listchanges

AUTO_UPGRADES_FILE="/etc/apt/apt.conf.d/20auto-upgrades"
AUTO_UPGRADES_CONTENT='APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::AutocleanInterval "7";
APT::Periodic::Unattended-Upgrade "1";'

if $DRY_RUN; then
    note_dry_run "$AUTO_UPGRADES_FILE would be created with the following content:"
    echo "$AUTO_UPGRADES_CONTENT"
else
    echo "$AUTO_UPGRADES_CONTENT" > "$AUTO_UPGRADES_FILE"
    systemctl enable --now unattended-upgrades
    c_ok "unattended-upgrades active; periodic security updates are on"
    c_info "Note: automatic reboot is left disabled by default"
    c_info "      To enable it: /etc/apt/apt.conf.d/50unattended-upgrades  Unattended-Upgrade::Automatic-Reboot"
fi
    mark_stage_done stage7
fi

# time sync
c_info "Stage 8: Time synchronization"
if ! $DRY_RUN && is_stage_done stage8 && is_time_synced; then
    c_ok "Stage 8 already done (time synced), skipping"
elif $DRY_RUN && is_stage_done stage8; then
    c_info "[DRY-RUN] Stage 8 would be skipped (already done)"
else

TIME_SYNCED="unknown"
if command -v timedatectl &>/dev/null; then
    TIME_SYNCED=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo "unknown")
fi

if [[ "$TIME_SYNCED" == "yes" ]]; then
    c_ok "System clock is already synchronized (NTP)"
    timedatectl status 2>/dev/null | sed -n '1,6p' || true
else
    c_warn "System clock does not appear to be synchronized (NTPSynchronized=$TIME_SYNCED)"
    if $DRY_RUN; then
        note_dry_run "Would check whether chrony is installed enable it if so otherwise install systemd-timesyncd"
    elif dpkg -s chrony &>/dev/null; then
        c_info "chrony is already installed skipping systemd-timesyncd to avoid a conflict"
        systemctl enable --now chrony
    elif systemctl is-active --quiet systemd-timesyncd; then
        c_info "systemd-timesyncd is already running sync may just take a moment"
    else
        c_info "No time-sync service found, installing systemd-timesyncd..."
        wait_for_apt_lock
        run_cmd "Installing systemd-timesyncd" apt-get install -y systemd-timesyncd
        run_cmd "Enabling systemd-timesyncd" systemctl enable --now systemd-timesyncd
    fi
    if ! $DRY_RUN; then
        sleep 2
        timedatectl status 2>/dev/null | sed -n '1,6p' || true
    fi
fi
    mark_stage_done stage8
fi

#openvmtools
c_info "Stage 9: open-vm-tools"
SKIP_VMTOOLS=false
if is_stage_done stage9; then
    if [[ "$DRY_RUN" == "false" ]]; then
        c_ok "Stage 9 already done (marker exists), skipping"
    else
        c_info "[DRY-RUN] Stage 9 would be skipped (already done, idempotent)"
    fi
    SKIP_VMTOOLS=true
fi
if [[ "$SKIP_VMTOOLS" == "true" ]]; then
    c_info "Skipping open-vm-tools installation (already done, idempotent)"
else
VIRT="unknown"
if command -v systemd-detect-virt &>/dev/null; then
    VIRT=$(systemd-detect-virt 2>/dev/null || echo "none")
fi
c_info "Detected virtualization platform: $VIRT"

if [[ "$VIRT" != "vmware" ]]; then
    c_warn "This machine doesn't appear to be running on VMware (detected: $VIRT)."
    case "$VIRT" in
        kvm|qemu)      c_warn "  Suggestion: 'qemu-guest-agent' may be a better fit than open-vm-tools" ;;
        oracle)        c_warn "  Suggestion: 'virtualbox-guest-utils' may be a better fit than open-vm-tools" ;;
        microsoft)     c_warn "  Suggestion: for Hyper-V, the in-kernel hv_* drivers are usually enough" ;;
        *) ;;
    esac
    if $DRY_RUN; then
        note_dry_run "Since this isnt VMware, would ask whether to install open-vm-tools anyway"
    else
        read -rp "Install open-vm-tools anyway? (y/N): " INSTALL_ANYWAY
        if [[ "${INSTALL_ANYWAY,,}" != "y" && "${INSTALL_ANYWAY,,}" != "yes" ]]; then
            SKIP_VMTOOLS=true
        fi
    fi
fi

if $SKIP_VMTOOLS; then
    c_info "Skipping open-vm-tools installation"
else
    if ! $DRY_RUN && ! dpkg -l 2>/dev/null | grep -qE 'xserver-xorg|wayland'; then
        c_warn "No desktop environment (X/Wayland) detected on this system"
        c_warn "open-vm-tools-desktops clipboard/drag&drop features wont work without a GUI installing anyway"
    fi

    wait_for_apt_lock
    run_cmd "Installing open-vm-tools and open-vm-tools-desktop" apt-get install -y open-vm-tools open-vm-tools-desktop
    run_cmd "Enabling open-vm-tools" systemctl enable --now open-vm-tools

    if $DRY_RUN; then
        note_dry_run "Would check open-vm-tools service status vmware-toolbox-cmd -v and kernel modules"
    else
        c_info "Verifying open-vm-tools..."

        if systemctl is-active --quiet open-vm-tools; then
            c_ok "open-vm-tools service is running"
        else
            c_err "open-vm-tools service is NOT running check: systemctl status open-vm-tools"
        fi

        if command -v vmware-toolbox-cmd &>/dev/null; then
            TOOLS_VERSION=$(vmware-toolbox-cmd -v 2>/dev/null || echo "unavailable")
            c_ok "vmware-toolbox-cmd version: $TOOLS_VERSION"
        else
            c_warn "vmware-toolbox-cmd not found"
        fi

        if lsmod | grep -qE 'vmwgfx|vmw_vsock|vmw_vmci'; then
            c_ok "VMware kernel modules (vmwgfx/vmw_vsock/vmw_vmci) are loaded"
        else
            c_warn "VMware kernel modules not found they may not be loaded yet (a reboot might be needed)"
        fi

        c_info "Note: shared folders / clipboard only work fully once a GUI session is"
        c_info "      open and the per-user 'vmtoolsd' process has started"
        c_info "      After logging into the desktop, verify with:"
        c_info "        pgrep -u \$USER vmtoolsd"
        c_info "        vmware-toolbox-cmd stat hosttime"
    fi
    mark_stage_done stage9
    fi
fi

# --- Stage 10: ETA Register + Ahenk ---
c_info "Stage 10: ETA Register + Ahenk"
# Idempotent: only skip if patch applied AND ahenk installed+active (meaning already registered and setup complete)
# If only patch is done but not registered/ahenk, still run registration check
if is_stage_done stage10 && grep -q "patched for VM setup" "$ETA_CHECKS_FILE" 2>/dev/null && is_pkg_installed ahenk && is_service_active ahenk.service; then
    if [[ "$DRY_RUN" == "false" ]]; then
        c_ok "Stage 10 already done (patch applied, ahenk installed and active), skipping"
    else
        c_info "[DRY-RUN] Stage 10 would be skipped (already done, ahenk active)"
    fi
    SKIP_STAGE10=true
else
    # If patch exists but ahenk not installed, still need to check registration (don't skip)
    if is_stage_done stage10 && grep -q "patched for VM setup" "$ETA_CHECKS_FILE" 2>/dev/null; then
        c_info "Stage 10 patch already applied, re-checking registration and ahenk"
    fi
    SKIP_STAGE10=false
fi
if [[ "$SKIP_STAGE10" == "false" ]]; then
# 10.0 Display check at start (required for GUI)
HAS_DISPLAY=false
if detect_eta_display; then
    HAS_DISPLAY=true
    c_ok "Display check passed"
else
    c_warn "Display not available at Stage 10 start - GUI launch will be skipped if needed"
    # Still continue with patch and registration check (patch is permanent)
fi

# 10.1 Patch eta-register source (permanent, includes user check bypass)
if ! patch_eta_register; then
    c_warn "Patch step failed, registration check may still be blocked by VM/vendor checks"
fi

# 10.2 Registration check (after patch)
c_info "Checking ETA registration status..."
REG_CHECK_OUTPUT=""
if REG_CHECK_OUTPUT=$(check_eta_registration_status); then
    ETA_STATUS="$REG_CHECK_OUTPUT"
else
    ETA_STATUS="$REG_CHECK_OUTPUT"
fi
# Fallback if python returned empty
if [[ -z "$ETA_STATUS" ]]; then
    ETA_STATUS="error-no-connection"
fi
c_info "ETA registration status: $ETA_STATUS"

# 10.3 Branch based on registration
if [[ "$ETA_STATUS" == "registered" ]]; then
    c_ok "Device is registered according to ETA server"
    if ! install_and_enable_ahenk; then
        c_warn "Ahenk installation/enabling had issues, check logs"
    fi

elif [[ "$ETA_STATUS" == "not-registered" ]]; then
    c_warn "Device is NOT registered - launching eta-register (patched) for user registration"
    c_info "After registration, eta-register's installer will automatically install ahenk (no manual install needed)"
    if ! $HAS_DISPLAY; then
        # Re-check display before launch (user requested check at start, but also before launch)
        if ! detect_eta_display; then
            c_err "Cannot launch eta-register GUI: no display available"
            c_err "Please run this script from desktop terminal (etapadmin session) or set DISPLAY=:0"
            ETA_STATUS="not-registered-no-display"
            AHENK_STATUS="skipped-not-registered"
        else
            HAS_DISPLAY=true
            launch_eta_register_once || c_warn "eta-register launch returned non-zero"
            # Single launch only, no re-check loop per requirements
            AHENK_STATUS="skipped-launched-eta-register (installer handles ahenk)"
        fi
    else
        launch_eta_register_once || c_warn "eta-register launch returned non-zero"
        AHENK_STATUS="skipped-launched-eta-register (installer handles ahenk)"
        # Verify if ahenk got installed by eta-register's installer.py/opr.py
        if dpkg -l 2>/dev/null | grep -q "^ii.* $AHENK_PACKAGE "; then
            c_ok "Ahenk was installed by eta-register flow"
            if systemctl is-active --quiet "$AHENK_SERVICE"; then
                c_ok "$AHENK_SERVICE is active"
                AHENK_STATUS="installed-by-eta-register+active"
            else
                c_info "Enabling $AHENK_SERVICE (post-registration)..."
                if $DRY_RUN; then
                    note_dry_run "Would enable $AHENK_SERVICE"
                else
                    systemctl enable --now "$AHENK_SERVICE" || c_warn "Failed to enable $AHENK_SERVICE post-registration"
                    if systemctl is-active --quiet "$AHENK_SERVICE"; then
                        AHENK_STATUS="installed-by-eta-register+active"
                    else
                        AHENK_STATUS="installed-by-eta-register+inactive"
                    fi
                fi
            fi
        else
            c_info "Ahenk not yet installed (user may have closed eta-register without completing registration)"
            c_info "If registration was completed, re-run script or check eta-register installer logs"
        fi
    fi

else
    # error cases: error-no-mac, error-no-connection, error-server-issue, error-missing-eta, etc.
    c_warn "Could not determine registration status ($ETA_STATUS)"
    if [[ "$ETA_STATUS" == "error-no-mac" ]]; then
        c_err "MAC address could not be retrieved (wired device missing?) - ETA uses MAC for board check"
    elif [[ "$ETA_STATUS" == "error-no-connection" ]]; then
        c_err "No connection to ETA server (check network / BACKEND_URL)"
    fi
    # Try to launch GUI anyway to show error to user (single launch)
    if $HAS_DISPLAY; then
        c_info "Launching eta-register GUI to show error (single launch)..."
        launch_eta_register_once || true
        AHENK_STATUS="skipped-error-launched-gui"
    else
        c_warn "Skipping GUI launch due to no display (error case)"
        AHENK_STATUS="skipped-error-no-display"
    fi
fi

# Ensure AHENK_STATUS is set for summary if not touched
if [[ -z "${AHENK_STATUS:-}" ]]; then
    AHENK_STATUS="skipped"
fi
    mark_stage_done stage10
fi

SUMMARY_TITLE="SETUP COMPLETE"
$DRY_RUN && SUMMARY_TITLE="DRY-RUN COMPLETE"

cat <<EOF

 ${SUMMARY_TITLE}

 - open-vm-tools status: $($SKIP_VMTOOLS && echo "skipped" || echo "installed/verified")
 - ETA patch: $(grep -q "patched for VM setup" "$ETA_CHECKS_FILE" 2>/dev/null && echo "applied (VM/vendor/user disabled, permanent)" || echo "not applied/unknown")
 - ETA registration: $ETA_STATUS
 - ahenk status: $AHENK_STATUS
 - sshd_config backup: $SSHD_CONFIG_BACKUP
 - Full log: $LOGFILE

EOF
if [[ "$ETA_STATUS" == "not-registered" ]]; then
    cat <<EOF
 Note: Device was not registered, eta-register was launched once.
       If user completed registration, ahenk should now be installing via
       eta-register's installer (opr.py). Verify with: dpkg -l ahenk && systemctl status ahenk.service
EOF
fi
