# ONVIF RTSP Bridge

Expose individual recorder channels as independent ONVIF cameras for UniFi Protect.
Python 3.5.3+, standard library only. No Docker, Node.js, FFmpeg, decoding or
transcoding is required on the bridge host.

## Tested status

The project owner confirmed live video from **nine independent cameras in UniFi
Protect**, using a Dahua DHI-XVR5216A and an ARMv7 Revolution Pi running Raspbian
Stretch with Python 3.5.3. VLC playback, macvlan discovery and systemd operation
were verified. A single-camera installation also recovered after a reboot.

A reboot with all nine cameras, extended simultaneous recording/soak testing and
the exact Protect/UniFi OS versions have not yet been recorded. These observations
cover one installation, not general ONVIF certification or all recorder models.
The release-preparation changes to XML parsing, logs and installer configuration
selection have local regression coverage and still need a hardware smoke test.

There is no fixed software camera count. Recorder session limits, RAM, threads,
file descriptors and Ethernet throughput determine practical capacity.

## Why this bridge exists

Protect was observed connecting to the virtual ONVIF device's IP for RTSP even
when its stream URI pointed directly at the recorder. The TCP listener at the
virtual IP makes that connection work. Each channel has its own IP, actual MAC,
ONVIF UUID, serial and HTTP endpoint; each endpoint exposes one `profile1`.

```text
UniFi Protect / VLC        Revolution Pi                  Recorder
    ONVIF --------------> camera IP 1:8000
    RTSP/RTP over TCP --> camera IP 1:554 ---- channel 1 ---\
    ONVIF --------------> camera IP 2:8000                  +--> recorder:554
    RTSP/RTP over TCP --> camera IP 2:554 ---- channel 2 ---/
                          ... one RTSP process
                          ... one ONVIF process
                          ... macvlan children on the existing Ethernet NIC
```

The RTSP proxy forwards requests, responses, Digest headers and binary media
**byte for byte**. It never rewrites RTSP URIs: that broke Digest authentication
during development. It does not inspect or restrict channel paths; the URI
returned by ONVIF selects the channel, and the recorder enforces authentication.
Every client connection opens its own upstream TCP connection.

## Requirements and network prerequisites

- Linux, Python 3.5.3 or newer, and `/sbin/ip` from iproute2.
- Wired Ethernet with kernel macvlan support and a switch port allowing multiple MACs.
- Unused camera IPs in the parent NIC's subnet, reserved outside the DHCP pool.
- A recorder with H.264 and RTP interleaved over RTSP/TCP.
- systemd 232 (Debian Stretch) or newer for the supplied units.
- Root for interface setup. Proxies run as `onvif-bridge` with
  `CAP_NET_BIND_SERVICE` for low ports and `CAP_NET_RAW` for `SO_BINDTODEVICE`.

Python 3.5 and Stretch are obsolete; keep the installation on a trusted network.
No extra runtime packages are required if the tools above are already present.

### Keep DHCP away from project interfaces

Before creating cameras, configure the host's network manager to ignore `ovb*`.
On a dhcpcd-managed Pi, back up `/etc/dhcpcd.conf` and place this at the **top**,
before any `interface` sections:

```text
denyinterfaces ovb*
```

Otherwise dhcpcd can assign a second address and unwanted routes to each child.
The helper deliberately does not edit DHCP configuration or restart networking.
If a child was already taken over, reload the exclusion in dhcpcd and recreate
only project children using `down`/`up` with their original configuration. Do not
restart all networking or flush the physical NIC over a remote connection.

### Check ARP policy

Linux can answer ARP for another local interface's addresses. The effective
`arp_ignore` value, `max(all, parent)`, must be 1 or 2 for this setup:

```sh
cat /proc/sys/net/ipv4/conf/all/arp_ignore
cat /proc/sys/net/ipv4/conf/eth0/arp_ignore
```

