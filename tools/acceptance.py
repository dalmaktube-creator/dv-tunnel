#!/usr/bin/env python3
"""Bounded two-host functional acceptance; no panel, installer or firewall edits."""
import argparse
import concurrent.futures as cf
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import platform
import selectors
import signal
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import zipfile

VERSION = 'dv-acceptance-v1'
TCP_PORT, UDP_PORT = 18081, 18082
SIZE = 256 * 1024
PAYLOAD = bytes(range(256)) * (SIZE // 256)
HASH = hashlib.sha256(PAYLOAD).hexdigest()


def read_exact(sock, size):
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(min(65536, size - len(result)))
        if not chunk:
            raise EOFError('short stream')
        result.extend(chunk)
    return bytes(result)


def send_json(sock, obj):
    raw = json.dumps(obj, separators=(',', ':')).encode()
    if len(raw) > 16384:
        raise ValueError('control report too large')
    sock.sendall(struct.pack('!I', len(raw)) + raw)


def recv_json(sock):
    size = struct.unpack('!I', read_exact(sock, 4))[0]
    if not 0 < size <= 16384:
        raise ValueError('control frame too large')
    return json.loads(read_exact(sock, size))


class Pacer:
    """Aggregate sender budget for all TCP test workers at one endpoint."""
    def __init__(self, mbps=2):
        self.rate = mbps * 1_000_000 / 8
        self.next = time.monotonic()
        self.lock = threading.Lock()

    def send(self, sock, data):
        for offset in range(0, len(data), 4096):
            part = data[offset:offset + 4096]
            with self.lock:
                due = max(self.next, time.monotonic())
                self.next = due + len(part) / self.rate
            time.sleep(max(0, due - time.monotonic()))
            sock.sendall(part)


def udp_probe(peer, port=UDP_PORT, count=2000, pps=200, stop=None):
    """Four application sockets; ~1.92 Mbps requested payload, 5s final drain.

    Functional result only; Python pacing is not a capacity or latency benchmark.
    """
    if not 1 <= count <= 2000 or not 1 <= pps <= 200:
        raise ValueError('probe bounds')
    stop = stop or threading.Event()
    sel = selectors.DefaultSelector()
    socks, expected, seen = [], {}, set()
    invalid = duplicates = sent = 0
    nonce = os.urandom(8)
    start = last_rx = time.monotonic()
    send_end = None
    try:
        for _ in range(4):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            socks.append(s)
            s.connect((peer, port))
            s.setblocking(False)
            sel.register(s, selectors.EVENT_READ)
        while True:
            now = time.monotonic()
            if stop.is_set():
                raise RuntimeError('safety guard stopped probe')
            if now - last_rx > 5:
                raise TimeoutError('five seconds without valid UDP reply')
            if sent < count and now >= start + sent / pps:
                seq = sent
                msg = b'DVAC1' + nonce + struct.pack('!I', seq) + bytes([seq % 251]) * 1183
                s = socks[seq % 4]
                try:
                    n = s.send(msg)
                    if n != len(msg):
                        raise RuntimeError('short datagram send')
                    expected[seq] = (msg, s)
                    sent += 1
                    if sent == count:
                        send_end = time.monotonic()
                except BlockingIOError:
                    pass
            if send_end is not None and (len(seen) == sent or now - send_end >= 5):
                break
            due = start + sent / pps if sent < count else now + .02
            for key, _ in sel.select(min(.02, max(0, due - time.monotonic()))):
                for _ in range(64):
                    try:
                        msg = key.fileobj.recv(2048)
                    except BlockingIOError:
                        break
                    if len(msg) != 1200 or msg[:13] != b'DVAC1' + nonce:
                        invalid += 1
                        continue
                    seq = struct.unpack('!I', msg[13:17])[0]
                    if seq not in expected or expected[seq] != (msg, key.fileobj):
                        invalid += 1
                        continue
                    if seq in seen:
                        duplicates += 1
                    seen.add(seq)
                    last_rx = time.monotonic()
        return dict(sent=sent, returned_unique=len(seen), missing_by_deadline=sent-len(seen),
                    invalid=invalid, duplicates=duplicates, flows=4,
                    requested_payload_mbps=pps*1200*8/1e6,
                    offered_window_s=round((send_end or time.monotonic())-start, 3),
                    pass_integrity=(sent == count and len(seen) == count and invalid == 0))
    finally:
        sel.close()
        for s in socks:
            s.close()


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 8

    def verify_request(self, request, address):
        if address[0] != self.owner.peer or not self.owner.slots.acquire(False):
            return False
        return True

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.owner.slots.release()

    def handle_error(self, request, address):
        # Do not put raw traffic or private runtime paths in console reports.
        self.owner.handler_errors += 1


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(30)
        o = self.server.owner
        obj = recv_json(self.request)
        op = obj.get('op')
        if op == 'ping':
            send_json(self.request, dict(ok=True, suite=VERSION))
        elif op == 'transfer':
            data = read_exact(self.request, SIZE)
            if data != PAYLOAD:
                raise ValueError('TCP upload mismatch')
            send_json(self.request, dict(bytes=SIZE, sha256=HASH))
            o.pacer.send(self.request, PAYLOAD)
            with o.lock:
                o.transfers += 1
        elif op == 'reverse_udp':
            with o.reverse_lock:
                result = udp_probe(o.peer, o.peer_udp_port, stop=o.stop)
                with o.lock:
                    o.reverse_result = result
            send_json(self.request, result)
        elif op == 'reconnect' and o.allow_reconnect:
            send_json(self.request, dict(accepted=True))
            o.reconnect.set()
        elif op == 'finish':
            send_json(self.request, o.snapshot())
            o.finished.set()
        else:
            raise ValueError('unsupported acceptance operation')


class Services:
    def __init__(self, local, peer, tcp_port=TCP_PORT, udp_port=UDP_PORT,
                 peer_udp_port=UDP_PORT, allow_reconnect=False):
        self.peer, self.peer_udp_port = peer, peer_udp_port
        self.stop = threading.Event()
        self.finished = threading.Event()
        self.reconnect = threading.Event()
        self.allow_reconnect = allow_reconnect
        self.pacer = Pacer()
        self.slots = threading.BoundedSemaphore(8)
        self.lock, self.reverse_lock = threading.Lock(), threading.Lock()
        self.transfers = self.handler_errors = 0
        self.reverse_result = None
        self.threads = []
        self.tcp = TCPServer((local, tcp_port), Handler, bind_and_activate=False)
        self.tcp.owner = self
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.tcp.server_bind()
            self.tcp.server_activate()
            self.udp.bind((local, udp_port))
            self.udp.settimeout(.2)
        except BaseException:
            self.tcp.server_close()
            self.udp.close()
            raise
        self.tcp_port = self.tcp.server_address[1]
        self.udp_port = self.udp.getsockname()[1]
        for target in (self.tcp.serve_forever, self.echo):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self.threads.append(t)

    def echo(self):
        while not self.stop.is_set():
            try:
                data, addr = self.udp.recvfrom(2048)
                if addr[0] == self.peer and len(data) == 1200 and data.startswith(b'DVAC1'):
                    self.udp.sendto(data, addr)
            except socket.timeout:
                pass
            except OSError:
                break

    def snapshot(self):
        with self.lock:
            return dict(completed_tcp_transfers=self.transfers,
                        handler_errors=self.handler_errors,
                        reverse_udp=self.reverse_result,
                        server_cleanup='pending_until_listener_stops')

    def close(self):
        self.stop.set()
        self.tcp.shutdown()
        self.tcp.server_close()
        self.udp.close()
        for t in self.threads:
            t.join(2)


def rpc(peer, op, port=TCP_PORT):
    with socket.create_connection((peer, port), timeout=30) as s:
        s.settimeout(30)
        send_json(s, dict(op=op))
        return recv_json(s)


def transfer(peer, pacer, port=TCP_PORT):
    with socket.create_connection((peer, port), timeout=30) as s:
        s.settimeout(30)
        send_json(s, dict(op='transfer'))
        pacer.send(s, PAYLOAD)
        meta = recv_json(s)
        data = read_exact(s, SIZE)
        if data != PAYLOAD or meta != dict(bytes=SIZE, sha256=HASH):
            raise ValueError('TCP payload integrity failure')
        return dict(upload_bytes=SIZE, download_bytes=len(data), sha256=HASH, pass_integrity=True)


class HostGuard:
    def __init__(self):
        self.stop = threading.Event()
        self.done = threading.Event()
        self.reason = None
        self.cpu_max = 0
        self.mem_min = None
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        prev = None
        high = 0
        while not self.done.is_set():
            try:
                vals = [int(x) for x in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
                total, idle = sum(vals), vals[3] + vals[4]
                if prev and total > prev[0]:
                    cpu = 100*(1-(idle-prev[1])/(total-prev[0]))
                    self.cpu_max = max(self.cpu_max, cpu)
                    high = high + 1 if cpu >= 85 else 0
                prev = total, idle
                mem = next(int(x.split()[1])/1024 for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:'))
                self.mem_min = mem if self.mem_min is None else min(mem, self.mem_min)
                if high >= 2 or mem < 192:
                    self.reason = 'host_cpu_85pct_twice' if high >= 2 else 'host_memory_below_192MiB'
                    self.stop.set()
                    return
            except (OSError, ValueError, StopIteration):
                self.reason = 'host_guard_unavailable'
                self.stop.set()
                return
            self.done.wait(1)

    def close(self):
        self.done.set()
        self.thread.join(2)


def checked(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10)


class Carrier:
    def __init__(self, args, work):
        self.args, self.work, self.proc, self.log = args, work, None, None
        source = Path(args.field_dir) / 'config.json'
        self.source = source
        self.before = hashlib.sha256(source.read_bytes()).hexdigest()
        cfg = json.loads(source.read_text())
        self.iface = cfg.get('tun')
        if self.iface != 'wft0' or cfg.get('role') != ('egress' if args.role == 'server' else 'ingress'):
            raise ValueError('this acceptance requires the existing direct wft0 field pair')
        if cfg.get('mode') != ('listen' if args.role == 'server' else 'dial'):
            raise ValueError('expected direct field pair; reverse topology needs a separate run')
        if cfg.get('batch', False) or not cfg.get('tun_offload', False):
            raise ValueError('expected offload-lowlat configuration')
        if cfg.get('notsent_lowat') != 131072 or cfg.get('queue_bytes') != 262144:
            raise ValueError('unexpected existing queue settings')
        net = ipaddress.ip_network(args.local + '/30', strict=False)
        if ipaddress.ip_address(args.peer) not in net or args.local == args.peer:
            raise ValueError('local/peer must be distinct addresses in the same /30')
        if ipaddress.ip_address(args.local) not in list(net.hosts()) or ipaddress.ip_address(args.peer) not in list(net.hosts()):
            raise ValueError('use the two usable /30 addresses')
        if cfg.get('allowed_sources') != [args.peer+'/32']:
            raise ValueError('expected the exact peer /32 source allowlist')
        if subprocess.run(['ip','link','show','dev',self.iface], capture_output=True).returncode == 0:
            raise RuntimeError('wft0 exists; refusing to reuse or delete it')
        generator = Path(__file__).with_name('dv_pair.py')
        spec = importlib.util.spec_from_file_location('dv_pair', generator)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        port = int(cfg.get('listen','0:42443').rsplit(':',1)[1])
        checked(['python3','-c',module.PREFLIGHT,str(net),cfg['mode'],str(port)])
        cfg['queue_packets'] = 128
        cfg['stats_file'] = str(work/'stats.json')
        path = work/'config.json'
        path.write_text(json.dumps(cfg))
        path.chmod(0o600)
        script = module.RUN
        for a,b in {'__IFACE__':self.iface,'__SUBNET__':str(net),'__MODE__':cfg['mode'],
                    '__PORT__':str(port),'__LOCAL__':args.local,'__PEER__':args.peer,
                    '__PREFLIGHT__':module.PREFLIGHT}.items():
            script = script.replace(a,b)
        (work/'run.sh').write_text(script)
        (work/'run.sh').chmod(0o600)
        checked([args.binary,'--config',str(path),'--check'])

    def start(self):
        self.log = (self.work/'private-carrier.log').open('a')
        self.proc = subprocess.Popen(['bash',str(self.work/'run.sh'),self.args.binary],
                                     cwd=self.work, stdout=self.log, stderr=self.log,
                                     start_new_session=True)
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise RuntimeError('owned carrier exited; private log retained only until cleanup')
            p = checked(['ip','-j','-4','addr','show'])
            rows = json.loads(p.stdout)
            if any(r.get('ifname') == self.iface and any(a.get('local') == self.args.local for a in r.get('addr_info',[])) for r in rows):
                return
            time.sleep(.1)
        raise TimeoutError('local TUN address not ready')

    def stats(self):
        try:
            p = self.work/'stats.json'
            if time.time()-p.stat().st_mtime > 6:
                return {}
            d = json.loads(p.read_text())
            return {k:d[k] for k in ('version','connected','sessions','tx_datagrams','rx_datagrams','drops','spoofed_drops','drop_reasons') if k in d}
        except (OSError, ValueError):
            return {}

    def halt(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGTERM)
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                    self.proc.wait(timeout=3)
            self.proc = None
        if self.log:
            self.log.close()
            self.log = None

    def cleanup(self):
        self.halt()
        absent = subprocess.run(['ip','link','show','dev',self.iface], capture_output=True).returncode != 0
        unchanged = hashlib.sha256(self.source.read_bytes()).hexdigest() == self.before
        return dict(test_interface_absent=absent, original_config_unchanged=unchanged,
                    status='OK' if absent and unchanged else 'CHECK_REQUIRED')


def wait_until(fn, seconds, guard, carrier):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if guard.stop.is_set():
            raise RuntimeError('host safety guard')
        if not carrier.proc or carrier.proc.poll() is not None:
            raise RuntimeError('owned carrier stopped')
        if fn():
            return
        time.sleep(.25)
    raise TimeoutError('acceptance wait expired')


def mixed(peer, stop, tcp_port=TCP_PORT, udp_port=UDP_PORT):
    pacer = Pacer()
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        udp = pool.submit(udp_probe, peer, udp_port, 2000, 200, stop)
        reverse = pool.submit(rpc, peer, 'reverse_udp', tcp_port)
        tcp = [pool.submit(transfer, peer, pacer, tcp_port) for _ in range(4)]
        return dict(tcp=[x.result(timeout=35) for x in tcp],
                    udp_local_initiated=udp.result(timeout=20),
                    udp_peer_initiated=reverse.result(timeout=20))


def run(args):
    args.binary = str(Path(args.binary).resolve())
    os.umask(0o077)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = dict(suite=VERSION, role=args.role, status='failed',
                  source_commit='4dd2174356d1df3eb4499615e3d9e508c8b18e71',
                  started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                  kernel=platform.release(), architecture=platform.machine(),
                  queue_packets=128, queue_bytes=262144,
                  scope='point-to-point standalone TUN; no forwarded clients, Internet NAT or panel integration',
                  notes=['Functional acceptance, not a capacity/latency benchmark.',
                         'UDP missing means no reply by five-second final drain, not proven permanent wire loss.',
                         'Whole-host resource guards cannot guarantee zero production impact.'])
    carrier = services = guard = None
    try:
        if os.geteuid() != 0:
            raise PermissionError('TUN acceptance requires root')
        report['binary_sha256'] = hashlib.sha256(Path(args.binary).read_bytes()).hexdigest()
        report['binary_version'] = checked([args.binary,'--version']).stdout.strip()
        guard = HostGuard()
        with tempfile.TemporaryDirectory(prefix='dv-private-') as tmp:
            try:
                carrier = Carrier(args,Path(tmp))
                carrier.start()
                services = Services(args.local,args.peer,allow_reconnect=args.role=='server')
                print('SERVER_READY — run client now' if args.role=='server' else 'CLIENT_WAITING_FOR_LINK',flush=True)
                wait_until(lambda: carrier.stats().get('connected') == 1,90,guard,carrier)
                if args.role == 'server':
                    end = time.monotonic() + 150
                    restarted = False
                    while not services.finished.is_set():
                        if time.monotonic() > end:
                            raise TimeoutError('server wall-clock limit')
                        if guard.stop.is_set():
                            services.stop.set()
                            raise RuntimeError('host safety guard')
                        if services.reconnect.is_set() and not restarted:
                            time.sleep(.4)
                            carrier.halt()
                            time.sleep(3)
                            carrier.start()
                            restarted = True
                            print('OWNED_CARRIER_RESTARTED',flush=True)
                        if carrier.proc.poll() is not None:
                            raise RuntimeError('owned carrier exited')
                        time.sleep(.1)
                    report['application'] = services.snapshot()
                    report['restart_performed'] = restarted
                    report['status'] = 'completed'
                    time.sleep(.4)
                else:
                    print('MIXED_TCP_UDP_RUNNING — about 15 seconds',flush=True)
                    report['mixed'] = mixed(args.peer,guard.stop)
                    report['mtu'] = dict()
                    ping = subprocess.run(['ping','-n','-c','3','-W','2','-M','do','-s','1308','-I','wft0',args.peer],capture_output=True,text=True,timeout=12)
                    report['mtu'] = dict(ipv4_packet_bytes=1336,pass_probe=ping.returncode==0,
                                         summary='\n'.join(ping.stdout.strip().splitlines()[-2:]))
                    before_state = carrier.stats()
                    if before_state.get('connected') != 1 or before_state.get('sessions',0) < 1:
                        raise RuntimeError('fresh connected session counters required')
                    before = before_state['sessions']
                    rpc(args.peer,'reconnect')
                    print('WAITING_FOR_RECONNECT',flush=True)
                    wait_until(lambda: carrier.stats().get('sessions',0)>before and carrier.stats().get('connected')==1,45,guard,carrier)
                    report['reconnect'] = dict(pass_probe=True,sessions_before=before,sessions_after=carrier.stats()['sessions'])
                    report['after_reconnect'] = dict(tcp=transfer(args.peer,Pacer()),
                                                    udp=udp_probe(args.peer,count=200,stop=guard.stop))
                    report['peer_application'] = rpc(args.peer,'finish')
                    good = (all(x['pass_integrity'] for x in report['mixed']['tcp']) and
                            report['mixed']['udp_local_initiated']['pass_integrity'] and
                            report['mixed']['udp_peer_initiated']['pass_integrity'] and
                            report['mtu']['pass_probe'] and report['after_reconnect']['tcp']['pass_integrity'] and
                            report['after_reconnect']['udp']['pass_integrity'])
                    report['status'] = 'passed' if good else 'quality_or_integrity_failure'
                time.sleep(2.2)
                report['carrier_final'] = carrier.stats()
            finally:
                if services:
                    services.close()
                if carrier:
                    report['cleanup'] = carrier.cleanup()
                    if report['cleanup']['status'] != 'OK':
                        report['status'] = 'cleanup_check_required'
    except (Exception, KeyboardInterrupt) as e:
        report['status'] = 'failed'
        report['error_type'] = type(e).__name__
        print('TEST_STOPPED:',type(e).__name__,flush=True)
    finally:
        if guard:
            guard.close()
            report['host'] = dict(cpu_max_sample_pct=round(guard.cpu_max,2),
                                  mem_available_min_sample_mib=guard.mem_min,stop_reason=guard.reason)
        report['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        text = json.dumps(report,indent=2)+'\n'
        path = output/('latest-'+args.role+'.json')
        path.write_text(text)
        with zipfile.ZipFile(output/('latest-'+args.role+'.zip'),'w',zipfile.ZIP_DEFLATED) as z:
            z.writestr('report.json',text)
        print('REPORT:',str(path),flush=True)
        print('STATUS:',report['status'],'CLEANUP:',report.get('cleanup',{}).get('status','NOT_STARTED'),flush=True)
    return 0 if report['status'] in ('passed','completed') else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('role',choices=['server','client'])
    ap.add_argument('--field-dir',required=True)
    ap.add_argument('--binary',required=True)
    ap.add_argument('--local',required=True)
    ap.add_argument('--peer',required=True)
    ap.add_argument('--output',default='./results')
    args = ap.parse_args()
    for addr in (args.local,args.peer):
        ipaddress.IPv4Address(addr)
    def interrupt(signum,frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupt)
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
