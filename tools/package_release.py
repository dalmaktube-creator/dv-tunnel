#!/usr/bin/env python3
"""Package only whitelisted release files; never include private configurations."""
import argparse
import hashlib
from pathlib import Path
import re
import tarfile
import tempfile
import shutil


def package(root,binary,version,arch,out):
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-(?:alpha|preview|beta|rc)\.\d+)?',version):raise ValueError('bad version')
    if arch not in ('amd64','arm64'):raise ValueError('bad architecture')
    root,binary,out=Path(root),Path(binary),Path(out)
    out.mkdir(parents=True,exist_ok=True)
    name='dv-tunnel-'+version+'-linux-'+arch+'.tar.gz'
    dest=out/name
    if dest.exists():raise FileExistsError(dest)
    with tempfile.TemporaryDirectory() as tmp:
        stage=Path(tmp)
        shutil.copyfile(binary,stage/'dv-tunnel')
        for n in ('dvctl.py','service_run.py','dv_pair.py'):shutil.copyfile(root/'tools'/n,stage/n)
        for n in ('LICENSE','THIRD_PARTY_NOTICES.md'):shutil.copyfile(root/n,stage/n)
        (stage/'VERSION').write_text(version+'\n')
        with tarfile.open(dest,'w:gz') as tar:
            for p in sorted(stage.iterdir()):
                info=tar.gettarinfo(str(p),arcname=p.name)
                info.uid=info.gid=0;info.uname=info.gname='root'
                info.mode=0o755 if p.name=='dv-tunnel' else 0o644
                with p.open('rb') as f:tar.addfile(info,f)
    return hashlib.sha256(dest.read_bytes()).hexdigest()+'  '+name


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--binary',required=True);ap.add_argument('--version',required=True)
    ap.add_argument('--arch',required=True);ap.add_argument('--output',required=True)
    a=ap.parse_args()
    print(package(Path(__file__).resolve().parents[1],a.binary,a.version,a.arch,a.output))
