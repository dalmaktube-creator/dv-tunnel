#!/usr/bin/env python3
"""Bounded live-link soak + staged mixed load. Never manages the carrier."""
import argparse
from array import array
import concurrent.futures as cf
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import re
import resource
import shutil
import subprocess
import signal
import socket
import struct
import threading
import time
import types
import urllib.request

LIVE_COMMIT='b7e920ad77c509b0d43cd46bc82e716100495325'
LIVE_SHA256='e4b66dd22f601ff3948585aeed6c0e0b3c1e96c2d8b19d9cb9f34531923c10c0'
LIVE_URL='https://raw.githubusercontent.com/dalmaktube-creator/dv-tunnel/'+LIVE_COMMIT+'/tools/live_check.py'
PHASES=[dict(id=1,seconds=120,pps=200),dict(id=2,seconds=30,pps=500),dict(id=3,seconds=30,pps=1000),dict(id=4,seconds=20,pps=2000)]
MAGIC=b'DVAC1'
PAYLOAD_BYTES=1200


def module_from_bytes(data):
    if hashlib.sha256(data).hexdigest()!=LIVE_SHA256:raise RuntimeError('Pinned live checker hash mismatch')
    m=types.ModuleType('dv_pinned_live');m.__file__='pinned-live-check.py'
    exec(compile(data,LIVE_URL,'exec'),m.__dict__)
    return m


def load_live():
    with urllib.request.urlopen(LIVE_URL,timeout=25) as r:data=r.read(131073)
    if len(data)>131072:raise RuntimeError('Helper too large')
    return module_from_bytes(data)


# Observation only: numeric TCP socket fields for this carrier PID, never addresses,
# process arguments, payloads, private configuration or another process's sockets.
TCP_KEYS=set("rto rtt ato mss pmtu cwnd ssthresh bytes_sent bytes_retrans bytes_acked bytes_received segs_out segs_in data_segs_out data_segs_in unacked retrans notsent minrtt rcv_rtt busy rwnd_limited sndbuf_limited".split())


def parse_tcp_samples(text,pid):
    result=[];current=None
    for line in text.splitlines():
        cols=line.split()
        if cols and cols[0] in {'ESTAB','SYN-SENT','SYN-RECV','FIN-WAIT-1','FIN-WAIT-2','TIME-WAIT','CLOSE-WAIT','LAST-ACK','CLOSING','CLOSED'}:
            current=None
            if len(cols)<5 or cols[0]!='ESTAB' or not re.search(r'pid='+str(pid)+r'(?=,|\))',line):continue
            try:current=dict(recv_q=int(cols[1]),send_q=int(cols[2]))
            except ValueError:continue
            fd=re.search(r'fd=(\d+)',line)
            if fd:current['fd']=int(fd.group(1))
            result.append(current)
        elif current is not None:
            for token in line.split():
                key,sep,value=token.partition(':')
                if sep and key in TCP_KEYS and re.fullmatch(r'[0-9./]+(?:ms|us|s)?',value):current[key]=value
    return result[:8]


def process_identity(pid):
    # /proc stat comm can contain spaces and parentheses; fields follow LAST ')'.
    text=(Path('/proc')/str(pid)/'stat').read_text()
    if len(text)>8192:raise ValueError('Process identity too large')
    fields=text.rsplit(')',1)[1].split()
    return int(fields[1]),int(fields[19])  # parent PID, kernel start ticks


def resolve_carrier_pid(service_pid):
    # MainPID belongs to service_run.py; only its verified direct carrier child
    # owns the WSS sockets. Never scan or trust arbitrary process names globally.
    root=Path('/proc')/str(service_pid)
    text=(root/'task'/str(service_pid)/'children').read_text()
    if len(text)>4096 or len(text.split())>16:raise RuntimeError('Unexpected service child inventory')
    group=(root/'cgroup').read_text();found=[]
    for token in text.split():
        child=int(token)
        if child<=0:raise RuntimeError('Invalid child PID')
        path=Path('/proc')/str(child)
        try:
            if Path(os.readlink(path/'exe')).name!='dv-tunnel':continue
            if process_identity(child)[0]!=service_pid or (path/'cgroup').read_text()!=group:continue
        except FileNotFoundError:continue
        found.append(child)
    if len(found)!=1:raise RuntimeError('Expected one direct carrier child in the service cgroup')
    return found[0]


