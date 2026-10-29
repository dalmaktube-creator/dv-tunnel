# Experimental split-udp-v1 profile (preview.3)

Opt-in only. The default remains single-stream, compatible with preview.2.
This profile uses TWO independently authenticated TLS/WSS/TCP connections on the
same existing listener/path/token: one for inner UDP/ICMP, one for inner TCP and
other/unknown IP. There is no new outer UDP protocol or extra listener port.
IPv4 fragments use their IP protocol. IPv6 walks at most eight extension headers;
unknown/encrypted/unparseable chains use bulk. Existing offload limitations remain.
No per-flow TCP sharding, bandwidth guarantee or complete HOL elimination is claimed.
All inner UDP flows still share one ordered TCP connection. The common WAN, TUN
reader and TUN receive device remain shared resources.

## Safety and accounting

- Explicit `--split-udp` core CLI flag, not an incompatible config.json key.
- Manager stores only a boolean in nonsecret root-owned `link.json`; preserves
  private config.json byte-for-byte and saves previous link metadata separately.
- Both peers must select the SAME profile. Mode/lane confirmation is mandatory;
  old or mismatched peers cannot silently claim connected split mode.
- Each lane has an independent writer and queue. Original aggregate packet/byte
  queue caps are DIVIDED, not duplicated: 64 packets/262144 bytes becomes
  32 packets/131072 bytes per lane. An odd remainder goes to bulk. Byte cap zero
  still means packet-limited only. Queue expiry remains one second; writes five.
- Separate connections necessarily have separate TLS/kernel buffers and bounded
  in-flight messages. This is NOT a claim of unchanged total process/kernel memory.
- `connected=1` requires both lanes; `sessions` counts outer session establishments
  (normally two on startup). Recovery reconnects only the failed lane. No replay
  queue is migrated between sessions. Surviving-lane traffic can continue while
  overall readiness is zero, but clients should treat that state as degraded.
- `transport_profile` and `lanes` add safe per-lane counters. `max_write_us` is
  cumulative since process start (includes host scheduling), not TCP retransmits
  and not an additive counter. Time in the bounded queue is separate.
- Upgrade alone NEVER selects this profile. No automatic import, startup/enable,
  NAT/firewall/default-route/sysctl changes or production-client attachment.

## Operator sequence on the existing pair

First update BOTH peers while they still use single-stream:

```sh
dv-tunnel update --version v0.1.0-preview.3 --restart
```

Then execute on BOTH peers (brief link interruption until the other matches):

```sh
dv-tunnel profile main --split-udp && dv-tunnel restart main --yes
```

Do not wait for a connected check on the first peer before switching the second.
After both restart, run the read-only mixed checker from this source revision,
server on Turkey first, client on Iran. The checker records profile and lane
counters; compare achieved delivery with the original run, not just menu green.

Recovery: on BOTH peers select the original profile and explicitly restart:

```sh
dv-tunnel profile main --single-stream && dv-tunnel restart main --yes
```

Switch both peers back before rolling back to preview.2: the new updater refuses
an older runner that would ignore an enabled split profile. Keep a second SSH
session open. Only this synthetic test link should be attached at this stage.

## Local validation, not field acceptance

TLS/WSS socket tests with pipe-backed fake TUNs intentionally stall the bulk
writer, exhaust its queue, and verify valid UDP packet delivery in both directions.
They also reconnect the UDP lane without replacing bulk, verify negotiation,
queue limits, lane classification, rejection of wrong-lane packets and cancellation.
Legacy tests and Go race checks remain required. These do not simulate production
WAN retransmissions or validate real Linux TUN/offload; operator tests remain a gate.

Prepublication local checks: 23 Go test functions passed under the race detector
with three repetitions; go vet passed; 52 Python tests passed; Linux amd64/arm64
built. The preview.2 updater accepted the exact seven-member preview.3 amd64
package locally. New real-server mixed traffic acceptance is still pending.
