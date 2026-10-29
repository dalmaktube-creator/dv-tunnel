# DV Tunnel

**Experimental, standalone Linux IP/UDP tunnel over TLS WebSocket/TCP.**
No WG panel, database, web UI, WireGuard or AmneziaWG installation is required
for raw-IP TUN mode. TCP and UDP applications can use the resulting IP link.
Linux amd64 and arm64 builds are supported. TUN needs `/dev/net/tun` and network
administration privileges; generation and compilation do not need root.

This is a research build, not a stable release, an 800 Mbps capacity claim,
a censorship-resistance guarantee, or a substitute for application acceptance.
Outer TCP head-of-line blocking remains, including for inner UDP/QUIC.

## Build and verify

Use a security-supported Go toolchain (verified here with Go 1.27.3), Python 3,
and OpenSSL. Build from this repository, not from an untrusted binary.

```bash
go test -race ./...
mkdir -p dist
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath \
  -ldflags '-X main.version=dv-initial-extraction' -o dist/dv-tunnel ./cmd/dv-tunnel
DV_CHECK_BINARY="$PWD/dist/dv-tunnel" python3 -m unittest discover -s tests -v
sha256sum dist/dv-tunnel
```

Change `GOARCH` to `arm64` for ARM servers. Compare the transferred executable's
hash on both servers. Keep build revision and hash with field results.

## Create a standalone two-server pair

Choose an unused private `/30` on **both** servers and a dedicated reachable TCP
port. The operator must allow that port from the peer; scripts edit no firewall.

```bash
python3 tools/dv_pair.py --out ./private-pair \
  --listen-host 198.51.100.10 --port 42443 \
  --subnet 10.254.253.0/30 --iface wft0 --topology direct \
  --variant offload-lowlat
```

Replace the documentation IP with the listener's actual IPv4. Transfer `node-a/`
to server A and `node-b/` to server B over authenticated SSH/SCP. **Do not publish
these directories:** they contain authentication material and the listener key.

Direct: A dials B; start B first. Reverse: B dials A; use `--topology reverse`
and A's reachable listener address, then start A first. Logical roles do not
change. On each server, in its transferred directory:

```bash
sudo timeout --signal=TERM --kill-after=5s 180s \
  bash ./run.sh /absolute/path/to/dv-tunnel
```

This creates only a foreground TUN and its connected `/30` route. It refuses an
existing interface, subnet overlap, or occupied listener. It installs no service,
NAT, forwarding rule, default route, DNS settings, panel or package. The carrier
owns a nonpersistent TUN; closing it removes its interface. Do not delete unknown
interfaces. Privileged execution always has risk: use a lab first.

A: `ping -I wft0 10.254.253.2`; B: `ping -I wft0 10.254.253.1`.
A live process is not proof of a connected TLS session: inspect fresh `stats.json`
locally for `connected: 1`. Never share `config.json` or private PEMs/tokens.

`--queue-packets 128` is experimental opt-in. Omitted/0 keeps 64; accepted explicit
bounds are 1..256. Byte and one-second age limits remain independent. A bigger
buffer is not a guarantee of better latency.

## Acceptance and integration

- [Standalone acceptance](docs/ACCEPTANCE.md): simultaneous TCP/UDP integrity and
  reconnection, using an existing operator-owned field pair.
- [Integration contract](docs/INTEGRATION.md): planned external-link attachment
  for WG panel and other systems; not implemented auto-discovery.
- [Research checkpoint](docs/RESEARCH.md): evidence, limitations and provenance.
- [Security boundaries](docs/SECURITY.md).

The original panel repository is not changed by this extraction. No public
release or open-source license is implied by this private repository.
