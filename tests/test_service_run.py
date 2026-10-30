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

    def test_accepts_exact_byte_limit(self):
        limit = 64 * 1024
        self.path.write_bytes(b'{}' + b' ' * (limit - 2))
        self.assertEqual(self.read(), {})

    def test_rejects_oversized_json(self):
        self.path.write_bytes(b'{}' + b' ' * (64 * 1024 - 1))
        with self.assertRaises(PrivateConfigError): self.read()

    def test_rejects_nonobject_json(self):
        for value in ([], 'private-value', 123, None, True):
            with self.subTest(value_type=type(value).__name__):
                self.path.write_text(json.dumps(value), encoding='utf-8')
                with self.assertRaises(PrivateConfigError): self.read()

    def test_rejects_invalid_utf8(self):
        self.path.write_bytes(b'{"v":"\xff"}')
        with self.assertRaises(UnicodeDecodeError): self.read()

    def test_read_stays_bounded_if_file_grows_after_validation(self):
        real_fdopen = S.os.fdopen
        def grow_before_read(*args, **kwargs):
            self.path.write_bytes(b'{}' + b' ' * (64 * 1024))
            return real_fdopen(*args, **kwargs)
        with patch.object(S.os, 'fdopen', side_effect=grow_before_read):
            with self.assertRaises(PrivateConfigError): self.read()

    def test_closes_descriptor_after_permission_failure(self):
        opened = []
        self.path.chmod(0o640)
        def capture_open(path, flags):
            fd = self.real_open(path, flags);opened.append(fd);return fd
        with patch.object(S.os, 'open', side_effect=capture_open):
            with self.assertRaises(PrivateConfigError): self.read()
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError) as caught: self.real_fstat(opened[0])
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_repeated_reads_under_low_descriptor_limit(self):
        import resource
        old_soft, old_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        soft = 64 if old_soft == resource.RLIM_INFINITY else min(64, old_soft)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, old_hard))
        try:
            self.path.write_text('invalid JSON', encoding='utf-8')
            for _ in range(256):
                with self.assertRaises(json.JSONDecodeError): self.read()
            self.path.write_text('{}', encoding='utf-8')
            for _ in range(256): self.assertEqual(self.read(), {})
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (old_soft, old_hard))

    def test_repeated_path_replacements_keep_opened_inode(self):
        for _ in range(128):
            self.path.write_text('{"v":"original"}', encoding='utf-8')
            replacement = self.root / 'replacement.json'
            replacement.write_text('{"v":"replacement"}', encoding='utf-8')
            replacement.chmod(0o600)
            def swap(path, flags):
                fd = self.real_open(path, flags)
                replacement.replace(path)
                return fd
            with patch.object(S.os, 'open', side_effect=swap):
                self.assertEqual(self.read(), {'v': 'original'})

    def test_validation_errors_do_not_echo_private_payload(self):
        private = 'DO-NOT-LOG-THIS-FIXTURE'
        for data in (json.dumps([private]).encode(), json.dumps({'v': private * 4000}).encode()):
            with self.subTest(size=len(data)):
                self.path.write_bytes(data)
                with self.assertRaises(PrivateConfigError) as caught: self.read()
                self.assertNotIn(private, str(caught.exception))

    def test_size_limit_is_bytes_not_unicode_characters(self):
        data = json.dumps({'v': 'ا' * (32 * 1024)}, ensure_ascii=False)
        self.assertLess(len(data), 64 * 1024)
        self.assertGreater(len(data.encode('utf-8')), 64 * 1024)
        self.path.write_text(data, encoding='utf-8')
        with self.assertRaises(PrivateConfigError): self.read()
