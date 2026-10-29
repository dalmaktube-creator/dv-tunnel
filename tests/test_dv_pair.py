"""Field-pair generation without root, server access or network changes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dv_pair", ROOT/"tools/dv_pair.py")
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)


class FieldPairTests(unittest.TestCase):
    def test_all_topologies_and_variants(self):
        tokens = set()
        with tempfile.TemporaryDirectory() as tmp:
            for topology in ("direct", "reverse"):
                for variant in F.VARIANTS:
                    root = Path(tmp)/(topology+"-"+variant)
                    summary = F.generate(root, "198.51.100.10", "10.254.253.0/30", topology, variant)
                    configs = [json.loads((root/side/"config.json").read_text()) for side in ("node-a", "node-b")]
                    self.assertEqual([c["role"] for c in configs], ["ingress", "egress"])
                    self.assertEqual([c["allowed_sources"] for c in configs], [["10.254.253.2/32"], ["10.254.253.1/32"]])
                    self.assertEqual(configs[0]["token"], configs[1]["token"])
                    self.assertNotIn(configs[0]["token"], tokens)
                    tokens.add(configs[0]["token"])
                    self.assertEqual(len(tokens), 1 + (0 if topology == "direct" else 4) + F.VARIANTS.index(variant))
                    self.assertEqual(summary["scope"].split(";")[0], "point-to-point IPv4 TUN only")
                    for side, c in zip(("node-a", "node-b"), configs):
                        listener = side == ("node-b" if topology == "direct" else "node-a")
                        self.assertEqual(c["mode"], "listen" if listener else "dial")
                        self.assertNotIn("udp", c)
                        self.assertEqual("key_pem" in c, listener)
                        self.assertEqual(c["tun_offload"], "offload" in variant)
                        self.assertEqual(c["batch"], variant == "batch")
                        self.assertNotIn("queue_packets", c)
                        self.assertEqual(c.get("queue_bytes", 0), 262144 if variant == "offload-lowlat" else 0)
                        if not listener:
                            self.assertIn(c["path"], c["url"])
                            self.assertNotIn("PRIVATE KEY", json.dumps(c))
                            with tempfile.NamedTemporaryFile(mode="w") as cert:
                                cert.write(c["ca_pem"]); cert.flush()
                                p = subprocess.run(["openssl", "verify", "-CAfile", cert.name,
                                    "-verify_hostname", c["server_name"], cert.name], capture_output=True)
                                self.assertEqual(p.returncode, 0, p.stderr)
                        script = root/side/"run.sh"
                        self.assertEqual(subprocess.run(["bash", "-n", str(script)]).returncode, 0)
                        text = script.read_text()
                        self.assertNotIn("__", text)
                        self.assertNotIn("ip route add default", text)
                        self.assertNotIn("iptables", text)
                        self.assertNotIn("sysctl", text.split("set -euo pipefail", 1)[1])
                        self.assertNotIn("ip link delete", text)
                        self.assertIn("refusing to reuse", text)
                        self.assertIn("trap cleanup EXIT", text)
                        for path in (root/side/"config.json", script):
                            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                        self.assertEqual((root/side).stat().st_mode & 0o777, 0o700)
                    self.assertEqual(root.stat().st_mode & 0o777, 0o700)

    @unittest.skipUnless(os.environ.get("DV_CHECK_BINARY"), "compiled core check is enabled in CI")
    def test_configs_accepted_by_real_core(self):
        binary = os.environ["DV_CHECK_BINARY"]
        with tempfile.TemporaryDirectory() as tmp:
            for topology in ("direct", "reverse"):
                for variant in F.VARIANTS:
                    root = Path(tmp)/(topology+"-"+variant)
                    F.generate(root, "198.51.100.10", "10.254.253.0/30", topology, variant)
                    for side in ("node-a", "node-b"):
                        p = subprocess.run([binary, "--config", str(root/side/"config.json"), "--check"],
                                           capture_output=True, timeout=10)
                        self.assertEqual(p.returncode, 0, "%s/%s/%s rejected" % (topology, variant, side))

    def test_explicit_packet_cap_and_legacy_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            for cap in (0, 1, 64, 128, 256):
                root = Path(tmp)/str(cap)
                summary = F.generate(root, "198.51.100.10", "10.254.253.0/30", queue_packets=cap)
                self.assertEqual(summary["queue_packets"], cap or 64)
                for side in ("node-a", "node-b"):
                    config = json.loads((root/side/"config.json").read_text())
                    self.assertEqual(config.get("queue_packets", 0), cap)
                    self.assertEqual(config["queue_bytes"], 262144)
                    if os.environ.get("DV_CHECK_BINARY"):
                        p = subprocess.run([os.environ["DV_CHECK_BINARY"], "--config", str(root/side/"config.json"), "--check"], capture_output=True, timeout=10)
                        self.assertEqual(p.returncode, 0, "explicit packet cap rejected")

    def test_rejects_bad_inputs_without_output(self):
        cases = [dict(listen_host=x) for x in ("https://example.com", "1.2.3.4;reboot", "::1", "127.0.0.1", "0.0.0.0", "224.0.0.1", "169.254.1.2", "10.254.253.1")]
        cases += [dict(subnet=x) for x in ("0.0.0.0/0", "10.1.2.0/24", "10.1.2.1/30", "198.18.1.0/30", "fd00::/126")]
        cases += [dict(port=x) for x in (0, 443, 65536, True)]
        cases += [dict(iface=x) for x in ("awg0", "../../etc", "wft0;id", "wft1000")]
        cases += [dict(topology="bad"), dict(variant="bad")]
        cases += [dict(queue_packets=x) for x in (-1, 257, True, "128")]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/"new"
            for overrides in cases:
                kwargs = dict(out=out, listen_host="198.51.100.10", subnet="10.254.253.0/30")
                kwargs.update(overrides)
                with self.assertRaises(ValueError):
                    F.generate(**kwargs)
                self.assertFalse(out.exists())

    def test_exclusive_output_and_cert_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/"pair"
            out.mkdir()
            (out/"keep").write_text("owned")
            with self.assertRaises(FileExistsError):
                F.generate(out, "198.51.100.10", "10.254.253.0/30")
            self.assertEqual((out/"keep").read_text(), "owned")
            target = Path(tmp)/"target"
            link = Path(tmp)/"link"
            link.symlink_to(target)
            with self.assertRaises(FileExistsError):
                F.generate(link, "198.51.100.10", "10.254.253.0/30")
            with patch.object(F.subprocess, "run", side_effect=subprocess.TimeoutExpired("openssl", 20)):
                with self.assertRaises(subprocess.TimeoutExpired):
                    F.generate(target, "198.51.100.10", "10.254.253.0/30")
            self.assertFalse(target.exists())

    def test_preflight_conflicts_and_clean_path(self):
        def preflight(routes, addresses, mode="dial"):
            def output(argv):
                self.assertEqual(argv[:2], ["ip", "-j"])
                return json.dumps(routes if "route" in argv else addresses).encode()
            with patch("subprocess.check_output", side_effect=output), patch("sys.argv", ["-", "10.254.253.0/30", mode, "42443"]):
                exec(compile(F.PREFLIGHT, "preflight", "exec"), {})
        preflight([dict(dst="default"), dict(dst="192.168.1.0/24")], [])
        for route in ("10.0.0.0/8", "10.254.253.0/30", "10.254.253.1/32"):
            with self.assertRaises(SystemExit):
                preflight([dict(dst=route)], [])
        with self.assertRaises(SystemExit):
            preflight([], [dict(addr_info=[dict(local="10.254.253.2", prefixlen=32)])])
        with patch("socket.socket") as sock:
            sock.return_value.__enter__.return_value.bind.side_effect = OSError("busy")
            with self.assertRaises(OSError):
                preflight([], [], "listen")

    @unittest.skipUnless(os.environ.get('DV_CHECK_BINARY'),'compiled core required')
    def test_split_queue_cli_override_validates_without_rewriting_config(self):
        binary=os.environ['DV_CHECK_BINARY']
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'pair';F.generate(root,'198.51.100.10','10.254.253.0/30',variant='offload-lowlat')
            p=root/'node-a/config.json';before=p.read_bytes()
            for n,split,expected in [(256,True,0),(64,True,0),(1,True,1),(-1,True,1),(257,True,1),(256,False,1),(0,False,0)]:
                args=[binary,'--config',str(p),'--check','--queue-packets',str(n)]+(['--split-udp'] if split else [])
                result=subprocess.run(args,capture_output=True,timeout=10)
                self.assertEqual(result.returncode==0,expected==0,(n,split,result.stderr))
                self.assertEqual(p.read_bytes(),before)

    @unittest.skipUnless(os.environ.get('DV_CHECK_BINARY'),'compiled core required')
    def test_udp_wait_cli_is_bounded_split_only_and_private_preserving(self):
        binary=os.environ['DV_CHECK_BINARY']
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'pair';F.generate(root,'198.51.100.10','10.254.253.0/30',variant='offload-lowlat')
            p=root/'node-a/config.json';before=p.read_bytes()
            for n,split,ok in [(200,True,True),(1,True,True),(0,False,True),(-1,True,False),(201,True,False),(200,False,False)]:
                result=subprocess.run([binary,'--config',str(p),'--check','--udp-wait-ms',str(n)]+(['--split-udp'] if split else []),capture_output=True,timeout=10)
                self.assertEqual(result.returncode==0,ok,(n,split,result.stderr));self.assertEqual(p.read_bytes(),before)


if __name__ == "__main__":
    unittest.main()
