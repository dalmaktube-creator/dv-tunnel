# Standalone acceptance v1

**Not a capacity benchmark or full forwarded-client/Internet test.**
It runs the independent binary using a private temporary copy of an already
operator-owned direct TUN field configuration. It requires no panel process.
Existing configs and their run scripts are not edited. Only the temp copy sets
`queue_packets=128` and a private statistics path; original byte/offload/lowat
settings are required. It refuses an existing wft0, subnet overlap or occupied
listener rather than stopping or deleting unknown resources.

Prerequisites: Linux root, Python 3, iproute2, ping, TUN; Python/OpenSSL for pair
generation if a valid pair does not exist yet. No automatic package installation.
Both peers need the same verified independent binary, `tools/acceptance.py` and
`tools/dv_pair.py`. Match the configured role, direct topology and peer /32 allowlist.

Start B/listener first; leave the terminal open. Start A/dialer when B says
`SERVER_READY`. Each role has automatic bounded cleanup and a final JSON/ZIP.
Stop with Ctrl-C only if needed; closing the SSH session is not a test procedure.

```bash
# B / listener; paths are examples
python3 -u tools/acceptance.py server --binary "$PWD/dist/dv-tunnel" \
  --field-dir /path/to/node-b --local 10.254.253.2 --peer 10.254.253.1
# A / dialer
python3 -u tools/acceptance.py client --binary "$PWD/dist/dv-tunnel" \
  --field-dir /path/to/node-a --local 10.254.253.1 --peer 10.254.253.2
```

Checks: four concurrent TCP uploads/downloads, exactly 256 KiB each direction per
flow, deterministic payload and SHA256; four-socket UDP echo in both initiating
directions, about 1.92 Mbps payload per sender for 10s; DF IPv4 1336-byte MTU
probe; intentional three-second stop/restart of **only the runner-owned listener
carrier**; verify dialer sessions advance and repeat TCP/UDP integrity. Existing
production VPN/tunnel processes are never killed. TCP senders share a 2 Mbps
payload pacer per endpoint; the UDP return traffic adds to total link load.

Whole-host CPU >=85% in two 1s samples or available memory <192 MiB stops the
run. Resource guards reduce risk but do not promise production is unaffected.
UDP has a five-second no-valid-reply guard and final drain; requested pacing is
not an achieved throughput guarantee. This Python functional harness is not used
for high-rate capacity, latency quantiles or fine jitter analysis.

The client archive includes its own cleanup and peer application measurements
captured before peer teardown. **It does not prove server cleanup:** read the
server console or server archive independently. Both should say `CLEANUP: OK`.
Server `completed` means its measurements finished; client `passed` is the
functional verdict. Reports never contain configurations, tokens, PEMs or logs.
Upload both `results/latest-client.zip` and `results/latest-server.zip` for review.

Unit tests exercise framing and actual loopback TCP/UDP payloads. They cannot
validate real TUN routing, kernel offload, production coexistence or WAN recovery;
those need the operator-run two-server test above.
