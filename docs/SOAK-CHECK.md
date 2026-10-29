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
- Carrier statistics once a second, PID check about every five seconds: queue
  full/byte/expiry growth, session/profile change, disconnected/stale stats or PID
  change stops the test. Stats publish every two seconds: repeated samples and
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
