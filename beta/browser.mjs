// Review-only beta. No payment authorization or final order-submission operation.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {setTimeout as delay} from 'node:timers/promises';
import {CDP} from './cdp.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ENDPOINT = 'http://127.0.0.1:9226';
const MODELS = {
  'MK1F4LL/A': {model:'iPhone Duo', color:'Night Sky', colorKey:'nightsky', slug:'iphone-duo'},
  'MK1E4LL/A': {model:'iPhone Duo', color:'Star White', colorKey:'starwhite', slug:'iphone-duo'},
  'MHRN4LL/A': {model:'iPhone 17e', color:'Black', colorKey:'black', slug:'iphone-17e', testOnly:true},
};
const emit = event => process.stdout.write(JSON.stringify(event) + '\n');
const progress = (stage,message) => emit({type:'progress',stage,message});

export function parseUSDCents(value) {
  if (typeof value !== 'string' || !/^\$(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2}$/.test(value.trim())) return null;
  const cents = Number(value.trim().slice(1).replaceAll(',','').replace('.',''));
  return Number.isSafeInteger(cents) && cents > 0 ? cents : null;
}

export function validatePlan(plan, stores) {
  if (!plan || typeof plan !== 'object' || Array.isArray(plan)) throw new Error('Invalid beta plan');
  const model = MODELS[plan.part];
  if (!model || typeof plan.is_test !== 'boolean' || Boolean(model.testOnly) !== plan.is_test) throw new Error('The beta part is not eligible for this mode');
  if (plan.mode !== 'review_only' || plan.quantity !== 1 || plan.applecare !== false || plan.max_total_cents !== 230000) throw new Error('Beta limits must be review-only, one phone, no AppleCare, and $2,300 including tax');
  if (plan.connection_mode && plan.connection_mode !== 'direct') throw new Error('This beta uses one direct Apple connection for the run');
  if (plan.storage !== '256GB' || plan.color !== model.color || plan.model !== model.model) throw new Error('The beta product configuration does not match its part number');
  const store = stores.find(s => s.id === plan.store_id);
  if (!store || store.name !== plan.store_name || store.zip !== plan.store_zip) throw new Error('The selected pickup store is not in the monitored catalog');
  if (typeof plan.run_id !== 'string' || !/^[a-zA-Z0-9_-]{1,100}$/.test(plan.run_id)) throw new Error('Invalid beta run identity');
  let url;
  try {url = new URL(plan.buy_url);} catch {throw new Error('Invalid Apple model link');}
  if (url.protocol !== 'https:' || url.hostname !== 'www.apple.com' || url.username || url.password || !url.pathname.startsWith('/shop/buy-iphone/' + model.slug + '/')) throw new Error('The beta model link is outside the selected Apple product');
  if (path.resolve(plan.profile_dir || '') !== path.join(ROOT,'.data','apple-beta-profile')) throw new Error('The beta requires its dedicated local Apple browser profile');
  if (typeof plan.chrome_path !== 'string' || path.basename(plan.chrome_path).toLowerCase() !== 'chrome.exe' || !path.isAbsolute(plan.chrome_path)) throw new Error('A local Chrome executable is required');
  return {...plan, product:model};
}

export function readOnePlan(input=process.stdin, timeout=15000) {
  const reader=createInterface({input,crlfDelay:Infinity});
  return new Promise((resolve,reject)=>{
    let received=false;
    const timer=setTimeout(()=>{reader.close();reject(new Error('Missing plan'));},timeout);
    reader.once('line',value=>{received=true;clearTimeout(timer);resolve(value);reader.close();input.pause();});
    reader.once('close',()=>{clearTimeout(timer);if(!received)reject(new Error('Missing plan'));});
  });
}

async function endpointReady() {
  try {const r=await fetch(ENDPOINT+'/json/version',{signal:AbortSignal.timeout(700)});return r.ok;} catch {return false;}
}

