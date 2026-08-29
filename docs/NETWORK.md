# Mercalde Network — Topology & Recovery Runbook

Last verified: 2026-08-01 (Termux→Proxmox access paths reconfirmed; SSH-alias &
migration-scheme section folded in; see "Session notes 2026-08-01" below)

This documents the SER8 → VPS → Pi5 → lab access paths, the known
persistence gaps that have caused outages, and step-by-step recovery.

---

## Machines & Interfaces

| Host        | Address(es)                                   | Role |
|-------------|-----------------------------------------------|------|
| SER8        | 192.168.1.229 (wlp3s0, Starlink WiFi)         | Primary dev machine |
| House Pi5   | 192.168.1.53 (eth0, Starlink) / 192.168.3.10 (eth1, lab) / 192.168.6.1 (wlan0, WiFi lab) / 10.8.0.2 (wg0) | Router / WireGuard hub |
| VPS (vultr) | 45.32.131.224 (public) / 10.8.0.1 (wg0)       | Public relay, WireGuard server, port-forwards |
| Zeus bare   | 192.168.3.127 (Ubuntu/TFM) — alias rzeus      | CUDA GPS sim (bare metal) |
| Zeus PVE    | 192.168.3.128 (Proxmox) — alias pzeus         | Proxmox host (pve-zeus) |
| Win VM      | 192.168.3.138                                 | Windows gaming/scanner VM (VMID 100) |

---

## SSH aliases & migration IP scheme (verified 2026-07-17)

Two classes of alias in `SER8:~/.ssh/config`:

* **LAN aliases** — direct `192.168.3.0/24` addresses (Path B; require the
  SER8 static route or being on-lab).
* **`r`/`p`-prefixed tunnel aliases** — all `HostName 45.32.131.224`
  (Vultr) on per-service ports (Path A; work from anywhere).

### LAN aliases (direct 192.168.3.x)

| Alias | Address | Host | User |
|---|---|---|---|
| `zeus` | .127 | Zeus **bare-metal** Ubuntu/TFM — HANDS-OFF fallback boot | michael |
| `rig6600` | .120 | rrig6600 bare-metal (NOT yet migrated) | michael |
| `rig6600b` | .154 | rrig6600b bare-metal (host now Proxmox — see scheme below) | michael |
| `rig6600c` | .162 | rrig6600c bare-metal (host now Proxmox — see scheme below) | michael |
| `kamrui` | .152 | Kamrui mini-PC | michael |
| `pzeus-lan` / `pve-zeus` | .128 | Zeus Proxmox host | root |
| `pve-rig6600b` | .155 | rrig6600b Proxmox host | root |
| `rig-6600c` / `pve-rig6600c` | .163 | rrig6600c Proxmox host | root |
| `pi5` | 192.168.1.53 | House Pi5 | michael |
| `pi5b` | 192.168.1.75 | (secondary Pi5) | michael |

### Tunnel aliases (via Vultr 45.32.131.224, Path A)

| Alias | Port | Target | User |
|---|---|---|---|
| `rzeus` | 2001 | .127 bare-metal TFM | michael |
| `rrig6600` | 2002 | .120 | michael |
| `rrig6600b` | 2003 | .154 | michael |
| `rkamrui` | 2004 | .152 | michael |
| `rser8` | 2005 | — | michael |
| `rrig6600c` | 2006 | .162 | michael |
| `pzeus` | 2008 | .128 Proxmox host | root |
| `rpi5` | 2222 | Pi5 | michael |
| `vultr` | (22) | VPS itself | root |

> **WARNING — the tunnel aliases point at BARE-METAL targets.** `rrig6600b`
> (port 2003) still forwards to `.154`, `rrig6600c` (2006) to `.162`. After
> the CT100 migration the *workers* no longer live at those addresses (see
> below). On-lab work should prefer LAN/CT100 addresses directly.

### Proxmox migration IP scheme

Rule (corrected in RUNBOOK_v1.6_PATCH FIX 1 — the old runbook table wrongly
used +10 and mis-addressed rrig6600b once):

> **Proxmox host = rig IP + 1. CT100 (the GPU-worker LXC) = host + 1.**
> CT IPs are STATIC (`pct create --net0 ...,ip=<addr>/24,gw=192.168.3.10`),
> never DHCP.

