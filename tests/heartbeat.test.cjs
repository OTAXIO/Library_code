const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {test}=require('node:test');
const code=fs.readFileSync(require('node:path').join(__dirname,'../extension/content.js'),'utf8');
test('old extension context stops its timer without throwing on every tick',()=>{
  let tick,cleared=0;
  const sandbox={chrome:{runtime:{id:'test',sendMessage:()=>{throw Error('Extension context invalidated.');}}},
    setInterval:fn=>{tick=fn;return 17;},clearInterval:id=>{assert.equal(id,17);cleared++;}};
  vm.createContext(sandbox);
  assert.doesNotThrow(()=>vm.runInContext(code,sandbox));
  assert.equal(cleared,1);
  assert.equal(sandbox.__saAssistantHeartbeat,false);
});
test('pairing replaces the prior interval and retains only one active heartbeat',()=>{
  let timers=0,calls=0;const active=new Set();
  const sandbox={chrome:{runtime:{id:'test',sendMessage:()=>{calls++;return Promise.resolve();}}},
    setInterval:()=>{const id=++timers;active.add(id);return id;},clearInterval:id=>active.delete(id)};
  vm.createContext(sandbox);vm.runInContext(code,sandbox);vm.runInContext(code,sandbox);
  assert.equal(active.size,1);assert.equal(calls,2);
});
