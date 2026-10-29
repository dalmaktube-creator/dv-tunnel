# Research checkpoint and extraction

## Provenance

Original repository: dalmaktube-creator/wg-gaming-panel; source commit
`4dd2174356d1df3eb4499615e3d9e508c8b18e71`, draft work on
`agent/webtcp-inner-bench`. Core Go files were copied byte-for-byte; hashes are
in `extraction-manifest.json`. The module path and standalone generator/test
naming were adapted. No panel application or private server config was imported.
This is a source extraction, not a transfer of original Git history. The original
repository remains untouched.

The core supports UDP relay and raw-IP TUN. Standalone primary use is TUN.
Wire encoding, TLS validation, reconnect, offload, bounded queues and default
queue settings remain unchanged by extraction.

## Operator field checkpoint, 2026-10-29

Two Ubuntu amd64 servers; IPv4 UDP echo over raw-IP TUN over WSS/TCP.
Not a full VPN-client-to-Internet acceptance result.

- Basic echo, MTU-sized DF packets, exact 256 KiB TCP content and reconnection
  after a five-second listener interruption passed earlier bounded probes.
- Native UDP: 2 Mbps for 120s, 25,000/25,000 replies. At 10 Mbps for 30s,
  538/31,248 replies were missing by the final drain deadline; aggregate
  carrier queue-full counters also increased by 538.
- Later matched 20-second ABBA near 10 Mbps held byte budget 262144 constant:

| Round | Packet slots | Sent | Missing by deadline | RTT p95 ms |
| --- | ---: | ---: | ---: | ---: |
| A1 | 64 | 20833 | 234 | 167.082 |
| B1 | 128 | 20833 | 24 | 109.810 |
| B2 | 128 | 20821 | 99 | 213.741 |
| A2 | 64 | 20834 | 83 | 110.564 |

Pooled missing: 317/41667 at 64 slots, 123/41654 at 128. This is observation,
not a causal guarantee: WAN baseline varied, B2 latency was worse, TCP retransmits
remained. Aggregate queue drops matched missing counts but cannot identify each
application sequence. Source archive SHA256:
`d7be2ab2e93f0348a7fd22e0f2a76419f87eef48d4ab58fa725c88a6501d4bc7`.

128 is experimental, not the production default or a head-of-line-blocking fix.
No 800 Mbps capacity claim is established. Port line rate is not tunnel goodput.

An earlier Python benchmark flushed/fsynced synchronously in the hot path and
timestamped before the pause; its high RTT peaks cannot be attributed solely to
the carrier. Later native sender/receiver tests write traces after measurement
and report actual offered rate, not only requested load.

## Next gates

1. Independent binary acceptance: concurrent TCP/UDP payload integrity and
   reconnection. Local tests do not replace real two-server TUN tests.
2. Routed synthetic-client acceptance in isolated network namespaces: MTU/error
   behavior, flow isolation, bidirectional mixed traffic. Not shipped yet.
3. Sustained load/capacity with verified generator, explicit resource/abort limits;
   not an unbounded 800 Mbps blast at production servers.
4. Review/implement external-link attachment in WG panel, after inspecting actual
   Easy Mesh lifecycle, policy routing and accounting contracts.