| Rig | bare-metal | Proxmox host | **CT100 (worker endpoint)** | Migrated? |
|---|---|---|---|---|
| zeus | .127 | .128 | — (Zeus uses VM 101, not CT100) | VM |
| rrig6600 | .120 | .121 | .122 | **NO — still bare-metal** |
| rrig6600b | .154 | .155 | **.156** | yes (ROCm, 8 GPUs) |
| rrig6600c | .162 | .163 | **.164** | yes (ROCm, 8 GPUs) |

The CT100 container is given the rig's canonical hostname
(`pct create --hostname rrig6600c`) so `socket.gethostname()` returns the
rig name for coordinator identity — per
`prng_cluster_public/docs/S172_INFRASTRUCTURE_INTERFACE_v1_0.md`.
**Coordinator / miner reaches WORKERS at the CT100 address, not bare-metal.**

> `dotfiles/ssh_config` in this repo is STALE for the migrated rigs — its
> `rig6600b`/`rig6600c` blocks still hold bare-metal `.154`/`.162`. Until it's
> refreshed, use `michael@192.168.3.156` / `192.168.3.164` for the CT100 workers.

### Zeus TFM under Proxmox — VM 101 (`zeus-ubuntu`)

Zeus is a boot-selector: bare-metal Ubuntu/TFM at `.127` (fallback, HANDS-OFF)
**or** Proxmox at `.128`. Under Proxmox, the TFM workload runs in **VM 101**,
a P2V clone of the `.127` box.

| Property | Value |
|---|---|
| VM 101 LAN IP | **192.168.3.177** (DHCP from Pi5 — *not yet static*) |
| GPU | `hostpci0: 0000:68:00,pcie=1` — one RTX 3080 Ti passed through |
| In-guest check | `nvidia-smi` sees "NVIDIA GeForce RTX 3080 Ti, 12288 MiB" |
| Project path | `/home/michael/distributed_prng_analysis` |
| Git remotes | `origin` + `public` present and working as **michael** |
| Other 3080 Ti | still on VM 100 (Windows) — 101 has ONE card, not two |

Notes:
* Run agents/tools in 101 as **michael**, not root — root has no TFM tree and
  the wrong SSH keys. `qm guest exec` runs as root, so use
  `qm guest exec 101 -- su - michael -c '...'` for michael-context checks.
* 101's `.177` is a DHCP lease — pin it static / add a Pi5 reservation before
  relying on it as the permanent canonical dev box.
* `.127` bare-metal remains the untouched fallback. Do not develop on both.

> **Zeus VMs are ONLY 100 and 101.** Confirmed 2026-08-01 by reading the
> Proxmox cluster DB directly (`config.db` tree holds `100.conf` + `101.conf`
> only; LVM shows disks for 100/101 only). **There is no VM 102** — if someone
> asks for "vm102" they mean VM 101 (`zeus-ubuntu`, `.177`).

---

## The TWO access paths (critical distinction)

There are two independent ways SER8 reaches the lab. They break independently.

### Path A — SSH via VPS tunnel (aliases: rzeus, pzeus, etc.)
SER8 → VPS:<port> → WireGuard(wg0) → Pi5 → lab host.
- Does NOT require SER8 to have a route to 192.168.3.0/24.
- Depends on: VPS autossh forward running + VPS ufw allowing the port +
  VPS↔Pi5 WireGuard tunnel up + Pi5 forwarding wg0→eth1.
- Port map (VPS): 2001→.127(rzeus), 2008→.128(pzeus), 2002→.120,
  2003→.154, 2004→.152, 2005→.6.24, 2006→.162, 2222→Pi5.

### Path B — Direct to lab over WiFi (browser: https://192.168.3.128:8006)
SER8 → Pi5 (192.168.1.53) → Pi5 forwards → 192.168.3.0/24.
- REQUIRES a static route on SER8:  192.168.3.0/24 via 192.168.1.53
- Without it, SER8 sends .3 traffic to the Starlink default gw (192.168.1.1),
  which drops it. Symptom: curl to .128:8006 fails INSTANTLY (000, connect=0),
  browser shows ERR_NETWORK_CHANGED / connection interrupted.
