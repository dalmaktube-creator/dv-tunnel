import concurrent.futures as cf
import importlib.util
import socket
import struct
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('acceptance',Path(__file__).resolve().parents[1]/'tools/acceptance.py')
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)


class AcceptanceTests(unittest.TestCase):
    def test_local_payloads_and_control(self):
        service = A.Services('127.0.0.1','127.0.0.1',tcp_port=0,udp_port=0)
        try:
            self.assertTrue(A.rpc('127.0.0.1','ping',service.tcp_port)['ok'])
            with cf.ThreadPoolExecutor(3) as pool:
                pacer = A.Pacer(mbps=20)
                tcp = pool.submit(A.transfer,'127.0.0.1',pacer,service.tcp_port)
                udp = pool.submit(A.udp_probe,'127.0.0.1',service.udp_port,100,200)
                self.assertTrue(tcp.result(10)['pass_integrity'])
                result = udp.result(10)
                self.assertEqual(result['sent'],100)
                self.assertEqual(result['returned_unique'],100)
                self.assertTrue(result['pass_integrity'])
            self.assertEqual(A.rpc('127.0.0.1','finish',service.tcp_port)['completed_tcp_transfers'],1)
        finally:
            service.close()

    def test_control_frame_bound(self):
        left,right = socket.socketpair()
        try:
            left.sendall(struct.pack('!I',16385))
            with self.assertRaises(ValueError):
                A.recv_json(right)
        finally:
            left.close();right.close()

    def test_udp_limits(self):
        for n in (0,2001):
            with self.assertRaises(ValueError):
                A.udp_probe('127.0.0.1',count=n)

    def test_no_production_mutation_commands(self):
        source = (Path(__file__).resolve().parents[1]/'tools/acceptance.py').read_text()
        for prohibited in ('iptables','nft flush','sysctl -w','ip link delete','pkill','killall'):
            self.assertNotIn(prohibited,source)

    def test_both_initiating_directions_mixed(self):
        client = A.Services('127.0.0.1','127.0.0.1',tcp_port=0,udp_port=0)
        server = A.Services('127.0.0.1','127.0.0.1',tcp_port=0,udp_port=0,peer_udp_port=client.udp_port)
        try:
            report = A.mixed('127.0.0.1',client.stop,server.tcp_port,server.udp_port)
            self.assertEqual(len(report['tcp']),4)
            self.assertTrue(all(x['pass_integrity'] for x in report['tcp']))
            for k in ('udp_local_initiated','udp_peer_initiated'):
                self.assertEqual(report[k]['sent'],2000)
                self.assertTrue(report[k]['pass_integrity'])
        finally:
            client.close();server.close()

    def test_runtime_copy_does_not_mutate_original(self):
        import argparse
        import hashlib
        import json
        import tempfile
        from unittest.mock import patch
        generator_path = Path(__file__).resolve().parents[1]/'tools/dv_pair.py'
        spec = importlib.util.spec_from_file_location('dv_pair',generator_path)
        f = importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f.generate(root/'pair','198.51.100.10','10.254.253.0/30')
            field = root/'pair/node-b'
            hashes = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in field.iterdir()}
            work = root/'runtime';work.mkdir()
            args = argparse.Namespace(field_dir=str(field),role='server',local='10.254.253.2',peer='10.254.253.1',binary='/test/binary')
            with patch.object(A.subprocess,'run') as run, patch.object(A,'checked'):
                run.return_value.returncode = 1
                A.Carrier(args,work)
            self.assertEqual(hashes,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in field.iterdir()})
            config = json.loads((work/'config.json').read_text())
            self.assertEqual(config['queue_packets'],128)
            self.assertEqual(config['queue_bytes'],262144)
            self.assertEqual(config['stats_file'],str(work/'stats.json'))
            self.assertEqual((work/'config.json').stat().st_mode&0o777,0o600)
            self.assertEqual((work/'run.sh').stat().st_mode&0o777,0o600)
