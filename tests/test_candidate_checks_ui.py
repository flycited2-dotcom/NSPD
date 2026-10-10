"""Candidate progress renders frozen observations without creating legal clearance."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_candidate_check_renderer_escapes_evidence_and_preserves_old_and_frozen_snapshots():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the candidate progress UI regression')
    source = Path(__file__).resolve().parents[1] / 'web' / 'recon.js'
    harness = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const bounds=[34.202832,44.992064,34.207553,44.995402];
const escapeHtml=value=>String(value??'—').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
function layer(){return {addTo(){return this},setView(){return this}}}
// Keep persisted dates recognizable and exercise escaping even with malformed date text.
class TestDate{constructor(value){this.value=value}toLocaleString(){return this.value}}
const context=vm.createContext({el:()=>({checked:false}),boundsInput:()=>bounds,escapeHtml,areaFmt:String,Date:TestDate,
 L:{map:layer,featureGroup:layer},LandBasemap:{bind(){}},fetch(){throw Error('Rendering must not fetch sources')}});
const code=fs.readFileSync(process.argv[1],'utf8');
vm.runInContext(code.slice(0,code.indexOf('let reconWatch=[];')),context);
const candidate={id:'saved-candidate',kind:'draft',area_m2:1000,flags:['Старая особенность'],
 matches:{restrictions:[],pzz:[]},lots:[],check_progress:{algorithm:'candidate-checks-v1',
 coverage_confirmed:false,rights_confirmed:false,ready_to_submit:false,attention_keys:['restrictions'],
 next_actions:['Первое действие','Второе действие','Третье действие'],rows:[
 {key:'cadastre',title:'Кадастр',state:'observed',observation:'Наблюдалось 42 объекта',
 remaining:'Полнота не подтверждена',check_keys:['rights'],confirmed:false,
 evidence:[{label:'Кадастр НСПД',source:'https://nspd.gov.ru/public-source',received_at:'saved-date',sha256:'saved-hash',id:'source-id',state:'received',count:0}]},
 {key:'restrictions',title:'Ограничения',state:'attention',observation:'Есть пересечение',remaining:'Уточнить режим',evidence:[]},
 {key:'rights',title:'Права',state:'unknown',observation:'Сведения не получены',remaining:'Получить официальный ответ',evidence:[]}
 ]}};
const original=JSON.stringify(candidate);
const render=(c,stale=false)=>context.reconCheckProgress(c,stale);
let html=render(candidate);
assert.ok(html.includes('Что проверить следующим'));
assert.ok(html.includes('Первое действие')&&html.includes('Второе действие'));
assert.ok(!html.includes('Третье действие'),'Compact card should show only two next actions');
for(const text of ['Ход проверки контура','Получено','Что осталось проверить','Наблюдения получены',
 'Есть пересечения или особенности','Сведения не установлены','saved-date','saved-hash','source-id','объектов 0','Кадастр НСПД'])assert.ok(html.includes(text),text);
assert.ok(html.includes('готовность к подаче не подтверждены'));
assert.ok(!html.includes('class="green"')&&!html.includes('%')&&!html.includes('проверка пройдена</'));
assert.ok(render(candidate,true).includes('Это снимок прежнего расчёта'));
for(const old of [{}, {check_progress:{...candidate.check_progress,algorithm:'future-version'}}]){
 html=render(old,true);
 assert.ok(html.includes('В этой версии таблица проверок не рассчитывалась; повторите локальный расчёт'));
 assert.ok(html.includes('Это снимок прежнего расчёта'));assert.ok(!html.includes('saved-date'));
}
const poison='<img src=x onerror="alert(1)"><script>bad</script>';
const malicious=structuredClone(candidate);
malicious.check_progress.next_actions[0]=poison;
Object.assign(malicious.check_progress.rows[0],{title:poison,observation:poison,remaining:poison,state:'pass" onclick="alert(1)'});
malicious.check_progress.rows[0].evidence=[{label:poison,source:'javascript:'+poison,received_at:poison,sha256:poison,id:poison,state:poison,count:poison}];
html=render(malicious);
assert.ok(!html.includes('<img')&&!html.includes('<script>'));
assert.ok(html.includes('&lt;img')&&html.includes('&quot;alert(1)&quot;'));
assert.ok(!html.includes('href="javascript:'));
assert.ok(html.includes('recon-check-state unknown'),'An unexpected state must not become a passed check or a CSS attribute');
// Current result and watch snapshot deliberately have different sources/actions.
const current=structuredClone(candidate);current.check_progress.next_actions=['CURRENT ACTION'];
current.check_progress.rows[0].evidence[0].received_at='current-date';
context.latest={id:'current-result',bounds,sources:{pzz_context:{id:'current-pzz',applied:true,layer:{count:3,received_at:'current-pzz-date'}}}};
vm.runInContext('reconResult=latest;reconStale=false',context);
html=context.reconRows([{candidate,result_id:'saved-result',stale:true}],true);
assert.ok(html.includes('Первое действие')&&html.includes('saved-date'));
assert.ok(!html.includes('CURRENT ACTION')&&!html.includes('current-date')&&!html.includes('current-pzz-date'));
assert.ok(html.includes('Это снимок прежнего расчёта'));
assert.ok(html.includes('result_id=saved-result'));
assert.ok(html.includes('Старая особенность')&&html.includes('Совпадения и окружение'));
const old=structuredClone(candidate);delete old.check_progress;
html=context.reconRows([{candidate:old,result_id:'old-result',stale:true}],true);
assert.ok(html.includes('В этой версии таблица проверок не рассчитывалась'));
assert.ok(html.includes('Старая особенность'),'Older matching information must remain available');
html=context.reconRows([current]);
assert.ok(html.includes('CURRENT ACTION')&&html.includes('current-date'));
assert.ok(!html.includes('Это снимок прежнего расчёта'));
assert.equal(JSON.stringify(candidate),original,'Rendering must not rewrite the saved candidate evidence');
'''
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
    completed = subprocess.run([node, '-e', harness, str(source)], capture_output=True, text=True,
                               encoding='utf-8', timeout=20, **options)
    assert completed.returncode == 0, completed.stdout + completed.stderr