- This is how the Proxmox WEB UI is reached. SSH (Path A) working does NOT
  imply the web UI works — they use different paths.

---

## Termux (Android) → Proxmox access (verified 2026-08-01)

The phone reaches the lab **only through Path A** (VPS relay) — it has no Pi5
static route. Working paths from Termux:

- **Web UI:** run the `pveweb` alias (lives in phone `~/.bashrc`, NOT in the
  repo dotfiles):
  `termux-wake-lock; ssh -N -L 8006:192.168.3.128:8006 vultr` — then open
  `https://localhost:8006` in Chrome. Ctrl-C to drop the tunnel. Rides VPS
  port 22, so it is unaffected by any 200X port block.
- **Shell — `ssh pve-zeus`** (added to phone `~/.ssh/config` 2026-08-01):
  ProxyJump via `vultr` (port 22) to `root@192.168.3.128` over the VPS
  WireGuard route. Works from anywhere, independent of the 2008 relay port and
  the portal firewall. Config block:
  ```
  Host pve-zeus
      HostName 192.168.3.128
      User root
      ProxyJump vultr
      IdentityFile ~/.ssh/id_ed25519
      IdentitiesOnly yes
  ```
  Same pattern reaches VM 101: `ssh -J vultr michael@192.168.3.177`.
- **Shell — `ssh pzeus`** (Vultr:2008 reverse tunnel): the original alias, but
  currently blocked at the Vultr portal firewall — see gap #3 and the
  2026-08-01 note below.
- Deliberate switch of Zeus into Proxmox from bare Ubuntu: `sudo boot-proxmox`
  (one-shot EFI target, then reboots). A Telegram "Cluster bot" announces each
  boot (host, IP, GPU count).
- The phone `~/.bashrc` also carries a remote-capable `wakezeus`
  = `ssh -t vultr "ssh -t michael@10.8.0.2 \"wakeonlan ...\""`.

> `pveweb`, remote `wakezeus`, and `pve-zeus` currently live ONLY on the phone
> (`~/.bashrc` / `~/.ssh/config`) — they are not in `zeus-proxmox-build`
> dotfiles, so repo searches won't find them and other devices won't pick them
> up. TODO: commit them to the repo dotfiles.

---

## Known persistence gaps (root causes of the 2026-07-01 outage)

1. **Pi5 MASQUERADE rule** (`-t nat -A POSTROUTING -o wg0 -j MASQUERADE`)
   was not persistent — flushed on reboot/service reload. Re-added by
   `setup_vpn_hotspot.sh`, but see #2.

2. **Pi5 `setup_vpn_hotspot.sh` has a DESTRUCTIVE bug**: it runs a `sed`
   forcing `AllowedIPs = 0.0.0.0/0, ::/0` in wg0.conf. The `::/0` fails on
   the Pi kernel (`ip -6 route add ::/0` → "Operation not supported"),
   breaking the whole tunnel. Re-running the script RE-BREAKS wg0.
   CORRECT Pi5 wg0.conf value:  AllowedIPs = 10.8.0.1/32
   (The wide set 10.8.0.2/32, 192.168.3.0/24, 192.168.6.0/24 belongs on the
   VPS side only — the Pi5 reaches lab subnets directly via eth1/wlan0.)
   TODO: fix the sed in setup_vpn_hotspot.sh so it can't clobber a working tunnel.

3. **VPS 2008 (pzeus) forward + firewall**: was a manual `ssh -L`, not a
   service, and `ufw allow 2008/tcp` was never added → SER8→VPS:2008 silently
   dropped. FIXED: zeus-proxmox-ssh-forward.service + ufw loop now in
   setup_vps_tunnels.sh. **RESURFACED 2026-08-01 — see note below.**

4. **SER8 static route to lab is NOT persistent**:
   `192.168.3.0/24 via 192.168.1.53` must exist for Path B (web UI).
   Runtime `ip route add` vanishes on reboot/reconnect. TODO: make persistent
   in SER8 netplan/NetworkManager.

5. **Duplicate `pzeus` blocks** in SER8 ~/.ssh/config (cosmetic; cleaned).