def cpu_snapshot(identities):
    """Numeric fixed-PID counters only; no argv, environment or process scan."""
    lines=Path('/proc/stat').read_text().splitlines()
    ticks=[int(x) for x in lines[0].split()[1:9]]
    if len(ticks)!=8 or min(ticks)<0:raise ValueError('Invalid host CPU counters')
    result=dict(host_ticks=dict(zip(('user','nice','system','idle','iowait','irq','softirq','steal'),ticks)),
                logical_cpus=sum(bool(re.fullmatch(r'cpu\d+ .*',x)) for x in lines),processes={})
    for label,(pid,identity) in identities.items():
        text=(Path('/proc')/str(pid)/'stat').read_text()
        if len(text)>8192:raise ValueError('Process stat too large')
        f=text.rsplit(')',1)[1].split()
        if (int(f[1]),int(f[19]))!=identity:raise RuntimeError('CPU observation PID identity changed')
        result['processes'][label]=dict(pid=pid,start_ticks=int(f[19]),user_ticks=int(f[11]),system_ticks=int(f[12]),threads=int(f[17]))
    return result


class QueueDiagnostics:
    def __init__(self,pid,iface,lean=False):
        self.lean=lean
        if not isinstance(pid,int) or pid<=0 or not re.fullmatch(r'[a-zA-Z0-9_-]{1,15}',iface):raise ValueError('Diagnostic identity invalid')
        self.service_pid=pid;self.pid=resolve_carrier_pid(pid);self.identity=process_identity(self.pid);self.iface=iface;self.ss=shutil.which('ss')
        if not self.ss:raise RuntimeError('ss is required for queue diagnostics')
        self.samples=[];self.done=threading.Event();self.errors=0;self.thread=None;self.load_stop=None;self.safety_stop_reason=None;self.kernel_initial=None
        self.cpu_ids={label:(p,process_identity(p)) for label,p in [('checker',os.getpid()),('runner',self.service_pid),('carrier',self.pid)]}
        self.clock_ticks=os.sysconf('SC_CLK_TCK')
        first=self.sample()
        if 'error_type' in first or len(first.get('tcp_sockets',[]))!=2:raise RuntimeError('Expected two established carrier-owned TCP sockets on port 42443')
        self.kernel_initial=first.get('tun_kernel')
        self.samples.append(first)
        self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()
    def sample(self):
        result=dict(monotonic=time.monotonic(),utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
        try:
            if process_identity(self.pid)!=self.identity:raise RuntimeError('Carrier process identity changed')
            if not getattr(self,'lean',False) or not self.samples:
                p=subprocess.run([self.ss,'-Htinp','( sport = :42443 or dport = :42443 )'],capture_output=True,text=True,timeout=.5,check=True)
                if len(p.stdout)>1048576:raise ValueError('Socket observation too large')
                result['tcp_sockets']=parse_tcp_samples(p.stdout,self.pid)
            else:result['tcp_observation']='not_sampled_lean_mode'
            usage=resource.getrusage(resource.RUSAGE_CHILDREN)
            result['checker_reaped_children_cpu_seconds']=dict(user=usage.ru_utime,system=usage.ru_stime)
            kernel={}
            for key in ('rx_packets','tx_packets','rx_dropped','tx_dropped','rx_errors','tx_errors'):
                kernel[key]=int((Path('/sys/class/net')/self.iface/'statistics'/key).read_text())
            result['tun_kernel']=kernel
            if getattr(self,'cpu_ids',None):result['cpu']=cpu_snapshot(self.cpu_ids)
        except Exception as exc:result['error_type']=type(exc).__name__
        return result
    def loop(self):
        while len(self.samples)<900 and not self.done.wait(.5):
            sample=self.sample();self.errors+=int('error_type' in sample);self.samples.append(sample)
            if 'error_type' in sample and getattr(self,'load_stop',None) is not None:
                self.safety_stop_reason='Queue observation failed during load';self.load_stop.set()
            initial=getattr(self,'kernel_initial',None);kernel=sample.get('tun_kernel')
            if initial is not None and kernel is not None and any(kernel.get(k,0)>initial.get(k,0) for k in ('rx_dropped','tx_dropped','rx_errors','tx_errors')):
                self.safety_stop_reason='TUN kernel drop/error counter increased'
                if self.load_stop is not None:self.load_stop.set()
    def close(self):
        self.done.set()
        if self.thread:self.thread.join(2)
        return dict(kind='carrier-owned-tcp-and-tun-v1',tcp_sampling='preflight_only' if getattr(self,'lean',False) else 'every_sample',sample_interval_s=.5,max_samples=900,
                    service_pid=self.service_pid,socket_pid=self.pid,cpu_clock_ticks_per_second=getattr(self,'clock_ticks',None),samples=self.samples,observation_errors=self.errors,safety_stop_reason=self.safety_stop_reason,
                    sample_cap_reached=len(self.samples)>=900,
                    notes=['Service MainPID is the Python runner; socket PID is its unique dv-tunnel child verified by PPid, cgroup and kernel start ticks. Numeric ss fields only, established sockets on TCP port 42443.',
                           'Lean mode verifies two owned TCP sockets at preflight only; subsequent CPU/TUN polling stays half-second. No interim TCP queue/retransmission evidence in lean mode.',
                           'Child CPU counters cover reaped checker subprocesses collectively, not production services; active not-yet-reaped child work is absent until reaped.',
                           'FD identifies a local socket only; not a proven UDP/TCP lane mapping. FD can be reused after reconnect.',
                           'Socket counters are cumulative: compare same socket over time, not endpoint totals.',
                           'Half-second polling can miss short stalls; no causal WAN or loss attribution from a correlation alone.',
                           'TUN kernel counters are interface-wide, not probe sequence counters. Sampling itself has some overhead.',
                           'CPU fields are cumulative numeric ticks for this checker, runner and carrier only, identity-checked. No production process inventory.',
                           'Host busy guard includes steal time. Separate host user/system/irq/softirq/steal deltas before attributing CPU to the carrier.',
                           'Process CPU: delta(user_ticks+system_ticks)/clock_ticks/elapsed_seconds*100 is one-core percent, not whole-host percent. Logical CPU count is recorded; do not mix denominators.'])


def packet(nonce,seq):
    return MAGIC+nonce+struct.pack('!I',seq)+bytes([seq%251])*1183


def udp_probe(peer,port,seconds,pps,stop,capture=None):
    """Four sockets; bounded timestamp/seen arrays, no fsync or traces in hot path."""
    if not .05<=seconds<=120 or not 1<=pps<=2000:raise ValueError('Probe bounds')
    count=math.ceil(seconds*pps)
    if count>60000:raise ValueError('Probe packet bound')
    nonce=os.urandom(8);sent_at=array('d',[0])*count;seen=bytearray(count);rtts=array('d')
    payloads=[bytes([s])*1183 for s in range(251)]
    sel=selectors.DefaultSelector();socks=[]
    invalid=duplicates=reordered=sent=returned=send_blocked=0
    highest=[-1]*4;start=last_rx=time.monotonic();send_end=None
    def summarize(aborted=False,error_type=None):
        elapsed=max(0.000001,(send_end or time.monotonic())-start)
        ordered=sorted(rtts)
        def quantile(q):return round(ordered[max(0,math.ceil(len(ordered)*q)-1)],3) if ordered else None
        result=dict(sent=sent,returned_unique=returned,
                    missing_by_deadline=None if aborted else sent-returned,
                    unreturned_at_stop=sent-returned if aborted else None,
                    aborted=aborted,error_type=error_type,
                    invalid=invalid,duplicates=duplicates,per_flow_reordered=reordered,
                    requested_payload_mbps=pps*PAYLOAD_BYTES*8/1e6,
                    actual_offered_payload_mbps=sent*PAYLOAD_BYTES*8/elapsed/1e6,
                    offered_window_s=elapsed,send_would_block=send_blocked,
                    echo_rtt_ms=dict(p50=quantile(.5),p95=quantile(.95),p99=quantile(.99),max=quantile(1)),
                    pass_integrity=not aborted and sent==count and returned==count and invalid==0)
        if capture:capture(result)
        return result
    try:
        for _ in range(4):
            s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);socks.append(s)
            s.connect((peer,port));s.setblocking(False);sel.register(s,selectors.EVENT_READ)
        while True:
            now=time.monotonic()
            if stop.is_set():raise RuntimeError('Safety stop')
            if now-start>seconds+12:raise TimeoutError('UDP phase wall-clock limit')
            if send_end is None and now-last_rx>5:raise TimeoutError('No valid UDP reply for five seconds')
            # Bounded catch-up: send at most eight due packets before receiving.
            for _ in range(8):
                now=time.monotonic()
                if sent>=count or now<start+sent/pps:break
                msg=MAGIC+nonce+struct.pack('!I',sent)+payloads[sent%251]
                try:
                    if socks[sent%4].send(msg)!=PAYLOAD_BYTES:raise RuntimeError('Short datagram')
                except BlockingIOError:
                    send_blocked+=1;break
                sent_at[sent]=now;sent+=1
                if sent==count:send_end=time.monotonic()
            now=time.monotonic()
            if send_end is not None and (returned==sent or now-send_end>=5):break
            due=start+sent/pps if sent<count else now+.01
            for key,_ in sel.select(min(.01,max(0,due-time.monotonic()))):
                flow=socks.index(key.fileobj)
                for _ in range(128):
                    try:msg=key.fileobj.recv(2048)
                    except BlockingIOError:break
                    received=time.monotonic()
                    if len(msg)!=PAYLOAD_BYTES or msg[:13]!=MAGIC+nonce:
                        invalid+=1;continue
                    seq=struct.unpack('!I',msg[13:17])[0]
                    if seq>=sent or seq%4!=flow or msg[17:]!=payloads[seq%251]:
                        invalid+=1;continue
                    last_rx=received
                    if seen[seq]:duplicates+=1;continue
                    seen[seq]=1;returned+=1
                    if seq<highest[flow]:reordered+=1
                    highest[flow]=max(highest[flow],seq)
                    rtts.append((received-sent_at[seq])*1000)
        return summarize()
    except (Exception,KeyboardInterrupt) as exc:
        result=summarize(True,type(exc).__name__)
        stop.set()
        return result
    finally:
        sel.close()
        for s in socks:s.close()


