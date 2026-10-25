// node --test tests/test_em_economy.mjs  (assistant economy card and time labels; no DOM needed)
import test from 'node:test';import assert from 'node:assert/strict';
const m=await import('../src/em-economy.js');
test('economy card escapes every field and links only exact markets',()=>{
 const html=m.economyCard({kind:'economy',title:'<img src=x onerror=alert(1)>',market:'robinhood:0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec',symbol:'NVDA',network:'robinhood',asOf:1792300000,facts:[{label:'<b>x</b>',value:'"1"'}],note:'n'});
 assert.ok(!html.includes('<img src=x'));assert.ok(html.includes('&lt;img'));assert.ok(html.includes('panel=economy'));assert.ok(html.includes('robinhood%3A0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec'));
 const bad=m.economyCard({market:'javascript:alert(1)',facts:[]});assert.ok(!bad.includes('href="#/console/workspace/javascript'));
});
test('as-of labels are relative plus UTC, and honest when unknown',()=>{
 const t=Math.floor(Date.now()/1000)-7200;assert.match(m.ago(t),/^2 h ago · \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC$/);assert.equal(m.ago(null),'time unknown');
});
