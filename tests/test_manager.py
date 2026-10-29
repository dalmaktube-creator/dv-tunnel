import argparse
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m
M=load('dvctl',ROOT/'tools/dvctl.py')
P=load('package_release',ROOT/'tools/package_release.py')


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        probe=patch.object(M,'link_probe',return_value=True);probe.start();self.addCleanup(probe.stop)
        self.stack=[]
        for key,path in {'ROOT':self.root/'opt','ETC':self.root/'etc/instances',
                         'STATE':self.root/'state','WRAPPER':self.root/'bin/dv-tunnel',
                         'UNIT':self.root/'systemd/dv-tunnel@.service'}.items():
            p=patch.object(M,key,path);p.start();self.addCleanup(p.stop)
        M.ROOT.mkdir();(M.ROOT/'releases').mkdir();M.ETC.mkdir(parents=True);M.STATE.mkdir()
        M.WRAPPER.parent.mkdir();M.UNIT.parent.mkdir()

    def version(self,v):
        p=M.ROOT/'releases'/v;p.mkdir();(p/'VERSION').write_text(v+'\n');return p

    def test_versions_and_instance_input(self):
        self.assertLess(M.version_key('v0.1.0-preview.1'),M.version_key('v0.1.0-preview.2'))
        self.assertLess(M.version_key('v0.1.0-rc.9'),M.version_key('v0.1.0'))
        for value in ('../oops','v01.1.0','v0.1.0;id'):
            with self.assertRaises(M.SafeError):M.version_key(value)
        for value in ('../oops','--all','x.service','X','a\n'):
            with self.assertRaises(M.SafeError):M.instance_name(value)

    def test_checksum_strict(self):
        digest='a'*64
        self.assertEqual(M.checksum((digest+'  test.tar.gz\n').encode(),'test.tar.gz'),digest)
        for content in (b'not a manifest',((digest+'  test.tar.gz\n')*2).encode()):
            with self.assertRaises(M.SafeError):M.checksum(content,'test.tar.gz')

    def test_archive_rejects_traversal_and_links(self):
        for path,kind in (('../escape',tarfile.REGTYPE),('dv-tunnel',tarfile.SYMTYPE)):
            archive=self.root/'bad.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:
                for name in M.MEMBERS:
                    info=tarfile.TarInfo(path if name=='dv-tunnel' else name)
                    info.type=kind if name=='dv-tunnel' else tarfile.REGTYPE
                    info.linkname='/etc/passwd' if info.type==tarfile.SYMTYPE else ''
                    tar.addfile(info,io.BytesIO(b''))
            dest=self.root/('out-'+kind.decode());dest.mkdir(exist_ok=True)
            with self.assertRaises(M.SafeError):M.unpack(archive,dest,'v0.1.0-preview.1')
            self.assertFalse((self.root/'escape').exists())

    def test_symlink_paths_refused(self):
        (M.ROOT/'current').symlink_to(self.root)
        with self.assertRaises(M.SafeError):M.pointer()
        d=self.root/'link';d.symlink_to(self.root)
        with self.assertRaises(M.SafeError):M.secure_dir(d)

    def test_live_update_requires_confirmation_before_switch(self):
        old=self.version('v0.1.0-preview.1');new=self.version('v0.1.0-preview.2')
        M.set_pointer('current',old)
        with patch.object(M,'instances',return_value=['test']),patch.object(M,'active',return_value=True),patch.object(M,'run') as run:
            with self.assertRaises(M.SafeError):M.activate(new)
            run.assert_not_called()
        self.assertEqual(M.pointer(),old)

    def test_successful_atomic_update_and_rollback(self):
        old=self.version('v0.1.0-preview.1');new=self.version('v0.1.0-preview.2')
        M.set_pointer('current',old)
        with patch.object(M,'instances',return_value=[]),patch.object(M,'validate_configs'),patch.object(M,'health'):
            M.activate(new)
            self.assertEqual(M.pointer(),new);self.assertEqual(M.pointer('previous'),old)
            M.activate(M.pointer('previous'))
            self.assertEqual(M.pointer(),old);self.assertEqual(M.pointer('previous'),new)

    def test_failed_connected_health_restores_old_runtime(self):
        old=self.version('v0.1.0-preview.1');new=self.version('v0.1.0-preview.2');M.set_pointer('current',old)
        with patch.object(M,'instances',return_value=['test']),patch.object(M,'active',return_value=True),patch.object(M,'validate_configs'),patch.object(M,'run') as run,patch.object(M,'health',side_effect=[M.SafeError('candidate failed'),None]):
            with self.assertRaisesRegex(M.SafeError,'previous runtime restored'):M.activate(new,restart=True)
            self.assertEqual(run.call_count,2)
        self.assertEqual(M.pointer(),old)

    def test_failed_rollback_health_is_not_reported_as_recovered(self):
        old=self.version('v0.1.0-preview.1');new=self.version('v0.1.0-preview.2');M.set_pointer('current',old)
        with patch.object(M,'instances',return_value=['test']),patch.object(M,'active',return_value=True),patch.object(M,'validate_configs'),patch.object(M,'run'),patch.object(M,'health',side_effect=M.SafeError('offline')):
            with self.assertRaisesRegex(M.SafeError,'manual inspection'):M.activate(new,restart=True)
        self.assertEqual(M.pointer(),old)

    def test_unmanaged_wrapper_never_overwritten(self):
        M.WRAPPER.write_text('existing unrelated binary wrapper')
        with self.assertRaises(M.SafeError):M.scaffolding()
        self.assertEqual(M.WRAPPER.read_text(),'existing unrelated binary wrapper')

    def test_release_hash_failure_preserves_current(self):
        old=self.version('v0.1.0-preview.1');M.set_pointer('current',old)
        filename='dv-tunnel-v0.1.0-preview.2-linux-amd64.tar.gz'
        with patch.object(M,'instances',return_value=[]),patch.object(M,'arch',return_value='amd64'),patch.object(M,'download',side_effect=[('a'*64+'  '+filename+'\n').encode(),b'wrong']):
            with self.assertRaisesRegex(M.SafeError,'SHA256 mismatch'):M.install('v0.1.0-preview.2')
        self.assertEqual(M.pointer(),old)
        self.assertFalse((M.ROOT/'releases/v0.1.0-preview.2').exists())

    def test_import_preserves_original_and_does_not_start_service(self):
        old=self.version('v0.1.0-preview.1');M.set_pointer('current',old)
        field=self.root/'field';field.mkdir()
        text=json.dumps(dict(tun='wft1',allowed_sources=['10.254.253.2/32']))
        (field/'config.json').write_text(text)
        with patch.object(M,'run') as run:
            M.import_instance('test',field,'10.254.253.1/30','10.254.253.2')
            self.assertEqual(run.call_count,1)
            self.assertNotEqual(run.call_args[0][0][0],'systemctl')
        self.assertEqual((field/'config.json').read_text(),text)
        self.assertEqual((M.ETC/'test/config.json').stat().st_mode&0o777,0o600)
        with self.assertRaises(M.SafeError):M.import_instance('test',field,'10.254.253.1/30','10.254.253.2')

    def test_release_packaging_excludes_private_files(self):
        root=self.root/'source';(root/'tools').mkdir(parents=True)
        for name in ('dvctl.py','service_run.py','dv_pair.py'):(root/'tools'/name).write_text('# release file\n')
        for name in ('LICENSE','THIRD_PARTY_NOTICES.md'):(root/name).write_text('notice\n')
        (root/'config.json').write_text('PRIVATE SHOULD NOT SHIP')
        binary=self.root/'binary';binary.write_bytes(b'ELF test fixture')
        line=P.package(root,binary,'v0.1.0-preview.1','amd64',self.root/'packages')
        file=next((self.root/'packages').iterdir())
        self.assertEqual(line.split()[0],hashlib.sha256(file.read_bytes()).hexdigest())
        with tarfile.open(file,'r:gz') as tar:self.assertEqual(set(tar.getnames()),M.MEMBERS)

    def test_fresh_health_rejects_stale_stats(self):
        state=M.STATE/'test';state.mkdir();p=state/'stats.json'
        p.write_text(json.dumps(dict(version='v0.1.0-preview.1',connected=1)))
        os.utime(p,(1,1))
        with patch.object(M,'active',return_value=True):
            with self.assertRaises(M.SafeError):M.health(['test'],'v0.1.0-preview.1',time_since:=2,seconds=.01)
        p.write_text(json.dumps(dict(version='v0.1.0-preview.1',connected=1)))
        with patch.object(M,'active',return_value=True):M.health(['test'],'v0.1.0-preview.1',time_since)

    def test_complete_first_install_stages_and_activates_version(self):
        source=self.root/'source';(source/'tools').mkdir(parents=True)
        for name in ('dvctl.py','service_run.py','dv_pair.py'):(source/'tools'/name).write_text('# syntax valid\n')
        for name in ('LICENSE','THIRD_PARTY_NOTICES.md'):(source/name).write_text('notice\n')
        binary=self.root/'fake';binary.write_bytes(b'ELF fixture')
        version='v0.1.0-preview.1'
        manifest=P.package(source,binary,version,'amd64',self.root/'pkgs')+'\n'
        data=next((self.root/'pkgs').iterdir()).read_bytes()
        with patch.object(M,'arch',return_value='amd64'),patch.object(M,'download',side_effect=[manifest.encode(),data]),patch.object(M,'run',return_value=version),patch.object(M,'health'):
            M.install(version)
        self.assertEqual(M.pointer().name,version)
        self.assertEqual(M.WRAPPER.read_text(),M.WRAPPER_TEXT)
        self.assertEqual(M.UNIT.read_text(),M.UNIT_TEXT)
        self.assertEqual(set(p.name for p in M.pointer().iterdir()),M.MEMBERS)

    def test_no_http_downgrade_redirect(self):
        redirect=M.HTTPSRedirect()
        for url in ('http://github.com/file','https://evil.example/file'):
            with self.assertRaises(M.SafeError):redirect.redirect_request(None,None,302,'',{},url)

    def test_failed_first_install_restores_bootstrap_and_can_retry(self):
        source=self.root/'source';(source/'tools').mkdir(parents=True)
        for name in ('dvctl.py','service_run.py','dv_pair.py'):(source/'tools'/name).write_text('# syntax valid\n')
        for name in ('LICENSE','THIRD_PARTY_NOTICES.md'):(source/name).write_text('notice\n')
        binary=self.root/'fake';binary.write_bytes(b'ELF fixture')
        version='v0.1.0-preview.1'
        manifest=P.package(source,binary,version,'amd64',self.root/'pkgs')+'\n'
        data=next((self.root/'pkgs').iterdir()).read_bytes()
        def failed_scaffolding():
            M.WRAPPER.write_text(M.WRAPPER_TEXT);M.UNIT.write_text(M.UNIT_TEXT)
            raise M.SafeError('daemon reload failed')
        with patch.object(M,'arch',return_value='amd64'),patch.object(M,'download',side_effect=[manifest.encode(),data]),patch.object(M,'run',return_value=version),patch.object(M,'scaffolding',side_effect=failed_scaffolding):
            with self.assertRaises(M.SafeError):M.install(version)
        self.assertIsNone(M.pointer())
        self.assertFalse((M.ROOT/'releases'/version).exists())
        self.assertFalse(M.WRAPPER.exists());self.assertFalse(M.UNIT.exists())
