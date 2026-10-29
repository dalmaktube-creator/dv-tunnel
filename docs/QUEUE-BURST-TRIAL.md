# preview.5: opt-in bounded packet-cap trial

Status: LOCAL VALIDATION ONLY until a new operator field result arrives.
This is not a claim of solved TCP HOL, improved WAN quality or accepted capacity.
No reference project implementation, new outer protocol or external dependency.

## Evidence and hypothesis

The focused preview.4 diagnostic had zero new Iran queue drops and 68 new Turkey
packet-cap drops, all in the datagram lane; Turkey hit the existing safety stop.
The entire phase did not complete, so its unreturned counts are not final loss.
Stable service PIDs/sessions and no sampled TUN kernel drop/error increases do not
prove absence of every transient. Both socket observations were collected without
sampling errors. Retransmission counters increased on the real outer TCP sockets;
they are not a direct count of lost inner UDP datagrams or proof of the sole cause.

The current split profile limits each lane to 32 packets despite a separate
131072-byte allowance. One scheduler yield was insufficient. Next test changes
ONLY the explicitly selected total packet cap from 64 to 256 (128 per lane),
leaving existing byte caps, notsent_lowat, expiry and write deadlines unchanged.
For this pair, 131072 bytes still limits the number of full-size UDP packets that
can actually queue below 128. This checks finite burst headroom, not unlimited
buffering or a causal claim that all drops came from WAN retransmissions.
Larger occupancy can increase latency and memory; the existing p99/host guards
remain. Half-second TCP polling can miss shorter stalls, and zero TUN kernel
counters are not full per-packet capture proof.

Earlier single-stream 64/128 ABBA tests were not a controlled latency/capacity
win; those results do not establish this split mixed-load trial will pass.
We do not repeat them or escalate beyond the failing 4.8 Mbps phase yet.

## Configuration and compatibility

- Upgrade alone preserves the original packet cap, private config and profile.
- `dv-tunnel queue main --packets 256` edits ONLY nonsecret root-owned link.json
  metadata, with link.queue.previous.json backup, after validating the new binary.
  It does not start, enable or restart a service. Private config.json stays exact.
- Only `dv-tunnel restart main --yes` applies this runtime setting explicitly.
- Core CLI `--queue-packets 2..256` is split-only and replaces the total packet cap;
  zero/omission retains the original private configuration setting. Invalid values
  fail before TUN/socket creation. No default cap, byte cap, socket option or
  packet wire format is changed.
- New runner capability marker is checked before upgrade/rollback activates any
  runtime. An older runner cannot silently ignore a selected queue override.
- `queue main --default` removes only this override; explicitly restart the instance
  before testing/restoring an older runtime. Clear override before selecting
  single-stream. The metadata backup is private and is not part of reports.
- Each endpoint can apply its packet cap independently; no protocol migration is
  required. For this controlled experiment, the checker verifies 128 packets in
  BOTH lanes at BOTH endpoints before offering load. Private byte limits remain
  the existing 131072 per lane on this field pair.

## Operator sequence

On BOTH peers, update to preview.5, explicitly select queue=256, restart only main,
and fetch the same pinned tools/soak_check.py revision. Both updates must finish
before running the test. Do not import profiles or touch WG/panel/default routes.
There are brief test-link interruptions during the explicit update/restart.

Turkey first:

```sh
python3 /root/dv-queue-check-v2.py server --queue-diagnostic --expect-version v0.1.0-preview.5 --expect-lane-packets 128
```

After SOAK_SERVER_READY, Iran:

```sh
python3 /root/dv-queue-check-v2.py client --queue-diagnostic --expect-version v0.1.0-preview.5 --expect-lane-packets 128
```

The same 30-second, 4.8 Mbps payload-per-initiating-sender mixed diagnostic runs.
Echo traffic adds return load; this is not total wire load or a line-rate benchmark.
Any new carrier drop fails the phase; the >=64 packet-drop / any byte-expiry-drop
hard-stop and other guards are NOT relaxed. A pass remains a single observed run,
not a randomized causal comparison or production-client acceptance.

Download the two ZIP aliases under /root/dv-queue-diag-results and send both once.
If it stops early, do not retry or enlarge caps again without interpreting evidence.

To undo only the trial on each peer:

```sh
dv-tunnel queue main --default && dv-tunnel restart main --yes
```

No automatic rollback, production firewall/NAT/sysctl edit, boot enablement or
server-side change is performed by the agent. Local fake-TUN/race/unit/package
checks do not replace the new real-server gate.
