"""Private-config regression tests: no root, network or carrier execution."""
import errno
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('service_run', ROOT / 'tools/service_run.py')
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


class PrivateConfigError(Exception):
    pass


class PrivateConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'config.json'
        self.path.write_text('{"fixture": "original"}', encoding='utf-8')
        self.path.chmod(0o600)
        self.real_open, self.real_fstat = os.open, os.fstat
        # Model root ownership without requiring privileged CI workers.
        fake = patch.object(S.os, 'fstat', side_effect=self.root_owned_stat)
        fake.start()
        self.addCleanup(fake.stop)

    def root_owned_stat(self, fd):
        return SimpleNamespace(st_mode=self.real_fstat(fd).st_mode, st_uid=0)

    def read(self, path=None):
        return S.read_private_json(self.path if path is None else path, PrivateConfigError)

    def test_accepts_private_regular_json(self):
        for mode in (0o400, 0o600):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                self.assertEqual(self.read(), {'fixture': 'original'})

    def test_rejects_group_or_world_access(self):
        for mode in (0o640, 0o620, 0o604, 0o602, 0o644):
            with self.subTest(mode=oct(mode)):
                self.path.chmod(mode)
                with self.assertRaises(PrivateConfigError): self.read()

    def test_rejects_nonroot_owner(self):
        info = self.path.stat()
        with patch.object(S.os, 'fstat', return_value=SimpleNamespace(st_mode=info.st_mode, st_uid=1)):
            with self.assertRaises(PrivateConfigError): self.read()

    def test_rejects_symlink(self):
        link = self.root / 'link.json';link.symlink_to(self.path)
        with self.assertRaises(OSError) as caught: self.read(link)
        self.assertEqual(caught.exception.errno, errno.ELOOP)

    def test_rejects_broken_symlink(self):
        link = self.root / 'broken.json';link.symlink_to(self.root / 'absent')
        with self.assertRaises(OSError) as caught: self.read(link)
        self.assertEqual(caught.exception.errno, errno.ELOOP)

    def test_rejects_directory(self):
        with self.assertRaises(PrivateConfigError): self.read(self.root)

    def test_rejects_fifo_without_waiting_for_writer(self):
        fifo = self.root / 'fifo';os.mkfifo(fifo, 0o600)
        def checked_open(path, flags):
            # Fail before opening if a regression would otherwise hang the test.
            self.assertTrue(flags & os.O_NONBLOCK)
            self.assertTrue(flags & os.O_NOFOLLOW)
            return self.real_open(path, flags)
        with patch.object(S.os, 'open', side_effect=checked_open):
            with self.assertRaises(PrivateConfigError): self.read(fifo)

    def test_reads_validated_inode_after_path_replacement(self):
        replacement = self.root / 'replacement.json'
        replacement.write_text('{"fixture": "replacement"}', encoding='utf-8')
        replacement.chmod(0o600)
        def swapped_open(path, flags):
            fd = self.real_open(path, flags)
            replacement.replace(path)
            return fd
        with patch.object(S.os, 'open', side_effect=swapped_open):
            self.assertEqual(self.read(), {'fixture': 'original'})
        self.assertEqual(json.loads(self.path.read_text()), {'fixture': 'replacement'})

    def assert_descriptor_closed(self, fail):
        opened = []
        def capture_open(path, flags):
            fd = self.real_open(path, flags);opened.append(fd);return fd
        if fail: self.path.write_text('invalid JSON', encoding='utf-8')
        with patch.object(S.os, 'open', side_effect=capture_open):
            if fail:
                with self.assertRaises(json.JSONDecodeError): self.read()
            else: self.read()
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError) as caught: self.real_fstat(opened[0])
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_closes_descriptor_after_success(self):
        self.assert_descriptor_closed(False)

    def test_closes_descriptor_after_invalid_json(self):
        self.assert_descriptor_closed(True)
