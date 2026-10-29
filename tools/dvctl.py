#!/usr/bin/env python3
"""DV Tunnel Ubuntu manager: explicit install, versioned updates and rollback."""
import argparse
import contextlib
import fcntl
import hashlib
import gzip
import io
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

REPO = 'dalmaktube-creator/dv-tunnel'
ROOT = Path('/opt/dv-tunnel')
ETC = Path('/etc/dv-tunnel/instances')
STATE = Path('/var/lib/dv-tunnel')
WRAPPER = Path('/usr/local/bin/dv-tunnel')
UNIT = Path('/etc/systemd/system/dv-tunnel@.service')
MARKER = '# Managed by DV Tunnel\n'
MEMBERS = {'dv-tunnel','dvctl.py','service_run.py','dv_pair.py','VERSION','LICENSE','THIRD_PARTY_NOTICES.md'}
VERSION_RE = re.compile(r'^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(preview|alpha|beta|rc)\.(0|[1-9]\d*))?$')
WRAPPER_TEXT = '#!/bin/sh\n'+MARKER+'exec /usr/bin/python3 /opt/dv-tunnel/current/dvctl.py "$@"\n'
UNIT_TEXT = MARKER+'''[Unit]
Description=DV Tunnel instance %i
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/dv-tunnel/current/service_run.py %i
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
KillMode=control-group
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=CAP_NET_ADMIN
ProtectHome=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/dv-tunnel
ReadOnlyPaths=/etc/dv-tunnel /opt/dv-tunnel
RestrictAddressFamilies=AF_INET AF_INET6 AF_NETLINK AF_UNIX
LimitNOFILE=512
MemoryMax=192M
TasksMax=64

[Install]
WantedBy=multi-user.target
'''


class SafeError(Exception):
    pass


def version_key(v):
    m = VERSION_RE.fullmatch(v)
    if not m:
        raise SafeError('Unsupported version: expected vMAJOR.MINOR.PATCH[-preview.N]')
    a,b,c,pre,n = m.groups()
    order = {'alpha':0,'preview':1,'beta':2,'rc':3,None:4}
    return int(a),int(b),int(c),order[pre],int(n or 0)


def instance_name(v):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,30}',v):
        raise SafeError('Instance name must be 1..31 lowercase letters/digits/hyphens, beginning with a letter')
    return v


