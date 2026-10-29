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


## preview.4 field soak: low-rate acceptance, next-step abort

Both peers reported preview.4 / split-udp-v1. The complete 120-second phase at
1.92 Mbps payload per initiating sender passed: 24000/24000 unique UDP echoes in
each direction (48000/48000 combined), 94 completed TCP upload/download integrity
transactions, no new carrier drop or session change. The UDP RTT p99 values were
209.409 ms (Iran initiated) and 214.892 ms (Turkey initiated). This is a single
observed pass, not a causal proof of the yield change or higher capacity.

The 4.8 Mbps-per-initiator step aborted roughly seven seconds after starting.
Final packet-cap drop deltas were Iran 43, Turkey 72, all in the datagram lane.
Turkey crossed the existing >=64-drop hard-stop. Both byte/expiry deltas stayed
zero. The one-yield policy retried successfully 5/48 full-queue attempts on Iran
and 0/72 on Turkey: it is insufficient, not a field fix. Cumulative max datagram
writer time since startup was 82.012 / 104.914 ms respectively, including host
scheduling, not proof of TCP retransmissions or WAN cause.

Partial UDP unreturned-at-stop counts were 521 Iran-initiated and 78 Turkey-initiated;
missing_by_deadline remains null because the drain did not complete. These counts
must not be presented as proven final loss. Four TCP workers returned EOFError
after early termination; this does not establish payload corruption. Neither the
9.6 nor 19.2 Mbps stage ran. Iran's subsequent ConnectionRefusedError concerns
the peer test service after it stopped, not proof that the carrier disconnected.

Both final carrier PIDs were unchanged, session delta zero, both lanes connected
at checkpoints and sampled times, own test socket cleanup OK. Whole-host CPU
peaked at 61.98% Iran / 89.11% Turkey, but neither host guard reported a stop
(the CPU guard requires two consecutive excessive samples). No guarantee of zero
production impact is implied. Source-allowlist rejection delta was four at each
endpoint; no capture identifies its source and it is not an attack diagnosis.

Original uploaded report.json SHA256:
- iran: 98f94d0f6a5c03b70d759fcbcd40bafc66750eb6bfc56355c2225c6481922943
- turkey: 04a7c6e4f8155fd679824859597a9eb3baf6f7aa6e2af10e928b05f1ba0df9f6

Next: QUEUE-DIAGNOSTIC.md. Only the failing load is repeated, with read-only
carrier-owned TCP/TUN observations. No new runtime release, queue enlargement or
server mutation is justified yet. No real-server diagnostic result is claimed.


## Focused preview.4 queue diagnostic

The 30-second / 4.8 Mbps-per-initiator phase aborted at about 12.64 seconds on
the client clock. Iran queue drop delta 0; Turkey queue_packets delta 68, all
in the UDP/ICMP lane. Turkey crossed the existing >=64-drop hard-stop. Neither
host guard reported the trigger; both PIDs/sessions remained unchanged, both
endpoint cleanup reports OK, and TUN kernel drop/error counters did not increase
in samples. Observation errors 0 at both endpoints.

Outer TCP cumulative retransmission deltas per observed FD: Iran fd6 +6 / fd7 +3;
Turkey fd7 +13 / fd8 +42. FD is not a verified protocol lane label. Max sampled
socket send queues: Iran 17128/110366 bytes, Turkey 33047/146592 bytes in the same
FD order. This records retransmission and backlog, not causal attribution of the
queue-cap drops or a one-to-one count of inner UDP loss. Half-second polling can
miss shorter stalls. The previous cumulative writer-duration maxima did not
increase during this run and must not be presented as new write stalls.

Partial UDP unreturned-at-stop counts: Iran initiated 480, Turkey initiated 91;
final drain did not finish, so missing_by_deadline is null. Four TCP transactions
completed before abort; subsequent EOF errors and the client peer-service
ConnectionRefusedError are not proof of TCP payload corruption/carrier failure.
No higher rate or full-duration capacity acceptance was obtained.

Original report.json SHA256:
- iran: 0328c9793710bfb06aa628960eb191a58030ce2d84c1c1b1423d551ca7548922
- turkey: 6b0e15c2453638b3ffde2541ca0483af63fad8a6dce0cc65162a156f86eacb1c

Next: QUEUE-BURST-TRIAL.md. An explicit preview.5 packet-cap trial keeps private
configuration bytes and byte/age/write limits unchanged. No operator result or
performance fix is claimed before field acceptance.


## Preview.5 packet-slot trial — failed byte-cap gate

The explicit 256-total-slot override (128 per split lane) was verified at both
endpoints, with the unchanged 131072-byte lane budgets. Turkey recorded 15 new
queue_bytes drops, all datagram; Iran recorded zero new carrier drops. This
triggered the existing any-byte-drop hard stop, not the >=64 packet-drop stop.
The planned 30-second / 4.8 Mbps-per-initiator mixed phase aborted; no full-rate
or higher-capacity acceptance was obtained. More packet slots were not enough.

