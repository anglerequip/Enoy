"use strict";
const assert=require('assert'), fs=require('fs'), vm=require('vm');
const source=fs.readFileSync('enoy/ui/app.js','utf8');
let opens=0, transitions=0, renders=0, reduce=false;
const busy=new Map();
const ctx={S:{project:{id:1,is_default:false},chats:[]},
  mode:()=>ctx.S.project?.is_default?'chat':'code',
  codeProjects:()=>[{id:1,is_default:false}],lsGet:()=>1,
  useProject:async()=>{ctx.S.project={id:1,is_default:false};},
  useDefaultProject:async()=>{ctx.S.project={id:2,is_default:true};},
  renderAll:()=>renders++,setView(){},closeSidebarIfNarrow(){},
  openRegularChatGPT:()=>opens++,regularChat:()=>true,
  $:id=>id==='prompt'?null:{setAttribute:(k,v)=>busy.set(k,v),removeAttribute:k=>busy.delete(k)},
  matchMedia:()=>({matches:reduce}),
  document:{documentElement:{dataset:{}},startViewTransition:fn=>{
    transitions++; const update=Promise.resolve().then(fn);
    return {ready:Promise.resolve(),updateCallbackDone:update,finished:update};
  }},
};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('async function switchWorkspace('),source.indexOf('async function loadChats(')),ctx);
const newChat=source.slice(source.indexOf('  "new-chat":'),source.indexOf('  "team-toggle":'));
vm.runInContext('this.newChat=({'+newChat+'})["new-chat"];',ctx);
(async()=>{
  await ctx.switchWorkspace('chat'); assert.equal(ctx.mode(),'chat');
  await ctx.switchWorkspace('chat'); assert.equal(transitions,1);
  ctx.newChat(); assert.equal(opens,0); assert.equal(ctx.S.chat,null);
  await ctx.switchWorkspace('code'); assert.equal(ctx.mode(),'code'); assert.equal(transitions,2);
  reduce=true; await ctx.switchWorkspace('chat'); assert.equal(transitions,2);
  assert.equal(opens,0); assert.equal(ctx.S.switchingMode,false); assert(!busy.has('aria-busy'));
  ctx.S.project={id:1,is_default:false}; ctx.useDefaultProject=async()=>{throw Error('Offline');};
  await assert.rejects(ctx.switchWorkspace('chat'),/Offline/);
  assert.equal(ctx.S.switchingMode,false); assert(!busy.has('aria-busy'));
  assert(renders>=3);
  console.log('Mode switching and New chat never open the browser; transitions respect reduced motion and recover from errors');
})().catch(e=>{console.error(e);process.exitCode=1;});
