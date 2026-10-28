import {setTimeout as delay} from 'node:timers/promises';

export class CDP {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 0;
    this.pending = new Map();
    ws.addEventListener('message', ({data}) => {
      let message;
      try { message = JSON.parse(data); } catch { return; }
      const pending = this.pending.get(message.id);
      if (!pending) return;
      clearTimeout(pending.timer);
      this.pending.delete(message.id);
      message.error ? pending.reject(new Error('Browser command failed: ' + pending.method)) : pending.resolve(message.result);
    });
    ws.addEventListener('close', () => {
      for (const pending of this.pending.values()) {clearTimeout(pending.timer);pending.reject(new Error('Browser disconnected'));}
      this.pending.clear();
    });
  }
  static async connect(endpoint, targetId) {
    const response = await fetch(endpoint + '/json/list', {signal:AbortSignal.timeout(3000)});
    const targets = await response.json();
    const page = targetId ? targets.find(t => t.id === targetId && t.type === 'page') : targets.find(t => t.type === 'page');
    if (!page) throw new Error('No owned browser page is available');
    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve,reject) => {
      const timer = setTimeout(() => {ws.close();reject(new Error('Browser connection timed out'));},5000);
      ws.addEventListener('open', () => {clearTimeout(timer);resolve();}, {once:true});
      ws.addEventListener('error', () => {clearTimeout(timer);reject(new Error('Browser connection failed'));}, {once:true});
    });
    const connection = new CDP(ws);
    await connection.call('Page.enable');
    await connection.call('Runtime.enable');
    return connection;
  }
  call(method, params = {}, timeout = 20000) {
    return new Promise((resolve,reject) => {
      const id = ++this.nextId;
      const timer = setTimeout(() => {this.pending.delete(id);reject(new Error('Browser command timed out: ' + method));},timeout);
      this.pending.set(id,{resolve,reject,timer,method});
      this.ws.send(JSON.stringify({id,method,params}));
    });
  }
  async evaluate(expression, timeout = 20000) {
    const result = await this.call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true,userGesture:true},timeout);
    if (result.exceptionDetails) throw new Error('Apple page operation failed');
    return result.result?.value;
  }
  async waitFor(expression, timeout = 15000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      try {const value = await this.evaluate(expression,Math.min(3000,Math.max(1000,deadline-Date.now())));if(value)return value;} catch {}
      await delay(200);
    }
    throw new Error('Apple page did not reach the expected state');
  }
  async navigate(url) {
    const result = await this.call('Page.navigate',{url});
    if (result.errorText) throw new Error('Apple navigation failed');
    const destination=new URL(url);
    await this.waitFor(`document.readyState === 'complete' && location.origin === ${JSON.stringify(destination.origin)} && location.pathname === ${JSON.stringify(destination.pathname)}`,20000);
  }
  async click(selector) {
    const point=await this.evaluate(`(() => {
      const matches=[...document.querySelectorAll(${JSON.stringify(selector)})].filter(e=>e.getClientRects().length&&!e.disabled);
      if(matches.length!==1)return null;
      const e=matches[0];e.scrollIntoView({block:'center',inline:'center',behavior:'instant'});
      const r=e.getBoundingClientRect();const x=r.left+r.width/2,y=r.top+r.height/2;
      const hit=document.elementFromPoint(x,y);
      return hit&&(hit===e||e.contains(hit))?{x,y}:null;
    })()`);
    if(!point || !Number.isFinite(point.x) || !Number.isFinite(point.y))throw new Error('The Apple button is missing, disabled, ambiguous or covered');
    await this.call('Input.dispatchMouseEvent',{type:'mouseMoved',...point});
    await this.call('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});
    // A release may navigate. Never retry it after an uncertain response.
    await this.call('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});
  }
  close() {this.ws.close();}
}