def phase_ok(result):
    probes=[result['udp_local_initiated'],result['udp_peer_initiated']]
    if result['tcp']['errors'] or result['tcp']['completed']<1:return False
    if any(not isinstance(p,dict) or p.get('aborted') for p in probes):return False
    for p in probes:
        if not p['pass_integrity'] or p['duplicates'] or p['per_flow_reordered']:return False
        if p['echo_rtt_ms']['p99'] is None or p['echo_rtt_ms']['p99']>500:return False
        if p['actual_offered_payload_mbps']<p['requested_payload_mbps']*.9:return False
    return True


def phase_gate(result,local_delta,stopped):
    return (not stopped and phase_ok(result) and local_delta.get('drops')==0
            and (result.get('peer_carrier_delta') or {}).get('drops')==0)


class Monitor:
    def __init__(self,live,args,stop):
        self.live,self.args,self.stop=live,args,stop
        self.done=threading.Event();self.lock=threading.Lock();self.samples=[];self.error=None
        self.initial=live.inspect_link(args);self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()
    def loop(self):
        tick=0
        while not self.done.is_set():
            try:
                d=self.live.stats(self.args.instance)
                with self.lock:self.samples.append(dict(monotonic=time.monotonic(),utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),stats=d))
                baseline=self.initial['stats'].get('drop_reasons',{})
                packet_drops=d.get('drop_reasons',{}).get('queue_packets',0)-baseline.get('queue_packets',0)
                if packet_drops>=64 or any(d.get('drop_reasons',{}).get(k,0)>baseline.get(k,0) for k in ('queue_bytes','queue_expired')):
                    raise RuntimeError('Carrier queue hard-stop: >=64 packet drops or byte/expiry drop')
                if d.get('version')!=self.initial['stats'].get('version'):raise RuntimeError('Carrier version changed')
                if d.get('transport_profile')!='split-udp-v1' or d.get('sessions')!=self.initial['stats']['sessions']:
                    raise RuntimeError('Carrier session or profile changed during test')
                if tick%5==0 and self.live.inspect_link(self.args)['main_pid']!=self.initial['main_pid']:
                    raise RuntimeError('Carrier PID changed during test')
            except Exception as e:
                self.error=type(e).__name__+': '+str(e)[:150];self.stop.set();return
            tick+=1;self.done.wait(1)
    def close(self):self.done.set();self.thread.join(12)


