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

# If another process holds the apt/dpkg lock (e.g. unattended-upgrades kicking in in the background while were waiting for ssh-copy-id) wait for it
wait_for_apt_lock() {
    $DRY_RUN && return 0
    local waited=0
    while pgrep -x apt-get >/dev/null 2>&1 || pgrep -x apt >/dev/null 2>&1 \
          || pgrep -x dpkg >/dev/null 2>&1 || pgrep -x unattended-upgr >/dev/null 2>&1; do
        [[ $waited -eq 0 ]] && c_warn "Another apt/dpkg process is running, waiting for it to finish..."
        sleep 3
        waited=$((waited + 3))
        if (( waited >= 300 )); then
            c_err "apt/dpkg has been busy for 300 seconds Check manually: ps aux | grep -E 'apt|dpkg'"
            exit 1
        fi
    done
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

if $DRY_RUN; then
    c_warn "DRY-RUN mode active: NO system changes will be made"
fi

APT_OPTS=(-o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold")
SSHD_CONFIG="/etc/ssh/sshd_config"
SSHD_CONFIG_BACKUP="/etc/ssh/sshd_config.bak.$(date +%Y%m%d%H%M%S)"

# system update
c_info "Stage 1: System update"
wait_for_apt_lock
run_cmd "apt-get update" apt-get update -y
wait_for_apt_lock
run_cmd "apt-get full-upgrade" apt-get "${APT_OPTS[@]}" full-upgrade -y
wait_for_apt_lock
run_cmd "apt-get autoclean" apt-get autoclean -y
c_ok "System update complete"

# openssh install and activation
c_info "Stage 2: OpenSSH installation"
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

# distrubate the keys
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

# make ssh key only
c_info "Stage 5: SSH hardening"

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

# ufw setup
c_info "Stage 6: ufw firewall"
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

# auto updates
c_info "Stage 7: unattended-upgrades"
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

# time sync
c_info "Stage 8: Time synchronization"

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

#openvmtools
c_info "Stage 9: open-vm-tools"

SKIP_VMTOOLS=false
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
fi

SUMMARY_TITLE="SETUP COMPLETE"
$DRY_RUN && SUMMARY_TITLE="DRY-RUN COMPLETE"

cat <<EOF

 ${SUMMARY_TITLE}

 - open-vm-tools status: $($SKIP_VMTOOLS && echo "skipped" || echo "installed/verified")
 - sshd_config backup: $SSHD_CONFIG_BACKUP
 - Full log: $LOGFILE

EOF
