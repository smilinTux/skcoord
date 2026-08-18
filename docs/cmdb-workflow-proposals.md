# CMDB & Discovery Workflow Proposals (CMDB-10)

Status: draft for review. Grounded in the 2026-08-18 whole-network populate
(CMDB-9), which took the CMDB from a laptop-sized seed to 919 CIs (32 agent, 19
host, 197 port, 671 service) across 13 SSH-managed hosts plus 6 network-observed
hosts.

## Where the gaps showed up

1. **SSH-key discovery only reached 13 of ~34 live hosts.** 4 hosts (`.199`,
   `.232`, `.206`, `.152`) accept only password auth and none of the 30+ vault
   password combos worked. 6 hosts were seeded from network fingerprints
   (ports + HTTP headers) with no shell, so their service list is empty.
2. **Port CIs accumulate.** The ephemeral-range filter fixed new scans, but
   orphaned/renamed port CIs from before still sit in the store (the CMDB-8
   item). `reconcile` never deletes by design, so garbage needs an explicit
   retirement path.
3. **Host CIs carry only 2 attributes** (`kernel`, `cores`) from
   `collect_host_facts`. No CPU model, RAM, disks, IPs, or hostname-to-IP map.
4. **The network is the source of truth for nothing yet.** The Proxmox APIs
   (both clusters, `root@pam` ticket) are unused as a collector. Device-class
   hosts (MyQ garage `.80`, camera `.160`, gSOAP `.151`) were fingerprinted
   but not folded.
5. **No automation.** The 3h timer runs `reconcile` on the local host only.
   Whole-network scans are a manual `--host a --host b ...` invocation. There
   is no drift report surfaced anywhere, no CI, no alerting.

## Proposals

### P1. Host facts collector upgrade
Extend `collect_host_facts` to also emit: `hostname -I` (all IPs), CPU model +
sockets, total RAM, and root/disk usage. Store as attributes on `ci-host-*`
so the CMDB answers "what runs where and on what hardware" without SSH in every
read path. Cheap, no new deps.

### P2. Proxmox collector (API, no SSH)
New collector using the existing `root@pam` ticket (`.12:8006`, `.13:8006`):
VM/CT inventory (vmid, name, status, vcpu, mem) becomes host CIs + `hosts`
relationships, and `running` VMs get port CIs from the cluster firewall/qemu
guest IPs. This covers hosts that refuse our SSH keys (the VMs are guest-
managed, but the hypervisor knows them). Also solves "norpv* nodes are the
only fully-known hosts".

### P3. Network sweep collector (fingerprint, no creds)
A `network-scan` command that runs the existing /24 ping sweep, connects to
open 22/80/443/8006/1713/11434/16379 and records: product headers, TLS cert
CN/SAN, banner strings. Folds into a `network-observed` host CI (as seeded
today) plus a `fingerprint` attribute. Turn the manual seeding script into a
collector so the 6 hand-seeded hosts are reproducible on every sweep.

### P4. Password host integration path
For the 4 password-auth hosts: add a `--creds-from skvault` option to the
runner that resolves `host:user:pass` from the vault by node name, plus a
documented convention for naming vault entries (e.g. `ssh @ noroc2027`). If a
vault entry names a host and SSH public-key auth fails, fall back to
`sshpass` with the vault password. This makes the remaining hosts scannable
once creds are stored, and the scanner degrades gracefully when they are not.

### P5. Port lifecycle: retire-not-delete
Add `retire` to reconcile's vocabulary for CIs tagged `discovered` that have
not been seen for N consecutive passes (default 3 = 9h). Mark status `retired`
with a note instead of deleting (store stays append-only). Solves the port
accumulation and the CMDB-8 orphans in one mechanism, with the existing
"never un-retire a manually retired CI" rule keeping it safe.

### P6. Drift surfaces on CI
Add `skcapstone cmdb drift` output to the reconcile timer log and emit a
`finding` message on pub/sub topic `cmdb.drift` when findings are non-empty.
Gate it to real drift (declared_not_observed / observed_not_declared /
stored_not_discovered) so the fleet operator hears about the fleet lying,
not about the scan finishing.

### P7. Whole-network reconcile orchestration
Wrap the multi-host scan in a single command: `cmdb reconcile --network` reads
the host list from SSH config + live-ping + Proxmox VM IPs, runs all runners
concurrently (thread pool, per-host), and applies. The 3h timer switches to
this so the CMDB converges on the whole 192.168.0.0/24 continuously instead
of one box at a time.

### P8. Hostname canonicalisation
SSH config maps `norap0015 -> 192.168.0.155` and carries a legacy `.87` entry
(dead in the live network, still in ssh config and vault). A `canonical_name`
attribute (or relationship) on host CIs collapses duplicate entries so the
CMDB does not count one machine twice across DNS / ssh config / vault
records.

## Suggested order

1. P1 (facts) and P5 (retire) land first, small and self-contained.
2. P3 (network sweep collector) replaces the manual seeding script; P2
   (Proxmox) gives the password-host coverage for free.
3. P4 (vault creds) is optional and needs the vault-entry convention decision.
4. P6 (drift CI) and P7 (network reconcile) wrap it all into the automated
   loop; the 3h timer becomes a real fleet-state engine.

## Open questions for the operator

- Are `.199` (quant box), `.11/.232` (nginx proxies), `.152/.206` Chef's own
  boxes? If yes, worth storing SSH creds in skvault under a convention so P4
  can scan them.
- Should device-class hosts (MyQ, camera, ESP32) be full CIs or a separate
  `device` CI type with its own lifecycle?
- Do we want the drift findings to open GTD items / incidents automatically,
  or only publish to the topic?