Partial unreturned-at-stop counts were 319 Iran-initiated and 45 Turkey-initiated;
both missing_by_deadline values are null. These are not final UDP loss rates.
No TCP transaction completed before abort; subsequent ConnectionResetError and
peer-service ConnectionRefusedError do not establish TCP payload corruption or
carrier failure. New datagram writer duration maxima were 149858 us Iran and
120328 us Turkey, including scheduling and socket wait, not proof of a sole WAN
cause. PIDs/session snapshots remained unchanged, both connected, cleanup OK.

TUN sampled drop/error counters did not increase; observation errors were zero.
Outer TCP observed cumulative retransmission deltas: Iran fd6 +0 / fd7 +0;
Turkey fd7 +1 / fd8 +7. Max sampled socket Send-Q bytes respectively:
Iran 15600 / 160202; Turkey 3576 / 182448. FD is not a verified protocol lane
label, polling can miss brief events, and no causal or per-probe attribution is
made from these socket counters. Host guards did not trigger.

Original uploaded report.json SHA256:
- iran: 491eedf5f7cc49bd3fb0e85b9b5003d62de3b012099b97000ed4e38f757f5b1f
- turkey: c9aab79b200d601dad7ebd615ffbd1de5b9d460295ac75ccdc704607d451152a

Next: [bounded UDP admission trial](UDP-WAIT-TRIAL.md), opt-in preview.6. Restore
original packet caps; permit one bounded admission deadline rather than enlarge
buffers again. The shared TUN reader can propagate backpressure and latency, so
application delivery/latency gates and added kernel-counter checks are required.
No preview.6 field result or performance improvement is claimed yet.


## Preview.6 bounded-wait trial — stopped by host CPU guard

Both nodes verified preview.6, 32 slots / 131072 bytes per lane, UDP wait 200 ms.
The intended 30-second / 4.8 Mbps-per-initiator mixed phase aborted after about
14 seconds on the client clock. Turkey reported host_cpu_85pct_twice and a
91% maximum whole-host CPU sample. Iran's later ConnectionRefusedError reflects
the stopped peer test service, not demonstrated carrier failure.

No new carrier drops at either endpoint; no increase in sampled TUN drop/error
counters; observation errors zero. Both PID/session snapshots were unchanged
and cleanup OK. However, queue_waits stayed ZERO at both nodes. The admission
wait was configured but never exercised: this run does not establish its benefit
or fix the previous congestion failure. Different WAN/host conditions can matter.

Partial unreturned-at-stop: 294 Iran-initiated, 25 Turkey-initiated; final
missing_by_deadline is null in both. These are not final loss rates. Five TCP
transactions completed before abort; later resets/EOF are not corruption proof.
No completed quality/capacity pass or higher-rate acceptance was obtained.

The host guard measures total minus idle+iowait across the first eight /proc/stat
CPU fields, including steal. It cannot attribute usage to Go, Python test load,
production services or VM contention. Do not raise/disable it to finish the test.
Next: numeric fixed-PID CPU sampling (checker, runner, carrier), separate host
user/system/IRQ/softIRQ/steal counters and a guarded no-test-load baseline. No
production process listing, config, cmdline or environment capture. Runtime and
queue settings remain unchanged; only the checker is changed.

Original uploaded report.json SHA256:
- iran: 81aeb72f410155ccf0cfc8263d06fa287596090f6760f79e9c0e8c83f0c4a5da
- turkey: d3a92d6dd10040078710d6233c4e681f7f3ceab7bc49fff1a0170393c4f7f0c6


## Preview.6 CPU-attribution run — host guard still stops mixed load

The planned 30-second mixed phase aborted around 27.73 seconds on the client
clock. Turkey reported host_cpu_85pct_twice again; maximum guard sample about
90.91%. No new carrier drops or sampled TUN drop/error counter increases at
either endpoint; PIDs and session snapshots unchanged; cleanup OK. Fourteen TCP
transactions completed before abort. Partial UDP unreturned-at-stop counts:
546 Iran-initiated, 23 Turkey-initiated; missing_by_deadline null, not final loss.

This time wait was actually exercised: one new admission wait and success on
each endpoint, zero wait timeouts. New maxima were 76897 us Iran / 58843 us
Turkey. This establishes a finite admission recovered in each run, not a
completed end-to-end quality pass or causal improvement against earlier runs.

Turkey reports one logical CPU. In adjacent observation intervals with host
busy >=85%, the aggregate host-normalized carrier CPU share was approximately
21.57% and checker process share 16.86%. The runner had no
measured CPU increase. The remaining busy ticks cannot be assigned to a named
production service: they include unobserved processes, kernel work, VM steal,
and possibly checker subprocesses. These half-second intervals are not the
exact same windows as the one-second guard, and process/host reads are not atomic.

