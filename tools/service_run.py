#!/usr/bin/env python3
"""Run one explicitly imported TUN instance, with no NAT/firewall/default routes."""
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time


SPLIT_UDP_SUPPORT = 1
QUEUE_OVERRIDE_SUPPORT = 1

def main():
    here=Path(__file__).resolve().parent
    spec=importlib.util.spec_from_file_location('dvctl',here/'dvctl.py')
    manager=importlib.util.module_from_spec(spec);spec.loader.exec_module(manager)
    generator_spec=importlib.util.spec_from_file_location('dv_pair',here/'dv_pair.py')
    generator=importlib.util.module_from_spec(generator_spec);generator_spec.loader.exec_module(generator)
    if len(sys.argv)!=2:raise manager.SafeError('Expected instance name')
    name=manager.instance_name(sys.argv[1])
    config_path=manager.ETC/name/'config.json'
    link_path=manager.ETC/name/'link.json'
    for p in (config_path,link_path):
        if p.is_symlink() or p.stat().st_uid!=0 or p.stat().st_mode & 0o077:
            raise manager.SafeError('Instance files must be root-owned 0600, not symlinks')
    cfg=json.loads(config_path.read_text());link=json.loads(link_path.read_text())
    addr=ipaddress.ip_interface(link['local']);peer=ipaddress.ip_address(link['peer'])
    if link.get('schema')!=1 or addr.version!=4 or addr.network.prefixlen!=30 or peer not in addr.network or addr.ip not in list(addr.network.hosts()) or peer not in list(addr.network.hosts()) or addr.ip==peer:
        raise manager.SafeError('Invalid local link metadata')
    iface=cfg.get('tun','')
    if not manager.re.fullmatch(r'[a-zA-Z0-9_-]{1,15}',iface) or cfg.get('allowed_sources')!=[str(peer)+'/32']:
        raise manager.SafeError('Invalid TUN or peer source allowlist')
    if cfg.get('stats_file')!=str(manager.STATE/name/'stats.json'):
        raise manager.SafeError('Statistics must remain inside managed instance state')
    binary=here/'dv-tunnel'
    extra=manager.split_args(here,link)
    manager.run([str(binary),'--config',str(config_path),'--check']+extra)
    if subprocess.run(['ip','link','show','dev',iface],capture_output=True).returncode==0:
        raise manager.SafeError('Interface already exists; refusing to reuse or delete it')
    port=0
    if cfg['mode']=='listen':
        host,_,p=cfg['listen'].rpartition(':');port=int(p)
        # The generator's conservative IPv4 port probe binds 0.0.0.0.
        socket.inet_aton(host)
    manager.run(['python3','-c',generator.PREFLIGHT,str(addr.network),cfg['mode'],str(port)])
    proc=None
    def interrupted(signum,frame):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupted)
    try:
        proc=subprocess.Popen([str(binary),'--config',str(config_path)]+extra)
        ready=False
        for _ in range(100):
            if proc.poll() is not None:raise manager.SafeError('Owned carrier exited before TUN setup')
            if subprocess.run(['ip','link','show','dev',iface],capture_output=True).returncode==0:
                ready=True;break
            time.sleep(.1)
        if not ready:raise manager.SafeError('TUN creation timeout')
        manager.run(['ip','address','add',str(addr),'dev',iface])
        manager.run(['ip','link','set','dev',iface,'mtu','1336','up'])
        print('DV managed instance ready; check fresh connected stats, not only systemd active',flush=True)
        return proc.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        # This Popen object, not a process-name scan or interface deletion,
        # establishes ownership. Closing its nonpersistent TUN removes the link.
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=3)


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as e:
        print('DV instance stopped:',type(e).__name__,'(inspect private configuration locally)',file=sys.stderr)
        raise SystemExit(1)
