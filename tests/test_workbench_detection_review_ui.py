"""Run the real recovery renderer and its user actions without model calls."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DetectionReviewUITest(unittest.TestCase):
    def test_paused_review_can_be_dismissed_and_links_to_the_existing_question(self):
        script = r"""
const assert = require('node:assert/strict');
const src = require('node:fs').readFileSync(process.argv[1], 'utf8');
const element = (tag, className = '', textContent = '') => ({
  tag, className, textContent, dataset:{}, children:[], handlers:{},
  append(...children) {this.children.push(...children);},
  replaceChildren() {this.children=[];},
  addEventListener(event, handler) {this.handlers[event]=handler;},
  setAttribute(name, value) {this[name]=value;},
});
const fields = Object.fromEntries(['detection-recovery','detection-recovery-summary','detection-recovery-items']
  .map(id => [id, element('div')]));
const $ = id => fields[id];
const text = (node, value) => {node.textContent=value;};
const state = {meetingId:'m', pendingButtons:new Set()};
const requests = [], opened = [], notices = [];
let refreshed = 0, resolveRequest;
const api = (path, options) => {requests.push([path, options]); return new Promise(resolve => {resolveRequest=resolve;});};
const perform = (_, action) => action();
const audioTime = () => '39:18';
const locateDetectionTask = () => {};
const openQuestionDetail = id => opened.push(id);
const notice = value => notices.push(value);
const refreshNow = async () => {refreshed++; renderDetectionRecovery([], true);};
eval(src.slice(src.indexOf('  function renderDetectionRecovery('), src.indexOf('  async function locateDetectionTask(')));
const task = {id:'task', state:'needs_review', issues:[{code:'question_not_found'}],
  proposal_title:'合成材料范围', question_id:'question-fixture', question_title:'合成材料范围'};
const buttons = () => fields['detection-recovery-items'].children[0].children.filter(node => node.tag === 'button');
(async () => {
  renderDetectionRecovery([task], true);
  const row = fields['detection-recovery-items'].children[0];
  assert.match(row.children[0].children[0].textContent, /未能关联到本场已有问题/);
  assert.match(row.children[0].children[1].textContent, /合成材料范围/);
  const related = buttons().find(node => node.textContent === '查看相关问题');
  related.handlers.click();
  assert.deepEqual(opened, ['question-fixture']);
  assert.equal(buttons().find(node => node.id === 'retry-task').disabled, true);
  const dismiss = buttons().find(node => node.id === 'dismiss-task');
  assert.equal(dismiss.disabled, false, 'Pausing models must not prevent an explicit dismissal');
  const pending = dismiss.handlers.click();
  assert.equal(requests[0][0], '/live/v2/meetings/m/detection-tasks/task/dismiss');
  assert.deepEqual(requests[0][1], {method:'POST', body:{}});
  resolveRequest({state:'dismissed'});
  await pending;
  assert.equal(refreshed, 1);
  assert.equal(fields['detection-recovery'].hidden, true);
  assert.match(notices[0], /已忽略/);
  renderDetectionRecovery([task], false);
  const late = buttons().find(node => node.id === 'dismiss-task').handlers.click();
  state.meetingId = 'other';
  resolveRequest({state:'dismissed'});
  await late;
  assert.equal(refreshed, 1, 'A late dismissal must not refresh a different meeting');
  assert.equal(notices.length, 1);
  state.meetingId = 'm';
  const scan = {...task, id:'range', kind:'scan', question_id:'',
    source_refs:[{segment_id:'segment', revision:1}], proposal_titles:['合成预算', '合成工期'],
    batch_proposal_titles:['合成其他范围问题']};
  renderDetectionRecovery([scan], true);
  const description = fields['detection-recovery-items'].children[0].children[0].children;
  const descriptionText = description.map(node => node.textContent).join('\n');
  assert.match(descriptionText, /1 段发言/);
  assert.match(descriptionText, /整个范围/);
  assert.match(descriptionText, /合成预算/);
  assert.match(descriptionText, /合成工期/);
  assert.deepEqual(description.filter(node => node.textContent.startsWith('已知待处理问题：'))
    .map(node => node.textContent), ['已知待处理问题：合成预算', '已知待处理问题：合成工期']);
  assert.match(descriptionText, /同批待处理（可能涉及其他发言）：合成其他范围问题/);
  assert.match(descriptionText, /忽略本范围不一定会关闭这些同批问题/);
  const dismissRange = buttons().find(node => node.id === 'dismiss-range');
  assert.equal(dismissRange.textContent, '忽略这段补充识别');
  assert.match(dismissRange.title, /全部补充识别/);
  const rangePending = dismissRange.handlers.click();
  resolveRequest({state:'dismissed'});
  await rangePending;
  assert.match(notices.at(-1), /全部补充识别/);
})().catch(error => {console.error(error); process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'static/workbench.js')],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_failed_dismissal_after_poll_can_retry_and_notice_stays_in_its_meeting(self):
        script = r"""
const assert = require('node:assert/strict');
const src = require('node:fs').readFileSync(process.argv[1], 'utf8');
const element = (tag, className = '', textContent = '') => ({
  tag, className, textContent, dataset:{}, children:[], handlers:{},
  append(...children) {this.children.push(...children);},
  replaceChildren() {this.children=[];},
  addEventListener(event, handler) {this.handlers[event]=handler;},
});
const fields = Object.fromEntries(['detection-recovery','detection-recovery-summary','detection-recovery-items']
  .map(id => [id, element('div')]));
const $ = id => fields[id];
const text = (node, value) => {node.textContent=value;};
const state = {meetingId:'m', snapshot:{}, pendingButtons:new Set()};
const notices = [];
const notice = value => notices.push(value);
const audioTime = () => '00:00';
const locateDetectionTask = () => {}, openQuestionDetail = () => {};
let response, finishRefresh;
const api = () => new Promise((resolve, reject) => {response={resolve, reject};});
const refreshNow = () => new Promise(resolve => {finishRefresh=resolve;});
const task = {id:'task', kind:'correction', state:'needs_review', issues:[]};
let tasks = [task];
const renderSnapshot = () => renderDetectionRecovery(tasks, false);
eval(src.slice(src.indexOf('  function renderDetectionRecovery('), src.indexOf('  async function locateDetectionTask(')));
eval(src.slice(src.indexOf('  async function perform('), src.indexOf('  async function refreshNow(')));
const dismissButton = () => fields['detection-recovery-items'].children[0].children.find(node => node.id === 'dismiss-task');
(async () => {
  renderSnapshot();
  const original = dismissButton();
  const failed = original.handlers.click();
  tasks = [task, {...task, id:'sibling', state:'pending'}];
  renderSnapshot();
  assert.notEqual(dismissButton(), original, 'Polling replaced the row during the request');
  assert.equal(dismissButton().disabled, true);
  response.reject(new Error('合成临时连接失败'));
  await failed;
  assert.equal(dismissButton().disabled, false, 'The visible replacement must allow a retry');
  assert.equal(notices.length, 1);
  const retry = dismissButton().handlers.click();
  response.resolve({state:'dismissed'});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(typeof finishRefresh, 'function');
  state.meetingId = 'other';
  tasks = [];
  finishRefresh();
  await retry;
  assert.equal(notices.length, 1, 'Switching while the snapshot refreshes must suppress the old meeting notice');
})().catch(error => {console.error(error); process.exitCode=1;});
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'static/workbench.js')],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