Post-baseline aggregates include waiting/load/cleanup, not a pure active-load
benchmark. Whole-host busy was about 62.94%, carrier 22.28%, checker 15.75%;
baseline busy about 14.65%. Do not describe these as tunnel-only CPU or capacity.
Iran has two logical CPUs; do not compare raw one-core process percentages
against host-normalized percentages. Host memory guard did not trigger.

A gap in the diagnostic: reaped ss/systemctl/etc subprocess CPU was not recorded
in the previous report. Periodic ss -p can perform process-FD discovery even
though returned sockets are filtered. Its actual contribution is NOT proven by
these reports. Next diagnostic removes periodic ss observations, after the same
owned-socket preflight, and records aggregate reaped-child CPU. CPU/TUN polling,
synthetic traffic, phase duration, all integrity gates and safety thresholds
remain unchanged. No production service is paused and no carrier update made.
See [lean observer trial](LEAN-OBSERVER.md).

Original uploaded report.json SHA256:
- iran: f3b748a3831361b12f9b91f126859fbb0f16ce32e22ca7b99f9af124b1a73424
- turkey: 4ad8bd3a486ba06677384a594d257b313e3d382c9a14b071b351fc93d16dbde3


## Lean observer / preview.6 — completed mixed-load gate

Client status passed; server completed. The full 30-second phase at 4.8 Mbps
UDP payload per initiating sender completed. Both directions each sent 15000
probes and received all 15000 unique valid replies: 30000/30000 combined, no
missing by deadline, invalid replies, duplicates or per-flow reorder. This
counts initiating probes and their replies, not total wire datagrams/bytes;
echo return adds traffic. Eighteen TCP upload/download transactions completed
with integrity checks and no errors. UDP p99 echo RTT was 151.979 ms for Iran
initiated probes and 154.457 ms for Turkey initiated probes; not one-way jitter.

No new carrier drops or sampled TUN drop/error increases at either endpoint.
Sessions/PID snapshots unchanged, cleanup OK, observation errors zero. Each
endpoint recorded one new bounded admission wait and one success, no timeout.
Whole-host guard maximum samples: Turkey 82.0%, Iran about 56.48%; no guard stop.
These are sampled maxima, not universal production-safety guarantees.

This is a real pass of this bounded functional load, NOT a capacity result,
long-term stability acceptance or proof of an 800 Mbps link. Lower CPU / passing
after changing observation is not a controlled causal A/B result: baseline host
load and WAN conditions differed. Lean mode deliberately has no periodic TCP
socket backlog/retransmission observations. Previous cumulative writer/wait
maxima must not be described as newly occurring stalls.

Next: [fixed-load stability trial](STEADY-TRIAL.md), same runtime/profile/queues
and mixed traffic but 120 seconds at the already-passed UDP rate. No speed
escalation yet. Keep all host and quality gates; retain lean CPU/TUN observation.

Original uploaded report.json SHA256:
- iran: 7a808ce02bd0a021e9d75c95fc8cca73ef107bb91b7683bf840a39a288bfbfce
- turkey: 1db3951272eb894d2948e0a977c5056f227565d56631dbc7dc44b26a5700f094


## Lean 120-second mixed-load trial — passed

Client passed; server completed the planned 120-second phase at 500 UDP
probes/second (4.8 Mbps payload per initiating sender). Each initiating side
sent 60000 probes and received 60000 unique valid replies: 120,000/120,000
combined, no missing-by-deadline, invalid replies, duplicates or per-flow
reorder. Counts are probes/replies, not total wire packets; echo return adds
traffic. Ninety-four TCP upload/download transactions completed without errors.
UDP echo RTT p99: Iran initiated 220.041 ms, Turkey initiated 222.317 ms.

No new carrier drops, sampled TUN drop/error increases, sessions/PID changes,
or observation errors. Cleanup OK both nodes. Eight new admission waits per
endpoint, all eight successful, zero timeout. Whole-host CPU max samples were
approximately 85.86% Turkey and 69.95% Iran, with no host guard stop. Do not
claim CPU always below 85%: the guard needs TWO consecutive high samples.
The Turkish host still has limited CPU headroom and normal production load.

This validates the longer bounded mixed load, not indefinite stability,
application acceptance or maximum capacity. Lean mode has preflight socket
verification only; no periodic TCP backlog/retransmission evidence. No causal
A/B performance claim is made against previous runs or different host loads.

Next: [bounded rate ladder](RATE-LADDER.md), two short steps with unchanged
runtime and safety thresholds. Step two executes only if step one passes all
quality gates. No queue changes, carrier restart, disabled guards or high-rate
production stress test.

Original uploaded report.json SHA256:
- iran: 2496693dfc248214d6f56748132653d9919136bbe4a7fac549b646a7417d664b
- turkey: 995405336ba77eb57ab10739b59bd01d687c1a97e238bb2b4b660665f2fc7c6b
