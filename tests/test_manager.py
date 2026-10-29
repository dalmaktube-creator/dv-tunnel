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

class SplitProfileTests(unittest.TestCase):
    setUp = ManagerTests.setUp
    version = ManagerTests.version
    def make_instance(self):
        d=M.ETC/'main';d.mkdir(mode=0o700)
        M.atomic_text(d/'link.json',json.dumps(dict(schema=1,local='10.254.253.1/30',peer='10.254.253.2',unrelated='keep')))
        M.atomic_text(d/'config.json','{"token":"PRIVATE_UNCHANGED"}')
        v=self.version('v0.1.0-preview.3');(v/'service_run.py').write_text('SPLIT_UDP_SUPPORT = 1\n')
        M.set_pointer('current',v)
        return d,v

    def test_profile_is_metadata_only_no_restart(self):
        d,v=self.make_instance();original=(d/'config.json').read_bytes();old=(d/'link.json').read_bytes()
        with patch.object(M,'run') as run,patch.object(M,'service_action') as action:
            M.set_profile('main',True)
            self.assertEqual(run.call_args.args[0],[str(v/'dv-tunnel'),'--config',str(d/'config.json'),'--check','--split-udp'])
            action.assert_not_called()
        self.assertEqual((d/'config.json').read_bytes(),original)
        self.assertEqual((d/'link.previous.json').read_bytes(),old)
        link=json.loads((d/'link.json').read_text());self.assertTrue(link['split_udp']);self.assertEqual(link['unrelated'],'keep')
        self.assertEqual((d/'link.json').stat().st_mode&0o777,0o600)

    def test_rollback_rejects_old_runner_before_pointer_change(self):
        d,v=self.make_instance();link=json.loads((d/'link.json').read_text());link['split_udp']=True;M.atomic_text(d/'link.json',json.dumps(link))
        old=self.version('v0.1.0-preview.2');(old/'service_run.py').write_text('# old runner\n')
        with patch.object(M,'run') as run:
            with self.assertRaisesRegex(M.SafeError,'cannot honor'):M.validate_configs(old)
            run.assert_not_called()
        self.assertEqual(M.pointer(),v)

    def test_profile_failure_leaves_files_unchanged(self):
        d,v=self.make_instance();original=(d/'link.json').read_bytes()
        with patch.object(M,'run',side_effect=M.SafeError('invalid feature')):
            with self.assertRaises(M.SafeError):M.set_profile('main',True)
        self.assertEqual((d/'link.json').read_bytes(),original)
        self.assertFalse((d/'link.previous.json').exists())

    def test_single_stream_recovery_without_old_runner_support(self):
        d,v=self.make_instance();link=json.loads((d/'link.json').read_text());link['split_udp']=True;M.atomic_text(d/'link.json',json.dumps(link))
        with patch.object(M,'run') as run:
            M.set_profile('main',False)
            self.assertNotIn('--split-udp',run.call_args.args[0])
        self.assertFalse(json.loads((d/'link.json').read_text())['split_udp'])

    def test_profile_rejects_nonboolean_and_symlink(self):
        d,v=self.make_instance()
        with self.assertRaises(M.SafeError):M.split_args(v,{'split_udp':'false'})
        p=d/'link.json';p.unlink();p.symlink_to(d/'config.json')
        with self.assertRaises(M.SafeError):M.set_profile('main',True)

