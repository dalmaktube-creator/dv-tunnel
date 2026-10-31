# Sofia: resilience, concurrency and established-flow checkpoint

## Decision
13/13 bounded gates passed in two consecutive groups. This adds recovery/boundary coverage to the earlier18 Sofia functional tests; it does not make the tunnel universally flawless or production-ready.

## Scope and identity
- Same verified release `v0.1.0-preview.6`; ELF SHA256 `b7b5f02da2227ec6bdd2a1ba2698f44d6c8ba5afa65908d1109358683dad9062`. No carrier source, Moory service or persistent host service changed.
- Isolated DV-Lab preview.8 capsules, Iran dialer / Sofia mother-local listener. Workspaces had expired on idle and were restored from the same verified public binary and private pair before testing.
- Same default Sofia0.5/0.4CPU,192/128MiB account/capsule; Iran1.5/1.35CPU,384/256MiB. Packet cap64 throughout, unchanged byte limits; verified TLS/WSS over TCP with split UDP lane.
- All restart/stall actions targeted only the carrier subprocesses created by these test scripts. No host routes, NAT, firewall, quotas, systemd or unrelated process changed.
- Recovery probe deadlines25s; negative credentials4s observation with a live client carrier and a successful valid-client recovery. These are functional rejection checks, not a full security audit.

## Group1: bounded recovery and concurrency
| Gate | Actual acceptance condition | Result |
|---|---|---|
| Client carrier restart |New verified HTTP after owned stop/start, plus3 HTTPs |PASS|
| Server carrier restart |Same recovery after remote owned stop/start |PASS|
| Three-second server stall |SIGSTOP3s/SIGCONT, new HTTP recovery plus3 HTTPs |PASS|
| Wrong token |No unauthorized HTTP; valid connection recovers |PASS|
| Wrong TLS hostname |No unauthorized HTTP; valid connection recovers |PASS|
| Datagram boundaries |64/512/1000/1308B,100 each; 400 unique valid echoes |PASS|
| HTTP connection churn |100 independently opened HTTP connections, verified bodies |PASS|
| Sixteen parallel TCP flows |16×512KiB at0.5Mbps each; every fixture SHA256 |PASS|
| Full-duplex TCP |8MiB each way simultaneously,4Mbps each, both hashes |PASS|
| Idle / resume |5s idle, then3 verified HTTPs |PASS|
| Sustained mixed traffic |3 consecutive32MiB TCP +2500-probe UDP cycles; 96MiB TCP and 7500/7500 UDP |PASS|

The mixed gate is about100s of offered traffic, not an hours-long soak. Mixed UDP is20s per cycle during the TCP pull; the intervals between cycles are not uninterrupted UDP. At MTU1336 the1308B UDP payload is the ordinary IPv4+UDP no-fragment boundary; larger fragmentation/path-MTU discovery was not tested. The16-flow generator was bounded to32 server-handler slots to avoid confusing a test-harness8-slot limit with carrier capacity.

## Group2: established TCP flow faults
| Gate | What was validated | Result |
|---|---|---|
| Server stall during existing stream |Same established8MiB TCP download,3s server stall, final SHA256 |PASS|
| Client restart during existing stream |Record old-stream outcome separately, require a fresh2MiB hashed transfer after recovery |PASS|

Old stream preserved after client restart in this trial: **True**. Old-stream exception type: `None`. The old-stream result was checked independently by its payload hash; it is a single-trial observation, not a guarantee across arbitrary outages or restart durations.

## Evidence, cleanup and caveats
Both roles independently exited0 in both groups. Every carrier created by each manager was stopped and each nonpersistent test TUN was absent at cleanup. Exact recovery probe durations, old-stream outcomes, per-stream hashes, payload counts and RTT quantiles are in `resilience-active-evidence.json`.

Final carrier counters reset during deliberate restarts. Do not sum these final snapshots as whole-batch packet loss or compare them directly with the previous no-restart18-test batch. Offered rates are capped generator settings, not maximum tunnel capacity.

Next gates before a release claim: controlled network loss/jitter/reordering and reconnect behavior, matched direct/tunnel capacity and resource baselines, repeated matched64/128 queue trials, hours-long mixed-load soak, many-user limits and real service integration. No new carrier patch is justified merely by these passing bounded tests.
