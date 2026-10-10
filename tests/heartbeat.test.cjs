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
test('same live context installs only one heartbeat',()=>{
  let timers=0,calls=0;
  const sandbox={chrome:{runtime:{id:'test',sendMessage:()=>{calls++;return Promise.resolve();}}},
    setInterval:()=>{timers++;return 17;},clearInterval:()=>assert.fail('live timer stopped')};
  vm.createContext(sandbox);vm.runInContext(code,sandbox);vm.runInContext(code,sandbox);
  assert.equal(timers,1);assert.equal(calls,1);
});