class QueueOverrideTests(SplitProfileTests):
    def make_queue_instance(self):
        d,v=self.make_instance();(v/'service_run.py').write_text('SPLIT_UDP_SUPPORT = 1\nQUEUE_OVERRIDE_SUPPORT = 1\n')
        link=json.loads((d/'link.json').read_text());link['split_udp']=True;M.atomic_text(d/'link.json',json.dumps(link));return d,v

    def test_queue_is_metadata_only_and_no_restart(self):
        d,v=self.make_queue_instance();private=(d/'config.json').read_bytes();old=(d/'link.json').read_bytes()
        with patch.object(M,'run') as run,patch.object(M,'service_action') as action:
            M.set_queue('main',256);action.assert_not_called()
            self.assertEqual(run.call_args.args[0][-3:],['--split-udp','--queue-packets','256'])
        self.assertEqual((d/'config.json').read_bytes(),private)
        self.assertEqual((d/'link.queue.previous.json').read_bytes(),old)
        self.assertEqual(json.loads((d/'link.json').read_text())['queue_packets_override'],256)
        self.assertEqual((d/'link.json').stat().st_mode&0o777,0o600)
        with patch.object(M,'run'):
            M.set_queue('main',0)
        self.assertNotIn('queue_packets_override',json.loads((d/'link.json').read_text()))
        self.assertEqual((d/'config.json').read_bytes(),private)

    def test_queue_failure_is_atomic_and_rollback_refuses_old_runner(self):
        d,v=self.make_queue_instance();old=(d/'link.json').read_bytes()
        with patch.object(M,'run',side_effect=M.SafeError('bad runtime')):
            with self.assertRaises(M.SafeError):M.set_queue('main',256)
        self.assertEqual((d/'link.json').read_bytes(),old)
        self.assertFalse((d/'link.queue.previous.json').exists())
        with patch.object(M,'run'):M.set_queue('main',256)
        (v/'service_run.py').write_text('SPLIT_UDP_SUPPORT = 1\n')
        with patch.object(M,'run') as run:
            with self.assertRaisesRegex(M.SafeError,'cannot honor queue'):M.validate_configs(v)
            run.assert_not_called()
        with patch.object(M,'run'):M.set_queue('main',0)

    def test_queue_rejects_invalid_metadata_and_profile_change(self):
        d,v=self.make_queue_instance()
        for q in [True,'256',1,-1,257]:
            with self.assertRaises(M.SafeError):M.split_args(v,dict(split_udp=True,queue_packets_override=q))
        with patch.object(M,'run'):M.set_queue('main',256)
        old=(d/'link.json').read_bytes()
        with self.assertRaisesRegex(M.SafeError,'Clear queue override'):M.set_profile('main',False)
        self.assertEqual((d/'link.json').read_bytes(),old)
        p=d/'link.json';p.unlink();p.symlink_to(d/'config.json')
        with self.assertRaises(M.SafeError):M.set_queue('main',256)

class UDPWaitMetadataTests(QueueOverrideTests):
    def test_wait_is_opt_in_private_preserving_and_old_runner_rejected(self):
        d,v=self.make_queue_instance();(v/'service_run.py').write_text('SPLIT_UDP_SUPPORT = 1\nQUEUE_OVERRIDE_SUPPORT = 1\nUDP_WAIT_SUPPORT = 1\n')
        private=(d/'config.json').read_bytes()
        with patch.object(M,'run'):
            M.set_queue('main',256)
            M.set_queue('main',0,200)
        link=json.loads((d/'link.json').read_text());self.assertNotIn('queue_packets_override',link);self.assertEqual(link['udp_wait_ms'],200)
        self.assertEqual(M.split_args(v,link),['--split-udp','--udp-wait-ms','200'])
        self.assertEqual((d/'config.json').read_bytes(),private)
        (v/'service_run.py').write_text('SPLIT_UDP_SUPPORT = 1\nQUEUE_OVERRIDE_SUPPORT = 1\n')
        with patch.object(M,'run') as run:
            with self.assertRaisesRegex(M.SafeError,'cannot honor UDP wait'):M.validate_configs(v)
            run.assert_not_called()
            M.set_queue('main',0)
        self.assertNotIn('udp_wait_ms',json.loads((d/'link.json').read_text()))

    def test_wait_metadata_validation_and_atomic_failure(self):
        d,v=self.make_queue_instance();old=(d/'link.json').read_bytes()
        for n in [-1,201,True,'200']:
            with self.assertRaises(M.SafeError):M.set_queue('main',None,n)
            with self.assertRaises(M.SafeError):M.split_args(v,dict(split_udp=True,udp_wait_ms=n))
        with self.assertRaisesRegex(M.SafeError,'cannot honor UDP wait'):M.set_queue('main',0,200)
        self.assertEqual((d/'link.json').read_bytes(),old)