async function openBrowser(plan) {
  await fs.mkdir(plan.profile_dir,{recursive:true});
  if (!await endpointReady()) {
    await fs.access(plan.chrome_path);
    // This is an interactive window explicitly requested for Apple review/authentication.
    const browser=spawn(plan.chrome_path,['--remote-debugging-address=127.0.0.1','--remote-debugging-port=9226','--user-data-dir='+plan.profile_dir,'--no-proxy-server','--enable-automation','--no-first-run','--no-default-browser-check','about:blank'],{detached:true,windowsHide:false,stdio:'ignore'});
    await new Promise((resolve,reject)=>{browser.once('spawn',resolve);browser.once('error',()=>reject(new Error('Chrome could not start')));});
    browser.unref();
    const deadline=Date.now()+12000;
    while (!await endpointReady()) {if(Date.now()>deadline)throw new Error('The dedicated Chrome window did not become ready');await delay(200);}
  }
  const cdp=await CDP.connect(ENDPOINT);
  try {
    const commandLine=await cdp.call('Browser.getBrowserCommandLine');
    const profile=commandLine.arguments.find(arg=>arg.startsWith('--user-data-dir='));
    if (!profile || path.resolve(profile.slice('--user-data-dir='.length))!==path.resolve(plan.profile_dir) || !commandLine.arguments.includes('--no-proxy-server')) throw new Error('Port 9226 belongs to a different browser profile or connection mode');
    return cdp;
  } catch {cdp.close();throw new Error('Cannot verify the dedicated beta browser. Close another browser using port 9226 and retry after inspecting the Apple bag');}
}

async function bagIsEmpty(cdp) {
  await cdp.navigate('https://www.apple.com/shop/bag');
  return cdp.evaluate("[...document.querySelectorAll('h1')].some(h=>h.textContent.trim()==='Your bag is empty.')");
}

async function chooseObservedRadio(cdp,key) {
  const selector='input[data-autom='+JSON.stringify(key)+']';
  return cdp.evaluate(`(() => {const input=document.querySelector(${JSON.stringify(selector)});if(!input||input.disabled)return false;if(!input.checked)(input.labels?.[0]||input).click();return true;})()`);
}

async function productState(cdp,plan) {
  const keys={color:'dimensionColor'+plan.product.colorKey,storage:'dimensionCapacity256gb',trade:'choose-noTradeIn',payment:'purchaseGroupOptionfullprice',carrier:'carrierModelUNLOCKED/US',care:'noapplecare'};
  return cdp.evaluate(`(() => {
    const keys=${JSON.stringify(keys)};
    const checked=Object.fromEntries(Object.entries(keys).map(([name,key])=>[name,Boolean(document.querySelector('input[data-autom='+JSON.stringify(key)+']')?.checked)]));
    const parts=[...document.querySelectorAll('input[name="product"][type="hidden"]')].map(e=>e.value).filter(Boolean);
    const prices=[...document.querySelectorAll('[data-autom="purchaseGroupOptionfullprice_price"]')].map(e=>e.textContent.trim()).filter(Boolean);
    return {origin:location.origin,parts,checked,prices:[...new Set(prices)],addEnabled:Boolean(document.querySelector('button[data-autom="add-to-cart"]:not(:disabled)'))};
  })()`);
}

async function addOnce(cdp,plan) {
  // This is the observed public helper request. Its page token never leaves Apple.
  return cdp.evaluate(`(async () => {
    const part=${JSON.stringify(plan.part)};
    if(location.origin!=='https://www.apple.com')return {attempted:false,reason:'Wrong origin'};
    const token=decodeURIComponent((document.cookie.split('; ').find(c=>c.startsWith('as_atb='))||'').slice(7)).split('|')[2];
    if(!token)return {attempted:false,reason:'Apple session token is not available'};
    const params=new URLSearchParams({product:part,purchaseOption:'fullPrice',cppart:'UNLOCKED/US',step:'select',acpart:'none',bfil:'2',atbtoken:token,'add-to-cart':'add-to-cart'});
    const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),12000);
    try {
      const response=await fetch(${JSON.stringify('/shop/buy-iphone/'+plan.product.slug)}+'?'+params,{credentials:'include',signal:controller.signal});
      return {attempted:true,http_status:response.status,confirmed:response.ok&&new URL(response.url).searchParams.get('step')==='attach'};
    } catch {return {attempted:true,ambiguous:true};} finally {clearTimeout(timer);}
  })()`,16000);
}

