#!/usr/bin/env python3
"""Generate a private, point-to-point field probe; never install or run it here."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile

VARIANTS = ("plain", "batch", "offload", "offload-lowlat")
PRIVATE = tuple(ipaddress.ip_network(n) for n in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))

# These checks run on EACH target, before the carrier or interface is created.
PREFLIGHT = r'''
import ipaddress, json, socket, subprocess, sys
net = ipaddress.ip_network(sys.argv[1])
for row in json.loads(subprocess.check_output(["ip", "-j", "-4", "route", "show", "table", "all"])):
    dest = row.get("dst", "default")
    if dest == "default":
        continue
    route = ipaddress.ip_network(dest, strict=False)
    if route.prefixlen and net.overlaps(route):
        sys.exit("test subnet overlaps an existing route; choose another /30")
for link in json.loads(subprocess.check_output(["ip", "-j", "-4", "addr", "show"])):
    for addr in link.get("addr_info", []):
        if ipaddress.ip_interface(str(addr["local"]) + "/" + str(addr["prefixlen"])).network.overlaps(net):
            sys.exit("test subnet overlaps an existing address")
if sys.argv[2] == "listen":
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", int(sys.argv[3])))
'''.strip()

RUN = r'''#!/usr/bin/env bash
# Foreground test only: no service installation, default route, NAT or sysctl.
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
binary="${1:?usage: bash run.sh /absolute/path/to/dv-tunnel}"
[[ "$binary" = /* && -f "$binary" && -x "$binary" ]] || { echo 'use an absolute executable path' >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo 'TUN test requires root; inspect this script first' >&2; exit 1; }
for cmd in ip python3; do command -v "$cmd" >/dev/null || exit 1; done
[ -c /dev/net/tun ] || { echo 'missing /dev/net/tun' >&2; exit 1; }
if ip link show dev __IFACE__ >/dev/null 2>&1; then echo 'test interface already exists; refusing to reuse it' >&2; exit 1; fi
python3 - __SUBNET__ __MODE__ __PORT__ <<'PREFLIGHT_EOF'
__PREFLIGHT__
PREFLIGHT_EOF
"$binary" --config "$PWD/config.json" --check
pid=''
cleanup() {
  rc=$?; trap - EXIT INT TERM
  if [ -n "$pid" ]; then kill -TERM "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi
  # The carrier owns a non-persistent TUN. Closing its fd removes its interface
  # and connected route. Never delete an interface by name during cleanup.
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$binary" --config "$PWD/config.json" & pid=$!
ready=0
for ((n=0; n<100; n++)); do
  kill -0 "$pid" 2>/dev/null || { wait "$pid"; exit 1; }
  if ip link show dev __IFACE__ >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.1
done
[ "$ready" = 1 ] || { echo 'TUN creation timed out' >&2; exit 1; }
ip address add __LOCAL__/30 dev __IFACE__
ip link set dev __IFACE__ mtu 1336 up
echo 'Field probe running; peer __PEER__. Ctrl-C stops only this carrier.'
wait "$pid"
'''


def generate(out, listen_host, subnet, topology="direct", variant="offload-lowlat",
             port=42443, iface="wft0", queue_packets=0):
    """Create a new directory only; sensitive configs never go to stdout."""
    host = ipaddress.IPv4Address(listen_host)
    if host.is_multicast or host.is_unspecified or host.is_loopback or host.is_link_local or int(host) == 0xffffffff:
        raise ValueError("listener must be a reachable unicast IPv4 address")
    net = ipaddress.ip_network(subnet, strict=True)
    if net.version != 4 or net.prefixlen != 30 or not any(net.subnet_of(n) for n in PRIVATE):
        raise ValueError("choose an unused RFC1918 IPv4 /30")
    if host in net:
        raise ValueError("listener address must be outside the TUN subnet")
    if topology not in ("direct", "reverse") or variant not in VARIANTS:
        raise ValueError("invalid topology or variant")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise ValueError("choose a dedicated TCP port in 1024..65535")
    if not re.fullmatch(r"wft[0-9]{1,3}", iface):
        raise ValueError("test interface must be wft0..wft999")
    if not isinstance(queue_packets, int) or isinstance(queue_packets, bool) or not 0 <= queue_packets <= 256:
        raise ValueError("queue_packets must be 0 (default 64) or 1..256")
    out = Path(out)
    # Validate everything before creating the final directory. Exclusive mkdir
    # rejects files, existing directories and symlinks without overwriting.
    if out.exists() or out.is_symlink():
        raise FileExistsError("output already exists")
    with tempfile.TemporaryDirectory(prefix="dv-field-cert-") as tmp:
        os.chmod(tmp, 0o700)
        cert, key = Path(tmp)/"cert.pem", Path(tmp)/"key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
            "ec_paramgen_curve:P-256", "-nodes", "-days", "7", "-subj", "/CN=dv-field.test",
            "-addext", "subjectAltName=DNS:dv-field.test", "-addext",
            "basicConstraints=critical,CA:TRUE", "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True, timeout=20)
        cert_pem, key_pem = cert.read_text(), key.read_text()
    ips = [str(a) for a in net.hosts()]
    token, path = secrets.token_urlsafe(32), "/field/" + secrets.token_hex(16)
    out.mkdir(mode=0o700)
    try:
        for index, side in enumerate(("node-a", "node-b")):
            directory = out/side
            directory.mkdir(mode=0o700)
            listen = side == ("node-b" if topology == "direct" else "node-a")
            mode = "listen" if listen else "dial"
            config = dict(role="ingress" if side == "node-a" else "egress", mode=mode,
                tun=iface, token=token, path=path, allowed_sources=[ips[1-index]+"/32"],
                stats_file="stats.json", batch=variant == "batch", tun_offload="offload" in variant)
            if queue_packets:
                config["queue_packets"] = queue_packets
            if variant == "offload-lowlat":
                config.update(notsent_lowat=131072, queue_bytes=262144)
            if listen:
                config.update(listen="0.0.0.0:%d" % port, cert_pem=cert_pem, key_pem=key_pem)
            else:
                config.update(url="wss://%s:%d%s" % (host, port, path),
                              ca_pem=cert_pem, server_name="dv-field.test")
            replacements = {"__IFACE__": iface, "__SUBNET__": str(net), "__MODE__": mode,
                "__PORT__": str(port), "__LOCAL__": ips[index], "__PEER__": ips[1-index],
                "__PREFLIGHT__": PREFLIGHT}
            script = RUN
            for old, new in replacements.items():
                script = script.replace(old, new)
            for name, text in (("config.json", json.dumps(config, indent=2)+"\n"), ("run.sh", script)):
                dest = directory/name
                dest.write_text(text)
                dest.chmod(0o600)
        summary = dict(schema_version=1, topology=topology, variant=variant, queue_packets=queue_packets or 64,
            subnet=str(net), node_a_ip=ips[0], node_b_ip=ips[1], iface=iface,
            listen_host=str(host), port=port, certificate_days=7,
            scope="point-to-point IPv4 TUN only; no forwarding, NAT, client migration or DPI proof")
        (out/"summary.json").write_text(json.dumps(summary, indent=2)+"\n")
        (out/"summary.json").chmod(0o600)
    except BaseException:
        shutil.rmtree(out)
        raise
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--listen-host", required=True, help="listener's reachable IPv4, not the dialer's")
    ap.add_argument("--subnet", required=True, help="unused RFC1918 /30 on BOTH hosts")
    ap.add_argument("--topology", choices=("direct", "reverse"), default="direct")
    ap.add_argument("--variant", choices=VARIANTS, default="offload-lowlat")
    ap.add_argument("--port", type=int, default=42443)
    ap.add_argument("--iface", default="wft0")
    ap.add_argument("--queue-packets", type=int, default=0, help="0/omitted keeps 64; experimental explicit cap 1..256")
    args = ap.parse_args()
    try:
        generate(args.out, args.listen_host, args.subnet, args.topology, args.variant, args.port, args.iface, args.queue_packets)
    except (ValueError, OSError, subprocess.SubprocessError):
        ap.exit(1, "Generation failed: check arguments, new output directory and openssl. No server was changed.\n")
    print("Private field-test pair created. Transfer each side securely; do not post config.json. Nothing installed or started.")


if __name__ == "__main__":
    main()
