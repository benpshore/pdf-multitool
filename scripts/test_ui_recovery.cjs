const fs=require('fs'),vm=require('vm'),assert=require('assert');
const root=require('path').join(__dirname,'..','src','pdf_multitool','static')+'/';
class El {
 constructor(tag){this.tagName=tag.toLowerCase();this.children=[];this.attrs={};this.listeners={};this.className='';this.hidden=false;this.disabled=false;this.checked=false;this._value='';this.textContent='';this.classList={add:c=>{this.className+=' '+c},remove:c=>{this.className=this.className.split(' ').filter(x=>x!==c).join(' ')},toggle:(c,on)=>{this.classList.remove(c);if(on)this.classList.add(c)}}}
 append(...els){for(const e of els){this.children.push(e);e.parent=this}}
 replaceChildren(...els){this.children=[];this.append(...els)}
 setAttribute(k,v){this.attrs[k]=String(v);if(k==='id')this.id=v;if(k==='class')this.className=v;if(k==='value')this._value=v;if(k==='type')this.type=v;if(k==='disabled')this.disabled=true;if(k==='hidden')this.hidden=true;if(k==='selected')this.selected=true}
 getAttribute(k){return this.attrs[k]}
 addEventListener(k,fn){(this.listeners[k]??=[]).push(fn)}
 async fire(k,extra={}){for(const f of this.listeners[k]||[])await f({target:this,stopPropagation(){},preventDefault(){},...extra})}
 click(){return this.fire('click')}
 focus(){} select(){} setSelectionRange(){} scrollIntoView(){} remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this)}
 get options(){return this.children.filter(c=>c.tagName==='option')}
 get value(){if(this.tagName==='select')return this._value==='__missing__'?'':this._value||this.options.find(o=>o.selected)?.value||this.options[0]?.value||'';return this._value}
 set value(v){this._value=String(v);if(this.tagName==='select'&&!this.options.some(o=>o.value===this._value))this._value='__missing__'}
 get selectedOptions(){return this.options.filter(o=>o.value===this.value)}
 querySelectorAll(selector){let out=[];const match=e=>selector.includes(',')?selector.split(',').includes(e.tagName):selector[0]==='.'?e.className.split(' ').includes(selector.slice(1)):e.tagName===selector;for(const c of this.children){if(match(c))out.push(c);out.push(...c.querySelectorAll(selector))}return out}
}
const rootEl=new El('document'),stack=[rootEl],ids={};
for(const token of fs.readFileSync(root+'index.html','utf8').match(/<[^>]+>|[^<]+/g)){
 if(token.startsWith('</')){if(stack.length>1)stack.pop();continue}if(token.startsWith('<!'))continue;
 if(token.startsWith('<')){const m=token.match(/^<([\w-]+)/);if(!m)continue;const el=new El(m[1]);for(const a of token.slice(m[0].length,-1).matchAll(/([\w-]+)(?:="([^"]*)")?/g))el.setAttribute(a[1],a[2]??'');stack.at(-1).append(el);if(el.id)ids[el.id]=el;if(!['input','meta','link','img','br','hr'].includes(el.tagName))stack.push(el)}else stack.at(-1).textContent+=token.trim();
}
// Mock DOM/API tests only: no browser or rendering claim.
const missingSource = process.argv.includes('--missing-source');
const legacyBundle = process.argv.includes('--legacy-bundle');
const saved={id:'b1',operation:'bundle',textMode:'view',bundleSourceId:'j1'};
if (legacyBundle) delete saved.bundleSourceId;
const storage=new Map([['pdf-workbench-token','fixture-token'],['pdf-workbench-job',JSON.stringify(saved)]]), posts=[];
const doc={documentElement:rootEl.querySelectorAll('html')[0],body:rootEl.querySelectorAll('body')[0],getElementById:id=>ids[id],createElement:tag=>new El(tag),addEventListener(){},execCommand(){return true}};doc.documentElement.dataset={};
const timers=[];let href='http://localhost:8765/',bundlePolls=0,holdJob=false;
const win={get location(){return new URL(href)},matchMedia:()=>({matches:false}),setTimeout:fn=>{timers.push(fn)},open:()=>null};
const json=o=>new Response(JSON.stringify(o),{status:200,headers:{'Content-Type':'application/json'}});
const source={id:'j1',state:'completed',progress:1,results:[{id:'md',name:'paper.md',url:'/api/results/md'}]};
const sandbox={window:win,document:doc,history:{replaceState:(_,__,url)=>{href=new URL(url,href).href}},sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},localStorage:{getItem(){},setItem(){}},navigator:{},Headers,URL,URLSearchParams,TextEncoder,Blob,Response,Set,Map,console,setTimeout:()=>{},fetch:async(url,opts)=>{
 const path=new URL(url).pathname;
 assert.equal(opts.headers.get('X-PDF-Token'),'fixture-token');
 if(path==='/api/status')return json({engines:{pymupdf:{available:true},poppler:{available:true}},limits:{}});
 if(path==='/api/files')return json({files:[{id:'f1',name:'paper.pdf',size:100}]});
 if(path==='/api/jobs/j1')return missingSource ? new Response(JSON.stringify({error:'Gone'}),{status:404}) : json(source);
 if(path==='/api/jobs/b1'){bundlePolls++;return json({id:'b1',state:bundlePolls===1?'running':'completed',progress:bundlePolls===1?.5:1,results:bundlePolls===1?[]:[{id:'zip',name:'results.zip',url:'/api/results/zip'}]})}
 if(path==='/api/bundles'){posts.push(JSON.parse(opts.body));bundlePolls=0;return json({id:'b1'})}
 if(path==='/api/jobs'&&opts.method==='POST'){posts.push(JSON.parse(opts.body));return json({id:'j2'})}
 if(path==='/api/jobs/j2')return json({id:'j2',state:holdJob?'running':'completed',progress:holdJob?.5:1,results:[]});
 throw Error('Unexpected '+path);
}};
vm.runInNewContext(fs.readFileSync(root+'app.js','utf8'),sandbox);
const wait=async(fn)=>{for(let i=0;i<100;i++){if(fn())return;await new Promise(r=>setTimeout(r,3))}throw Error('Condition timed out: '+ids.notice.textContent)};
(async()=>{
 await wait(()=>timers.length||ids.notice.textContent.includes('Could not connect'));
 assert(!ids.notice.textContent.includes('Could not connect'),ids.notice.textContent);
 assert.equal(bundlePolls,1,'Restored bundle must reconnect');
 await timers.shift()();await wait(()=>!ids.operation.disabled);
 if (missingSource || legacyBundle) {
   assert.equal(ids.results.children.length,1,'Existing package remains downloadable');
   assert(ids['bundle-controls'].hidden,'Do not recursively package a package without its original source');
   console.log(JSON.stringify({passed:true,checks:[missingSource?'Missing source does not block bundle recovery':'Legacy bundle recovery remains compatible','package stays downloadable without recursive packaging']}));
   return;
 }
 assert.equal(ids.results.children.length,2,'Original results survive bundle reload');
 await ids['bundle-tar'].click();assert.equal(posts.at(-1).job,'j1','Repack original results, never the ZIP');
 assert.equal(JSON.parse(storage.get('pdf-workbench-job')).bundleSourceId,'j1');
 await timers.shift()();await wait(()=>!ids.operation.disabled);
 ids.operation.value='text';await ids.operation.fire('change');ids.pages.value='2';
 ids['text-export'].value='markdown';await ids['text-export'].fire('change');
 assert(ids['pages-field'].hidden,'Do not advertise ignored page ranges');
 assert.equal(ids['run-label'].textContent,'Export Markdown');
 ids['text-export'].value='view';await ids['text-export'].fire('change');assert(!ids['pages-field'].hidden);
 ids.operation.value='forms';await ids.operation.fire('change');ids['form-mode'].value='fill';await ids['form-mode'].fire('change');ids['form-values'].value='{"Name":"Fixture"}';
 holdJob=true;await ids['run-button'].click();assert(ids['form-values'].disabled,'Form JSON must freeze during execution');
 holdJob=false;await timers.shift()();await wait(()=>!ids.operation.disabled);assert(!ids['form-values'].disabled);
 console.log(JSON.stringify({passed:true,checks:['bundle reload reconnects with a real-select value model','original results and source ID survive reload','repack uses original job','structured text hides unsupported page selection','effective export label','form textarea disabled while busy']}));
})().catch(e=>{console.error(e);process.exit(1)});
