import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('soak_check',ROOT/'tools/soak_check.py')
S=importlib.util.module_from_spec(spec);spec.loader.exec_module(S)

def helper():
    l=S.module_from_bytes((ROOT/'tools/live_check.py').read_bytes())
    return l,l.helper_from_bytes((ROOT/'tools/acceptance.py').read_bytes())

class Echo:
    def __init__(self,mode='normal'):
        self.s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);self.s.bind(('127.0.0.1',0));self.s.settimeout(.05)
        self.port=self.s.getsockname()[1];self.stop=threading.Event();self.mode=mode
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def run(self):
        while not self.stop.is_set():
            try:
                p,a=self.s.recvfrom(2048)
                if self.mode=='loss' and int.from_bytes(p[13:17],'big')%5==0:continue
                if self.mode=='corrupt':p=p[:-1]+bytes([p[-1]^1])
                self.s.sendto(p,a)
                if self.mode=='duplicate':self.s.sendto(p,a)
            except socket.timeout:pass
            except OSError:return
    def close(self):self.stop.set();self.s.close();self.thread.join(1)

class SoakTests(unittest.TestCase):
    def test_pinned_live_helper_hash(self):
        l,h=helper();self.assertIsNotNone(h.PAYLOAD)
        with self.assertRaisesRegex(RuntimeError,'hash'):S.module_from_bytes(b'not pinned')

    def test_four_socket_udp_payload_and_rate(self):
        e=Echo();self.addCleanup(e.close)
        r=S.udp_probe('127.0.0.1',e.port,.2,200,threading.Event())
        self.assertEqual(r['sent'],40);self.assertEqual(r['returned_unique'],40)
        self.assertTrue(r['pass_integrity']);self.assertEqual(r['missing_by_deadline'],0)
        self.assertGreater(r['actual_offered_payload_mbps'],1.8)
        self.assertIsNotNone(r['echo_rtt_ms']['p99'])

    def test_top_step_achieved_rate_loopback(self):
        e=Echo();self.addCleanup(e.close)
        r=S.udp_probe('127.0.0.1',e.port,.2,2000,threading.Event())
        self.assertTrue(r['pass_integrity']);self.assertEqual(r['sent'],400)
        self.assertGreaterEqual(r['actual_offered_payload_mbps'],r['requested_payload_mbps']*.9)

    def test_duplicates_do_not_inflate_unique_delivery(self):
        e=Echo('duplicate');self.addCleanup(e.close)
        r=S.udp_probe('127.0.0.1',e.port,.2,100,threading.Event())
        self.assertEqual(r['returned_unique'],r['sent']);self.assertGreater(r['duplicates'],0)

    def test_missing_packets_reported_after_drain_not_as_success(self):
        e=Echo('loss');self.addCleanup(e.close)
        r=S.udp_probe('127.0.0.1',e.port,.1,100,threading.Event())
        self.assertEqual(r['sent'],10);self.assertEqual(r['returned_unique'],8)
        self.assertEqual(r['missing_by_deadline'],2);self.assertFalse(r['pass_integrity'])

    def test_guard_stops_probe_and_invalid_bounds(self):
        stop=threading.Event();stop.set()
        r=S.udp_probe('127.0.0.1',9,.2,100,stop)
        self.assertTrue(r['aborted']);self.assertIsNone(r['missing_by_deadline']);self.assertEqual(r['unreturned_at_stop'],0)
        for seconds,pps in ((121,200),(120,2000),(.2,2001),(0,1)):
            with self.assertRaises(ValueError):S.udp_probe('127.0.0.1',9,seconds,pps,threading.Event())

    def test_quality_gate_stops_loss_tail_underfilled_and_tcp_errors(self):
        u=dict(pass_integrity=True,duplicates=0,per_flow_reordered=0,echo_rtt_ms=dict(p99=50),actual_offered_payload_mbps=1.92,requested_payload_mbps=1.92)
        r=dict(tcp=dict(completed=1,errors=[]),udp_local_initiated=u,udp_peer_initiated=copy.deepcopy(u))
        self.assertTrue(S.phase_ok(r))
        for key,val in (('pass_integrity',False),('duplicates',1),('per_flow_reordered',1),('actual_offered_payload_mbps',1.0),('echo_rtt_ms',dict(p99=600))):
            x=copy.deepcopy(r);x['udp_peer_initiated'][key]=val;self.assertFalse(S.phase_ok(x))
        r['tcp']['errors']=['TimeoutError'];self.assertFalse(S.phase_ok(r))

    def test_even_one_carrier_drop_still_fails_phase_gate(self):
        u=dict(pass_integrity=True,duplicates=0,per_flow_reordered=0,echo_rtt_ms=dict(p99=50),actual_offered_payload_mbps=1.92,requested_payload_mbps=1.92)
        r=dict(tcp=dict(completed=1,errors=[]),udp_local_initiated=u,udp_peer_initiated=copy.deepcopy(u),peer_carrier_delta=dict(drops=0))
        self.assertTrue(S.phase_gate(r,dict(drops=0),False))
        self.assertFalse(S.phase_gate(r,dict(drops=1),False))
        r['peer_carrier_delta']['drops']=1
        self.assertFalse(S.phase_gate(r,dict(drops=0),False))

    def test_expected_runtime_mismatch_opens_no_test_services(self):
        l,h=helper()
        before=dict(main_pid=1,stats=dict(version='v0.1.0-preview.3',connected=1))
        with tempfile.TemporaryDirectory() as tmp,patch.object(l,'inspect_link',return_value=before),patch.object(h,'Services') as services:
            args=argparse.Namespace(role='client',local='127.0.0.1',peer='127.0.0.1',instance='main',iface='unused',output=tmp,expect_version='v0.1.0-preview.4')
            self.assertEqual(S.run(args,live=l,helper=h),1);services.assert_not_called()
            d=json.loads((Path(tmp)/'latest-client.json').read_text())
            self.assertIn('version',d['error']);self.assertEqual(d['status'],'failed')

    def test_monitor_detects_session_change(self):
        l,h=helper();args=argparse.Namespace(instance='main')
        start=dict(main_pid=1,stats=dict(sessions=2,transport_profile='split-udp-v1'))
        with patch.object(l,'inspect_link',return_value=start),patch.object(l,'stats',return_value=dict(sessions=3,transport_profile='split-udp-v1')):
            stop=threading.Event();m=S.Monitor(l,args,stop);m.thread.join(1);m.close()
            self.assertTrue(stop.is_set());self.assertIn('session',m.error)

    def test_monitor_stops_on_queue_growth(self):
        l,h=helper();args=argparse.Namespace(instance='main')
        start=dict(main_pid=1,stats=dict(sessions=2,transport_profile='split-udp-v1',drop_reasons=dict(queue_packets=0)))
        current=dict(start['stats'],drop_reasons=dict(queue_packets=64))
        with patch.object(l,'inspect_link',return_value=start),patch.object(l,'stats',return_value=current):
            stop=threading.Event();m=S.Monitor(l,args,stop);m.thread.join(1);m.close()
            self.assertTrue(stop.is_set());self.assertIn('queue',m.error);self.assertEqual(len(m.samples),1)

    def test_small_queue_loss_is_recorded_but_does_not_abort_measurement(self):
        l,h=helper();args=argparse.Namespace(instance='main')
        start=dict(main_pid=1,stats=dict(sessions=2,transport_profile='split-udp-v1',drop_reasons=dict(queue_packets=0)))
        current=dict(start['stats'],drop_reasons=dict(queue_packets=3))
        with patch.object(l,'inspect_link',return_value=start),patch.object(l,'stats',return_value=current):
            stop=threading.Event();m=S.Monitor(l,args,stop);time.sleep(.03);m.close()
            self.assertFalse(stop.is_set());self.assertIsNone(m.error);self.assertGreaterEqual(len(m.samples),1)

    def test_mid_probe_abort_keeps_counts_without_calling_unreturned_loss(self):
        e=Echo();self.addCleanup(e.close);stop=threading.Event();captured=[]
        timer=threading.Timer(.12,stop.set);timer.start()
        try:r=S.udp_probe('127.0.0.1',e.port,1,200,stop,capture=captured.append)
        finally:timer.cancel()
        self.assertTrue(r['aborted']);self.assertFalse(r['pass_integrity']);self.assertGreater(r['sent'],0)
        self.assertIsNone(r['missing_by_deadline']);self.assertEqual(r['unreturned_at_stop'],r['sent']-r['returned_unique'])
        self.assertEqual(captured,[r])

    def test_partial_phase_retains_tcp_results_when_peer_report_fails(self):
        l,h=helper();args=argparse.Namespace(peer='127.0.0.1')
        stop=threading.Event();progress={}
        partial=dict(sent=4,returned_unique=3,missing_by_deadline=None,unreturned_at_stop=1,aborted=True,pass_integrity=False)
        def probe(*args):
            if len(args)>5 and args[5]:args[5](partial)
            return partial
        with patch.object(S,'rpc',side_effect=TimeoutError),patch.object(S,'udp_probe',side_effect=probe),patch.object(S,'transfer_worker',return_value=dict(completed=2,errors=[])):
            r=S.run_phase(h,args,dict(id=1,seconds=.2,pps=100),stop,(1,2),progress)
        self.assertTrue(stop.is_set());self.assertEqual(r['tcp']['completed'],8)
        self.assertEqual(r['udp_local_initiated'],partial);self.assertIsNone(r['udp_peer_initiated'])
        self.assertIn('TimeoutError',r['tcp']['errors']);self.assertFalse(S.phase_ok(r))

    def _two_role_suite(self,diagnostic=False):
        l,h=helper();h.SIZE=4096;h.PAYLOAD=bytes(range(256))*16;h.HASH=hashlib.sha256(h.PAYLOAD).hexdigest()
        pacer=h.Pacer
        before=dict(main_pid=123,stats=dict(version='v0.1.0-preview.3',connected=1,sessions=2,drops=0,spoofed_drops=0,tx_datagrams=0,rx_datagrams=0,drop_reasons={},transport_profile='split-udp-v1'))
        # Reserve four distinct test ports before closing probes.
        held=[];ports=[]
        for typ in (socket.SOCK_STREAM,socket.SOCK_DGRAM,socket.SOCK_STREAM,socket.SOCK_DGRAM):
            s=socket.socket(socket.AF_INET,typ);s.bind(('127.0.0.1',0));ports.append(s.getsockname()[1]);held.append(s)
        for s in held:s.close()
        phases=[dict(id=1,seconds=.2,pps=100),dict(id=2,seconds=.2,pps=200)]
        with tempfile.TemporaryDirectory() as tmp,patch.object(l,'inspect_link',return_value=before),patch.object(l,'stats',return_value=before['stats']),patch.object(h,'Pacer',side_effect=lambda mbps=2:pacer(mbps=100)),patch.object(h,'Carrier',side_effect=AssertionError('No carrier control')):
            server=argparse.Namespace(role='server',local='127.0.0.1',peer='127.0.0.1',instance='main',iface='unused',output=tmp+'/server',queue_diagnostic=diagnostic)
            client=argparse.Namespace(**dict(vars(server),role='client',output=tmp+'/client'))
            values=[]
            t=threading.Thread(target=lambda:values.append(S.run(server,live=l,helper=h,phases=phases,ports=tuple(ports))),daemon=True);t.start()
            deadline=time.monotonic()+3
            while True:
                try:
                    greeting=S.rpc(h,'127.0.0.1',ports[0],dict(op='ping'))
                    if greeting.get('suite')==('dv-queue-diagnostic-v1' if diagnostic else 'dv-soak-v2'):break
                except OSError:pass
                if time.monotonic()>deadline:self.fail('Server did not start')
                time.sleep(.02)
            result=S.run(client,live=l,helper=h,phases=phases,ports=(ports[2],ports[3],ports[0],ports[1]))
            t.join(15);self.assertFalse(t.is_alive());self.assertEqual(values,[0]);self.assertEqual(result,0)
            d=json.loads((Path(tmp)/'client/latest-client.json').read_text())
            self.assertEqual(d['status'],'passed');self.assertEqual(len(d['phases']),2)
            self.assertTrue(d['carrier_main_pid_unchanged']);self.assertEqual(d['test_socket_cleanup'],'OK')
            self.assertTrue(all(x['tcp']['completed']>0 for x in d['phases']))

            self.assertEqual(d['suite'],'dv-queue-diagnostic-v1' if diagnostic else 'dv-soak-v2')
            if diagnostic:self.assertIn('queue_diagnostics',d)

    def test_two_role_suite_without_carrier_or_config_access(self):
        self._two_role_suite()

    def test_diagnostic_two_roles_keep_orchestration_and_report(self):
        class Observer:
            def __init__(self,pid,iface):pass
            def close(self):return dict(samples=[dict(tcp_sockets=[])],observation_errors=0)
        with patch.object(S,'QueueDiagnostics',Observer):self._two_role_suite(True)

    def test_tcp_parser_limits_to_owned_established_numeric_fields(self):
        text='ESTAB 0 131072 31.40.204.56:42443 87.107.206.82:45000 users:(("secret-command",pid=123,fd=6))\n     cubic rtt:52.1/3.2 rto:204 cwnd:10 retrans:0/4 bytes_retrans:4800 notsent:32000 forbidden:secret\nESTAB 1 9 1.2.3.4:1 5.6.7.8:2 users:(("unrelated",pid=1234,fd=7))\n     cubic rtt:99/1 cwnd:2\n CLOSE-WAIT 2 3 1.2.3.4:1 5.6.7.8:2 users:(("same",pid=123,fd=9))\n     cubic rtt:11/2\n  ESTAB 1 2 1.2.3.4:1 5.6.7.8:2 users:(("owned",pid=123,fd=8))\n     cubic rtt:50/2 send:4Mbps\n'
        result=S.parse_tcp_samples(text,123)
        self.assertEqual(len(result),2);self.assertEqual(result[0]['fd'],6)
        self.assertEqual(result[0]['retrans'],'0/4');self.assertEqual(result[0]['send_q'],131072)
        self.assertEqual(result[1]['rtt'],'50/2')
        saved=json.dumps(result)
        for forbidden in ['31.40','87.107','secret','unrelated','Mbps']:self.assertNotIn(forbidden,saved)

    def test_cpu_baseline_guard_stops_before_test_services(self):
        from types import SimpleNamespace
        l,h=helper();stop=threading.Event();stop.set()
        guard=SimpleNamespace(stop=stop,reason='host_cpu_85pct_twice',cpu_max=91,mem_min=400,close=lambda:None)
        monitor=SimpleNamespace(error=None,samples=[],close=lambda:None)
        before=dict(main_pid=123,stats=dict(version='v0.1.0-preview.6',connected=1,sessions=2,transport_profile='split-udp-v1'))
        with tempfile.TemporaryDirectory() as tmp,patch.object(l,'inspect_link',return_value=before),patch.object(l,'deltas',return_value={'drops':0}),patch.object(h,'HostGuard',return_value=guard),patch.object(S,'Monitor',return_value=monitor),patch.object(S,'QueueDiagnostics') as obs,patch.object(h,'Services') as service,patch.object(l,'write_report') as write:
            obs.return_value.close.return_value={'samples':[]}
            a=argparse.Namespace(role='server',local='127.0.0.1',peer='127.0.0.1',instance='main',iface='wft0',output=tmp,queue_diagnostic=True,cpu_baseline_seconds=10)
            self.assertEqual(S.run(a,live=l,helper=h),1);service.assert_not_called()
            report=write.call_args.args[2];self.assertEqual(report['error'],'host_cpu_85pct_twice');self.assertIn('end_monotonic',report['cpu_baseline'])

    def test_cpu_snapshot_numeric_fixed_pids_and_steal(self):
        fields=['S']+['0']*23
        for i,v in {1:100,11:25,12:5,17:4,19:999}.items():fields[i]=str(v)
        def read(path,*a,**kw):
            if str(path)=='/proc/stat':return 'cpu 10 1 20 30 4 5 6 70 99 99\ncpu0 0\ncpu1 0\n'
            if str(path)=='/proc/123/stat':return '123 (private name ) has spaces) '+' '.join(fields)
            raise AssertionError('Unexpected read '+str(path))
        with patch.object(Path,'read_text',read):
            result=S.cpu_snapshot({'carrier':(123,(100,999))})
            self.assertEqual(result['host_ticks']['steal'],70);self.assertEqual(sum(result['host_ticks'].values()),146)
            self.assertEqual(result['logical_cpus'],2);self.assertEqual(result['processes']['carrier']['user_ticks'],25)
            self.assertNotIn('private',json.dumps(result))
            with self.assertRaisesRegex(RuntimeError,'identity'):S.cpu_snapshot({'carrier':(123,(100,998))})

    def test_lean_observer_preserves_preflight_but_skips_periodic_ss(self):
        from types import SimpleNamespace
        obj=S.QueueDiagnostics.__new__(S.QueueDiagnostics);obj.ss='/usr/bin/ss';obj.pid=123;obj.identity=(100,234);obj.iface='wft0';obj.lean=True;obj.samples=[]
        output='ESTAB 0 0 A:42443 B:1 users:(("dv-tunnel",pid=123,fd=6))\n    cubic rtt:50/2\n'
        with patch.object(S,'process_identity',return_value=(100,234)),patch.object(S.subprocess,'run',return_value=SimpleNamespace(stdout=output)) as run,patch.object(Path,'read_text',return_value='0'),patch.object(S.resource,'getrusage',return_value=SimpleNamespace(ru_utime=1.0,ru_stime=.5)):
            first=obj.sample();self.assertEqual(len(first['tcp_sockets']),1);obj.samples.append(first)
            second=obj.sample();self.assertEqual(run.call_count,1)
            self.assertEqual(second['tcp_observation'],'not_sampled_lean_mode');self.assertNotIn('tcp_sockets',second)
            self.assertEqual(second['tun_kernel']['tx_dropped'],0);self.assertEqual(second['checker_reaped_children_cpu_seconds'],dict(user=1.0,system=.5))

    def test_diagnostic_sampling_is_read_only_fixed_argv(self):
        from types import SimpleNamespace
        obj=S.QueueDiagnostics.__new__(S.QueueDiagnostics);obj.ss='/usr/bin/ss';obj.pid=123;obj.identity=(100,234);obj.iface='wft0'
        output='ESTAB 0 0 A:42443 B:1 users:(("dv-tunnel",pid=123,fd=6))\n    cubic rtt:50/2\n'
        with patch.object(S,'process_identity',return_value=(100,234)),patch.object(S.subprocess,'run',return_value=SimpleNamespace(stdout=output)) as run,patch.object(Path,'read_text',return_value='0'):
            result=obj.sample()
        self.assertEqual(len(result['tcp_sockets']),1);self.assertEqual(result['tun_kernel']['tx_dropped'],0)
        self.assertEqual(run.call_args.args[0],['/usr/bin/ss','-Htinp','( sport = :42443 or dport = :42443 )'])
        self.assertNotIn('shell',run.call_args.kwargs);self.assertEqual(run.call_args.kwargs['timeout'],.5)

    def test_diagnostic_preflight_fails_before_any_thread_when_sockets_missing(self):
        with patch.object(S,'resolve_carrier_pid',return_value=124),patch.object(S,'process_identity',return_value=(123,234)),patch.object(S.shutil,'which',return_value='/usr/bin/ss'),patch.object(S.QueueDiagnostics,'sample',return_value=dict(tcp_sockets=[])),patch.object(S.threading,'Thread') as t:
            with self.assertRaisesRegex(RuntimeError,'two established'):S.QueueDiagnostics(123,'wft0')
            t.assert_not_called()
        with self.assertRaises(ValueError):S.QueueDiagnostics(123,'../../etc')

    def test_diagnostic_sample_retention_is_bounded(self):
        from types import SimpleNamespace
        obj=S.QueueDiagnostics.__new__(S.QueueDiagnostics);obj.samples=[];obj.errors=0
        obj.done=SimpleNamespace(wait=lambda _:False);obj.sample=lambda:dict(error_type='TimeoutExpired')
        obj.loop();self.assertEqual(len(obj.samples),900);self.assertEqual(obj.errors,900)

    def test_diagnostic_mismatch_rejected_before_phase(self):
        l,h=helper();before=dict(main_pid=123,stats=dict(version='v0.1.0-preview.4',connected=1,sessions=2,drops=0,spoofed_drops=0,tx_datagrams=0,rx_datagrams=0,drop_reasons={},transport_profile='split-udp-v1'))
        with tempfile.TemporaryDirectory() as tmp,patch.object(l,'inspect_link',return_value=before),patch.object(l,'stats',return_value=before['stats']),patch.object(S,'QueueDiagnostics') as obs,patch.object(h,'Services'),patch.object(S,'rpc',return_value={'suite':'dv-soak-v2'}),patch.object(S,'run_phase') as phase:
            obs.return_value.close.return_value={'samples':[]}
            a=argparse.Namespace(role='client',local='127.0.0.1',peer='127.0.0.1',instance='main',iface='wft0',output=tmp,queue_diagnostic=True,expect_version='v0.1.0-preview.4')
            # Mock service counters are made JSON-serializable.
            with patch.object(l,'write_report') as write:
                result=S.run(a,live=l,helper=h)
                self.assertEqual(result,1);phase.assert_not_called()
                self.assertIn('same checker mode',write.call_args.args[2]['error'])

    def test_carrier_socket_pid_is_runner_child_not_mainpid(self):
        def read(path,*a,**kw):
            text=str(path)
            if text.endswith('/children'):return '124 125'
            if text.endswith('/cgroup'):return '0::/system.slice/dv-tunnel@main.service'
            raise AssertionError('Unexpected file '+text)
        with patch.object(Path,'read_text',read),patch.object(S.os,'readlink',side_effect=lambda p:'/opt/dv-tunnel/releases/v0.1.0-preview.4/'+('dv-tunnel' if '/124/' in str(p) else 'other')),patch.object(S,'process_identity',return_value=(123,555)):
            self.assertEqual(S.resolve_carrier_pid(123),124)
        with patch.object(Path,'read_text',read),patch.object(S.os,'readlink',return_value='/opt/bin/dv-tunnel'),patch.object(S,'process_identity',return_value=(123,555)):
            with self.assertRaisesRegex(RuntimeError,'one direct'):S.resolve_carrier_pid(123)
        with patch.object(Path,'read_text',read),patch.object(S.os,'readlink',return_value='/opt/bin/dv-tunnel'),patch.object(S,'process_identity',return_value=(999,555)):
            with self.assertRaisesRegex(RuntimeError,'one direct'):S.resolve_carrier_pid(123)

    def test_reused_socket_pid_is_not_observed_as_same_carrier(self):
        obj=S.QueueDiagnostics.__new__(S.QueueDiagnostics);obj.pid=124;obj.identity=(123,555)
        with patch.object(S,'process_identity',return_value=(123,556)),patch.object(S.subprocess,'run') as run:
            result=obj.sample();run.assert_not_called();self.assertEqual(result['error_type'],'RuntimeError')

    def test_wrong_local_expected_packet_cap_opens_no_test_sockets(self):
        l,h=helper();before=dict(main_pid=123,stats=dict(version='v0.1.0-preview.5',connected=1,sessions=2,lanes={'tcp':{'packet_limit':32},'udp':{'packet_limit':32}}))
        a=argparse.Namespace(role='client',instance='main',iface='wft0',queue_diagnostic=True,expect_version='v0.1.0-preview.5',expect_lane_packets=128,output='unused')
        with patch.object(l,'inspect_link',return_value=before),patch.object(l,'write_report') as write,patch.object(h,'Services') as services,patch.object(S,'QueueDiagnostics') as obs:
            self.assertEqual(S.run(a,live=l,helper=h),1);services.assert_not_called();obs.assert_not_called()
            self.assertIn('Local lane packet cap',write.call_args.args[2]['error'])

    def test_wrong_peer_expected_packet_cap_rejected_before_load(self):
        l,h=helper();before=dict(main_pid=123,stats=dict(version='v0.1.0-preview.5',connected=1,sessions=2,drops=0,spoofed_drops=0,tx_datagrams=0,rx_datagrams=0,drop_reasons={},transport_profile='split-udp-v1',lanes={'tcp':{'packet_limit':128},'udp':{'packet_limit':128}}))
        a=argparse.Namespace(role='client',local='127.0.0.1',peer='127.0.0.1',instance='main',iface='wft0',queue_diagnostic=True,expect_version='v0.1.0-preview.5',expect_lane_packets=128,output='unused')
        with patch.object(l,'inspect_link',return_value=before),patch.object(l,'stats',return_value=before['stats']),patch.object(l,'write_report') as write,patch.object(S,'QueueDiagnostics'),patch.object(h,'Services'),patch.object(S,'rpc',return_value={'suite':'dv-queue-diagnostic-v1','version':'v0.1.0-preview.5','packet_limits':{'tcp':32,'udp':32}}),patch.object(S,'run_phase') as phase:
            self.assertEqual(S.run(a,live=l,helper=h),1);phase.assert_not_called()
            self.assertIn('Peer lane packet cap',write.call_args.args[2]['error'])

    def test_kernel_counter_increase_stops_test_load(self):
        from types import SimpleNamespace
        obj=S.QueueDiagnostics.__new__(S.QueueDiagnostics);obj.samples=[];obj.errors=0;obj.kernel_initial={'rx_dropped':0};obj.load_stop=threading.Event();obj.safety_stop_reason=None
        obj.done=SimpleNamespace(wait=lambda _:len(obj.samples)>0);obj.sample=lambda:dict(tun_kernel={'rx_dropped':1})
        obj.loop();self.assertTrue(obj.load_stop.is_set());self.assertIn('TUN kernel',obj.safety_stop_reason)

    def test_wait_expectation_fails_before_observer_or_app_load(self):
        l,h=helper();before=dict(main_pid=123,stats=dict(version='v0.1.0-preview.6',lanes={'udp':{'queue_wait_ms':0}}))
        a=argparse.Namespace(role='client',instance='main',iface='wft0',queue_diagnostic=True,expect_version='v0.1.0-preview.6',expect_udp_wait_ms=200,output='unused')
        with patch.object(l,'inspect_link',return_value=before),patch.object(l,'write_report') as write,patch.object(h,'Services') as services,patch.object(S,'QueueDiagnostics') as obs:
            self.assertEqual(S.run(a,live=l,helper=h),1);services.assert_not_called();obs.assert_not_called()
            self.assertIn('Local UDP wait',write.call_args.args[2]['error'])