def rpc(h,peer,port,obj,timeout=15):
    with socket.create_connection((peer,port),timeout=5) as s:
        s.settimeout(timeout);h.send_json(s,obj);return h.recv_json(s)


def make_handler(h):
    class Handler(h.Handler):
        def handle(self):
            self.request.settimeout(15);o=self.server.owner
            obj=h.recv_json(self.request);op=obj.get('op')
            if op=='ping':
                # Services starts its listener thread during construction; an
                # early probe must not turn that tiny setup window into a
                # permanent handler failure or claim the wrong experiment.
                initial=getattr(o,'initial',None);suite=getattr(o,'suite',None)
                if initial is None or suite is None:
                    h.send_json(self.request,dict(ok=False,suite='initializing'));return
                h.send_json(self.request,dict(ok=True,suite=suite,version=initial['stats'].get('version'),packet_limits={k:v.get('packet_limit') for k,v in initial['stats'].get('lanes',{}).items()},udp_wait_ms=initial['stats'].get('lanes',{}).get('udp',{}).get('queue_wait_ms')));return
            if op=='transfer':
                data=h.read_exact(self.request,h.SIZE)
                if data!=h.PAYLOAD:raise ValueError('Upload integrity failure')
                h.send_json(self.request,dict(bytes=h.SIZE,sha256=h.HASH));o.pacer.send(self.request,h.PAYLOAD)
                with o.lock:o.transfers+=1
                return
            if op=='soak_phase':
                ident=obj.get('id')
                with o.phase_lock:
                    if ident!=o.next_phase or not isinstance(ident,int) or isinstance(ident,bool):raise ValueError('Unexpected phase')
                    o.next_phase+=1
                spec=o.phase_specs[ident-1]
                self.request.settimeout(spec['seconds']+20)
                try:
                    before=o.live.inspect_link(o.args)
                    result=udp_probe(o.peer,o.peer_udp_port,spec['seconds'],spec['pps'],o.stop,capture=lambda r:o.partial_probes.__setitem__(str(ident),r))
                    checkpoint=o.live.inspect_link(o.args)
                    answer=dict(id=ident,udp=result,carrier_checkpoint=checkpoint,carrier_delta=o.live.deltas(before['stats'],checkpoint['stats']))
                except Exception as e:
                    o.stop.set();answer=dict(id=ident,error_type=type(e).__name__,udp=o.partial_probes.get(str(ident)))
                with o.lock:o.phase_results.append(answer)
                h.send_json(self.request,answer);return
            if op=='finish':
                with o.lock:answer=dict(completed_tcp_transfers=o.transfers,handler_errors=o.handler_errors,phases=[dict(id=p['id'],complete='udp' in p) for p in o.phase_results])
                checkpoint=o.live.inspect_link(o.args)
                answer['carrier_counter_deltas']=o.live.deltas(o.initial['stats'],checkpoint['stats'])
                h.send_json(self.request,answer);o.finished.set();return
            raise ValueError('Unsupported soak operation')
    return Handler


