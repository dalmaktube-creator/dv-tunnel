import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('menu_manager',Path(__file__).resolve().parents[1]/'tools/dvctl.py')
M=importlib.util.module_from_spec(spec);spec.loader.exec_module(M)
ANSI=re.compile(r'\x1b\[[0-9;]*[mHJ]')


class MenuTests(unittest.TestCase):
    def render(self,width,color=False,unicode=False,rows=None):
        out=io.StringIO();ui=M.TerminalUI(stream=out,width=width,color=color,unicode=unicode)
        with patch.object(M,'pointer',side_effect=lambda key='current':Path('/release/v0.1.0-preview.2') if key=='current' else None),patch.object(M.platform,'node',return_value='tr-vps'):
            M.render_dashboard(ui,rows or [])
        return out.getvalue()

    def test_colored_dashboard_geometry(self):
        text=self.render(78,color=True,unicode=True)
        self.assertIn('\x1b[',text)
        plain=ANSI.sub('',text)
        self.assertIn('D V   T U N N E L',plain)
        self.assertIn('v0.1.0-preview.2',plain)
        self.assertIn('No managed tunnels yet',plain)
        self.assertTrue(all(len(line)==78 for line in plain.splitlines()))
        self.assertLessEqual(len(plain.splitlines()),23)

    def test_narrow_and_ascii_terminals(self):
        for width in (24,40,60,78):
            text=self.render(width)
            self.assertTrue(text.isascii())
            self.assertNotIn('\x1b',text)
            self.assertTrue(all(len(line)==width for line in text.splitlines()),width)

    def test_pipe_no_color_and_dumb_terminal(self):
        class TTY(io.StringIO):
            def isatty(self):return True
        with patch.dict(os.environ,{'TERM':'xterm-256color','NO_COLOR':''},clear=True):
            self.assertFalse(M.TerminalUI(TTY()).color)
        with patch.dict(os.environ,{'TERM':'dumb'},clear=True):
            self.assertFalse(M.TerminalUI(TTY()).color)
        with patch.dict(os.environ,{'TERM':'xterm-256color'},clear=True):
            self.assertFalse(M.TerminalUI(io.StringIO()).color)
            self.assertTrue(M.TerminalUI(TTY()).color)

    def test_terminal_control_injection_is_sanitized(self):
        out=io.StringIO();ui=M.TerminalUI(out,width=40,color=False)
        ui.line('node\x1b[2J\n\r\x07name')
        text=out.getvalue()
        self.assertNotIn('\x1b',text);self.assertNotIn('\x07',text)
        self.assertEqual(len(text.splitlines()),1)

    def test_stale_or_inactive_stats_never_look_connected(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(M,'STATE',Path(tmp)),patch.object(M,'instances',return_value=['main']):
            d=Path(tmp)/'main';d.mkdir();p=d/'stats.json'
            p.write_text(json.dumps(dict(connected=1,drops=3)))
            os.utime(p,(1,1))
            with patch.object(M,'active',return_value=True):self.assertEqual(M.instance_rows()[0]['state'],'STALE / UNKNOWN')
            p.write_text(json.dumps(dict(connected=1,drops=3)))
            with patch.object(M,'active',return_value=False):self.assertEqual(M.instance_rows()[0]['state'],'INACTIVE')
            with patch.object(M,'active',return_value=True):self.assertEqual(M.instance_rows()[0]['state'],'CONNECTED')

    def test_cancelled_update_keeps_confirmation_guard(self):
        out=io.StringIO()
        with patch.object(M,'pointer',return_value=None),patch.object(M,'instance_rows',return_value=[dict(name='main',running=True,state='CONNECTED',tone='ok',drops=0)]),patch.object(M,'instances',return_value=['main']),patch.object(M,'active',return_value=True),patch.object(M,'install') as install,patch('builtins.input',side_effect=['3','NO','0']),contextlib.redirect_stdout(out):
            M.menu()
            install.assert_not_called()
        self.assertIn('Cancelled. No change was made.',out.getvalue())

    def test_missing_instance_never_starts_any_service(self):
        with patch.object(M,'pointer',return_value=None),patch.object(M,'instance_rows',return_value=[]),patch.object(M,'service_action') as action,patch('builtins.input',side_effect=['5','0']),contextlib.redirect_stdout(io.StringIO()):
            M.menu();action.assert_not_called()

    def test_confirmed_update_exits_old_menu(self):
        current=[Path('/releases/v0.1.0-preview.1')]
        def installed(**kwargs):current[0]=Path('/releases/v0.1.0-preview.2')
        with patch.object(M,'pointer',side_effect=lambda key='current':current[0] if key=='current' else None),patch.object(M,'instance_rows',return_value=[]),patch.object(M,'instances',return_value=[]),patch.object(M,'lock',side_effect=contextlib.nullcontext),patch.object(M,'install',side_effect=installed) as install,patch('builtins.input',side_effect=['3']),contextlib.redirect_stdout(io.StringIO()):
            M.menu();install.assert_called_once_with(restart=False)

    def test_eof_exits_without_service_changes(self):
        with patch.object(M,'pointer',return_value=None),patch.object(M,'instance_rows',return_value=[]),patch.object(M,'service_action') as action,patch('builtins.input',side_effect=EOFError),contextlib.redirect_stdout(io.StringIO()):
            M.menu();action.assert_not_called()
