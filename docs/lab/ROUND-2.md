# Encrypted private WSS functional checkpoint — 2026-10-30

## Decision
18/18 bounded application integrity gates passed in 3 sequential batches. This validates basic lab functionality, not maximum capacity, a performance optimization or production readiness.

## Changed scope
- Only temporary runtime and private generated configurations in DV-Lab capsules were used.
- No Moory service, DV-Lab service, production install or carrier source was modified.
- This branch adds sanitized test evidence only; the separate private-config PR is not merged.

## Identity and setup
- Published release `v0.1.0-preview.6`; stripped executable SHA256 `b7b5f02da2227ec6bdd2a1ba2698f44d6c8ba5afa65908d1109358683dad9062`.
- Archive SHA256 `aac2bd25d287282e21bc5a88ed8acb1e29ae84a90fe47f220fca31166849bcc2`.
- Private direct TUN /30, Iran dialer / Turkey listener, MTU1336, TLS verification with generated CA and hostname, offload-lowlat + explicit --split-udp.
- Default byte cap262144 and lowat131072 unchanged; no UDP admission wait, padding or camouflage.
- Batches1/2:64 packets aggregate (32 per lane in split mode). Batch3:temporary128 packets aggregate (64 per lane, confirmed in live statistics), matching the existing standalone acceptance profile; no permanent default changed.
- Source/GitHub download from capsules timed out. Public release was fetched on the computer, stripped without code edits, chunk-written to Iran, then copied over a bounded one-shot lab TCP transfer to Turkey. Both final executable hashes and versions matched.
- One private /30 only; no host default route, NAT, forwarding, firewall, panel or persistent service changes.
- Iran account/capsule:1.5/1.35 CPU and384/256MiB. Turkey account/capsule:0.75/0.65 CPU and240/176MiB. Quotas differ from round1; do not compare the two rounds as a speedup.

## Functional gates
| Case | Offered workload | Passing batches | First-batch receiver Mbps |
|---|---|---:|---:|
| http | 30 requests; status/body verified | 3/3 | Not a bulk-rate test |
| tcp-push | 8 MiB; Iran → Turkey; SHA256 verified | 3/3 | 8.023 |
| tcp-pull | 8 MiB; Turkey → Iran; SHA256 verified | 3/3 | 7.901 |
| tcp-four-streams | 4 × 2 MiB parallel; every SHA256 verified | 3/3 | 7.871 |
| udp-echo | 2500 × 1000-byte echo probes; 1 Mbps offered | 3/3 | Not a bulk-rate test |
| mixed-tcp-udp | 32 MiB TCP pull at 8 Mbps offered + 2500 UDP probes at 1 Mbps | 3/3 | 7.969 |

Across these finite synthetic tests: 90 HTTP requests, 168 MiB receiver TCP payload, and 15000/15000 unique UDP echo replies. UDP missing/corrupt/duplicate counters were zero in all six UDP phases. This is not a claim of losslessness on arbitrary traffic.

TCP was paced at8Mbps aggregate (four-stream phase2Mbps each). UDP probes were paced at1Mbps each initiating direction; echo doubles that application UDP payload across both directions. The mixed phase ran TCP for about34s, UDP for20s after the first TCP data arrived. TCP remained active but delivered rate during the exact UDP-only window was not separately measured.

## Latency, CPU and internal counters
UDP latency is client-clock nearest-rank RTT, not one-way delay. Every standalone and mixed UDP phase has2500 samples; quantiles remain per phase and are never pooled. Full p50/p95/p99/max and CPU/RSS are retained in JSON.

| Batch | UDP-only p95 / p99 ms | Mixed UDP p95 / p99 ms | Iran / Turkey queue-packet drops |
|---|---:|---:|---:|
| default64 | 127.065 / 153.829 | 121.703 / 147.063 | 19 / 0 |
| diagnostic64 | 169.593 / 206.484 | 180.767 / 239.463 | 0 / 0 |
| diagnostic128 | 122.727 / 149.347 | 116.310 / 163.107 | 0 / 0 |

- First batch: Iran recorded19 internal queue-packet drops despite complete application delivery. Do not translate internal counters into physical-WAN loss or hide them behind the application PASS.
- The second64-packet batch already recorded zero queue-packet drops on both peers. A later128-packet result cannot prove a fix; repeats at matched conditions are still needed.
- Two no_session_or_oversize drops per node occurred before the diagnostic64 first HTTP phase, not during its application phases. That counter combines causes; this does not prove which packet caused it.
- spoofed_drops also increased. They are not an application packet-loss denominator and no underlying packet capture was taken to establish their origin.
- Carrier statistics update every2s. Diagnostic64 added1.1s settling, insufficient to certify exact phase boundaries. Diagnostic128 uses2.2s and retains statistics-file age and per-lane counters.
- CPU is the carrier process only, measured from /proc/PID/stat with monotonic phase elapsed; percent of one logical core, not account or host CPU. Python harness CPU is recorded separately. Diagnostic idle intervals change CPU denominators; compare neither batch CPU nor round1 CPU as an efficiency gain.
- RSS is a .25s sampled carrier-process peak, not container RAM or total overhead.

## Completion and remaining gate
Both roles independently exited0 and confirmed their own carrier stopped and nonpersistent test TUN absent after every batch. No live or persistent tunnel was left installed. The one server short-header diagnostic per batch is the readiness connection closed without an application command, not a failed data case.

Before production or subscription use: repeat matched direct/tunnel load and queue-cap trials, reconnect/fault injection, longer mixed-load latency/soak, many-flow limits and actual integration. No carrier rewrite was justified by these passing integrity checks alone.

Machine-readable source values, exact application counts, fixture hashes, diagnostic snapshots, sampling limitations, and cleanup verdicts are in `round-2-evidence.json`. No private keys, tokens, configs, server addresses or carrier logs are included.
