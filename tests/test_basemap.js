'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../web/basemap.js'),'utf8');
function fixture(checked=false){
 const created=[],notices=[];
 const checkbox={checked,handlers:{},addEventListener(name,fn){this.handlers[name]=fn},change(value){this.checked=value;this.handlers.change()}};
 const vectors={kind:'cadastral data'};
 const map={layers:new Set([vectors]),removeLayer(layer){this.layers.delete(layer)},getContainer(){return {insertAdjacentElement(position,notice){notices.push(notice)}}}};
 const sandbox={document:{createElement(){return {setAttribute(){},textContent:''}}},L:{tileLayer(url,options){
  const layer={url,options,events:{},on(name,fn){this.events[name]=fn;return this},addTo(map){map.layers.add(this);return this},fire(name){this.events[name]?.()}};
  created.push(layer);return layer;
 }}};
 sandbox.window=sandbox;vm.runInNewContext(source,sandbox);
 const controller=sandbox.LandBasemap.bind(map,checkbox);
 return {map,checkbox,vectors,created,notice:notices[0],controller};
}
// No invisible default requests; vectors remain independently available.
const disabled=fixture();assert.equal(disabled.created.length,0);assert(disabled.map.layers.has(disabled.vectors));
// Browser tile images send only the true origin, with attribution and no prefetch buffer.
const f=fixture(true),first=f.created[0];
assert.equal(first.options.referrerPolicy,'origin');assert.equal(first.options.keepBuffer,0);
assert(first.options.attribution.includes('openstreetmap.org/copyright'));
first.fire('load');assert(f.notice.textContent.includes('загружена'));
// Failed tiles remove the background only and cannot clear the warning on late load.
first.fire('tileerror');assert.equal(f.checkbox.checked,false);assert(!f.map.layers.has(first));
assert(f.map.layers.has(f.vectors));assert(f.notice.textContent.includes('не загрузилась'));
first.fire('load');first.fire('tileerror');assert(f.notice.textContent.includes('не загрузилась'));assert.equal(f.created.length,1);
// Only explicit re-enabling starts a new layer; stale callbacks cannot remove it.
f.checkbox.change(true);const second=f.created[1];first.fire('tileerror');first.fire('load');
assert(f.map.layers.has(second));assert.equal(f.checkbox.checked,true);assert.equal(f.created.length,2);
second.fire('load');assert(f.notice.textContent.includes('загружена'));
f.controller.disable();assert.equal(f.checkbox.checked,false);assert(!f.map.layers.has(second));assert(f.map.layers.has(f.vectors));
second.fire('load');assert(f.notice.textContent.includes('отключена'));
console.log('Basemap behavior verified: origin policy, opt-in, error removal, preserved vectors, no retries, stale events');