def transfer_worker(h,peer,port,pacer,stop,end):
    completed=0;errors=[]
    while time.monotonic()<end and not stop.is_set():
        try:
            with socket.create_connection((peer,port),timeout=5) as s:
                s.settimeout(12);h.send_json(s,dict(op='transfer'));pacer.send(s,h.PAYLOAD)
                meta=h.recv_json(s);body=h.read_exact(s,h.SIZE)
                if meta!=dict(bytes=h.SIZE,sha256=h.HASH) or body!=h.PAYLOAD:raise ValueError('TCP integrity failure')
                completed+=1
        except Exception as e:
            errors.append(type(e).__name__);stop.set();break
    return dict(completed=completed,errors=errors)


def run_phase(h,args,spec,stop,ports,progress=None):
    progress=progress if progress is not None else {}
    tcp,udp=ports;start=time.monotonic();end=start+spec['seconds'];pacer=h.Pacer(mbps=2)
    def captured(result):progress['udp_local_initiated']=result
    errors=[];a=b=None;tcp_results=[]
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        reverse=pool.submit(rpc,h,args.peer,tcp,dict(op='soak_phase',id=spec['id']),spec['seconds']+20)
        local=pool.submit(udp_probe,args.peer,udp,spec['seconds'],spec['pps'],stop,captured)
        workers=[pool.submit(transfer_worker,h,args.peer,tcp,pacer,stop,end) for _ in range(4)]
        try:
            a=local.result(timeout=spec['seconds']+15)
            b=reverse.result(timeout=spec['seconds']+20)
            if not isinstance(b.get('udp'),dict):raise RuntimeError('Peer phase summary unavailable')
        except (Exception,KeyboardInterrupt) as exc:
            stop.set();errors.append(type(exc).__name__)
        finally:
            for worker in workers:
                try:tcp_results.append(worker.result(timeout=15))
                except (Exception,KeyboardInterrupt) as exc:
                    stop.set();errors.append(type(exc).__name__)
    completed=sum(x['completed'] for x in tcp_results)
    progress.update(spec=spec,tcp=dict(completed=completed,errors=errors+[e for x in tcp_results for e in x['errors']],
                    upload_bytes=completed*h.SIZE,download_bytes=completed*h.SIZE),
                    udp_local_initiated=a or progress.get('udp_local_initiated'),
                    udp_peer_initiated=b.get('udp') if b else None,
                    peer_checkpoint=b.get('carrier_checkpoint') if b else None,
                    peer_carrier_delta=b.get('carrier_delta') if b else None,
                    elapsed_s=time.monotonic()-start)
    return dict(progress)



