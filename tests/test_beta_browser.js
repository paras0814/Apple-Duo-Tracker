const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {Readable} = require('node:stream');
const root=path.resolve(__dirname,'..');
const store={id:'R654',name:'World Trade Center',zip:'10007'};
const base={run_id:'fixture_run',part:'MK1F4LL/A',store_id:store.id,store_name:store.name,store_zip:store.zip,model:'iPhone Duo',storage:'256GB',color:'Night Sky',buy_url:'https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-night-sky-unlocked',max_total_cents:230000,quantity:1,applecare:false,mode:'review_only',is_test:false,profile_dir:path.join(root,'.data','apple-beta-profile'),chrome_path:'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'};
test('money parsing accepts exact full USD prices and rejects installment/unknown formats',async()=>{
 const {parseUSDCents}=await import('../beta/browser.mjs');
 assert.equal(parseUSDCents('$1,999.00'),199900);
 assert.equal(parseUSDCents('$699.00'),69900);
 for(const value of ['$83.29/mo.','from $1999','CA$1,999.00','$1,999.001','$0.00','unknown'])assert.equal(parseUSDCents(value),null);
});
test('plan validation binds exact part/configuration, store, limits and dedicated profile',async()=>{
 const {validatePlan}=await import('../beta/browser.mjs');
 assert.equal(validatePlan(base,[store]).product.colorKey,'nightsky');
 for(const change of [{quantity:2},{applecare:true},{mode:'purchase'},{max_total_cents:230001},{color:'Star White'},{storage:'512GB'},{store_id:'R999'},{profile_dir:path.join(root,'.data','another-profile')},{buy_url:'https://example.com/shop/buy-iphone/iphone-duo/a'},{part:'MHRN4LL/A',is_test:false}])assert.throws(()=>validatePlan({...base,...change},[store]));
});
test('invalid stdin plan emits one blocked result without opening Chrome',()=>{
 const result=spawnSync(process.execPath,[path.join(root,'beta','browser.mjs')],{input:JSON.stringify({...base,quantity:2})+'\n',encoding:'utf8',timeout:5000});
 assert.equal(result.status,0);
 const lines=result.stdout.trim().split('\n').map(JSON.parse);
 assert.equal(lines.length,1);
 assert.equal(lines[0].type,'result');
 assert.equal(lines[0].status,'blocked');
 assert.equal(lines[0].stage,'plan');
});
test('newline plan input survives reader closure and does not consume a second plan',async()=>{
 const {readOnePlan}=await import('../beta/browser.mjs');
 assert.equal(await readOnePlan(Readable.from(['{"first":true}\n{"second":true}\n']),1000),'{"first":true}');
 await assert.rejects(readOnePlan(Readable.from([]),1000));
});
test('native cart submission makes only one attempt when navigation is uncertain',async()=>{
 const {addOnce}=await import('../beta/browser.mjs');
 const attempts=[];
 const result=await addOnce({click:async selector=>{attempts.push(selector);throw Error('connection lost after release');},evaluate:()=>{throw Error('must not reconstruct requests or read cookies');}});
 assert.deepEqual(attempts,['button[data-autom="add-to-cart"]']);
 assert.deepEqual(result,{attempted:true,ambiguous:true});
});
test('missing or covered native cart button cannot dispatch input',async()=>{
 const {CDP}=await import('../beta/cdp.mjs');
 let dispatched=0;
 const c=Object.create(CDP.prototype);
 c.evaluate=async()=>null;c.call=async()=>{dispatched++;};
 await assert.rejects(c.click('button[data-autom="add-to-cart"]'),/missing, disabled, ambiguous or covered/);
 assert.equal(dispatched,0);
});
