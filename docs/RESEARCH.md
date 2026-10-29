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
queue settings remained unchanged at initial extraction. Later DV changes are
listed separately in the manifest and SPLIT-UDP.md.

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

## Mixed live-link failure checkpoint

The operator provided dv-live-iran.zip and dv-live-turkey.zip for the same existing
preview.2 link. Four TCP upload/download checks (256 KiB each direction) passed
content integrity. Concurrent UDP initiation returned 1681/2000 from Iran
(319 missing by drain, 15.95%) and 1486/2000 from Turkey (514, 25.7%). Zero invalid
or duplicate UDP payloads were reported. PID unchanged and session delta zero on
both peers; test sockets closed, carrier left running.

Carrier drop deltas: Iran545 (queue_packets481, queue_expired64), Turkey313
(queue_packets249, queue_expired64). Aggregate858 differs from UDP missing833:
these counters include TCP/control/IP packets, not sequence-attributed losses.
Queue-byte drops zero. This establishes local queue pressure/expiry, NOT measured
WAN retransmission causality. No capacity claim follows.

Input archive SHA256:
- Iran: e333b059d62601e8f268a8b81044a7b4c0f5b72d81aae07d21cb551c4d7d1512
- Turkey: bc6e69cb619757ff552941ae06c4942d2ad4d25e9545a303051b4945a495958e

Next opt-in experiment: SPLIT-UDP.md. TCP-only static reference review:
TCP-REVIEW.md. No remote change or automatic profile migration was performed.

## First preview.3 split-profile field checkpoint

Operator run: 4/4 TCP payload checks and 4000/4000 unique UDP returns across the two initiating directions; 0 missing by drain. Queue drop/expiry delta 0 across both lanes/endpoints, session delta 0. Both carrier PIDs unchanged, both lanes connected, own test socket cleanup OK.

Iran source-allowlist rejection counter increased by one, Turkey by zero; this is not a valid-probe loss or evidence of an attack. No capture identifies the rejected source. Historical startup drops were unchanged. This single short run is NOT a randomized causal proof versus the earlier failure, sustained stability, forwarded-client acceptance or capacity evidence.

Report JSON hashes:
- iran: 7aa1672728574bcd1c2aab8a9b0b74cb4d710893733a3bfb4d820cb8edf50ada
- turkey: 2af4f6df9d5441ef5c8531a677dab418fada29b4c32ec0c560ee0c557317788f

Next gate: SOAK-CHECK.md; existing runtime remains unchanged.

## First soak aborted: measurement not accepted

During the first low-rate phase, Iran datagram-lane queue_packets increased by 3; Turkey queue drops increased by 0. No completed phases were saved. Both endpoints remained connected with stable session counters in sampled statistics; no final PID checkpoint was saved. These datagram-lane counters include UDP/ICMP, not a per-probe sequence mapping.

No queue-byte or expiry drop increase was present. Host resource guard did not report the trigger: the Iran queue monitor stopped the test, then the peer aborted. Neither permanent wire loss nor a particular WAN mechanism is established. Incomplete application sent/received/TCP counters were discarded by checker v1, so a percentage or TCP-integrity verdict must NOT be invented.

Source report JSON hashes:
- iran: a32d3fb74407911bd45fd0df4192e4ec53d20ed81445feb71aa379b97de488e5
- turkey: 678d9b405cc302a829b4a3015846f629cc974b5eca859e43179767cbee0fe76d

Checker v2 corrects partial reporting and separates measurement termination from the zero-drop pass/escalation criterion. This is a checker change, not a claimed carrier fix. Details: SOAK-CHECK.md. Runtime queues and private settings remain unchanged.

preview.4 evaluates one-shot datagram writer scheduling yield/retry, keeping the
existing caps and budgets unchanged. This directly tests a bounded runnable-writer
burst hypothesis, not larger buffers. See SPLIT-UDP.md; new field acceptance pending.
