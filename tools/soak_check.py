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


def packet(nonce,seq):
    return MAGIC+nonce+struct.pack('!I',seq)+bytes([seq%251])*1183


def udp_probe(peer,port,seconds,pps,stop):
    """Four sockets; bounded timestamp/seen arrays, no fsync or traces in hot path."""
    if not .05<=seconds<=120 or not 1<=pps<=2000:raise ValueError('Probe bounds')
    count=math.ceil(seconds*pps)
    if count>60000:raise ValueError('Probe packet bound')
    nonce=os.urandom(8);sent_at=array('d',[0])*count;seen=bytearray(count);rtts=array('d')
    payloads=[bytes([s])*1183 for s in range(251)]
    sel=selectors.DefaultSelector();socks=[]
    invalid=duplicates=reordered=sent=returned=send_blocked=0
    highest=[-1]*4;start=last_rx=time.monotonic();send_end=None
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
        elapsed=send_end-start
        ordered=sorted(rtts)
        def quantile(q):return round(ordered[max(0,math.ceil(len(ordered)*q)-1)],3) if ordered else None
        return dict(sent=sent,returned_unique=returned,missing_by_deadline=sent-returned,
                    invalid=invalid,duplicates=duplicates,per_flow_reordered=reordered,
                    requested_payload_mbps=pps*PAYLOAD_BYTES*8/1e6,
                    actual_offered_payload_mbps=sent*PAYLOAD_BYTES*8/elapsed/1e6,
                    offered_window_s=elapsed,send_would_block=send_blocked,
                    echo_rtt_ms=dict(p50=quantile(.5),p95=quantile(.95),p99=quantile(.99),max=quantile(1)),
                    pass_integrity=sent==count and returned==count and invalid==0)
    finally:
        sel.close()
        for s in socks:s.close()


def phase_ok(result):
    probes=[result['udp_local_initiated'],result['udp_peer_initiated']]
    if result['tcp']['errors'] or result['tcp']['completed']<1:return False
    for p in probes:
        if not p['pass_integrity'] or p['duplicates'] or p['per_flow_reordered']:return False
        if p['echo_rtt_ms']['p99'] is None or p['echo_rtt_ms']['p99']>500:return False
        if p['actual_offered_payload_mbps']<p['requested_payload_mbps']*.9:return False
    return True


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
                if any(d.get('drop_reasons',{}).get(k,0)>baseline.get(k,0) for k in ('queue_packets','queue_bytes','queue_expired')):
                    raise RuntimeError('Carrier queue drop increased')
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
            if op=='ping':h.send_json(self.request,dict(ok=True,suite='dv-soak-v1'));return
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
                    result=udp_probe(o.peer,o.peer_udp_port,spec['seconds'],spec['pps'],o.stop)
                    checkpoint=o.live.inspect_link(o.args)
                    answer=dict(id=ident,udp=result,carrier_checkpoint=checkpoint,carrier_delta=o.live.deltas(before['stats'],checkpoint['stats']))
                except Exception as e:
                    o.stop.set();answer=dict(id=ident,error_type=type(e).__name__)
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


def run_phase(h,args,spec,stop,ports):
    tcp,udp=ports;start=time.monotonic();end=start+spec['seconds'];pacer=h.Pacer(mbps=2)
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        reverse=pool.submit(rpc,h,args.peer,tcp,dict(op='soak_phase',id=spec['id']),spec['seconds']+20)
        local=pool.submit(udp_probe,args.peer,udp,spec['seconds'],spec['pps'],stop)
        workers=[pool.submit(transfer_worker,h,args.peer,tcp,pacer,stop,end) for _ in range(4)]
        try:
            a=local.result(timeout=spec['seconds']+15);b=reverse.result(timeout=spec['seconds']+20)
            if 'udp' not in b:raise RuntimeError('Peer phase failed')
        except BaseException:stop.set();raise
        tcp_results=[x.result(timeout=15) for x in workers]
    completed=sum(x['completed'] for x in tcp_results)
    return dict(spec=spec,tcp=dict(completed=completed,errors=[e for x in tcp_results for e in x['errors']],
                upload_bytes=completed*h.SIZE,download_bytes=completed*h.SIZE),
                udp_local_initiated=a,udp_peer_initiated=b['udp'],peer_checkpoint=b['carrier_checkpoint'],peer_carrier_delta=b['carrier_delta'],elapsed_s=time.monotonic()-start)


