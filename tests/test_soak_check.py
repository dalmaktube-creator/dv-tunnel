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
        with self.assertRaisesRegex(RuntimeError,'Safety'):S.udp_probe('127.0.0.1',9,.2,100,stop)
        for seconds,pps in ((121,200),(120,2000),(.2,2001),(0,1)):
            with self.assertRaises(ValueError):S.udp_probe('127.0.0.1',9,seconds,pps,threading.Event())

    def test_quality_gate_stops_loss_tail_underfilled_and_tcp_errors(self):
        u=dict(pass_integrity=True,duplicates=0,per_flow_reordered=0,echo_rtt_ms=dict(p99=50),actual_offered_payload_mbps=1.92,requested_payload_mbps=1.92)
        r=dict(tcp=dict(completed=1,errors=[]),udp_local_initiated=u,udp_peer_initiated=copy.deepcopy(u))
        self.assertTrue(S.phase_ok(r))
        for key,val in (('pass_integrity',False),('duplicates',1),('per_flow_reordered',1),('actual_offered_payload_mbps',1.0),('echo_rtt_ms',dict(p99=600))):
            x=copy.deepcopy(r);x['udp_peer_initiated'][key]=val;self.assertFalse(S.phase_ok(x))
        r['tcp']['errors']=['TimeoutError'];self.assertFalse(S.phase_ok(r))

    def test_monitor_detects_session_change(self):
        l,h=helper();args=argparse.Namespace(instance='main')
        start=dict(main_pid=1,stats=dict(sessions=2,transport_profile='split-udp-v1'))
        with patch.object(l,'inspect_link',return_value=start),patch.object(l,'stats',return_value=dict(sessions=3,transport_profile='split-udp-v1')):
            stop=threading.Event();m=S.Monitor(l,args,stop);m.thread.join(1);m.close()
            self.assertTrue(stop.is_set());self.assertIn('session',m.error)

    def test_monitor_stops_on_queue_growth(self):
        l,h=helper();args=argparse.Namespace(instance='main')
        start=dict(main_pid=1,stats=dict(sessions=2,transport_profile='split-udp-v1',drop_reasons=dict(queue_packets=0)))
        current=dict(start['stats'],drop_reasons=dict(queue_packets=1))
        with patch.object(l,'inspect_link',return_value=start),patch.object(l,'stats',return_value=current):
            stop=threading.Event();m=S.Monitor(l,args,stop);m.thread.join(1);m.close()
            self.assertTrue(stop.is_set());self.assertIn('queue',m.error);self.assertEqual(len(m.samples),1)

    def test_two_role_suite_without_carrier_or_config_access(self):
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
            server=argparse.Namespace(role='server',local='127.0.0.1',peer='127.0.0.1',instance='main',iface='unused',output=tmp+'/server')
            client=argparse.Namespace(**dict(vars(server),role='client',output=tmp+'/client'))
            values=[]
            t=threading.Thread(target=lambda:values.append(S.run(server,live=l,helper=h,phases=phases,ports=tuple(ports))),daemon=True);t.start()
            deadline=time.monotonic()+3
            while True:
                try:S.rpc(h,'127.0.0.1',ports[0],dict(op='ping'));break
                except OSError:
                    if time.monotonic()>deadline:self.fail('Server did not start')
                    time.sleep(.02)
            result=S.run(client,live=l,helper=h,phases=phases,ports=(ports[2],ports[3],ports[0],ports[1]))
            t.join(15);self.assertFalse(t.is_alive());self.assertEqual(values,[0]);self.assertEqual(result,0)
            d=json.loads((Path(tmp)/'client/latest-client.json').read_text())
            self.assertEqual(d['status'],'passed');self.assertEqual(len(d['phases']),2)
            self.assertTrue(d['carrier_main_pid_unchanged']);self.assertEqual(d['test_socket_cleanup'],'OK')
            self.assertTrue(all(x['tcp']['completed']>0 for x in d['phases']))