def arch():
    a = {'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine())
    if platform.system() != 'Linux' or not a:
        raise SafeError('Only Linux amd64/arm64 are supported')
    return a


def run(argv, timeout=20):
    p = subprocess.run(argv,capture_output=True,text=True,timeout=timeout)
    if p.returncode:
        raise SafeError('Command failed: '+Path(argv[0]).name+' (inspect local service status; no configs printed)')
    return p.stdout.strip()


def secure_dir(path, mode=0o700):
    if path.is_symlink():
        raise SafeError('Refusing symlink directory: '+str(path))
    path.mkdir(parents=True,exist_ok=True,mode=mode)
    s = path.stat()
    if s.st_uid != os.geteuid() or s.st_mode & 0o022:
        raise SafeError('Directory must be owned by root and not group/world writable: '+str(path))


def atomic_text(path,text,mode=0o600):
    if path.is_symlink():
        raise SafeError('Refusing symlink output: '+str(path))
    fd,tmp = tempfile.mkstemp(prefix='.dv-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            os.fchmod(f.fileno(),mode)
            f.write(text)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


@contextlib.contextmanager
def lock():
    secure_dir(ROOT,0o755)
    flags = os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW
    fd = os.open(ROOT/'.manager.lock',flags,0o600)
    with os.fdopen(fd,'w') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SafeError('Another DV Tunnel change is in progress')
        yield


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        u = urllib.parse.urlsplit(newurl)
        hosts = ('github.com','api.github.com','release-assets.githubusercontent.com','objects.githubusercontent.com')
        if u.scheme != 'https' or u.hostname not in hosts:
            raise SafeError('Refusing untrusted release redirect')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def download(url,limit):
    u = urllib.parse.urlsplit(url)
    if u.scheme != 'https' or u.hostname not in ('api.github.com','github.com'):
        raise SafeError('Unexpected download origin')
    req = urllib.request.Request(url,headers={'User-Agent':'DV-Tunnel-Updater/1','Accept':'application/vnd.github+json' if u.hostname=='api.github.com' else 'application/octet-stream'})
    opener = urllib.request.build_opener(HTTPSRedirect())
    with opener.open(req,timeout=60) as r:
        data = r.read(limit+1)
    if len(data)>limit:raise SafeError('Release download exceeded size limit')
    return data


def latest_version():
    releases = json.loads(download('https://api.github.com/repos/'+REPO+'/releases?per_page=30',1024*1024))
    choices = [r['tag_name'] for r in releases if not r.get('draft') and VERSION_RE.fullmatch(r.get('tag_name',''))]
    if not choices:raise SafeError('No published DV Tunnel release yet; preview releases are included')
    return max(choices,key=version_key)


def checksum(manifest,filename):
    matches = []
    for line in manifest.decode('ascii').splitlines():
        m = re.fullmatch(r'([a-fA-F0-9]{64})  ([A-Za-z0-9._-]+)',line)
        if not m:raise SafeError('Invalid SHA256SUMS format')
        if m[2]==filename:matches.append(m[1].lower())
    if len(matches)!=1:raise SafeError('Missing or duplicate release checksum')
    return matches[0]


def unpack(archive,dest,version):
    with gzip.open(archive,'rb') as compressed:
        raw=compressed.read(34*1024*1024+1)
    if len(raw)>34*1024*1024:raise SafeError('Decompressed release exceeded size limit')
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as tar:
        rows=tar.getmembers()
        if len(rows)!=len(MEMBERS) or {m.name for m in rows}!=MEMBERS:
            raise SafeError('Release archive must contain exactly the expected flat files')
        if any(not m.isfile() or m.size<0 or m.size>24*1024*1024 for m in rows) or sum(m.size for m in rows)>32*1024*1024:
            raise SafeError('Unsafe release member type or size')
        for member in rows:
            with tar.extractfile(member) as src:
                data=src.read(member.size+1)
            if len(data)!=member.size:raise SafeError('Truncated archive member')
            target=dest/member.name
            with target.open('xb') as f:f.write(data)
            target.chmod(0o755 if member.name=='dv-tunnel' else 0o644)
    if (dest/'VERSION').read_text().strip()!=version:raise SafeError('Release VERSION mismatch')
    for name in ('dvctl.py','service_run.py','dv_pair.py'):
        compile((dest/name).read_text(),name,'exec')
    if run([str(dest/'dv-tunnel'),'--version'])!=version:
        raise SafeError('Binary version mismatch')


def pointer(which='current'):
    p=ROOT/which
    if not p.exists() and not p.is_symlink():return None
    if not p.is_symlink():raise SafeError('Expected managed version symlink: '+str(p))
    target=p.resolve(strict=True)
    releases=(ROOT/'releases').resolve()
    if target.parent!=releases or not target.is_dir():raise SafeError('Version pointer escapes managed releases')
    version_key(target.name)
    return target


def set_pointer(which,target):
    p=ROOT/which
    if p.exists() and not p.is_symlink():raise SafeError('Refusing unmanaged pointer')
    tmp=ROOT/('.'+which+'-'+str(os.getpid()))
    try:
        tmp.symlink_to(Path('releases')/target.name,target_is_directory=True)
        os.replace(tmp,p)
    finally:
        if tmp.is_symlink():tmp.unlink()


def instances():
    if not ETC.exists():return []
    if ETC.is_symlink():raise SafeError('Refusing symlink instance root')
    result=[]
    for p in sorted(ETC.iterdir()):
        if p.is_symlink():raise SafeError('Refusing symlink instance')
        if p.is_dir() and (p/'link.json').is_file():result.append(instance_name(p.name))
    return result


def active(name):
    return subprocess.run(['systemctl','is-active','--quiet','dv-tunnel@'+instance_name(name)+'.service'],capture_output=True,timeout=10).returncode==0


def validate_configs(release):
    for name in instances():
        p=ETC/name/'config.json'
        if p.is_symlink() or p.stat().st_mode & 0o077:raise SafeError('Private config permissions must be 0600')
        run([str(release/'dv-tunnel'),'--config',str(p),'--check'])


def require_managed_files():
    for path in (WRAPPER,UNIT):
        if path.is_symlink():raise SafeError('Refusing symlink managed file: '+str(path))
        if path.exists() and MARKER not in path.read_text().splitlines(keepends=True)[:2]:
            raise SafeError('Refusing to overwrite unmanaged file: '+str(path))


def scaffolding():
    require_managed_files()
    secure_dir(ETC)
    secure_dir(STATE)
    if not WRAPPER.parent.is_dir() or not UNIT.parent.is_dir():raise SafeError('Ubuntu/systemd install paths are missing')
    atomic_text(WRAPPER,WRAPPER_TEXT,0o755)
    atomic_text(UNIT,UNIT_TEXT,0o644)
    run(['systemctl','daemon-reload'])


def link_probe(name):
    try:
        cfg=json.loads((ETC/name/'config.json').read_text())
        link=json.loads((ETC/name/'link.json').read_text())
        iface=cfg.get('tun','')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,15}',iface):return False
        peer=str(ipaddress.IPv4Address(link['peer']))
        return subprocess.run(['ping','-n','-c','1','-W','1','-I',iface,peer],capture_output=True,timeout=2).returncode==0
    except (OSError,ValueError,KeyError,subprocess.TimeoutExpired):return False


def health(names,version,since,seconds=45):
    end=time.monotonic()+seconds
    pending=set(names)
    while pending and time.monotonic()<end:
        for name in list(pending):
            try:
                p=STATE/name/'stats.json'
                d=json.loads(p.read_text())
                if active(name) and p.stat().st_mtime>=since and 0<=time.time()-p.stat().st_mtime<6 and d.get('version')==version and d.get('connected')==1 and link_probe(name):
                    pending.remove(name)
            except (OSError,ValueError):pass
        if pending:time.sleep(1)
    if pending:raise SafeError('Fresh connected-session health check failed: '+', '.join(sorted(pending)))


def activate(target,restart=False):
    old=pointer()
    running=[n for n in instances() if active(n)]
    if running and not restart:raise SafeError('Active instances require explicit --restart: '+', '.join(running))
    validate_configs(target)
    switched=False
    try:
        set_pointer('current',target);switched=True
        since=time.time()
        for name in running:run(['systemctl','restart','dv-tunnel@'+name+'.service'])
        health(running,target.name,since)
    except Exception as original:
        if switched and old:
            set_pointer('current',old)
            failed=[]
            since=time.time()
            for name in running:
                try:run(['systemctl','restart','dv-tunnel@'+name+'.service'])
                except Exception:failed.append(name)
            try:health([n for n in running if n not in failed],old.name,since)
            except Exception:failed.extend(n for n in running if n not in failed)
            if failed:raise SafeError('Update failed; old pointer restored BUT service recovery needs manual inspection: '+', '.join(failed)) from original
            raise SafeError('Update failed; previous runtime restored and connected health rechecked') from original
        raise
    if old and old!=target:set_pointer('previous',old)


def install(version=None,restart=False):
    require_managed_files()
    version=version or latest_version();version_key(version)
    old=pointer()
    if old and version_key(version)<version_key(old.name):raise SafeError('Downgrade refused; use local rollback explicitly')
    if old and version==old.name:
        print('Already installed:',version);return
    running=[n for n in instances() if active(n)]
    if running and not restart:raise SafeError('Updating live tunnels needs --restart; no changes made')
    secure_dir(ROOT/'releases',0o755)
    target=ROOT/'releases'/version
    if target.exists() or target.is_symlink():raise SafeError('Version directory already exists; refusing overwrite. Use rollback for an installed version.')
    filename='dv-tunnel-'+version+'-linux-'+arch()+'.tar.gz'
    base='https://github.com/'+REPO+'/releases/download/'+version+'/'
    with tempfile.TemporaryDirectory(prefix='.stage-',dir=ROOT/'releases') as tmp:
        stage=Path(tmp); package=stage/'download.tar.gz'
        expected=checksum(download(base+'SHA256SUMS',8192),filename)
        data=download(base+filename,24*1024*1024)
        if hashlib.sha256(data).hexdigest()!=expected:raise SafeError('Release SHA256 mismatch; current runtime unchanged')
        package.write_bytes(data)
        unpack(package,stage,version);package.unlink()
        validate_configs(stage)
        os.rename(stage,target)
    # Existing scaffolding is not rewritten on updates; those changes need an
    # explicit migration, not a silent rewrite of the live service contract.
    backups={}
    if old is None:
        backups={p:(p.read_text(),p.stat().st_mode & 0o777) if p.exists() else None for p in (WRAPPER,UNIT)}
    try:
        if old is None:scaffolding()
        activate(target,restart)
    except BaseException:
        if old is None:
            p=ROOT/'current'
            if p.is_symlink() and p.resolve()==target:p.unlink()
            for p,prior in backups.items():
                if prior is None:
                    if p.exists() and MARKER in p.read_text().splitlines(keepends=True)[:2]:p.unlink()
                else:atomic_text(p,prior[0],prior[1])
            try:run(['systemctl','daemon-reload'])
            except Exception:pass
        # Delete only the newly created candidate if no current/previous pointer
        # still references it. Retain it if recovery could not establish safety.
        try:
            if all(pointer(k)!=target for k in ('current','previous')):shutil.rmtree(target)
        except Exception:pass
        raise
    print('Installed:',version,'(preview builds remain experimental)')


def import_instance(name,field,local,peer):
    name=instance_name(name)
    current=pointer()
    if not current:raise SafeError('Install DV Tunnel first')
    field=Path(field)
    cfg=json.loads((field/'config.json').read_text())
    addr=ipaddress.ip_interface(local);p=ipaddress.ip_address(peer)
    if addr.version!=4 or addr.network.prefixlen!=30 or not any(addr.network.subnet_of(ipaddress.ip_network(n)) for n in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')) or p not in addr.network or p==addr.ip or addr.ip not in list(addr.network.hosts()) or p not in list(addr.network.hosts()):
        raise SafeError('Use two distinct usable private IPv4 addresses in one /30')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,15}',cfg.get('tun','')) or cfg.get('udp'):
        raise SafeError('Import requires a raw-IP TUN configuration')
    if cfg.get('allowed_sources')!=[str(p)+'/32']:raise SafeError('Expected exact peer /32 source allowlist')
    dest=ETC/name
    if dest.exists() or dest.is_symlink():raise SafeError('Instance already exists; never overwriting private settings')
    secure_dir(ETC);secure_dir(STATE)
    state=STATE/name
    if state.exists() or state.is_symlink():raise SafeError('Instance state already exists; inspect it first')
    dest.mkdir(mode=0o700);state.mkdir(mode=0o700)
    try:
        cfg['stats_file']=str(state/'stats.json')
        atomic_text(dest/'config.json',json.dumps(cfg,indent=2)+'\n')
        atomic_text(dest/'link.json',json.dumps(dict(schema=1,local=str(addr),peer=str(p)),indent=2)+'\n')
        run([str(current/'dv-tunnel'),'--config',str(dest/'config.json'),'--check'])
    except BaseException:
        shutil.rmtree(dest);state.rmdir();raise
    print('Imported',name,'without starting or enabling it. Original files unchanged.')


def service_action(action,name,yes=False):
    name=instance_name(name)
    if name not in instances():raise SafeError('Unknown managed instance')
    if action in ('stop','restart') and not yes:raise SafeError('Use --yes to confirm interruption of this instance only')
    run(['systemctl',action,'dv-tunnel@'+name+'.service'])
    print(action, name, 'requested; use status to verify a fresh connected session')


class TerminalUI:
    """Dependency-free SSH UI; no ANSI in pipes, NO_COLOR or dumb terminals."""
    COLORS={'brand':'1;36','title':'1;37','muted':'90','ok':'1;32',
            'warn':'1;33','error':'1;31','accent':'1;35','plain':'37'}

    def __init__(self,stream=None,width=None,color=None,unicode=None):
        self.stream=stream if stream is not None else sys.stdout
        self.interactive=bool(getattr(self.stream,'isatty',lambda:False)())
        self.width=max(12,min(78,width if width is not None else shutil.get_terminal_size((80,24)).columns-2))
        self.inner=self.width-4
        self.color=(self.interactive and 'NO_COLOR' not in os.environ and os.environ.get('TERM','dumb')!='dumb') if color is None else color
        encoding=getattr(self.stream,'encoding',None) or 'ascii'
        try:'╭╮╰╯─│├┤'.encode(encoding);supported=True
        except (UnicodeError,LookupError):supported=False
        self.unicode=(self.interactive and supported and os.environ.get('DV_ASCII')!='1') if unicode is None else unicode

    @staticmethod
    def safe(text):
        # All labels are ASCII; hostnames/errors cannot inject terminal controls
        # or double-width glyphs into a carefully measured frame.
        return ''.join(c for c in str(text) if 32<=ord(c)<=126)

    def paint(self,text,tone='plain'):
        text=self.safe(text)
        return '\033['+self.COLORS.get(tone,'37')+'m'+text+'\033[0m' if self.color else text

    def emit(self,text=''):
        print(text,file=self.stream)

    def edge(self,kind='middle'):
        a,b,c=({'top':('╭','─','╮'),'middle':('├','─','┤'),'bottom':('╰','─','╯')}[kind]
               if self.unicode else ('+','-','+'))
        line=a+b*(self.width-2)+c
        # Border characters, unlike untrusted text, are emitted verbatim.
        self.emit('\033[36m'+line+'\033[0m' if self.color else line)

    def line(self,text='',tone='plain'):
        import textwrap
        rows=textwrap.wrap(self.safe(text),width=self.inner,replace_whitespace=True,
                           break_long_words=True,break_on_hyphens=False) or ['']
        side='│' if self.unicode else '|'
        border='\033[36m'+side+'\033[0m' if self.color else side
        for row in rows:self.emit(border+' '+self.paint(row.ljust(self.inner),tone)+' '+border)

    def clear(self):
        if self.interactive and os.environ.get('TERM','dumb')!='dumb' and not os.environ.get('DV_NO_CLEAR'):
            self.emit('\033[2J\033[H')

    def note(self,message,tone='muted'):
        self.emit(self.paint('  '+message,tone))


def instance_rows():
    rows=[]
    for name in instances():
        running=active(name)
        state='INACTIVE';tone='muted';drops=None
        if running:
            state='STALE / UNKNOWN';tone='warn'
            try:
                p=STATE/name/'stats.json';d=json.loads(p.read_text())
                if 0<=time.time()-p.stat().st_mtime<6:
                    state='CONNECTED' if d.get('connected')==1 else 'CONNECTING'
                    tone='ok' if state=='CONNECTED' else 'warn'
                    value=d.get('drops')
                    if isinstance(value,int) and not isinstance(value,bool) and value>=0:drops=value
            except (OSError,ValueError):pass
        rows.append(dict(name=name,running=running,state=state,tone=tone,drops=drops))
    return rows


def render_dashboard(ui,rows,show_options=True):
    current=pointer();previous=pointer('previous')
    connected=sum(r['state']=='CONNECTED' for r in rows)
    running=sum(r['running'] for r in rows)
    ui.edge('top')
    ui.line('D V   T U N N E L','title')
    ui.line('STANDALONE TRANSPORT  /  PREVIEW','accent')
    ui.edge()
    ui.line('Installed  '+(current.name if current else 'not installed'),'brand')
    ui.line('Rollback   '+(previous.name if previous else 'not available yet'),'muted')
    ui.line('Node       '+platform.node()+'  /  '+platform.machine(),'muted')
    ui.edge()
    ui.line('LINK OVERVIEW','title')
    ui.line('Managed: '+str(len(rows))+'   Running: '+str(running)+'   WSS connected: '+str(connected),'ok' if connected else 'muted')
    if not rows:
        ui.line('No managed tunnels yet. Installing the menu does not start a tunnel.','warn')
        ui.line('Existing field tests are not imported automatically.','muted')
    else:
        for r in rows:
            text=r['name']+'  |  '+r['state']
            if r['drops'] is not None:text+='  |  drops '+str(r['drops'])
            ui.line(text,r['tone'])
        ui.line('CONNECTED = fresh WSS stats, not full application acceptance.','muted')
    if show_options:
        ui.edge()
        if ui.inner>=58:
            col=(ui.inner-3)//2
            ui.line('MONITOR                       MAINTENANCE','accent')
            ui.line('[1] Status & connections'.ljust(col)+' | [3] Update DV Tunnel')
            ui.line('[2] Check latest release'.ljust(col)+' | [4] Roll back version')
            ui.edge();ui.line('TUNNEL CONTROL','accent')
            ui.line('[5] Start       [6] Stop       [7] Restart','warn')
        else:
            ui.line('MONITOR','accent')
            ui.line('[1] Status & connections');ui.line('[2] Check latest release')
            ui.edge();ui.line('MAINTENANCE','accent')
            ui.line('[3] Update DV Tunnel','brand');ui.line('[4] Roll back version')
            ui.edge();ui.line('TUNNEL CONTROL','accent')
            ui.line('[5] Start instance','ok');ui.line('[6] Stop instance','warn');ui.line('[7] Restart instance','warn')
        ui.edge();ui.line('[0] Exit  |  No automatic restarts','muted')
    ui.edge('bottom')


def status():
    ui=TerminalUI()
    render_dashboard(ui,instance_rows(),show_options=False)


def menu():
    ui=TerminalUI()
    while True:
        ui.clear()
        rows=instance_rows()
        render_dashboard(ui,rows)
        try:choice=input(ui.paint('  Select [0-7] > ','brand')).strip()
        except (EOFError,KeyboardInterrupt):ui.note('Exited. No tunnel was stopped.');return
        if choice=='0':ui.note('Exited. Tunnels keep their current state.');return
        try:
            if choice=='1':status()
            elif choice=='2':
                ui.note('Checking published releases...','brand')
                ui.note('Newest version: '+latest_version(),'ok')
            elif choice in ('3','4'):
                running=[n for n in instances() if active(n)]
                confirmed=True
                if running:
                    ui.note('This briefly interrupts ONLY: '+', '.join(running),'warn')
                    confirmed=input(ui.paint('  Type RESTART to continue > ','warn')).strip()=='RESTART'
                if not confirmed:ui.note('Cancelled. No change was made.','muted')
                else:
                    before=pointer()
                    with lock():
                        if choice=='3':
                            ui.note('Downloading, verifying and validating the release...','brand')
                            install(restart=bool(running))
                        else:
                            target=pointer('previous')
                            if not target:raise SafeError('No previous local version')
                            ui.note('Restoring the previous local runtime...','brand')
                            activate(target,restart=bool(running));ui.note('Rolled back to '+target.name,'ok')
                    after=pointer()
                    if before!=after:
                        ui.note('Version changed. Run dv-tunnel again to load its menu.','ok')
                        return
            elif choice in ('5','6','7'):
                if not rows:
                    ui.note('No managed instance. Import a pair with dv-tunnel import first.','warn')
                    ui.note('See docs/UPDATE.md; existing test directories stay untouched.')
                else:
                    ui.note('Available instances: '+', '.join(r['name'] for r in rows),'brand')
                    name=instance_name(input(ui.paint('  Instance name > ','brand')).strip())
                    action={'5':'start','6':'stop','7':'restart'}[choice]
                    confirm=action=='start' or input(ui.paint('  Type YES to interrupt this instance > ','warn')).strip()=='YES'
                    if confirm:
                        with lock():service_action(action,name,yes=True)
                    else:ui.note('Cancelled. No change was made.')
            else:ui.note('Choose a number from 0 to 7.','warn')
        except EOFError:ui.note('Input closed. No further action taken.');return
        except (Exception,KeyboardInterrupt) as e:
            ui.note('Not completed: '+(str(e) if isinstance(e,SafeError) else type(e).__name__),'error')
        if ui.interactive:
            try:input(ui.paint('  Press Enter to return to the menu...','muted'))
            except (EOFError,KeyboardInterrupt):return


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command',nargs='?',default='menu',choices=['menu','install','update','rollback','check-update','status','import','start','stop','restart'])
    ap.add_argument('name',nargs='?')
    ap.add_argument('--version',dest='version')
    ap.add_argument('--restart',action='store_true',help='explicitly allow restarting active managed instances')
    ap.add_argument('--yes',action='store_true')
    ap.add_argument('--field-dir');ap.add_argument('--local');ap.add_argument('--peer')
    args=ap.parse_args()
    if os.geteuid()!=0:ap.exit(1,'Run with sudo/root. No change was made.\n')
    try:
        if args.command=='menu':menu()
        elif args.command=='status':status()
        elif args.command=='check-update':print(latest_version())
        else:
            with lock():
                if args.command in ('install','update'):install(args.version,args.restart)
                elif args.command=='rollback':
                    target=pointer('previous')
                    if not target:raise SafeError('No previous local version')
                    activate(target,args.restart);print('Rolled back to',target.name)
                elif args.command=='import':
                    if not all((args.name,args.field_dir,args.local,args.peer)):raise SafeError('import needs NAME --field-dir --local CIDR --peer IP')
                    import_instance(args.name,args.field_dir,args.local,args.peer)
                else:service_action(args.command,args.name or '',args.yes)
    except (Exception,KeyboardInterrupt) as e:
        ap.exit(1,'Not completed: '+(str(e) if isinstance(e,SafeError) else type(e).__name__)+ '\n')


if __name__=='__main__':main()