---

## Session notes 2026-08-01 (Termux debugging session)

- **`ssh pzeus` (port 2008) blocked again — but at the Vultr *portal* firewall,
  not on-host.** Proven by packet capture: `tcpdump -ni any port 2008` on the
  VPS saw **0 packets** while an external connect timed out — traffic dropped
  before reaching the instance. On-host ufw *allows* 2008/tcp and the reverse
  tunnel is healthy (sshd banner returns via localhost). This is gap #3's
  firewall half resurfacing one layer up (the portal group, which ufw and
  localhost tests cannot see).
  **Fix:** my.vultr.com → instance → Settings → Firewall → allow TCP 2008
  (match the 2001–2006 rules). Not urgent — `ssh pve-zeus` (ProxyJump) covers
  the same need without it.
- **Diagnostic that distinguishes a dead tunnel from a firewall drop:** from
  the phone, `ssh vultr "timeout 5 bash -c 'echo | nc -w 3 localhost <port>'"`
  — an `SSH-2.0-...` banner means the tunnel is healthy (so a hang is upstream
  filtering); silence means a stale/zombie tunnel (kill the ssh pid on vultr,
  autossh re-establishes). Stale listeners also die on their own when the far
  host reboots.
- **Reading Proxmox VM configs when Proxmox is OFF (from bare-metal Ubuntu):**
  `/etc/pve` is a FUSE view of SQLite, so mount the PVE root and read the DB —
  `sudo vgchange -ay pve; sudo mount -o ro /dev/pve/root /mnt/pveroot;`
  `sudo sqlite3 /mnt/pveroot/var/lib/pve-cluster/config.db "SELECT name FROM tree WHERE name LIKE '%.conf';"`
  This is how VM-102-does-not-exist was confirmed.

---

## RECOVERY RUNBOOK — "I can't reach X"

### Can't SSH to pzeus (ssh pzeus hangs / no route to host)
1. VPS listening on 2008?    `ssh vultr 'ss -tlnp | grep 2008'`
2. VPS ufw allows 2008?      `ssh vultr 'ufw status | grep 2008'`
   - if missing: `ssh vultr 'ufw allow 2008/tcp'`
   - **Also check the Vultr PORTAL firewall** (separate from ufw; a portal drop
     shows 0 packets in `tcpdump -ni any port 2008` on the VPS). See 2026-08-01 note.
3. Forward alive & not stale/duplicated?
   `ssh vultr 'pgrep -af "L 0.0.0.0:2008"'`  (kill dupes if >1)
4. Rebuild all forwards + firewall (idempotent):
   `ssh vultr 'bash /root/setup_vps_tunnels.sh'`
5. VPS→Proxmox directly works?
   `ssh vultr 'ssh -i /root/.ssh/zeus_forward root@192.168.3.128 hostname'`
6. **Workaround that always works:** `ssh pve-zeus` (ProxyJump via vultr:22) —
   bypasses the 2008 port entirely.

### Can't reach Proxmox WEB UI (https://192.168.3.128:8006) but SSH works
This is Path B — SER8's static route to the lab is missing.
1. Check route:  `ip route get 192.168.3.128`
   - BAD:  "via 192.168.1.1"  (Starlink — dead end)
   - GOOD: "via 192.168.1.53" (Pi5)
2. Fix:  `sudo ip route add 192.168.3.0/24 via 192.168.1.53`
3. Verify: `curl -k -s -o /dev/null -w "%{http_code}\n" https://192.168.3.128:8006/`
   → expect 200.
4. (Pi5 must answer on Starlink side: `ping 192.168.1.53`)
   - From the PHONE (no Pi5 route): use `pveweb` instead (Path A tunnel to :8006).

### WiFi clients (.6) or lab (.3) unreachable through Pi5
1. Pi5 forwarding on?   `sysctl net.ipv4.ip_forward` (=1)
2. MASQUERADE present?  `sudo iptables -t nat -C POSTROUTING -o wg0 -j MASQUERADE`
   - if missing: re-add + `netfilter-persistent save`
