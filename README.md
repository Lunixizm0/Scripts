# scripts

My automation scripts.

## Contents

| Script | Description |
|---|---|
| [`pardus-vm-setup.sh`](./pardus-vm-setup.sh) | Bootstraps a fresh Pardus ETAP (Debian-based) VM: system update, SSH, key-only hardening, firewall, automatic security updates, time sync check, and VMware guest tools. |

---

## pardus-vm-setup.sh

Bootstraps and hardens a fresh Pardus ETAP / Debian-based VM in one pass. Built for spinning up disposable VMs quickly.

### What it does

1. **System update** `apt-get update`, `full-upgrade`, `autoclean`
2. **OpenSSH** installs and enables `openssh-server`.
3. **Key onboarding** prints the IP, waits (via `inotifywait`) for you to run `ssh-copy-id` from your own machine, and shows a PowerShell fallback for Windows users without `ssh-copy-id`. Supports adding keys from multiple machines/users in one run.
4. **Key distribution** pushes the collected key(s) to **every account with a home directory** (root included, via `getent passwd` so LDAP/SSSD accounts are picked up too), fixing `~/.ssh` (700) and `authorized_keys` (600) ownership/permissions along the way.
5. **SSH hardening** sets `PasswordAuthentication no`, `PubkeyAuthentication yes`, `PermitRootLogin prohibit-password`, and (importantly) scans `/etc/ssh/sshd_config.d/*.conf` drop-ins for conflicting directives that would otherwise silently override the main config.
6. **Firewall (ufw)** default-deny inbound, allows OpenSSH, and optionally any extra ports you specify (useful if the VM also runs an agent/service that needs inbound access).
7. **unattended-upgrades** enables periodic, automatic security updates (automatic reboot stays off by default).
8. **Time sync check** verifies NTP sync via `timedatectl`; installs `systemd-timesyncd` only if `chrony` isn't already present, to avoid two NTP daemons fighting over port 123.
9. **open-vm-tools** detects the hypervisor first (`systemd-detect-virt`) and warns/suggests an alternative if it's not VMware, then installs `open-vm-tools` + `open-vm-tools-desktop` and verifies the service, binary, and kernel modules.
10. 
---

### Usage

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/Lunixizm0/Scripts/refs/heads/main/pardus-vm-setup.sh)" # run it for real
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/Lunixizm0/Scripts/refs/heads/main/pardus-vm-setup.sh)" -- --dry-run # preview every action, change nothing
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/Lunixizm0/Scripts/refs/heads/main/pardus-vm-setup.sh)" -- --help # help
```


## Notes:

This repo is a collecting common repo for my personal scripts. It will probably maintained poorly. Use it with cation.
