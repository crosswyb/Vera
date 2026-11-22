"""Keep local workbench UI bindings, endpoint patterns and native host in sync."""

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
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

    def test_editing_custom_model_name_preserves_pending_key_clear(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const state = {clearModelKey: true};
const fields = {'service-model-name': {value:'saved'}, 'service-model-url': {value:'example'}, 'service-model-profile': {value:''}};
const $ = id => fields[id];
const saved = {};
const matchingModelProfile = (_, url, name) => name === 'saved' ? saved : null;
const modelProfiles = () => [saved];
const fn = src.slice(src.indexOf('  function syncCustomSelection()'), src.indexOf("  $('service-model-name').addEventListener('input'"));
eval(fn + '; globalThis.syncSelection = syncCustomSelection;');
for (const name of ['new', 'saved']) {
  fields['service-model-name'].value = name;
  syncSelection();
  assert.equal(state.clearModelKey, true);
  assert.equal(fields['service-model-profile'].value, name === 'saved' ? '0' : '');
}
"""
        subprocess.run(['node', '-e', script, str(ROOT / 'static/workbench.js')], check=True)

    def test_speech_consent_is_cleared_when_destination_or_source_changes(self):
        script = r"""
const assert = require('node:assert/strict');
globalThis.window = {};
require(process.argv[1]);
const nodes = new Map();
const $ = id => {
  if (!nodes.has(id)) nodes.set(id, {addEventListener() {}, checked:false});
  return nodes.get(id);
};
const saved = (revision, destination) => ({revision, speech: {live_provider:'dashscope', providers:{dashscope:{destinations:{live:destination}}}}});
const state = {token:'fixture', home:{member:{role:'owner'}}, serviceSettings:saved(1, 'wss://initial.example')};
let refreshed = saved(2, 'wss://initial.example');
const speech = window.createSpeechSettings({
  api: async (path) => { assert.equal(path, '/live/v2/settings'); return refreshed; },
  state, $, text: (node, value) => { node.textContent = value; },
  renderRuntime: () => speech.renderUpload('meeting', speech.scope('live', ['system'])),
});
(async () => {
  const oldScope = speech.scope('live', ['system']);
  speech.renderUpload('meeting', oldScope);
  $('meeting-upload-agree').checked = true;
  assert.equal((await speech.checkScope('live', ['system'], oldScope)).revision, 2);
  assert.equal($('meeting-upload-agree').checked, true);
  refreshed = saved(3, 'wss://changed.example');
  await assert.rejects(speech.checkScope('live', ['system'], oldScope), /接收地址已变化/);
  assert.equal($('meeting-upload-agree').checked, false);
  assert.match($('meeting-upload-scope').textContent, /changed.example/);
  $('meeting-upload-agree').checked = true;
  assert.equal(speech.renderUpload('meeting', speech.scope('live', ['system', 'microphone'])), false);
  assert.equal($('meeting-upload-agree').checked, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        subprocess.run(['node', '-e', script, str(ROOT / 'static/workbench-speech.js')], check=True)

    def test_native_release_does_not_confirm_live_capture_finish(self):
        script = r"""
const assert = require('node:assert/strict');
const src = require('node:fs').readFileSync(process.argv[1], 'utf8');
const state = {native:{meetingId:'meeting-active', inputKind:'live', canStop:true}};
const window = new EventTarget();
const requests = [];
const nativePost = (action, meeting) => requests.push([action, meeting]);
const emit = detail => window.dispatchEvent(new CustomEvent('zsjh-native-state', {detail}));
const source = src.slice(src.indexOf('  async function stopCaptureAndWait()'), src.indexOf('  function calendarDate('));
eval(source + '; globalThis.waitForCapture = stopCaptureAndWait;');
(async () => {
  let completed = false;
  const waiting = waitForCapture().then(result => { completed = true; return result; });
  assert.deepEqual(requests, [['stopCapture', 'meeting-active']]);
  emit({meetingId:null});
  emit({meetingId:null, captureFinishMeetingId:'another-meeting', captureFinish:'complete'});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(completed, false);
  emit({meetingId:null, captureFinishMeetingId:'meeting-active', captureFinish:'with_gaps'});
  assert.equal(await waiting, 'with_gaps');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        subprocess.run(['node', '-e', script, str(ROOT / 'static/workbench.js')], check=True)

    def test_browser_keeps_native_command_and_event_contract(self):
        swift = (ROOT / 'native/Sources/DesktopHost/DesktopHost.swift').read_text()
        commands = set(re.findall(r"nativePost\('([A-Za-z]+)'", self.js))
        required = set('checkModel chooseAudioFile chooseOutputDirectory importAudioFile installModel openMeetingAudio openMicrophoneSettings openOutput requestState setAudioSource setThemePreference setLayoutPreference startCapture stopCapture'.split())
        self.assertFalse(required - commands)
        for command in commands:
            self.assertIn('"' + command + '"', swift)
        for marker in ('zsjh-native-state', 'zsjh-storage-changed', 'wb_access'):
            self.assertIn(marker, self.js)
            self.assertIn(marker, swift)
        for marker in ('themePreference', '__ZSJH_THEME_PREFERENCE__', 'layoutPreference', '__ZSJH_LAYOUT_PREFERENCE__'):
            self.assertIn(marker, self.js)
            self.assertIn(marker, swift)

    def test_browser_retains_server_backed_operations(self):
        calls = re.findall(r"api\([`'](/live/v2/[^`']+)[`']", self.js)
        paths = {re.sub(r'\$\{[^}]+\}', '{}', call.partition('?')[0]) for call in calls}
        required = {'/live/v2/' + p for p in ('home', 'meetings', 'settings', 'storage', 'settings/test/{}', 'artifacts/{}', 'questions/{}', 'questions/{}/actions')}
        required.add('/live/v2/meetings/{}')
        required |= {'/live/v2/meetings/{}/' + p for p in ('snapshot', 'segments', 'transcript', 'utterances', 'questions', 'permissions', 'controls', 'demo', 'deletion', 'summary', 'files/{}', 'files/retry')}
        self.assertFalse(required - paths)

    def test_storage_refresh_ignores_stale_sessions_and_reloads_after_native_change(self):
        """Exercise actual async loader: a picker event cannot be lost behind a GET."""
        script = r"""
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync(process.argv[1], 'utf8');
const loader = source.slice(source.indexOf('  async function loadStorageSettings('), source.indexOf('  function providerMessage('));
const state = {token:'owner', home:{}, storageRequest:null, storageSettings:null, storageError:'', storageRefreshPending:false};
const requests=[];
const api = path => new Promise((resolve,reject) => { assert.equal(path,'/live/v2/storage'); requests.push({resolve,reject}); });
const renderStorageSettings=()=>{};
eval(loader + '; globalThis.load = loadStorageSettings;');
(async()=>{
  const first=load();
  await load(true);
  assert.equal(requests.length,1);
  requests[0].resolve({output_root:'/old'});
  await first;
  assert.equal(requests.length,2);
  requests[1].resolve({output_root:'/new'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(state.storageSettings.output_root,'/new');
  const oldSession=load(true);
  state.token='new-owner'; state.storageRequest=null; state.storageSettings=null;
  const newSession=load(true);
  requests[2].resolve({output_root:'/stale'});
  await oldSession;
  assert.equal(state.storageSettings,null);
  requests[3].resolve({output_root:'/new-session'});
  await newSession;
  assert.equal(state.storageSettings.output_root,'/new-session');
  const failed=load(true);
  requests[4].reject(new Error('synthetic unavailable'));
  await failed;
  assert.match(state.storageError,/synthetic unavailable/);
  assert.equal(state.storageSettings.output_root,'/new-session');
  assert.equal(state.storageRequest,null);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'static/workbench.js')],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_open_files_folder_targets_the_current_meeting_even_before_files_exist(self):
        panel = (ROOT / 'templates/workbench.html').read_text().split('id="meeting-files-panel"', 1)[1].split('</section>', 1)[0]
        self.assertIn('id="open-files-folder"', panel)
        self.assertIn("$('open-files-folder').addEventListener('click', openMeetingFolder)", self.js)
        script = r"""
const assert = require('node:assert/strict');
const src = require('node:fs').readFileSync(process.argv[1], 'utf8');
const state = {meetingId:'first', snapshot:{meeting:{id:'first', directory:'first-dir', state:'active'}}};
const posts = [];
const window = {webkit:{messageHandlers:{workbench:{postMessage:msg => posts.push(msg)}}}};
const handlers = src.slice(src.indexOf('  function nativeAvailable()'), src.indexOf('  async function stopCaptureAndWait('));
eval(handlers + '; globalThis.click = openMeetingFolder;');
click();
state.meetingId = 'second';
click(); // Previous snapshot has not been replaced yet.
state.snapshot.meeting = {id:'second', directory:'second-dir', state:'ended'};
click();
state.snapshot.meeting.directory = '';
click();
state.snapshot = null;
click();
state.snapshot = {meeting:{id:'second', directory:'second-dir', state:'active'}};
delete window.webkit;
click();
assert.deepEqual(posts, [{action:'openOutput',meetingId:'first'},{action:'openOutput',meetingId:'second'}]);
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'static/workbench.js')],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unavailable_recording_reports_storage_without_claiming_audio_loss(self):
        script = r"""
const assert=require('node:assert/strict');
const source=require('node:fs').readFileSync(process.argv[1],'utf8');
const render=source.slice(source.indexOf('  function renderMeetingFiles() {'),source.indexOf('  function renderMeetingMarkdown('));
const element=(tag,cls='',value='')=>({tag,dataset:{},textContent:value,children:[],append(...nodes){this.children.push(...nodes);},prepend(...nodes){this.children.unshift(...nodes);},replaceChildren(...nodes){this.children=nodes;},addEventListener(){}});
const nodes=new Map();const $=id=>{if(!nodes.has(id))nodes.set(id,element('div'));return nodes.get(id);};
const text=(node,value)=>node.textContent=value;const readable=value=>value;const nativeAvailable=()=>false;
const state={snapshot:{meeting:{id:'offline',state:'ended',recordings:[{id:'audio',source:'microphone',state:'unavailable',storage_error:'原保存目录不可用，录音是否完整尚未核实；请连接原磁盘后重开应用。'}]}},home:{}};
eval(render+';renderMeetingFiles();');
const collect=node=>[node.textContent,...node.children.map(collect)].join(' ');
assert.equal($('archive-status').textContent,'保存位置不可用');
assert.match($('archive-guidance').textContent,/录音是否完整尚未核实/);
assert.match(collect($('meeting-files-list')),/请连接原磁盘后重开应用/);
assert.doesNotMatch(collect($('meeting-files-list')),/本场没有已保存的录音|录音尚未保存/);
state.snapshot.meeting.recordings[0]={id:'audio',source:'microphone',state:'missing',frames_received:0};
eval(render+';renderMeetingFiles();');
assert.equal($('archive-status').textContent,'录音不完整');
assert.match(collect($('meeting-files-list')),/未收到音频数据，未生成录音文件/);
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'static/workbench.js')],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_post_meeting_scan_progress_and_retry_controls(self):
        """Exercise the actual renderer with a minimal DOM and synthetic snapshots."""
        script = r"""
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync(process.argv[1], 'utf8');
const render = source.slice(source.indexOf('  function renderSnapshot() {'), source.indexOf('  let filePreviewRequest'));
const nodes = new Map();
const $ = id => {
  if (!nodes.has(id)) nodes.set(id, {dataset:{}, hidden:false, disabled:false, textContent:'', setAttribute(){}});
  return nodes.get(id);
};
const text = (node, value) => node.textContent = value;
const state = {meetingId:'test', route:'meeting', pendingButtons:new Set(), snapshot:{
  meeting:{id:'test', title:'Synthetic', state:'ended', research_paused:true, coordinator_state:'idle'},
  segments:[], questions:[], automation_progress:{processed:12, pending:3, held_questions:0}
}};
const meetingEnded = () => state.snapshot.meeting.state !== 'active';
const researchAvailable = () => ['active','ended'].includes(state.snapshot.meeting.state);
const meetingStates = {ended:'会议已结束'};
const activeStates = new Set(['queued','running','paused']);
const coordinatorIssue = meeting => meeting.coordinator_error_message || '';
const calendarDate = () => '', time = () => '', retracted = () => false;
const renderCapabilities=()=>{}, renderCapture=()=>{}, renderTranscripts=()=>{}, renderQuestions=()=>{}, renderDetail=()=>{}, renderActivity=()=>{}, renderMeetingFiles=()=>{}, renderDetectionRecovery=()=>{};
eval(render + '\nrenderSnapshot();');
assert.equal($('research-control').disabled, false);
assert.match($('automation-hint').textContent, /3 段转写待分析/);
assert.equal($('automation-hint').hidden, false);
assert.match($('automation-progress').textContent, /已分析 12 段 · 待分析 3 段/);
state.snapshot.meeting.coordinator_error_message='Synthetic connection failure';
state.snapshot.meeting.coordinator_state='failed';
eval(render + '\nrenderSnapshot();');
assert.equal($('coordinator-warning').hidden, false);
assert.equal($('retry-coordinator').hidden, false);
assert.equal($('retry-coordinator').disabled, true);
state.snapshot.meeting.research_paused=false;
eval(render + '\nrenderSnapshot();');
assert.equal($('retry-coordinator').disabled, false);
assert.match($('automation-status').textContent, /待重试/);
state.snapshot.meeting.state='active';
eval(render + '\nrenderSnapshot();');
assert.equal($('retry-coordinator').disabled, false);
state.snapshot.meeting.state='deleting';
eval(render + '\nrenderSnapshot();');
assert.equal($('research-control').disabled, true);
assert.equal($('retry-coordinator').disabled, true);
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'static/workbench.js')],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
