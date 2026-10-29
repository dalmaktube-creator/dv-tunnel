# Bounded mixed-load and resource acceptance

This is one operator-run suite on the existing managed point-to-point test link.
It never updates, restarts or tunes the carrier, changes private configuration,
routes or firewall, or inventories production processes. Only its own application
sockets are opened. Resource sharing with production remains real.

## Why this replaces another single-rate retry

The supplied rate-ladder reports from 2026-10-29 22:53–22:55 UTC reconcile across
both endpoints: 102,000 unique UDP echoes returned, 37 TCP upload/download
transactions completed, and carrier drop/session and kernel TUN error deltas
were zero. Both 6.72 and 9.6 Mbps per-initiator steps passed. At 9.6 Mbps the
reported p99 RTT was about 213–215 ms despite zero missing replies.

Run-wide host CPU peaked at 93% in Turkey and about 57.1% in Iran. These are
whole-host sampled values, not carrier CPU or phase-specific measurements. A
single host sample above 85% does not by itself trip the existing two-consecutive
sample guard. Do not attribute those peaks to the carrier or say they occurred
at the highest load without phase markers.

## Plan

Use `--queue-diagnostic --lean-observer --comprehensive
--cpu-baseline-seconds 15` on both peers, with the existing version/profile/cap
and wait expectations. Both sides must use the exact same script; the greeting
checks the complete phase-plan SHA-256 before test load starts.

| Phase | Seconds | UDP payload Mbps per initiating sender | Bulk TCP workers |
|---|---:|---:|---:|
| UDP isolation | 30 | 9.6 | 0 |
| Mixed sustained known rate | 60 | 9.6 | 4 |
| Mixed higher step | 30 | 14.4 | 4 |
| Mixed upper test step | 30 | 19.2 | 4 |
| Light-load recovery | 30 | 4.8 | 4 |

Control RPC still uses TCP in the first phase; only bulk TCP data is absent.
Each UDP probe uses four flows and 1200-byte payloads. Both endpoints initiate
UDP, and echoes add return traffic. Mixed phases retain the original aggregate
2 Mbps TCP payload pacer at each endpoint; this is NOT a TCP-only maximum
throughput benchmark. At the upper planned step, each link direction carries
about 38.4 Mbps of UDP payload including echoes, plus paced TCP and overhead.

The whole plan offers approximately 1.08 GB of application payload including
echoes and nominal TCP pacing, plus protocol overhead, if all stages run.
Actual TCP transactions, rate and resource usage are measured, not assumed.
Allow about 4–5 minutes after starting both peers.

Every higher step, including final light-load recovery, requires the prior gate
to pass. Any quality failure blocks escalation; a hard safety stop stops load.
Recovery is deliberately skipped after failure rather than bypassing a guard.
Save both reports even on early stop; do not rerun automatically.

## What each report provides

- UDP requested/achieved rate, sent/unique echoes, missing by drain deadline,
  corruption, duplication, per-flow ordering and p50/p95/p99/max RTT.
- Completed bidirectional TCP content checks and completed payload byte counts.
- New carrier drops by reason, wait/retry outcomes, session/PID continuity and
  kernel TUN error counters.
- Explicit endpoint-local monotonic phase windows, including partial/aborted
  probes. CPU summaries never infer windows from cumulative packet counts.
- Per-stage and pre-load-baseline mean and peak sampled CPU: whole host, host
  steal, carrier child, Python service runner, and checker.
- Process CPU both as one-core percent and percent of total logical-CPU
  capacity. Compare the latter to whole-host CPU; 100% of one core on a
  two-vCPU machine is 50% of total CPU capacity.
- Minimum available host memory and peak RSS of the three observed processes.
- Sample coverage, identity/counter/gap rejection counts, raw numeric samples,
  and highest passed mixed rate. Missing resource coverage cannot claim a
  complete successful comprehensive measurement.

The half-second observer remains lean: one owned-socket preflight, no periodic
`ss` subprocess scans. Sampled CPU peaks are interval averages, not instantaneous
peaks. Only intervals fully inside local phase markers are included; start/end
fringes are omitted and sampled coverage is explicit. Endpoint windows differ
slightly because the client waits for in-flight TCP transfers to complete.

Host CPU includes production work, kernel network work and steal; process CPU
does not include all kernel work on its behalf. Checker CPU includes its threads,
not every active external child. RSS excludes socket/kernel buffers. Baseline
versus load is observational, not a controlled causal attribution.

## Unchanged safety and limits

- Whole-host CPU >=85% on two consecutive guard samples or available memory
  below 192 MiB stops the test.
- Any carrier drop fails the phase. The existing immediate hard stop remains
  >=64 new packet drops, any byte/expiry drop, or increased TUN kernel errors.
- Session/PID/profile changes, lost observation, missing echo and payload errors
  retain their stops. P99 over 500 ms or achieved rate below 90% fails the gate.
- Original probe limits remain <=2000 packets/s, <=60,000 packets per probe and
  <=120 seconds. No buffer, queue, delay, guard or service setting is increased.

`highest_passed_mixed_udp_mbps_per_initiator` means the highest passing level of
this finite plan, NOT the maximum capacity of the tunnel, path or server port.
`capacity_limit_established` remains false. If all steps pass, capacity above
the plan ceiling remains untested. If a generator underfills, resource guard
fires, or latency fails first, report that specific limit instead of a line-rate
capacity claim.

This suite does not validate forwarded users/NAT/panel integration, other UDP
packet sizes, induced WAN loss, long-duration uptime or censorship resistance.
Those remain separate acceptance questions; production traffic is not moved.