def diagnostic_phases(seconds):
    if not isinstance(seconds,int) or isinstance(seconds,bool) or seconds not in (30,120):raise ValueError('Diagnostic duration must be 30 or 120 seconds')
    return [dict(id=1,seconds=seconds,pps=500)]


def run(args,live=None,helper=None,phases=None,ports=None):
    os.umask(0o077);diagnostic=getattr(args,'queue_diagnostic',False);lean=getattr(args,'lean_observer',False);report=dict(suite=('dv-queue-diagnostic-lean-v1' if lean else 'dv-queue-diagnostic-v1') if diagnostic else 'dv-soak-v2',role=args.role,status='failed',started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
        notes=['Only existing test link; no carrier/service/config/firewall/routes changed.',
               'Four TCP workers, aggregate 2 Mbps payload pacer at each endpoint, overlap both initiating UDP directions.',
               'UDP echo return adds load; requested payload rate is per initiating sender, not total wire traffic.',
               'Python achieved rate is measured; not a line-rate capacity benchmark.',
               'UDP missing is after up to five-second final drain; not proven permanent wire loss.',
               'RTT includes host scheduling; no one-way jitter claim.',
               'Statistics sampled once per second; runtime publishes every two seconds, so samples may repeat and can miss brief transients.',
               'Any carrier drop still fails the phase and blocks escalation; immediate queue hard-stop is >=64 new packet drops or any byte/expiry drop.',
               'Other immediate guards and phase gates remain: host limits, session/PID/profile change, no echo, integrity/reordering, p99>500ms, achieved rate<90%.',
               'Aborted probes use unreturned_at_stop, NOT missing_by_deadline, because the final drain did not complete.',
               'Whole-host CPU85% twice/memory192MiB guards reduce risk, not zero production-impact guarantee.'])
    guard=services=monitor=observer=None;live=live or load_live();h=helper or live.load_helper();duration=getattr(args,'diagnostic_seconds',30);specs=phases or (diagnostic_phases(duration) if diagnostic else PHASES)
    if diagnostic and duration==120:report['suite']+='-120s'
    report['planned_phases']=specs
    args.expect_profile='split-udp-v1'
    try:
        if not diagnostic and duration!=30:raise ValueError('Diagnostic duration requires queue diagnostic')
        report['before']=live.inspect_link(args)
        expected=getattr(args,'expect_version',None)
        if expected and report['before']['stats'].get('version')!=expected:raise RuntimeError('Installed carrier version does not match experiment')
        expected_packets=getattr(args,'expect_lane_packets',None)
        if expected_packets is not None and (not 1<=expected_packets<=128 or any(report['before']['stats'].get('lanes',{}).get(k,{}).get('packet_limit')!=expected_packets for k in ('tcp','udp'))):raise RuntimeError('Local lane packet cap does not match experiment')
        expected_wait=getattr(args,'expect_udp_wait_ms',None)
        if expected_wait is not None and (not 0<=expected_wait<=200 or report['before']['stats'].get('lanes',{}).get('udp',{}).get('queue_wait_ms')!=expected_wait):raise RuntimeError('Local UDP wait does not match experiment')
        baseline=getattr(args,'cpu_baseline_seconds',0)
        if not 0<=baseline<=30 or (baseline and not diagnostic):raise ValueError('CPU baseline needs queue diagnostic and 0..30 seconds')
        if lean and not diagnostic:raise ValueError('Lean observer requires queue diagnostic')
        if diagnostic:observer=QueueDiagnostics(report['before']['main_pid'],args.iface,lean=True) if lean else QueueDiagnostics(report['before']['main_pid'],args.iface)
        guard=h.HostGuard();monitor=Monitor(live,args,guard.stop)
        if observer:observer.load_stop=guard.stop
        if baseline:
            report['cpu_baseline']=dict(start_monotonic=time.monotonic(),requested_seconds=baseline)
            print('CPU_BASELINE — no test load; safety guards remain active',flush=True)
            stopped=guard.stop.wait(baseline)
            report['cpu_baseline']['end_monotonic']=time.monotonic()
            if stopped:raise RuntimeError(monitor.error or guard.reason or 'Baseline safety stop')
        h.Handler=make_handler(h)
        tl,ul,tp,up=ports or (h.TCP_PORT,h.UDP_PORT,h.TCP_PORT,h.UDP_PORT)
        services=h.Services(args.local,args.peer,tcp_port=tl,udp_port=ul,peer_udp_port=up,allow_reconnect=False)
        services.stop=guard.stop;services.live=live;services.args=args;services.initial=report['before'];services.suite=report['suite']
        services.phase_lock=threading.Lock();services.next_phase=1;services.phase_specs=specs;services.phase_results=[];services.partial_probes={}
        report['phases']=[]
        if args.role=='server':
            print('SOAK_SERVER_READY — now run Iran; leave this terminal open',flush=True)
            deadline=time.monotonic()+420
            while not services.finished.wait(.2):
                if guard.stop.is_set():raise RuntimeError(monitor.error or guard.reason or 'Test safety stop')
                if services.handler_errors:raise RuntimeError('Test application handler error')
                if time.monotonic()>deadline:raise TimeoutError('Server suite wall-clock limit')
            with services.lock:report['phases']=list(services.phase_results)
            report['status']='completed';report['tcp_transfers_served']=services.transfers
        else:
            greeting=rpc(h,args.peer,tp,dict(op='ping'))
            if greeting.get('suite')!=report['suite']:raise RuntimeError('Both endpoints must run the same checker mode')
            if expected and greeting.get('version')!=expected:raise RuntimeError('Peer carrier version does not match experiment')
            if expected_packets is not None and any(greeting.get('packet_limits',{}).get(k)!=expected_packets for k in ('tcp','udp')):raise RuntimeError('Peer lane packet cap does not match experiment')
            if expected_wait is not None and greeting.get('udp_wait_ms')!=expected_wait:raise RuntimeError('Peer UDP wait does not match experiment')
            for spec in specs:
                print('PHASE',spec['id'],'seconds',spec['seconds'],'UDP Mbps per initiator',spec['pps']*.0096,flush=True)
                before=live.inspect_link(args)
                report['current_phase']=dict(spec=spec)
                result=run_phase(h,args,spec,guard.stop,(tp,up),report['current_phase'])
                report['phases'].append(result)
                time.sleep(2.2);after=live.inspect_link(args)
                result['carrier_delta']=live.deltas(before['stats'],after['stats'])
                passed=phase_gate(result,result['carrier_delta'],guard.stop.is_set())
                result['gate_passed']=passed
                print('PHASE_RESULT',spec['id'],'PASS' if passed else 'STOP',
                      'UDP missing:',(result.get('udp_local_initiated') or {}).get('missing_by_deadline'),(result.get('udp_peer_initiated') or {}).get('missing_by_deadline'),flush=True)
                if not passed:break
            report['peer_finish']=rpc(h,args.peer,tp,dict(op='finish'))
            report['status']='passed' if len(report['phases'])==len(specs) and all(p['gate_passed'] for p in report['phases']) else 'stopped_at_quality_gate'
        time.sleep(2.2)
        report['after']=live.inspect_link(args)
        report['carrier_counter_deltas']=live.deltas(report['before']['stats'],report['after']['stats'])
        report['carrier_main_pid_unchanged']=report['after']['main_pid']==report['before']['main_pid']
        if not report['carrier_main_pid_unchanged'] or monitor.error:raise RuntimeError(monitor.error or 'Carrier PID changed')
        if guard.stop.is_set():raise RuntimeError(guard.reason or 'Test safety stop')
        if args.role=='client' and (report['carrier_counter_deltas']['drops'] or report['peer_finish']['carrier_counter_deltas']['drops']):
            report['status']='stopped_at_quality_gate'
    except (Exception,KeyboardInterrupt) as e:
        report['status']='failed';report['error_type']=type(e).__name__
        if isinstance(e,(ValueError,RuntimeError,TimeoutError)):report['error']=str(e)[:200]
    finally:
        if monitor:
            try:
                report['after']=live.inspect_link(args)
                report['carrier_counter_deltas']=live.deltas(report['before']['stats'],report['after']['stats'])
                report['carrier_main_pid_unchanged']=report['after']['main_pid']==report['before']['main_pid']
            except Exception as exc:report['after_error_type']=type(exc).__name__
            monitor.close();report['samples']=monitor.samples;report['monitor_error']=monitor.error
        if services:
            services.close();report['test_socket_cleanup']='OK'
            with services.lock:
                report['served_tcp_transfers']=services.transfers
                report['handler_errors']=services.handler_errors
                report['server_phase_results']=list(services.phase_results)
                report['udp_probe_summaries']=dict(services.partial_probes)
        if guard:
            guard.close();report['host']=dict(cpu_max_sample_pct=guard.cpu_max,mem_available_min_sample_mib=guard.mem_min,stop_reason=guard.reason)
        if observer:
            report['queue_diagnostics']=observer.close()
            if report['queue_diagnostics'].get('safety_stop_reason'):
                report['status']='failed';report.setdefault('error',report['queue_diagnostics']['safety_stop_reason'])
        report['finished_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        live.write_report(args.output,args.role,report)
    return 0 if report['status'] in ('passed','completed') else 1


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('role',choices=['server','client'])
    ap.add_argument('--queue-diagnostic',action='store_true',help='One bounded 4.8Mbps phase with read-only carrier-owned TCP and TUN counters; no tuning or carrier changes');ap.add_argument('--expect-version');ap.add_argument('--expect-lane-packets',type=int);ap.add_argument('--expect-udp-wait-ms',type=int);ap.add_argument('--instance',default='main');ap.add_argument('--iface',default='wft0')
    ap.add_argument('--diagnostic-seconds',type=int,choices=(30,120),default=30,help='Queue diagnostic duration only; fixed UDP rate and unchanged safety gates')
    ap.add_argument('--lean-observer',action='store_true',help='Queue diagnostic only: verify sockets once, keep CPU/TUN and safety polling; no periodic ss process scans')
    ap.add_argument('--cpu-baseline-seconds',type=int,default=0,help='0..30 seconds without test load before services; queue diagnostic only; safety guards unchanged')
    ap.add_argument('--local');ap.add_argument('--peer');ap.add_argument('--output')
    args=ap.parse_args();args.output=args.output or ('/root/dv-queue-diag-results' if args.queue_diagnostic else '/root/dv-soak-results');args.local=args.local or ('10.254.253.2' if args.role=='server' else '10.254.253.1');args.peer=args.peer or ('10.254.253.1' if args.role=='server' else '10.254.253.2')
    signal.signal(signal.SIGTERM,lambda s,f:(_ for _ in ()).throw(KeyboardInterrupt()))
    return run(args)


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as e:
        print('SOAK_STARTUP_FAILED:',type(e).__name__,'— no carrier changes made',flush=True)
        raise SystemExit(1)
