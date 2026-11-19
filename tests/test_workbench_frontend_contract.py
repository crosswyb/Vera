"""Keep local workbench UI bindings, endpoint patterns and native host in sync."""

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))


class WorkbenchFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = Document((ROOT / 'templates/workbench.html').read_text())
        cls.ids = Counter(attrs['id'] for _, attrs in cls.doc.nodes if 'id' in attrs)
        scripts = [a['src'] for t, a in cls.doc.nodes if t == 'script' and 'src' in a]
        cls.js = '\n'.join((ROOT / src.lstrip('/')).read_text() for src in scripts)

    def test_control_bindings_and_dialog_targets_exist_once(self):
        self.assertFalse([key for key, n in self.ids.items() if n != 1])
        bound = set(re.findall(r"\$\('([\w-]+)'\)", self.js))
        self.assertIn('access-code', bound)
        self.assertFalse(bound - self.ids.keys())
        for _, attrs in self.doc.nodes:
            for name in ('for', 'aria-controls', 'aria-labelledby', 'aria-describedby', 'data-close-dialog'):
                for target in (attrs.get(name) or '').split():
                    self.assertIn(target, self.ids, f'{name}={target}')
            if attrs.get('href', '').startswith('#i-'):
                self.assertIn(attrs['href'][1:], self.ids)

    def test_dedicated_operation_forms_remain_reachable(self):
        forms = {a['id'] for t, a in self.doc.nodes if t == 'form' and 'id' in a}
        required = set('login-form transcript-form service-settings-form meeting-form import-form permissions-form question-form search-query-form rename-meeting-form delete-meeting-form end-form switch-form'.split())
        self.assertFalse(required - forms)
        for form in required:
            self.assertRegex(self.js, rf"\$\('{form}'\)\.addEventListener\('submit'")

    def test_browser_keeps_native_command_and_event_contract(self):
        swift = (ROOT / 'native/Sources/DesktopHost/DesktopHost.swift').read_text()
        commands = set(re.findall(r"nativePost\('([A-Za-z]+)'", self.js))
        required = set('checkModel chooseAudioFile importAudioFile installModel openMeetingAudio openMicrophoneSettings openOutput requestState setAudioSource setThemePreference startCapture stopCapture'.split())
        self.assertFalse(required - commands)
        for command in commands:
            self.assertIn('"' + command + '"', swift)
        for marker in ('zsjh-native-state', 'wb_access'):
            self.assertIn(marker, self.js)
            self.assertIn(marker, swift)
        for marker in ('themePreference', '__ZSJH_THEME_PREFERENCE__'):
            self.assertIn(marker, self.js)
            self.assertIn(marker, swift)

    def test_browser_retains_server_backed_operations(self):
        calls = re.findall(r"api\([`'](/live/v2/[^`']+)[`']", self.js)
        paths = {re.sub(r'\$\{[^}]+\}', '{}', call.partition('?')[0]) for call in calls}
        required = {'/live/v2/' + p for p in ('home', 'meetings', 'settings', 'settings/test/{}', 'artifacts/{}', 'questions/{}', 'questions/{}/actions')}
        required.add('/live/v2/meetings/{}')
        required |= {'/live/v2/meetings/{}/' + p for p in ('snapshot', 'segments', 'transcript', 'utterances', 'questions', 'permissions', 'controls', 'demo', 'deletion', 'summary', 'files/{}', 'files/retry')}
        self.assertFalse(required - paths)


if __name__ == '__main__':
    unittest.main()
