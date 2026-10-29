#!/usr/bin/env python3
"""Functional mixed TCP/UDP check of an ALREADY RUNNING managed TUN link."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import time
import tempfile
import types
import urllib.request
import zipfile

HELPER_COMMIT='d5709c0a731d3a3c351b505999e6abebe986b9f5'
HELPER_SHA256='c686c9ae1b87250bcf7fedc6c639da450b51e7d7c1d8e14fa4eeb5d6b9fcc6b4'
HELPER_URL='https://raw.githubusercontent.com/dalmaktube-creator/dv-tunnel/'+HELPER_COMMIT+'/tools/acceptance.py'
STATE=Path('/var/lib/dv-tunnel')
VERSION='dv-live-check-v1'


def load_helper():
    with urllib.request.urlopen(HELPER_URL,timeout=25) as r:data=r.read(131073)
    if len(data)>131072 or hashlib.sha256(data).hexdigest()!=HELPER_SHA256:
        raise RuntimeError('Pinned functional helper hash mismatch')
    return helper_from_bytes(data)


def helper_from_bytes(data):
    if hashlib.sha256(data).hexdigest()!=HELPER_SHA256:
        raise RuntimeError('Pinned functional helper hash mismatch')
    module=types.ModuleType('dv_live_helper')
    module.__file__='pinned-acceptance.py'
    exec(compile(data,HELPER_URL,'exec'),module.__dict__)
    return module


def read_command(argv):
    # Read-only inspection only. This module never manages a service or link.
    allowed=(argv[:2] in (['systemctl','show'],['systemctl','is-active']) or
             argv[:5] in (['ip','-j','-4','addr','show'],['ip','-j','-4','route','get']))
    if not allowed:raise ValueError('Unexpected inspection command')
    return subprocess.run(argv,check=True,capture_output=True,text=True,timeout=10).stdout.strip()


def stats(name):
    p=STATE/name/'stats.json'
    if p.is_symlink() or not 0<=time.time()-p.stat().st_mtime<6:
        raise RuntimeError('Managed statistics missing or stale')
    d=json.loads(p.read_text())
    if d.get('connected')!=1:raise RuntimeError('Managed link is not connected')
    return {k:d[k] for k in ('version','connected','sessions','tx_datagrams','rx_datagrams','drops','spoofed_drops','drop_reasons') if k in d}


def inspect_link(args):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,30}',args.instance):raise ValueError('Invalid instance name')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,15}',args.iface):raise ValueError('Invalid interface name')
    local=ipaddress.IPv4Address(args.local);peer=ipaddress.IPv4Address(args.peer)
    net=ipaddress.ip_network(str(local)+'/30',strict=False)
    if not any(net.subnet_of(ipaddress.ip_network(n)) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')) or local==peer or local not in net.hosts() or peer not in net.hosts():
        raise ValueError('Use the two usable RFC1918 /30 addresses')
    unit='dv-tunnel@'+args.instance+'.service'
    if read_command(['systemctl','is-active',unit])!='active':raise RuntimeError('Managed service is not active')
    pid=int(read_command(['systemctl','show','--property=MainPID','--value',unit]))
    if pid<=0:raise RuntimeError('Managed service has no live PID')
    rows=json.loads(read_command(['ip','-j','-4','addr','show','dev',args.iface]))
    if not any(a.get('local')==args.local and a.get('prefixlen')==30 for row in rows for a in row.get('addr_info',[])):
        raise RuntimeError('Local /30 is not assigned to the selected interface')
    route=json.loads(read_command(['ip','-j','-4','route','get',args.peer]))
    if not route or any(r.get('dev')!=args.iface for r in route):raise RuntimeError('Peer route does not use the selected TUN')
    return dict(main_pid=pid,stats=stats(args.instance))


def deltas(before,after):
    result={}
    for k in ('sessions','tx_datagrams','rx_datagrams','drops','spoofed_drops'):
        if isinstance(before.get(k),int) and isinstance(after.get(k),int):
            result[k]=after[k]-before[k] if after[k]>=before[k] else None
    keys=set(before.get('drop_reasons',{}))|set(after.get('drop_reasons',{}))
    result['drop_reasons']={}
    for k in sorted(keys):
        a=after.get('drop_reasons',{}).get(k,0);b=before.get('drop_reasons',{}).get(k,0)
        result['drop_reasons'][k]=a-b if a>=b else None
    return result


def verdict(mixed,peer):
    return (len(mixed.get('tcp',[]))==4 and all(x.get('pass_integrity') for x in mixed['tcp']) and
            mixed.get('udp_local_initiated',{}).get('pass_integrity') is True and
            mixed.get('udp_peer_initiated',{}).get('pass_integrity') is True and
            peer.get('application',{}).get('handler_errors',1)==0 and
            peer.get('application',{}).get('completed_tcp_transfers',0)==4 and
            peer.get('carrier_main_pid_unchanged') is True)


def write_report(output,role,report):
    output=Path(output)
    if output.is_symlink():raise RuntimeError('Refusing symlink output directory')
    output.mkdir(parents=True,exist_ok=True,mode=0o700)
    if output.stat().st_uid!=os.geteuid() or output.stat().st_mode&0o022:
        raise RuntimeError('Output directory must be owned by the runner and not group/world writable')
    text=json.dumps(report,indent=2)+'\n'
    # Exclusive per-run folder prevents clobbering user files or following links.
    folder=output/(role+'-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+os.urandom(3).hex())
    folder.mkdir(mode=0o700)
    (folder/'report.json').write_text(text)
    with zipfile.ZipFile(folder/'report.zip','w',zipfile.ZIP_DEFLATED) as z:z.writestr('report.json',text)
    for name,source in (('latest-'+role+'.zip',folder/'report.zip'),('latest-'+role+'.json',folder/'report.json')):
        dest=output/name
        if dest.is_symlink():raise RuntimeError('Refusing symlink report alias')
        fd,tmp=tempfile.mkstemp(prefix='.report-',dir=output)
        try:
            with os.fdopen(fd,'wb') as f:
                f.write(source.read_bytes());f.flush();os.fsync(f.fileno())
            os.replace(tmp,dest)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
    print('STATUS:',report.get('status'),flush=True)
    if report.get('error'):print('ERROR:',report['error'],flush=True)
    if report.get('mixed'):
        print('TCP_INTEGRITY:',sum(bool(x.get('pass_integrity')) for x in report['mixed']['tcp']),'/ 4',flush=True)
        for label,key in (('LOCAL_UDP','udp_local_initiated'),('PEER_UDP','udp_peer_initiated')):
            r=report['mixed'][key]
            print(label,'sent:',r['sent'],'returned:',r['returned_unique'],'missing:',r['missing_by_deadline'],flush=True)
    print('OWN_TEST_SOCKET_CLEANUP:',report.get('test_socket_cleanup','NOT_STARTED'),flush=True)
    print('CARRIER_MAIN_PID_UNCHANGED:',report.get('carrier_main_pid_unchanged','NOT_CHECKED'),flush=True)
    print('REPORT_ZIP:',output/('latest-'+role+'.zip'),flush=True)


def run(args,helper=None,ports=None):
    os.umask(0o077)
    report=dict(suite=VERSION,role=args.role,status='failed',
                started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                helper_commit=HELPER_COMMIT,kernel=platform.release(),architecture=platform.machine(),
                scope='existing point-to-point TUN; not forwarded clients, NAT, panel integration or capacity acceptance',
                notes=['No service, carrier, link, route, firewall or private configuration is modified by this checker.',
                       'Requested UDP payload is about 1.92 Mbps per initiating endpoint; echoes and paced TCP add to total link load.',
                       'Python functional pacing is not a capacity benchmark or precise latency measurement.',
                       'UDP missing means no reply by the five-second final drain, not proven permanent wire loss.',
                       'Counter snapshots include all carrier traffic and are not frozen transactions or per-sequence attribution.',
                       'Whole-host CPU/memory guards reduce risk but cannot guarantee production is unaffected.'])
    services=guard=None
    try:
        before=inspect_link(args);report['before']=before
        h=helper or load_helper()
        guard=h.HostGuard()
        tcp_local,udp_local,tcp_peer,udp_peer=ports or (h.TCP_PORT,h.UDP_PORT,h.TCP_PORT,h.UDP_PORT)
        services=h.Services(args.local,args.peer,tcp_port=tcp_local,udp_port=udp_local,peer_udp_port=udp_peer,allow_reconnect=False)
        # Both endpoint UDP loops and the reverse sender share the host guard.
        services.stop=guard.stop
        if args.role=='server':
            original_snapshot=services.snapshot
            def snapshot():
                app=original_snapshot();app.pop('server_cleanup',None)
                checkpoint=inspect_link(args)
                return dict(application=app,carrier_checkpoint=checkpoint,carrier_main_pid_unchanged=checkpoint['main_pid']==before['main_pid'],test_socket_cleanup='pending')
            services.snapshot=snapshot
            print('SERVER_READY — now run the Iran block; existing tunnel stays running',flush=True)
            deadline=time.monotonic()+120
            while not services.finished.is_set():
                if guard.stop.is_set():raise RuntimeError('Host safety guard')
                if services.handler_errors:raise RuntimeError('Test application handler failed')
                if time.monotonic()>deadline:raise TimeoutError('Server test wall-clock limit')
                time.sleep(.1)
            report['application']=original_snapshot();report['application'].pop('server_cleanup',None)
            report['status']='completed'
        else:
            print('MIXED_TCP_UDP_RUNNING — about 15 seconds; do not stop the tunnel',flush=True)
            report['mixed']=h.mixed(args.peer,guard.stop,tcp_port=tcp_peer,udp_port=udp_peer)
            time.sleep(2.2)
            report['peer']=h.rpc(args.peer,'finish',port=tcp_peer)
            report['status']='passed' if verdict(report['mixed'],report['peer']) else 'quality_or_integrity_failure'
            if report['peer']['carrier_checkpoint']['main_pid']<=0:raise RuntimeError('Peer runtime not observed')
        time.sleep(2.2)
        after=inspect_link(args);report['after']=after
        report['carrier_main_pid_unchanged']=after['main_pid']==before['main_pid']
        report['carrier_counter_deltas']=deltas(before['stats'],after['stats'])
        if not report['carrier_main_pid_unchanged']:report['status']='runtime_changed_during_test'
        if guard.stop.is_set():raise RuntimeError('Host safety guard')
    except (Exception,KeyboardInterrupt) as e:
        report['status']='failed';report['error_type']=type(e).__name__
        if isinstance(e,(ValueError,RuntimeError,TimeoutError)):
            report['error']=''.join(c for c in str(e) if 32<=ord(c)<=126)[:200]
    finally:
        if services:
            services.close();report['test_socket_cleanup']='OK'
        if guard:
            guard.close();report['host']=dict(cpu_max_sample_pct=round(guard.cpu_max,2),mem_available_min_sample_mib=guard.mem_min,stop_reason=guard.reason)
        report['finished_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        write_report(args.output,args.role,report)
    return 0 if report['status'] in ('passed','completed') else 1


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('role',choices=['server','client'])
    ap.add_argument('--instance',default='main');ap.add_argument('--iface',default='wft0')
    ap.add_argument('--local');ap.add_argument('--peer')
    ap.add_argument('--output',default='/root/dv-live-check-results')
    args=ap.parse_args()
    args.local=args.local or ('10.254.253.2' if args.role=='server' else '10.254.253.1')
    args.peer=args.peer or ('10.254.253.1' if args.role=='server' else '10.254.253.2')
    def interrupted(signum,frame):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupted)
    return run(args)


if __name__=='__main__':raise SystemExit(main())