export async function runBeta(plan) {
  let cdp;
  let stage='browser';
  try {
    progress(stage,'Opening the dedicated Apple beta browser for this one-phone run.');
    cdp=await openBrowser(plan);
    stage='bag_preflight';progress(stage,'Checking that the dedicated Apple bag is empty.');
    if (!await bagIsEmpty(cdp)) return {status:'needs_attention',stage,message:'The Apple bag is not confirmed empty. Inspect and clear it manually before resetting this run.'};
    stage='product';progress(stage,'Selecting the exact 256GB color, unlocked full-price payment, no trade-in, and no AppleCare.');
    await cdp.navigate(plan.buy_url);
    const keys=['dimensionColor'+plan.product.colorKey,'dimensionCapacity256gb','choose-noTradeIn','purchaseGroupOptionfullprice','carrierModelUNLOCKED/US','noapplecare'];
    for (const key of keys) {
      let selected=false;
      const deadline=Date.now()+8000;
      while (!selected && Date.now()<deadline) {selected=await chooseObservedRadio(cdp,key);if(!selected)await delay(200);}
      if(!selected)return {status:'needs_attention',stage,message:'An observed Apple product control is unavailable. Finish the configuration in the open browser.'};
      await delay(150);
    }
    let product;
    const deadline=Date.now()+8000;
    do {product=await productState(cdp,plan);if(Object.values(product.checked).every(Boolean)&&product.parts.includes(plan.part)&&product.addEnabled)break;await delay(200);} while(Date.now()<deadline);
    if(product.origin!=='https://www.apple.com'||!product.parts.includes(plan.part)||!Object.values(product.checked).every(Boolean)||!product.addEnabled)return {status:'needs_attention',stage,message:'Apple did not confirm every requested product selection. No bag addition was attempted.'};
    const cents=product.prices.length===1?parseUSDCents(product.prices[0]):null;
    if(cents===null)return {status:'blocked',stage:'price',message:'The selected full-price amount could not be verified. No bag addition was attempted.'};
    if(cents>plan.max_total_cents)return {status:'blocked',stage:'price',message:'The phone alone exceeds the $2,300 tax-inclusive limit. No bag addition was attempted.'};
    stage='adding';progress(stage,'Adding one selected phone once. Final tax, pickup store, and checkout still require verification.');
    const add=await addOnce(cdp,plan);
    stage='bag_reconciliation';progress(stage,'Inspecting the Apple bag after the single add attempt. No automatic retry will be made.');
    const empty=await bagIsEmpty(cdp);
    if(empty)return {status:'needs_attention',stage,message:add.http_status?`Apple did not add the phone (HTTP ${add.http_status}); the bag is empty. Inspect the browser before resetting.`:'Apple did not confirm a bag addition; the bag is empty. Inspect the browser before resetting.'};
    // No populated-bag/pickup/final-total selectors have been observed in this environment.
    // Fail closed here instead of manufacturing review facts or clicking checkout/payment.
    return {status:'needs_attention',stage:'pickup_review',message:`The bag needs manual review. Confirm exactly one ${plan.model}, choose Apple ${plan.store_name} for pickup, verify no AppleCare and the final tax-inclusive total. Automatic pickup and final review are unverified; this beta has not submitted an order.`};
  } catch {
    return {status:'needs_attention',stage,message:'The beta browser flow stopped. Inspect the open Apple bag before resetting; no add or submission will be retried automatically.'};
  } finally {cdp?.close();}
}

async function main() {
  let terminal=false;
  const finish=result=>{if(!terminal){terminal=true;emit({type:'result',...result});}};
  process.once('SIGINT',()=>{finish({status:'needs_attention',stage:'interrupted',message:'The run was interrupted. Inspect the Apple bag before resetting.'});process.exit(130);});
  process.once('SIGTERM',()=>{finish({status:'needs_attention',stage:'interrupted',message:'The run was interrupted. Inspect the Apple bag before resetting.'});process.exit(143);});
  try {
    const line=await readOnePlan();
    if(Buffer.byteLength(line)>65536)throw new Error('Invalid plan');
    const catalog=JSON.parse(await fs.readFile(path.join(ROOT,'catalog.json'),'utf8'));
    const plan=validatePlan(JSON.parse(line),catalog.stores);
    finish(await runBeta(plan));
  } catch {finish({status:'blocked',stage:'plan',message:'The beta plan is invalid or unavailable. No Apple action was attempted.'});}
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href===import.meta.url) await main();
