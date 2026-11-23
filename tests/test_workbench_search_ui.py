"""Regression checks for meeting search focus and concurrent history navigation."""

from html.parser import HTMLParser
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SearchUIRegressionTests(unittest.TestCase):
    def run_javascript(self, script):
        result = subprocess.run(
            ["node", "-e", script, str(ROOT / "static/workbench.js")],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_escape_restores_a_real_focus_target_and_tab_dismisses_results(self):
        self.run_javascript(r"""
const assert = require('node:assert/strict');
const src = require('node:fs').readFileSync(process.argv[1], 'utf8');
const focusEvents = new Map();
const listenForFocus = (target, type, handler) => focusEvents.set(target + ':' + type, handler);
const document = {activeElement: null, addEventListener: (type, handler) => listenForFocus('document', type, handler),
  querySelector: () => ({addEventListener: (type, handler) => listenForFocus('input-wrapper', type, handler)})};
const node = id => ({id, isConnected:true, closest(){return this.inside ? this : null;},
  focus(){document.activeElement = this;}, blur(){document.activeElement = document.body;},
  setAttribute(){}});
document.body = node('body'); document.body.focus = () => {};
document.documentElement = node('html'); document.documentElement.focus = () => {};
const fields = {main:node('main'), 'library-search':node('library-search'), 'meeting-search-popover':node('panel')};
const $ = id => fields[id];
const input = $('library-search'), panel = $('meeting-search-popover');
input.inside = panel.inside = true;
panel.open = true; panel.matches = () => panel.open; panel.hidePopover = () => {panel.open=false;};
panel.contains = candidate => candidate === panel;
panel.addEventListener = (type, handler) => listenForFocus('panel', type, handler);
const meetingSearch = {sequence:0, previousFocus:null};
const code = src.slice(src.indexOf('  function closeMeetingSearch('), src.indexOf('  function positionMeetingSearch('));
const focusListeners = src.slice(src.indexOf("  $('library-search').addEventListener('focus'"),
  src.indexOf("  $('library-search').addEventListener('keydown'"));
input.addEventListener = (type, handler) => listenForFocus('input', type, handler);
eval(code + focusListeners + ';globalThis.closeSearch = closeMeetingSearch;');
(async () => {
  const oldButton = node('prior-button');
  const detached = {...node('detached'), isConnected:false};
  const hidden = {...node('hidden'), focus() {}};
  for (const previous of [document.body, document.documentElement, null, detached, hidden, input]) {
    document.activeElement=input; panel.open=true; meetingSearch.previousFocus=previous;
    closeSearch(true);
    assert.equal(panel.open, false);
    assert.equal(document.activeElement, $('main'), `Focus must leave search for ${previous?.id}`);
  }
  document.activeElement=input; panel.open=true; meetingSearch.previousFocus=oldButton;
  closeSearch(true);
  assert.equal(document.activeElement, oldButton);
  panel.open=true; document.activeElement=document.body;
  // macOS WKWebView blurs the input without focusing the clicked button.
  focusEvents.get('input-wrapper:focusout')?.({relatedTarget:null});
  await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(panel.open, true, 'Keep results available between mouse-down and click');
  document.activeElement=panel;
  focusEvents.get('document:focusin')?.({target:panel});
  assert.equal(panel.open, true, 'Moving keyboard focus within search must retain results');
  document.activeElement=oldButton;
  focusEvents.get('document:focusin')?.({target:oldButton});
  assert.equal(panel.open, false, 'Tabbing outside must dismiss results');
})().catch(error => {console.error(error); process.exitCode=1;});
""")

    def test_late_search_response_cannot_replace_newer_history_navigation(self):
        self.run_javascript(r"""
const assert = require('node:assert/strict');
const src = require('node:fs').readFileSync(process.argv[1], 'utf8');
const state = {route:'meeting', meetingId:'m', transcriptNavigationSequence:0,
  transcriptHistory:null, snapshot:{segments:[], transcript_page:{revision_epoch:1}}};
const fields = {'follow-transcript':{checked:true}, 'transcript-list':{children:[]}};
const $ = id => fields[id];
let resolvePage;
const api = () => new Promise(resolve => {resolvePage=resolve;});
const setMeetingTab = () => {}, notice = () => {}, renderTranscripts = () => {};
const code = src.slice(src.indexOf('  async function locateSearchSegment('), src.indexOf('  async function runMeetingSearch('));
eval(code + ';globalThis.locate = locateSearchSegment;');
(async () => {
  const pending = locate({segment_id:'old', event_seq:4}, 'm');
  const newer = {rows:new Map([['manual-history', {segment_id:'manual-history'}]])};
  state.transcriptHistory=newer;
  state.transcriptNavigationSequence += 1;
  resolvePage({revision_epoch:1, segments:[{segment_id:'old'}]});
  await pending;
  assert.equal(state.transcriptHistory, newer, 'A later history click must win');
  state.transcriptHistory=null;
  const returning = locate({segment_id:'old', event_seq:4}, 'm');
  state.transcriptNavigationSequence += 1;
  resolvePage({revision_epoch:1, segments:[{segment_id:'old'}]});
  await returning;
  assert.equal(state.transcriptHistory, null, 'Return to latest must cancel pending location');
})().catch(error => {console.error(error); process.exitCode=1;});
""")

    def test_returning_to_latest_resumes_following(self):
        self.run_javascript(r"""
const assert = require('node:assert/strict');
const src = require('node:fs').readFileSync(process.argv[1], 'utf8');
const state={transcriptHistory:{}, transcriptNavigationSequence:0, snapshot:{segments:[]}};
const fields={'latest-transcript':{addEventListener:(_, handler)=>{globalThis.backToLatest=handler;}},
 'follow-transcript':{checked:false}, 'transcript-list':{focus(){}}};
const $=id=>fields[id], renderTranscripts=()=>{}, viewport={scrollTop:0, scrollHeight:100};
const transcriptViewport=()=>viewport;
const start=src.indexOf("  $('latest-transcript').addEventListener('click'");
eval(src.slice(start, src.indexOf("  $('transcript-list').addEventListener('click'", start)));
backToLatest();
assert.equal(state.transcriptHistory, null);
assert.equal($('follow-transcript').checked, true);
assert.equal(state.transcriptNavigationSequence, 1);
assert.equal(viewport.scrollTop, viewport.scrollHeight);
""")

    def test_switching_panels_cancels_pending_search_location(self):
        self.run_javascript(r"""
const assert = require('node:assert/strict');
const src = require('node:fs').readFileSync(process.argv[1], 'utf8');
const state = {route:'meeting', meetingId:'m', transcriptNavigationSequence:0,
  transcriptHistory:null, snapshot:{segments:[], transcript_page:{revision_epoch:1}}};
const fields = {'follow-transcript':{checked:true}, 'transcript-list':{children:[]},
  'work-grid':{dataset:{leftPane:'transcript', mobilePanel:'transcript'}},
  'back-questions-label':{}, 'meeting-files-panel':{}, 'meeting-files-heading':{}};
const $ = id => fields[id], text = () => {}, highlightQuestionNote = () => {},
  syncMeetingPanels = () => {}, notice = () => {}, renderTranscripts = () => {},
  revealAnswer = () => {}, document = {querySelectorAll:() => []},
  history = {replaceState() {}}, meetingHash = () => '#meeting/m';
let filePreviewRequest = 0, resolvePage;
const api = () => new Promise(resolve => {resolvePage=resolve;});
eval(src.slice(src.indexOf('  function setMeetingTab('), src.indexOf('  function setDetailTab('))
  + ';globalThis.switchPanel = setMeetingTab;');
eval(src.slice(src.indexOf('  async function locateSearchSegment('), src.indexOf('  async function runMeetingSearch('))
  + ';globalThis.locate = locateSearchSegment;');
(async () => {
  for (const panel of ['files', 'results']) {
    fields['work-grid'].dataset.mobilePanel = 'transcript';
    const pending = locate({segment_id:'old', event_seq:4}, 'm');
    switchPanel(panel, false);
    resolvePage({revision_epoch:1, segments:[{segment_id:'old'}]});
    await pending;
    assert.equal(state.transcriptHistory, null, `Switching to ${panel} must cancel location`);
    assert.equal(fields['work-grid'].dataset.mobilePanel, panel);
  }
})().catch(error => {console.error(error); process.exitCode=1;});
""")

    def test_searchbox_uses_supported_aria(self):
        class SearchDocument(HTMLParser):
            field = None

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if attrs.get("id") == "library-search":
                    self.field = attrs

        document = SearchDocument()
        document.feed((ROOT / "templates/workbench.html").read_text())
        self.assertEqual(document.field["type"], "search")
        # Search results navigate to content; they do not select an input value.
        self.assertNotIn("aria-expanded", document.field)
        self.assertNotIn("role", document.field)
        self.assertEqual(document.field["aria-controls"], "meeting-search-popover")


if __name__ == "__main__":
    unittest.main()
