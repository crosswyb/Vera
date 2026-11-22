"""Layout changes preserve work and persist through either storage mechanism."""

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WorkbenchLayoutTests(unittest.TestCase):
    def test_visual_preferences_survive_storage_failure_and_respect_native_defaults(self):
        script = r"""
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync(process.argv[1], 'utf8');
const code = source.slice(source.indexOf("  const layoutValues ="), source.indexOf('  const state ='));
function boot({nativePreference, saved, unavailable=false, native=false}={}) {
  let width=1440,stored=saved;
  const nodes = Object.fromEntries(['work-grid','questions-panel','results-panel','layout-preference','layout-status'].map(id=>[id,{dataset:{},hidden:false,value:'',textContent:''}]));
  nodes['work-grid'].dataset.mobilePanel='results';
  const buttons=['a','b'].map(layout=>({dataset:{layout},setAttribute(key,value){this[key]=value;}}));
  const document={documentElement:{dataset:{}},querySelectorAll:()=>buttons};
  const window={__ZSJH_LAYOUT_PREFERENCE__:nativePreference,matchMedia:()=>({matches:width>=1201}),addEventListener(){}};
  const localStorage={getItem(){if(unavailable)throw Error('denied');return stored;},setItem(_,v){if(unavailable)throw Error('denied');stored=v;}};
  const posts=[],$=id=>nodes[id],text=(node,value)=>node.textContent=value;
  const wideMeeting=()=>width>=1101,nativeAvailable=()=>native,nativePost=(...args)=>posts.push(args);
  let apply;
  eval(code+';apply=applyLayout;');
  apply();
  return {nodes,document,buttons,posts,apply,stored:()=>stored,resize(w){width=w;apply();}};
}
// Native preference wins over browser storage, which changes with desktop ports.
let app=boot({nativePreference:'b',saved:'a',native:true});
assert.equal(app.document.documentElement.dataset.layout,'b');
assert.equal(app.nodes['work-grid'].dataset.layoutMode,'triple');
assert.equal(app.nodes['questions-panel'].hidden,false);
assert.equal(app.nodes['results-panel'].hidden,false);
const grid=app.nodes['work-grid'];
grid.draft='未提交的讨论';grid.selectedQuestion='synthetic-q2';grid.scrollTop=123;
app.apply('a',true);
assert.equal(app.nodes['work-grid'],grid);
assert.equal(grid.draft,'未提交的讨论');assert.equal(grid.selectedQuestion,'synthetic-q2');assert.equal(grid.scrollTop,123);
assert.equal(grid.dataset.layoutMode,'reading');
assert.equal(app.stored(),'a');
assert.deepEqual(app.posts,[['setLayoutPreference',null,null,null,null,'a']]);
assert.equal(app.buttons[0]['aria-pressed'],'true');assert.equal(app.buttons[1]['aria-pressed'],'false');
app.apply('b',true);app.resize(1000);
assert.equal(grid.dataset.layoutMode,'single');assert.equal(app.nodes['questions-panel'].hidden,true);
assert.equal(app.document.documentElement.dataset.layout,'b');
app.resize(1440);assert.equal(grid.dataset.layoutMode,'triple');
// Storage failure must never stop the live meeting or disable switching.
app=boot({unavailable:true,native:true});
assert.equal(app.document.documentElement.dataset.layout,'a');
app.apply('b',true);assert.equal(app.document.documentElement.dataset.layout,'b');
assert.equal(app.posts[0][0],'setLayoutPreference');
app=boot({saved:'invalid'});assert.equal(app.document.documentElement.dataset.layout,'a');
app=boot({saved:'b'});assert.equal(app.document.documentElement.dataset.layout,'b');
"""
        result = subprocess.run(['node', '-e', script, str(ROOT / 'static/workbench.js')],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
