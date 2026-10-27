// Browser fixture only: never connects to Apple or places an order.
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../web/bookmarklet.js'),'utf8');
async function run(overrides={}) {
  const calls=[],alerts=[];
  const location={hostname:'www.apple.com',pathname:'/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-night-sky-unlocked',search:'',hash:'#MK1F4LL/A',href:'',...overrides.location};
  const banner={style:{},remove(){this.removed=true;}};
  const document={cookie:overrides.cookie ?? 'as_atb=one%7Ctwo%7Ctest_token',querySelectorAll:()=>[],createElement:()=>banner,body:{appendChild(){}}};
  const fetch=async(url,options)=>{calls.push({url,options});return overrides.response ?? {ok:true,url:'https://www.apple.com/shop/buy-iphone/iphone-duo?step=attach'};};
  await vm.runInNewContext(source,{document,location,fetch,alert:x=>alerts.push(x),URL,URLSearchParams,setTimeout:fn=>{fn();return 1;}});
  return {calls,alerts,location,banner};
}
test('only exact256GB part, full price, unlocked, and noAppleCare go to Apple bag',async()=>{
  const r=await run();assert.equal(r.calls.length,1);
  const url=new URL(r.calls[0].url,'https://www.apple.com');
  assert.equal(url.searchParams.get('product'),'MK1F4LL/A');
  assert.equal(url.searchParams.get('purchaseOption'),'fullPrice');
  assert.equal(url.searchParams.get('cppart'),'UNLOCKED/US');
  assert.equal(url.searchParams.get('acpart'),'none');
  assert.equal(r.calls[0].options.credentials,'include');
  assert.equal(r.location.href,'/shop/bag');
});
test('StarWhite part is supported',async()=>{const r=await run({location:{hash:'#MK1E4LL/A'}});assert.match(r.calls[0].url,/product=MK1E4LL/);});
test('part can be resolved from exactmodel path',async()=>{const r=await run({location:{hash:''}});assert.equal(r.calls.length,1);});
test('a foreign host cannot run cart requests',async()=>{const r=await run({location:{hostname:'example.com'}});assert.equal(r.calls.length,0);assert.equal(r.alerts.length,1);});
test('512GB is refused',async()=>{const r=await run({location:{hash:'#MK1H4LL/A'}});assert.equal(r.calls.length,0);});
test('no sessiontoken means no request',async()=>{const r=await run({cookie:''});assert.equal(r.calls.length,0);assert.match(r.alerts[0],/not finished loading/);});
test('openbag gives checkout handoff without adding again',async()=>{const r=await run({location:{pathname:'/shop/bag'}});assert.equal(r.calls.length,0);});
test('unconfirmed response does not navigate or place an order',async()=>{const r=await run({response:{ok:true,url:'https://www.apple.com/shop/buy-iphone/iphone-duo'}});assert.equal(r.location.href,'');assert.match(r.alerts[0],/did not confirm/);});