3. Status checker:      `sudo /usr/local/bin/vpn-hotspot-status.sh`
4. wg0 up?              `sudo wg show`  (peer handshake recent)
   - if down after editing wg0.conf: ensure AllowedIPs = 10.8.0.1/32
     (NOT 0.0.0.0/0 — see gap #2), then `wg-quick down wg0; wg-quick up wg0`

### Pi5 lost its own route to Zeus (192.168.3.128 via wg0 instead of eth1)
Symptom: Pi5 `ip route get 192.168.3.128` shows "dev wg0".
Cause: 192.168.3.0/24 wrongly in Pi5 wg0 AllowedIPs (see gap #2).
Fix: `sudo ip route del 192.168.3.0/24 dev wg0`  (restores direct eth1 path)
     and set wg0.conf AllowedIPs back to 10.8.0.1/32.

---

## VPS WireGuard reference (authoritative)
VPS wg0.conf, House Pi5 peer:
  AllowedIPs = 10.8.0.2/32, 192.168.3.0/24, 192.168.6.0/24   ← WIDE set here (correct)
Pi5 wg0.conf, VPS peer:
  AllowedIPs = 10.8.0.1/32                                    ← NARROW set here (correct)

---

## Appendix — VPS infrastructure reference (from network_config.txt, 2026-03-08)

_Older doc; Proxmox/migration details above supersede its machine descriptions,
but these VPS-internal specifics are not recorded elsewhere. GPS-spoofing
operational content from that file is a SEPARATE project and is not included here._

### VPS WireGuard peers (/etc/wireguard/wg0.conf)
Interface `10.8.0.1/24`, ListenPort `51820`, MTU `1320`.
- House Pi5 — pubkey `ywXufcqqhH2ct5TG0dWg7anPxLi2CSu1wcK3vICU+Co=`,
  AllowedIPs `10.8.0.2/32, 192.168.3.0/24, 192.168.6.0/24`, keepalive 25.
- Pi5b — pubkey `gcXDFTGU9h7WYVWGXS9e2dKIzCx23QC/aeu38JFWPiM=`,
  AllowedIPs `10.8.0.3/32`, keepalive 25 (disabled on Pi5b when on LAN;
  private key at `/root/pi5b_private.key` on VPS).

### VPS iptables NAT PREROUTING (/etc/iptables/rules.v4)
| Port | Destination | Purpose |
|---|---|---|
| 2222 | 10.8.0.2:22 | SSH → House Pi5 (via WireGuard) |
| 443 | 10.8.0.2:443 | HTTPS → House Pi5 (solar dashboard) |
| 8080 | 10.8.0.2:8080 | HTTP → House Pi5 |
| 5000 | 192.168.3.152:5000 | Flask → KAMRUI |
| 5001 | 192.168.3.152:5001 | Flask alt → KAMRUI |
| 2001 | 192.168.3.127:22 | SSH → Zeus |
| 2002 | 192.168.3.120:22 | SSH → rig-6600 |
| 2003 | 192.168.3.154:22 | SSH → rig-6600b |
| 2004 | 192.168.3.152:22 | SSH → KAMRUI |
| 2005 | 192.168.6.24:22 | SSH → device on .6 subnet |
| 2006 | 192.168.3.162:22 | SSH → rig-6600c |
| 9000 | 192.168.3.150:9000 | Service |

(Port 2008 → .128 was added later for pzeus; see gap #3 / 2026-08-01 note.)

### VPS nginx (mercalde-solar.org)
HTTP:80 → redirect to HTTPS:443. HTTPS:443 → SSL termination (Let's Encrypt).
- `/alexa` → proxy to `http://127.0.0.1:5000/alexa` — the Alexa skill backend
  runs **on the VPS itself**, `/var/www/alexa_solar.py` under
  `alexa-solar.service`. It is not on the KAMRUI.
- `/` → proxy to `https://10.8.0.2:443` (House Pi5 — solar dashboard),
  `proxy_ssl_verify off` (internal self-signed cert).

### VPS SSH keys (/root/.ssh/)
- `zeus_forward` / `zeus_forward.pub` — key used by VPS to SSH into LAN machines.
- `authorized_keys` contains: michael-laptop, michael@zeus, michael@SER8,
  root@vultr, michael@Michael, ser8-master-key, michael@raspberrypi.
