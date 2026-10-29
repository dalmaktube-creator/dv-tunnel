# Bounded split-link soak and staged mixed load

Run `tools/soak_check.py server` on the existing Turkey link and wait for
SOAK_SERVER_READY, then `client` on Iran. Requires the active managed `main`,
10.254.253.2/.1 on wft0, split-udp-v1 (optional explicit instance/interface/local/peer).
No update, config edit, service restart, route/firewall/sysctl or TUN mutation.
The script pins and hashes the previous live checker, which pins/hashes the
original application helper. It never constructs that helper's Carrier.

## Fixed sequence

| Phase | Seconds of UDP offered load | UDP packets/s per initiator | Requested UDP payload Mbps per initiator |
| --- | ---: | ---: | ---: |
| Soak | 120 | 200 | 1.92 |
| Step 1 | 30 | 500 | 4.8 |
| Step 2 | 30 | 1000 | 9.6 |
| Step 3 | 20 | 2000 | 19.2 |

Both endpoints independently initiate four-socket UDP echo streams. Echo returns
add load: at the last target, approximately 38.4 Mbps UDP payload is sent in EACH
direction if pacing is achieved, before TCP and protocol overhead. Four client
TCP workers continuously repeat integrity-checked 256KiB upload/downloads in
parallel, using a shared 2 Mbps sender pacer per endpoint. TCP remains active
through each UDP phase, not just an initial burst. Last transfers may finish
slightly after the UDP window. Typical whole execution is about four minutes;
server's seven-minute wall limit includes operator startup delay.

A new bounded-array UDP probe records achieved offered payload rate, unique valid
returns, missing by up to five-second drain, corruption/duplicates/per-flow
reordering, and RTT p50/p95/p99/max (includes host scheduling). No packet capture,
private config or hot-path file writes. At most 60000 packet timestamps and seen
bits per probe. Python pacing is measured, NOT a capacity or line-rate benchmark.
No 800 Mbps inference is valid.

## Conservative safety gates

- Existing whole-host guard: CPU85% twice at one-second samples, or available
  memory below192MiB. Guard stops TEST traffic, never restarts/stops the carrier.
- Carrier statistics once a second, PID check about every five seconds: 64 new packet-cap drops, any byte/expiry drop, session/profile change,
  disconnected/stale stats or PID change stops the test. Any smaller packet-cap
  increase is recorded and still fails the phase and blocks escalation. Stats publish every two seconds: repeated samples and
  missed short transients are possible. Whole-host guards cannot guarantee no
  production impact. Source-allowlist rejections are recorded, not called attacks.
- Do not escalate after a failed phase: missing/corrupt/duplicate/reordered UDP,
  TCP integrity/error, no completed TCP transfer, p99 RTT above500ms, achieved
  offered rate below90% of requested, or carrier drop growth. These are experiment
  limits, not universal application requirements. No echo while still offering for
  five seconds stops probing. Final-drain missing is separately counted.
- Socket waits are bounded. Cleanup closes only test-owned sockets. Neither
  Ctrl-C nor a guard should stop the existing tunnel. Do not restart the service
  manually during the run.

Each endpoint saves safe JSON and ZIP, unique-run folder plus latest aliases under
`/root/dv-soak-results/`. Samples, phase results, app/counter integrity and guard
reason survive normal handled failures. An early pinned-helper download failure
prints SOAK_STARTUP_FAILED before any test or carrier changes. A failed phase may
have no completed probe summary; samples/error describe that failure, not a pass.
Server status completed means it served the run, NOT a quality verdict. Client
quality gates plus both endpoint reports must be reviewed. Finish control replies
are compact; full samples remain in the separate endpoint ZIPs.

No remote test or high-rate acceptance has been claimed by adding this script.

## Checker v2: honest abort reports and separate gates

The first v1 soak stopped in its first phase when the Iran datagram lane recorded
three packet-cap drops. The abort path omitted incomplete UDP/TCP outcomes; no
UDP loss percentage or sustained success can be reconstructed from that report.
This was an instrumentation limitation, NOT evidence that the carrier drops were
imaginary. The runtime has NOT been changed or declared fixed by checker v2.

V2 preserves incomplete probe counts and completed TCP transfer counts, attempts
final carrier/PID checkpoints even on errors, and records server probe summaries
in the cleanup report. Aborted probes label unresolved packets as
`unreturned_at_stop`, not `missing_by_deadline`: a full drain did not finish.

Measurement stop and pass/fail are now separate:
- ANY new carrier drop still fails the phase and forbids the next rate step.
- A small packet-cap drop is recorded while the bounded low-rate phase finishes,
  so its delivery percentage is measurable rather than cut off immediately.
- Immediate queue hard-stop is 64 NEW packet-cap drops cumulatively per endpoint
  since its test start, OR any byte-cap/expiry drop. No carrier queue size, byte
  budget, source allowlist or runtime setting is modified. Existing CPU/memory,
  connection/PID/profile, socket, no-echo and wall-clock bounds remain.

Thus a three-drop run is not converted into a pass; only the measurement can
finish. This replaces v1's immediate first-packet-cap-drop stop above. The fixed
rates and zero-drop escalation criterion are unchanged. Use v2 on BOTH endpoints.

For a version-controlled preview.4 experiment, pass `--expect-version v0.1.0-preview.4`
on BOTH server and client. The local runtime is checked before test sockets open;
the client also verifies the server greeting version. No implicit version upgrade
or carrier restart is performed by the checker.
