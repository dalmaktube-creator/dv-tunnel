# First autonomous DV Lab tunnel checkpoint — 2026-10-30

## Decision

Do not rewrite the carrier or advertise production capacity from this run.
Measure direct-link baseline and isolate queue/CPU costs first. This checkpoint
records an experiment, not a performance optimization or release.

## Reproduction identity and boundary

- Public release: v0.1.0-preview.6; source main at review: cc82b4816ace194caff44182e8a7e2fce8590bff.
- Release archive SHA256: aac2bd25d287282e21bc5a88ed8acb1e29ae84a90fe47f220fca31166849bcc2.
- Executable stripped locally without source edits; SHA256: b7b5f02da2227ec6bdd2a1ba2698f44d6c8ba5afa65908d1109358683dad9062.
- Two real, isolated Linux capsules: Iran dialer and Turkey listener.
- Generated `offload-lowlat` pair, explicit `--split-udp` on both ends.
- One TUN /30 only; no host default route, host panel, host NAT, production service or firewall changes.
- Each lab account: 0.5 CPU, 192 MiB; capsule: 0.4 CPU, 128 MiB.
- Three sequential ten-second iperf3 trials, no repetitions or direct baseline.
- TCP: one stream, then four streams. UDP: 5 Mbps offered, 1000-byte datagrams.
- Receiver goodput, not sender throughput. CPU is the WHOLE lab account,
  including controller, rootless networking and benchmark, not Go-only CPU.
  CPU values below are percent of ONE logical core; Iran exposes two cores,
  Turkey one. Approximately one-second cumulative-counter sampling, aligned to
  integer-second iperf start times. Do not compare to unrelated panel CPU figures.

| Trial | Receiver Mbps | Iran lab CPU, one-core % | Turkey lab CPU, one-core % |
| --- | ---: | ---: | ---: |
| One TCP stream | 44.395 | 42.45 | 19.05 |
| Four TCP streams, aggregate | 27.334 | 48.60 | 25.81 |
| UDP, 5 Mbps offered | 4.914 | 38.77 | 27.10 |

## Interpretation and unresolved issues

- Four-stream aggregate goodput was lower, not higher. A single sequential run
  cannot distinguish path variation, TCP-over-TCP interaction, dropping queues,
  offload behavior and CPU throttling. It does NOT prove a language bottleneck.
- CPU quotas throttled during the tests. The Iran account was near its 50%-of-one-core ceiling in the four-stream phase.
- UDP application traffic traversed the TCP/WSS carrier. iperf reported 0.144%
  loss, but sent/received/lost packet counts do not reconcile; do not certify
  this loss percentage, losslessness or suitability for gaming.
- TCP sender retransmission counters were high. Treat them as inner-TCP
  observations, not direct measurements of physical WAN packet loss.
- A temporary HTTP binary transfer timed out; bounded peer TCP transfer later
  succeeded and executable hashes matched. Setup time is not a carrier benchmark.
- No reverse, simultaneous TCP+UDP, browser/DPI, game, panel integration,
  many-user, long-running or baseline-relative efficiency acceptance was run.
- TLS verification and token authentication were not disabled. Website fallback
  is not browser impersonation or proof against blocking.

## Next controlled experiments

1. Repeat direct TCP baseline and tunnel trials in both directions under identical quotas.
2. Record carrier-process CPU separately from whole-account CPU and rootless networking.
3. A/B offload and split lanes before changing queues; compare delivered bytes,
   retransmits, CPU throttling, memory and tail latency at matched offered load.
4. Mixed TCP download + paced UDP integrity/latency with reconciled application counters.
5. Only after comparable measurements, choose bounded code optimization and test
   its regression suite. Rust or kernel code is not automatically lighter or safer.

Machine-readable measurements and methodological caveats are in
`round-1-evidence.json`. No private keys, tokens, configs or server addresses are included.
