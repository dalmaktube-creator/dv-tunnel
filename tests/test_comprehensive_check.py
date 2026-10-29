import argparse
import copy
import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_soak_check as T

S = T.S


def sample(t, cores=2, user=0, idle=0, steal=0, process=0):
    return dict(monotonic=t, memory_available_mib=500-t,
                rss_mib=dict(carrier=12, checker=8, runner=4),
                cpu=dict(logical_cpus=cores,
                         host_ticks=dict(user=user, nice=0, system=0, idle=idle,
                                         iowait=0, irq=0, softirq=0, steal=steal),
                         processes={label:dict(pid=i+1, start_ticks=1,
                                               user_ticks=process, system_ticks=0)
                                    for i,label in enumerate(('carrier','checker','runner'))}))


class ComprehensiveTests(unittest.TestCase):
    def test_plan_preserves_probe_and_wall_clock_budgets(self):
        phases = S.comprehensive_phases()
        self.assertEqual([p['pps'] for p in phases], [1000,1000,1500,2000,500])
        self.assertEqual([p['tcp_workers'] for p in phases], [0,4,4,4,4])
        self.assertEqual(sum(p['seconds'] for p in phases),180)
        self.assertTrue(all(p['seconds']*p['pps']<=60000 and p['pps']<=2000 for p in phases))
        phases[0]['pps']=9999
        self.assertEqual(S.comprehensive_phases()[0]['pps'],1000)

    def test_cpu_denominators_steal_peaks_and_boundary_exclusion(self):
        samples=[sample(-1),sample(0),sample(1,user=80,idle=100,steal=20,process=20),
                 sample(2,user=120,idle=200,steal=80,process=60),sample(3)]
        r=S.resource_summary(samples,0,2,100)
        self.assertEqual(r['sampled_coverage_s'],2)
        self.assertEqual(r['metrics']['host_busy_pct']['mean'],50)
        self.assertEqual(r['metrics']['host_steal_pct']['mean'],20)
        self.assertEqual(r['metrics']['host_steal_pct']['peak_sample'],30)
        self.assertEqual(r['metrics']['carrier_one_core_pct']['mean'],30)
        self.assertEqual(r['metrics']['carrier_host_capacity_pct']['mean'],15)
        self.assertEqual(r['metrics']['carrier_host_capacity_pct']['peak_sample'],20)
        self.assertEqual(r['memory_available_min_mib'],498)
        self.assertEqual(r['rss_peak_mib']['carrier'],12)

    def test_invalid_identity_gap_or_counter_reset_is_not_filled_as_zero(self):
        a=sample(0);b=sample(1,user=100,idle=100,process=20)
        b['cpu']['processes']['carrier']['start_ticks']=9
        r=S.resource_summary([a,b],0,1,100)
        self.assertEqual(r['rejected_intervals'],1)
        self.assertEqual(r['metrics'],{})
        self.assertEqual(S.resource_summary([a,sample(4)],0,4,100)['interval_count'],0)

    def test_missing_resources_cannot_claim_complete_success(self):
        r=dict(role='client',status='passed',queue_diagnostics=dict(samples=[]),
               phases=[dict(spec=dict(id=1,pps=1000,tcp_workers=0),gate_passed=True,
                            resource_window=dict(start_monotonic=1,end_monotonic=30))])
        S.attach_resource_summaries(r)
        self.assertEqual(r['status'],'resource_measurement_incomplete')
        self.assertIsNone(r['highest_passed_mixed_udp_mbps_per_initiator'])
        self.assertFalse(r['capacity_limit_established'])

    def test_udp_only_gate_does_not_require_a_bulk_tcp_transaction(self):
        u=dict(pass_integrity=True,aborted=False,duplicates=0,per_flow_reordered=0,
               echo_rtt_ms=dict(p99=50),actual_offered_payload_mbps=9.6,requested_payload_mbps=9.6)
        r=dict(spec=dict(tcp_workers=0),tcp=dict(completed=0,errors=[]),
               udp_local_initiated=u,udp_peer_initiated=copy.deepcopy(u),peer_carrier_delta=dict(drops=0))
        self.assertTrue(S.phase_gate(r,dict(drops=0),False))
        r['spec']['tcp_workers']=4
        self.assertFalse(S.phase_gate(r,dict(drops=0),False))
        r['spec']['tcp_workers']=0
        self.assertFalse(S.phase_gate(r,dict(drops=1),False))

    def test_two_endpoints_with_udp_only_then_mixed_and_resource_windows(self):
        class Observer:
            def __init__(self,*a,**kw):self.start=time.monotonic()
            def close(self):
                end=time.monotonic()
                samples=[]
                for i in range(int((end-self.start)*100)+1):
                    samples.append(sample(self.start+i/100,user=2*i,idle=2*i,process=i))
                return dict(samples=samples,cpu_clock_ticks_per_second=100,observation_errors=0)
        with patch.object(S,'QueueDiagnostics',Observer):
            T.SoakTests()._two_role_suite(True,30,True,False,True)

    def test_first_failed_phase_blocks_all_higher_steps_and_recovery(self):
        live,h=T.helper()
        stop=threading.Event()
        guard=SimpleNamespace(stop=stop,reason=None,cpu_max=0,mem_min=1000,close=lambda:None)
        monitor=SimpleNamespace(error=None,samples=[],close=lambda:None)
        before=dict(main_pid=123,stats=dict(version='v0.1.0-preview.6',connected=1,sessions=2,drops=0,transport_profile='split-udp-v1'))
        bad=dict(pass_integrity=False,aborted=False,missing_by_deadline=1)
        result=dict(spec=S.comprehensive_phases()[0],tcp=dict(completed=0,errors=[]),
                    udp_local_initiated=bad,udp_peer_initiated=bad,peer_carrier_delta=dict(drops=0))
        plan_hash=S.hashlib.sha256(json.dumps(S.comprehensive_phases(),sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp,patch.object(live,'inspect_link',return_value=before),patch.object(live,'deltas',return_value=dict(drops=0)),patch.object(h,'HostGuard',return_value=guard),patch.object(S,'Monitor',return_value=monitor),patch.object(S,'QueueDiagnostics') as obs,patch.object(S,'rpc',side_effect=[dict(suite='dv-comprehensive-lean-v1',plan_sha256=plan_hash),dict(carrier_counter_deltas=dict(drops=0))]),patch.object(S,'run_phase',return_value=result) as phase,patch.object(S.time,'sleep'):
            obs.return_value.close.return_value=dict(samples=[])
            args=argparse.Namespace(role='client',local='127.0.0.1',peer='127.0.0.1',instance='main',iface='wft0',output=tmp,queue_diagnostic=True,lean_observer=True,comprehensive=True)
            self.assertEqual(S.run(args,live=live,helper=h,ports=(0,0,1,1)),1)
            self.assertEqual(phase.call_count,1)
            d=json.loads((T.Path(tmp)/'latest-client.json').read_text())
            self.assertEqual(d['status'],'stopped_at_quality_gate')

    def test_wrong_peer_plan_rejected_before_test_load(self):
        live,h=T.helper()
        before=dict(main_pid=123,stats=dict(connected=1,sessions=2,drops=0,transport_profile='split-udp-v1'))
        stop=threading.Event()
        guard=SimpleNamespace(stop=stop,reason=None,cpu_max=0,mem_min=1000,close=lambda:None)
        monitor=SimpleNamespace(error=None,samples=[],close=lambda:None)
        with patch.object(live,'inspect_link',return_value=before),patch.object(live,'deltas',return_value=dict(drops=0)),patch.object(live,'write_report') as write,patch.object(h,'HostGuard',return_value=guard),patch.object(S,'Monitor',return_value=monitor),patch.object(S,'QueueDiagnostics') as obs,patch.object(S,'rpc',return_value=dict(suite='dv-comprehensive-lean-v1',plan_sha256='wrong')),patch.object(S,'run_phase') as phase:
            obs.return_value.close.return_value=dict(samples=[])
            args=argparse.Namespace(role='client',local='127.0.0.1',peer='127.0.0.1',instance='main',iface='wft0',output='unused',queue_diagnostic=True,lean_observer=True,comprehensive=True)
            self.assertEqual(S.run(args,live=live,helper=h,ports=(0,0,1,1)),1)
            phase.assert_not_called()
            self.assertIn('phase plan',write.call_args.args[2]['error'])
