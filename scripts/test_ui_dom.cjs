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
 get value(){if(this.tagName==='select')return this._value||this.options.find(o=>o.selected)?.value||this.options[0]?.value||'';return this._value}
 set value(v){this._value=String(v)}
 get selectedOptions(){return this.options.filter(o=>o.value===this.value)}
 querySelectorAll(selector){let out=[];const match=e=>selector==='input,select'?['input','select'].includes(e.tagName):selector[0]==='.'?e.className.split(' ').includes(selector.slice(1)):e.tagName===selector;for(const c of this.children){if(match(c))out.push(c);out.push(...c.querySelectorAll(selector))}return out}
}
const rootEl=new El('document'),stack=[rootEl],ids={};
for(const token of fs.readFileSync(root+'index.html','utf8').match(/<[^>]+>|[^<]+/g)){
 if(token.startsWith('</')){if(stack.length>1)stack.pop();continue}if(token.startsWith('<!'))continue;
 if(token.startsWith('<')){const m=token.match(/^<([\w-]+)/);if(!m)continue;const el=new El(m[1]);for(const a of token.slice(m[0].length,-1).matchAll(/([\w-]+)(?:="([^"]*)")?/g))el.setAttribute(a[1],a[2]??'');stack.at(-1).append(el);if(el.id)ids[el.id]=el;if(!['input','meta','link','img','br','hr'].includes(el.tagName))stack.push(el)}else stack.at(-1).textContent+=token.trim();
}
const storage=new Map(),calls=[],posts=[],files=[];let count=0,polls=0,lastOp='';
const doc={documentElement:rootEl.querySelectorAll('html')[0],body:rootEl.querySelectorAll('body')[0],getElementById:id=>ids[id],createElement:tag=>new El(tag),addEventListener(){},execCommand(){return true}};doc.documentElement.dataset={};
let href='http://localhost:8765/#token=fixture-token';const win={get location(){return new URL(href)},matchMedia:()=>({matches:false}),setTimeout:fn=>setTimeout(fn,1),open:()=>null};
const json=o=>new Response(JSON.stringify(o),{status:200,headers:{'Content-Type':'application/json'}});
const sandbox={window:win,document:doc,history:{replaceState:(_,__,url)=>{href=new URL(url,href).href}},sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},navigator:{clipboard:{writeText:async text=>{sandbox.copied=text}}},Headers,URL,URLSearchParams,Blob,Response,Set,Map,console,setTimeout:(f,t)=>{if(t<10000)return setTimeout(f,1)},fetch:async(url,opts)=>{url=new URL(url);calls.push(url);assert.equal(opts.headers.get('X-PDF-Token'),'fixture-token');assert.equal(url.search,'');if(url.pathname==='/api/status')return json({engines:Object.fromEntries(['pymupdf','poppler','weasyprint','ghostscript'].map(k=>[k,{available:true}])),limits:{}});if(url.pathname==='/api/files'&&opts.method==='POST'){const item={id:String(++count),name:decodeURIComponent(opts.headers.get('X-Filename')),size:opts.body.size};files.push(item);return json(item)}if(url.pathname==='/api/files')return json({files});if(url.pathname==='/api/jobs'&&opts.method==='POST'){const body=JSON.parse(opts.body);posts.push(body);lastOp=body.operation;polls=0;return json({id:'j1'})}if(url.pathname==='/api/jobs/j1'){polls++;return json({id:'j1',state:polls===1?'running':'completed',progress:polls===1?.5:1,results:lastOp==='text'?[{id:'r',name:'text.txt',url:'/api/results/r',text:'Extracted fixture'}]:[{id:'r',name:'out.pdf',url:'/api/results/r'}]})}if(url.pathname.startsWith('/api/results'))return new Response('content');if(url.pathname.startsWith('/api/files/')&&opts.method==='DELETE'){files.splice(files.findIndex(f=>f.id===url.pathname.split('/').pop()),1);return json({})}throw new Error('Unhandled '+url.pathname)}};
vm.runInNewContext(fs.readFileSync(root+'app.js','utf8'),sandbox);
const wait=async(fn)=>{for(let i=0;i<100;i++){if(fn())return;await new Promise(r=>setTimeout(r,3))}throw new Error('Condition timed out')};
(async()=>{
 await wait(()=>!ids['choose-files'].disabled);assert.equal(new URL(href).hash,'');assert.equal(storage.get('pdf-workbench-token'),'fixture-token');
 const a=new Blob(['%PDF-a'],{type:'application/pdf'});a.name='a.pdf';const b=new Blob(['%PDF-b'],{type:'application/pdf'});b.name='<img onerror=bad>.pdf';const h=new Blob(['<p>Hi</p>'],{type:'text/html'});h.name='file.html';
 ids['file-input'].files=[a,b,h];await ids['file-input'].fire('change');await wait(()=>!ids['choose-files'].disabled&&ids['file-count'].textContent===3);assert.equal(ids['file-list'].children.length,3);assert.equal(ids['file-list'].querySelectorAll('img').length,0);
 const up=ids['file-list'].children[2].querySelectorAll('.order-button')[0];await up.click();await ids['run-button'].click();await wait(()=>!ids.operation.disabled);assert.deepEqual(posts[0].files,['1','3','2']);assert.equal(ids['job-percent'].textContent,'100%');
 ids.operation.value='split';await ids.operation.fire('change');ids.pages.value='3-1';await ids['run-button'].click();assert.equal(posts.length,1);assert.match(ids.notice.textContent,/Use page groups/);ids.pages.value='1-3;4;5-7';await ids['run-button'].click();await wait(()=>!ids.operation.disabled);assert.equal(posts[1].options.pages,'1-3;4;5-7');
 ids.operation.value='text';await ids.operation.fire('change');ids.pages.value='';await ids['run-button'].click();await wait(()=>!ids.operation.disabled);assert.equal(sandbox.copied,'Extracted fixture');assert.equal(ids['extracted-text'].value,'Extracted fixture');
 ids.operation.value='compress';await ids.operation.fire('change');assert(ids['run-button'].disabled);ids['allow-lossy'].checked=true;await ids['allow-lossy'].fire('change');assert(!ids['run-button'].disabled);await ids['run-button'].click();await wait(()=>!ids.operation.disabled);assert.equal(posts[3].options.compress_profile,'ebook');assert.equal(posts[3].options.allow_lossy,true);
 await ids['remove-selected'].click();await wait(()=>!ids.operation.disabled&&ids['file-count'].textContent===0);assert.equal(files.length,0);
 console.log(JSON.stringify({passed:true,apiRequests:calls.length,checks:['token removed from URL and reused in headers','PDF and HTML upload','hostile filename inert','ordered job input','normalized progress','bad page ranges rejected','split groups sent','clipboard-first text','loss acknowledgment required','compression profile','remove selected']}));
})().catch(e=>{console.error(e);process.exit(1)});
