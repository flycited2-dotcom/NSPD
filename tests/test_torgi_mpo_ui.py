import shutil
import subprocess
from pathlib import Path

import pytest


def test_mpo_ui_names_primary_frame_and_never_displays_other_frame_ocr():
    node=shutil.which('node')
    if not node:pytest.skip('Node required for actual renderer')
    source=Path(__file__).resolve().parents[1]/'web'/'torgi_documents.js'
    script=r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const text=fs.readFileSync(process.argv[1],'utf8');
const start=text.indexOf('function torgiImageInfo('),end=text.indexOf('function torgiFileEvidence(');
const escapeHtml=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const context=vm.createContext({escapeHtml});vm.runInContext(text.slice(start,end),context);
const scope={preview_algorithm:'torgi-image-preview-v2',source_format:'MPO',source_frame_count:2,
 preview_frame_index:0,processed_frame_count:1,unprocessed_frame_count:1,complete_frame:true,complete_source:false};
context.file={key:'photo',sha256:'a'.repeat(64),unit:'image',image:{...scope,source_width:4032,source_height:3024},
 visual_ocr:{source_sha256:'a'.repeat(64),pages:[{page:1,state:'received',processed_at:'date',image_scope:{...scope},
  mentions:[{cadastral_number:'90:12:172101:420',agreement:'both'}]}]}};
const image=vm.runInContext('torgiImageInfo(file)',context);
for(const expected of ['основной кадр 1','заявлено кадров: 2','Остальные 1 кадр(а) не прочитаны','OCR основного кадра выполняется отдельно'])assert.ok(image.includes(expected),expected);
let html=vm.runInContext('torgiOcrInfo(file)',context);
assert.ok(html.includes('Основной кадр 1')&&html.includes('90:12:172101:420'));
context.file.visual_ocr.pages[0].image_scope.preview_frame_index=1;
html=vm.runInContext('torgiOcrInfo(file)',context);
assert.ok(html.includes('не связан с текущим основным кадром MPO'));
assert.ok(!html.includes('90:12:172101:420')&&!html.includes('view=0'));
context.file.visual_ocr.pages[0].image_scope.preview_frame_index=false;
assert.ok(vm.runInContext('torgiOcrInfo(file)',context).includes('не связан'));
context.file.image.source_format='JPEG';
assert.ok(vm.runInContext('torgiImageInfo(file)',context).includes('один полный кадр'));
'''
    result=subprocess.run([node,'-e',script,str(source)],capture_output=True,encoding='utf-8',timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
