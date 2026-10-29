import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('live_check',ROOT/'tools/live_check.py')
L=importlib.util.module_from_spec(spec);spec.loader.exec_module(L)


class LiveCheckTests(unittest.TestCase):
    def test_pinned_helper_and_local_payloads_without_carrier(self):
        h=L.helper_from_bytes((ROOT/'tools/acceptance.py').read_bytes())
        service=h.Services('127.0.0.1','127.0.0.1',tcp_port=0,udp_port=0)
        try:
            with patch.object(h,'Carrier',side_effect=AssertionError('carrier must not be constructed')):
                self.assertTrue(h.transfer('127.0.0.1',h.Pacer(mbps=20),service.tcp_port)['pass_integrity'])
                result=h.udp_probe('127.0.0.1',service.udp_port,count=40)
                self.assertTrue(result['pass_integrity'])
        finally:service.close()
        with self.assertRaises(RuntimeError):L.helper_from_bytes(b'untrusted code')

    def test_preflight_rejects_wrong_route(self):
        args=argparse.Namespace(instance='main',iface='wft0',local='10.254.253.1',peer='10.254.253.2')
        replies=['active','123',json.dumps([dict(addr_info=[dict(local=args.local,prefixlen=30)])]),json.dumps([dict(dev='eth0')])]
        with patch.object(L,'read_command',side_effect=replies):
            with self.assertRaisesRegex(RuntimeError,'Peer route'):L.inspect_link(args)

    def test_only_read_commands_allowed(self):
        with self.assertRaises(ValueError):L.read_command(['systemctl','restart','dv-tunnel@main'])
        with self.assertRaises(ValueError):L.read_command(['ip','link','delete','wft0'])
        with self.assertRaises(ValueError):L.read_command(['ip','-j','-4','route','flush','table','main'])

    def test_stats_stale_never_accepts_connected(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(L,'STATE',Path(tmp)):
            p=Path(tmp)/'main';p.mkdir();f=p/'stats.json'
            f.write_text(json.dumps(dict(connected=1,version='v0.1.0-preview.2',token='MUST_NOT_APPEAR')))
            data=L.stats('main');self.assertNotIn('token',data)
            os.utime(f,(1,1))
            with self.assertRaises(RuntimeError):L.stats('main')

    def test_verdict_checks_both_udp_directions_and_tcp(self):
        mixed=dict(tcp=[dict(pass_integrity=True)]*4,udp_local_initiated=dict(pass_integrity=True),udp_peer_initiated=dict(pass_integrity=True))
        peer=dict(application=dict(completed_tcp_transfers=4,handler_errors=0),carrier_main_pid_unchanged=True)
        self.assertTrue(L.verdict(mixed,peer))
        mixed['udp_peer_initiated']['pass_integrity']=False
        self.assertFalse(L.verdict(mixed,peer))

    def test_counter_reset_not_negative_loss(self):
        self.assertIsNone(L.deltas(dict(drops=10),dict(drops=1))['drops'])

    def test_report_is_per_run_and_contains_no_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'reports'
            with patch('sys.stdout',new=io.StringIO()):
                L.write_report(out,'client',dict(status='passed'))
                L.write_report(out,'client',dict(status='passed'))
            folders=[p for p in out.iterdir() if p.is_dir()];self.assertEqual(len(folders),2)
            self.assertTrue((out/'latest-client.zip').is_file())
            self.assertTrue(all(set(x.name for x in p.iterdir())=={'report.json','report.zip'} for p in folders))

    def test_complete_two_role_orchestration_without_service_control(self):
        import concurrent.futures as cf
        import contextlib
        import socket
        import threading
        h=L.helper_from_bytes((ROOT/'tools/acceptance.py').read_bytes())
        guards=[];ready=threading.Event()
        class FakeGuard:
            def __init__(self):
                self.stop=threading.Event();self.reason=None;self.cpu_max=0;self.mem_min=1024
                guards.append(self)
            def close(self):pass
        original=h.Services
        def services(*a,**kw):
            obj=original(*a,**kw)
            ready.set()
            return obj
        sockets=[]
        try:
            for typ in (socket.SOCK_STREAM,socket.SOCK_DGRAM,socket.SOCK_STREAM,socket.SOCK_DGRAM):
                s=socket.socket(socket.AF_INET,typ);s.bind(('127.0.0.1',0));sockets.append(s)
            st,su,ct,cu=[s.getsockname()[1] for s in sockets]
        finally:
            for s in sockets:s.close()
        snapshot=dict(main_pid=123,stats=dict(version='preview',connected=1,sessions=1,drops=0,drop_reasons={}))
        with tempfile.TemporaryDirectory() as tmp,patch.object(h,'HostGuard',FakeGuard),patch.object(h,'Services',side_effect=services),patch.object(h,'Carrier',side_effect=AssertionError('must not manage carrier')),patch.object(L,'inspect_link',return_value=snapshot),contextlib.redirect_stdout(io.StringIO()):
            a=argparse.Namespace(role='server',local='127.0.0.1',peer='127.0.0.1',instance='main',iface='unused',output=str(Path(tmp)/'server'))
            b=argparse.Namespace(role='client',local='127.0.0.1',peer='127.0.0.1',instance='main',iface='unused',output=str(Path(tmp)/'client'))
            with cf.ThreadPoolExecutor(2) as pool:
                server=pool.submit(L.run,a,h,(st,su,ct,cu))
                try:
                    self.assertTrue(ready.wait(3))
                    client=pool.submit(L.run,b,h,(ct,cu,st,su))
                    self.assertEqual(client.result(timeout=40),0)
                    self.assertEqual(server.result(timeout=10),0)
                finally:
                    for g in guards:g.stop.set()
            c=json.loads((Path(tmp)/'client/latest-client.json').read_text())
            s=json.loads((Path(tmp)/'server/latest-server.json').read_text())
            self.assertEqual(c['status'],'passed')
            self.assertEqual(s['status'],'completed')
            self.assertTrue(c['peer']['carrier_main_pid_unchanged'])
            self.assertEqual(c['test_socket_cleanup'],'OK');self.assertEqual(s['test_socket_cleanup'],'OK')