If both are 0, review the host's networking before an administrator changes and
persists the parent NIC's ARP policy. The bridge checks it but never changes it.
For its own children only, it applies `arp_ignore=1`, `arp_announce=2` and loose
`rp_filter=2`; deleting the children removes these settings. See the
[Linux kernel IP sysctl documentation](https://docs.kernel.org/networking/ip-sysctl.html).

## Get the files and preserve the prototype

Download/extract the source archive, or clone the repository on a development PC.
Use FileZilla to upload `src/`, `deploy/` and `config/config.example.json` into
`~/onvif-rtsp-bridge` on the Pi. Preserve LF line endings. Git is not required on
the Pi. Do not upload local backups, private configs, `.git/` or test runtimes.

If migrating the original scripts, keep them and make a private backup first:

```sh
backup="$HOME/bridge-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$backup"
cp -p ~/onvif_proxy.py ~/rtsp_proxy.py "$backup/"
df -h /
```

Backups may contain passwords; keep them outside the public repository.

## Configure

For a new installation:

```sh
cd ~/onvif-rtsp-bridge
cp config/config.example.json config/config.json
chmod 600 config/config.json
```

Edit that private file. All `192.0.2.x` addresses in the example are documentation
placeholders. Start with one entry in `cameras`; expand after its smoke test.
Check IP availability administratively: a failed ping does not prove an IP is free.

| Setting | Purpose |
| --- | --- |
| `bridge.id` | Unique installation name; keep stable after adoption |
| `bridge.rtsp_port` | RTSP listening port on each camera IP, normally 554 |
| `bridge.log_requests` | Optional boolean; detailed ONVIF action logs, default `false` |
| `network.mode` | `macvlan` creates children; `existing` uses already assigned IPs |
| `network.parent_interface` | Existing Ethernet NIC such as `eth0` |
| `network.prefix_length` | Must match the parent subnet, normally 24 |
| `network.route_metric` | Base child route metric, default 42700; higher than parent routes |
| `network.gateway` | LAN router for routed/VPN clients; `null` for LAN-only clients |
| `upstream.host`, `rtsp_port` | Recorder endpoint |
| `upstream.username`, `password` | Optional credentials in the returned RTSP URI |
| `upstream.rtsp_path` | Vendor path template with `{channel}` and `{subtype}` |
| `upstream.snapshot_url` | Recorder snapshot template; empty if unavailable |
| `camera_defaults` | Shared metadata, ONVIF port and video description |
| `cameras` | Nonempty array with one `id`, `ip` and `channel` per camera |

For example:

```json
{"id": "channel-2", "ip": "192.0.2.102", "channel": 2, "name": "Yard"}
```

A camera may override `subtype`, `onvif_port`, `prefix_length`, `name`,
`manufacturer`, `model`, `firmware`, `hardware_id`, `video`, `uuid`, `serial_number`,
`mac` and `interface_name`. Video overrides merge with defaults; fields are
`width`, `height`, `fps`, `bitrate_kbps` and `gov_length`. They describe the actual
stream rather than configure it. The advertised codec/profile is H.264 Main.

UUID, serial, locally administered MAC and `ovb...` interface name are derived
from `bridge.id` and camera `id`. Preserve these IDs and the first camera's entry
when adding channels. `name` is the ONVIF profile label; Protect may retain its own
camera name. Explicit interface names must start with `ovb` and fit 15 characters.
Duplicate IPs/identities are rejected. The same ports can be used on different IPs.

Child LAN routes use `noprefixroute` plus explicit routes, with a metric equal to
the base plus the array index. Ordinary host traffic keeps the lower-metric parent
routes. Set `network.gateway` for VPN/routed clients: without it, HTTP/RTSP replies
from device-bound sockets may fail even when ping succeeds. The helper does not
change the parent address, DHCP settings, gateway or default route.

`existing` mode makes no network changes. It can preserve the original one-camera
setup using the primary IP and its actual MAC. In `macvlan` mode, use additional
unused IPs, never the Pi's existing primary IP.

Validate and preview without opening listeners or changing networking:

```sh
python3 src/onvif_proxy.py --config config/config.json --check-config
python3 src/rtsp_proxy.py --config config/config.json --check-config
python3 src/bridge_network.py --config config/config.json plan
```

`plan` shows iproute2 operations; `up` also applies the child-only sysctls above.
Only owned children may be reused/removed. Failed creation attempts roll back
only interfaces created during that attempt.

## Install and start systemd

```sh
sudo sh deploy/install.sh
```

The installer validates config, creates the service user, saves private backups
under `/var/backups/onvif-rtsp-bridge/`, installs files and reloads systemd. It does
not stop processes, create interfaces, start services or enable boot startup.

**Installed configuration: `/etc/onvif-rtsp-bridge/config.json`.** This becomes
the source of truth. The uploaded `config/config.json` is not kept in sync.
Reinstalling without arguments preserves the installed config. An explicit path,
for example `sudo sh deploy/install.sh /path/to/config.json`, selects a replacement.
Installation refuses to overwrite an active bridge; stop its target first, using
the old config so its existing children can be removed.

For initial migration, stop manual ONVIF/RTSP instances with Ctrl+C. Inspect first
if necessary; do not use a broad `pkill python`:

```sh
ps -eo pid,args | grep -E '[o]nvif_proxy.py|[r]tsp_proxy.py'
sudo ss -lntup
sudo systemctl start onvif-rtsp-bridge.target
sudo systemctl status bridge-network onvif-bridge rtsp-bridge --no-pager
```

Expected: network `active (exited)`, both proxies `active (running)`. Network setup
waits up to 60 seconds for the parent's address/routes, including a default route
when a gateway is configured. Proxy crashes restart automatically. After checking
video, enable boot startup and verify it after a reboot:

```sh
sudo systemctl enable onvif-rtsp-bridge.target
```

Manage the target, not independently enabled service units. Inspect logs with:

```sh
sudo journalctl -u bridge-network -u onvif-bridge -u rtsp-bridge -n 80 --no-pager
```

Normal logs show startup and per-camera RTSP connection events, including which
side closed. They never include RTSP payloads, credentials or full stream URLs.
Enable `bridge.log_requests` only when diagnosing ONVIF actions.

## Add cameras or change configuration

Stop **before editing**, because cleanup needs the old config:

```sh
sudo systemctl stop onvif-rtsp-bridge.target
sudo cp -p /etc/onvif-rtsp-bridge/config.json /etc/onvif-rtsp-bridge/config.json.backup-$(date +%Y%m%d-%H%M%S)
sudo nano /etc/onvif-rtsp-bridge/config.json
sudo python3 /opt/onvif-rtsp-bridge/src/onvif_proxy.py --config /etc/onvif-rtsp-bridge/config.json --check-config
```

Preserve existing entries and append new channel/IP definitions. Keep all addresses
outside DHCP. If validation fails, correct the config or restore the backup before
starting again:

```sh
sudo systemctl start onvif-rtsp-bridge.target
```

The optional `deploy/add_channels.py` can extend a single-camera config; manual
editing is the normal workflow. Use `--help` and `--check` before applying it. It
backs up the config and preserves the first device's identity, but does not check
LAN IP availability. Editing/reordering entries may change child route metrics.

## VLC and UniFi Protect

From a different LAN/VPN machine, force RTSP-over-TCP in VLC and open:

```text
rtsp://192.0.2.101:554/cam/realmonitor?channel=1&subtype=0
```

Enter recorder credentials in the client. Then enable **Discover Third-Party
Cameras** in Protect, adopt each device and enter those credentials. Verify that
each device shows its intended channel, live video and recordings. Exact menu
locations depend on Protect version; see the
[official third-party camera guide](https://help.ui.com/hc/en-us/articles/26301104828439-Third-Party-Cameras-in-UniFi-Protect).

If credentials are blank in config, clients must supply them. If credentials are
configured, the URI includes percent-encoded username/password. ONVIF itself does
not authenticate callers: anyone with endpoint access can request that URI.
See the security limits below.

## Troubleshooting and rollback

- **Extra DHCP IP/default route on a child:** fix the network-manager exclusion,
  then stop/recreate project interfaces with the config that created them.
- **Boot setup fails:** inspect the network journal. Parent address/routes must
  become ready; fix the host network and restart the target if the wait expires.
- **No discovery:** allow multicast `239.255.255.250:3702`, UDP replies and the
  configured TCP ports. Use the same LAN for discovery; VPN multicast forwarding
  is not provided by this project. Switch port security must allow multiple MACs.
- **Ping works but HTTP/RTSP fails over VPN:** check `network.gateway` and client
  routes. A tunnel neighbor entry need not have an Ethernet MAC address.
- **Local Pi tests fail:** macvlan parent/child communication differs from remote
  LAN access; test playback from another device. A recorder on the parent host is
  not supported by this setup.
- **Upstream connection timeout:** verify the recorder host/port and spelling,
  then test TCP connectivity. An upstream timeout happens before Digest login.
- **Digest failure:** check credentials and unchanged URI; never add TCP payload
  substitutions. `test_rtsp.py` is an interactive diagnostic for simple MD5 Digest
  challenges, not a full authentication/video test.
- **First camera reconnects while new cameras work:** in the tested installation,
  removing/re-adopting that camera resolved the issue. Stale Protect stream
  settings are a possible explanation, not a proven root cause. First check VLC,
  logs and encoder metadata. Review recording-retention implications before
  removing a camera from Protect.
- **No video/stalls:** force TCP, match actual H.264 settings and check recorder
  connection/bandwidth limits. The proxy does not relay RTP-over-UDP ports.
- **Port busy:** inspect `ss -lntup` and old wildcard-bound manual scripts.
- **Permission denied:** use the supplied units for privileged ports/device binding.
- **Config replaced before stopping:** restore the old config, stop to remove old
  children, then apply the new config. Never flush the physical NIC.

To roll back, stop/disable the target and start the original backed-up scripts
with their previous commands. Leave unrelated interfaces untouched:

```sh
sudo systemctl stop onvif-rtsp-bridge.target
sudo systemctl disable onvif-rtsp-bridge.target
ip -o link show
```

For manually created children, use `bridge_network.py ... down` with the original
config. Private installer backups retain replaced source/config/units for review.

## Tests, security and limitations

```sh
python3 -m unittest discover -s tests -v
sh -n deploy/install.sh
```

CI runs the standard-library tests on modern Linux Python and the official
portable Python 3.5.3 Windows runtime. Linux also exercises the installer using
temporary paths and harmless command shims. Tests cover config/identities,
SOAP/XML isolation and declaration rejection, HTTP/TCP sockets, byte-preserving
Digest headers/binary payloads, boot waiting and mocked network rollback. They do
not replace macvlan/multicast/systemd/Protect checks on the Pi. CI tools are not
runtime dependencies on the bridge host.

This is a minimal Device/Media1 implementation, not a complete or certified
ONVIF server. No TLS, ONVIF authentication enforcement, motion events, PTZ, audio
processing, UDP media relay, encoder probing or automatic IP allocation is
implemented. One configuration targets one shared recorder. Snapshot URIs point
directly at the recorder; the bridge does not proxy/authenticate snapshots.

Use only on a trusted, access-controlled network. ONVIF can disclose configured
recorder credentials, and the TCP listener exposes recorder RTSP to reachable
clients. XML inputs are bounded and reject DTD/entities, but that does not make
legacy Python/Stretch suitable for Internet exposure. Keep configs, backups,
packet captures and logs with sensitive information out of public issues and Git.

## Roadmap

- Record exact Protect/UniFi OS versions and test reboot recovery with nine cameras.
- Run an extended simultaneous live/recording soak test and measure host resources.
- Test other recorder vendors using path templates without changing passthrough.
- Expand ONVIF interoperability and consider multiple recorders after validation.

## License

MIT. See [LICENSE](LICENSE). Python and system networking tools retain their own licenses.