def run(args,live=None,helper=None,phases=None,ports=None):
    os.umask(0o077);report=dict(suite='dv-soak-v1',role=args.role,status='failed',started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
        notes=['Only existing test link; no carrier/service/config/firewall/routes changed.',
               'Four TCP workers, aggregate 2 Mbps payload pacer at each endpoint, overlap both initiating UDP directions.',
               'UDP echo return adds load; requested payload rate is per initiating sender, not total wire traffic.',
               'Python achieved rate is measured; not a line-rate capacity benchmark.',
               'UDP missing is after up to five-second final drain; not proven permanent wire loss.',
               'RTT includes host scheduling; no one-way jitter claim.',
               'Statistics sampled once per second; runtime publishes every two seconds, so samples may repeat and can miss brief transients.',
               'Escalation stops for delivery/integrity/reordering, p99>500ms, achieved rate<90%, carrier queue drops, session/PID change or host guard.',
               'Whole-host CPU85% twice/memory192MiB guards reduce risk, not zero production-impact guarantee.'])
    guard=services=monitor=None;live=live or load_live();h=helper or live.load_helper();specs=phases or PHASES
    args.expect_profile='split-udp-v1'
    try:
        report['before']=live.inspect_link(args);guard=h.HostGuard();monitor=Monitor(live,args,guard.stop)
        h.Handler=make_handler(h)
        tl,ul,tp,up=ports or (h.TCP_PORT,h.UDP_PORT,h.TCP_PORT,h.UDP_PORT)
        services=h.Services(args.local,args.peer,tcp_port=tl,udp_port=ul,peer_udp_port=up,allow_reconnect=False)
        services.stop=guard.stop;services.live=live;services.args=args;services.initial=report['before']
        services.phase_lock=threading.Lock();services.next_phase=1;services.phase_specs=specs;services.phase_results=[]
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
            rpc(h,args.peer,tp,dict(op='ping'))
            for spec in specs:
                print('PHASE',spec['id'],'seconds',spec['seconds'],'UDP Mbps per initiator',spec['pps']*.0096,flush=True)
                before=live.inspect_link(args)
                result=run_phase(h,args,spec,guard.stop,(tp,up))
                time.sleep(2.2);after=live.inspect_link(args)
                result['carrier_delta']=live.deltas(before['stats'],after['stats'])
                passed=phase_ok(result) and result['carrier_delta']['drops']==0 and result['peer_carrier_delta']['drops']==0 and not guard.stop.is_set()
                result['gate_passed']=passed;report['phases'].append(result)
                print('PHASE_RESULT',spec['id'],'PASS' if passed else 'STOP',
                      'UDP missing:',result['udp_local_initiated']['missing_by_deadline'],result['udp_peer_initiated']['missing_by_deadline'],flush=True)
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
            monitor.close();report['samples']=monitor.samples;report['monitor_error']=monitor.error
        if services:services.close();report['test_socket_cleanup']='OK'
        if guard:
            guard.close();report['host']=dict(cpu_max_sample_pct=guard.cpu_max,mem_available_min_sample_mib=guard.mem_min,stop_reason=guard.reason)
        report['finished_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        live.write_report(args.output,args.role,report)
    return 0 if report['status'] in ('passed','completed') else 1


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('role',choices=['server','client'])
    ap.add_argument('--instance',default='main');ap.add_argument('--iface',default='wft0')
    ap.add_argument('--local');ap.add_argument('--peer');ap.add_argument('--output',default='/root/dv-soak-results')
    args=ap.parse_args();args.local=args.local or ('10.254.253.2' if args.role=='server' else '10.254.253.1');args.peer=args.peer or ('10.254.253.1' if args.role=='server' else '10.254.253.2')
    signal.signal(signal.SIGTERM,lambda s,f:(_ for _ in ()).throw(KeyboardInterrupt()))
    return run(args)


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as e:
        print('SOAK_STARTUP_FAILED:',type(e).__name__,'— no carrier changes made',flush=True)
        raise SystemExit(1)
