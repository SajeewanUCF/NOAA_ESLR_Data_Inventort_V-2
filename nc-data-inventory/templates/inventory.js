// Inventory view: ported from nc_data_inventory_V1, driven by live DATA from inventory.py
const R = DATA.results, S = DATA.stations, NOW = DATA.now, YNOW = Math.floor(DATA.now);
const LBL = {3:'Good',2:'Fair',1:'Poor / gap',0:'Not needed','-1':'No data'};
const KIND = [['wl','Water level'],['wave','Waves'],['wind','Wind / met'],['river','River flow']];
const MODELS = [['wis','USACE WIS','wave'],['gtsm','CoDEC / GTSM','wl'],['cora','NOAA CORA','wl'],['era5','ERA5','wind'],['conus404','CONUS404','wind']];
const NS='http://www.w3.org/2000/svg';
const el=(t,a={},p)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);if(p)p.appendChild(e);return e};
const proj=(lon,lat)=>[(lon-LAND.x0)*LAND.lat0*LAND.sx,(LAND.y1-lat)*LAND.sx];
const css=v=>`var(--${v})`;
const cssVal=v=>getComputedStyle(document.documentElement).getPropertyValue('--'+v).trim();
const pcls=v=>v<0?'p0':'p'+v;
const ccls=v=>v<0?'cna':'c'+v;
const score=(r,k)=>r[k+'_score'];
const gmap=(lat,lon)=>`https://www.google.com/maps/search/?api=1&query=${(+lat).toFixed(5)},${(+lon).toFixed(5)}`;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let colourBy='overall', stMode='key', current=R.length?R[0].num:null;
document.getElementById('h1').textContent=`Data availability at the ${R.length} RCCP study sites`;

// ---------------- summary strip ----------------
function strip(){
  const n=[3,2,1].map(s=>R.filter(r=>r.overall===s).length);
  const improved=R.filter(r=>r.overall_all>r.overall).length;
  const c=k=>(S[k]||[]).length;
  document.getElementById('strip').innerHTML=
   `<div><b>${R.length}</b><span>study sites</span></div>`+
   `<div><b>${c('tide')+c('usgs_wl')+c('wave')+c('met')+c('river')}</b><span>stations (${c('tide')} NOAA tide, ${c('usgs_wl')} USGS water level, ${c('wave')+c('met')} NDBC, ${c('river')} USGS flow)</span></div>`+
   `<div><b>${c('wis')+c('gtsm')}</b><span>model output points (${c('wis')} WIS, ${c('gtsm')} GTSM) + 3 gridded models</span></div>`+
   `<div><span class="dot" style="background:var(--good)"></span><b>${n[0]}</b><span>good overall</span></div>`+
   `<div><span class="dot" style="background:var(--fair)"></span><b>${n[1]}</b><span>fair</span></div>`+
   `<div><span class="dot" style="background:var(--poor)"></span><b>${n[2]}</b><span>poor / major gaps</span></div>`+
   `<div><b>${improved}</b><span>sites where models close a gap</span></div>`;
}
strip();

// ---------------- map ----------------
const svg=document.getElementById('map');
const VB={x:0,y:0,w:LAND.W,h:LAND.H}; const KMAX=8;
el('rect',{x:-2000,y:-2000,width:LAND.W+4000,height:LAND.H+4000,fill:css('water')},svg);
el('path',{d:LAND.path,fill:css('land'),stroke:css('coast'),'stroke-width':0.8,'vector-effect':'non-scaling-stroke','fill-rule':'evenodd'},svg);
const gLinks=el('g',{},svg), gSt=el('g',{},svg), gSites=el('g',{},svg);
const STY={tide:['tide','tri'],usgs_wl:['usgswl','sq'],wave:['wave','circ'],met:['met','circ'],river:['river','sq'],wis:['model','dia'],gtsm:['model-ext','dia']};
const NAMES={tide:'NOAA tide gauge',usgs_wl:'USGS water-level gauge',wave:'NDBC wave buoy',met:'NDBC wind / met (no waves)',river:'USGS stream gauge',wis:'WIS output point',gtsm:'GTSM output point'};
const hidden=new Set();
const pins=[];
function shapeAt(shape,col,parent,sz=5){
  if(shape==='tri')return el('path',{d:`M0,${-sz*1.3}L${sz*1.2},${sz*0.8}L${-sz*1.2},${sz*0.8}Z`,fill:col,stroke:css('panel'),'stroke-width':1},parent);
  if(shape==='sq')return el('rect',{x:-sz*0.85,y:-sz*0.85,width:sz*1.7,height:sz*1.7,fill:col,stroke:css('panel'),'stroke-width':1},parent);
  if(shape==='dia')return el('path',{d:`M0,${-sz}L${sz},0L0,${sz}L${-sz},0Z`,fill:col,stroke:css('panel'),'stroke-width':1},parent);
  return el('circle',{r:sz*0.85,fill:col,stroke:css('panel'),'stroke-width':1},parent);
}
// which sites each station is nearest for
const nearestFor={};
R.forEach(r=>{KIND.forEach(([k])=>{if(r[k]&&r[k][0]){(nearestFor[r[k][0].id]=nearestFor[r[k][0].id]||new Set()).add(r.num)}});
  ['wis','gtsm'].forEach(k=>{const m=r.models[k]; if(m&&m.id)(nearestFor[m.id]=nearestFor[m.id]||new Set()).add(r.num)});});
['river','met','usgs_wl','wave','gtsm','wis','tide'].forEach(k=>(S[k]||[]).forEach(s=>{
  const [x,y]=proj(s.lon,s.lat); if(y<-50||y>LAND.H+50||x<-50||x>LAND.W+50)return;
  const serving=s.key||!!nearestFor[s.id];
  const g=el('g',{class:'st-mark',tabindex:0,role:'button','aria-label':`${s.id} ${s.name}`},gSt);
  shapeAt(STY[k][1],css(STY[k][0]),g,(k==='wis'||k==='gtsm')?4:5);
  el('circle',{r:9,fill:'transparent'},g);
  const lab=el('text',{x:8,y:4,class:'st-lab','font-size':10.5,fill:css('ink')},g); lab.textContent=s.id;
  const t=el('title',{},g); t.textContent=`${s.id} – ${s.name}\n${NAMES[k]} · ${s.record}`;
  pins.push({g,x,y,lab,k,serving});
  const open=()=>showCard(s,k); g.addEventListener('click',()=>{if(!dragMoved)open()}); g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open()}});
}));
function legend(){
  const lg=document.getElementById('legend');
  lg.innerHTML=Object.keys(STY).filter(k=>(S[k]||[]).length).map(k=>{
    const [c,sh]=STY[k]; const shape=sh==='tri'?`<path d="M6,1L11,10L1,10Z" fill="var(--${c})"/>`:sh==='sq'?`<rect x="1.5" y="1.5" width="9" height="9" fill="var(--${c})"/>`:sh==='dia'?`<path d="M6,1L11,6L6,11L1,6Z" fill="var(--${c})"/>`:`<circle cx="6" cy="6" r="4.5" fill="var(--${c})"/>`;
    return `<button type="button" class="lg${hidden.has(k)?' off':''}" data-k="${k}" aria-pressed="${!hidden.has(k)}"><svg width="12" height="12" viewBox="0 0 12 12">${shape}</svg>${NAMES[k]} (${S[k].length})</button>`}).join('')+
    `<span><span class="dot" style="background:var(--good)"></span><span class="dot" style="background:var(--fair)"></span><span class="dot" style="background:var(--poor)"></span>Study site, by overall rating</span>`;
  lg.querySelectorAll('button.lg').forEach(b=>b.onclick=()=>{const k=b.dataset.k; hidden.has(k)?hidden.delete(k):hidden.add(k); legend(); applyView();});
}
legend();
const sc={3:'good',2:'fair',1:'poor',0:'na'};
const sitePins=[];
R.forEach(r=>{
  const [x,y]=proj(r.lon,r.lat);
  const g=el('g',{class:'site-mark',tabindex:0,role:'button','aria-label':`${r.num}. ${r.name}`,'data-n':r.num},gSites);
  const ring=el('circle',{class:'ring',r:11,fill:css(sc[r.overall]||'na'),stroke:css('panel'),'stroke-width':2},g);
  const t=el('text',{y:4,'text-anchor':'middle','font-size':11,'font-weight':700,fill:'#fff'},g); t.textContent=r.num;
  const lab=el('text',{x:15,y:4,class:'site-lab','font-size':11.5,'font-weight':600,fill:css('ink')},g); lab.textContent=r.name;
  sitePins.push({g,x,y,lab,ring,r});
  g.addEventListener('click',()=>{if(!dragMoved)select(r.num)}); g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();select(r.num)}});
});
// spread pins that overlap at full extent (e.g. Cape Carteret / Swansboro)
(function declutter(){
  sitePins.forEach(p=>p.off=[0,0]);
  for(let i=0;i<sitePins.length;i++)for(let j=i+1;j<sitePins.length;j++){
    const a=sitePins[i],b=sitePins[j],dx=b.x-a.x,dy=b.y-a.y,d=Math.hypot(dx,dy);
    if(d<22){const ux=d?dx/d:1,uy=d?dy/d:0,push=(22-d)/2+2; a.off=[a.off[0]-ux*push,a.off[1]-uy*push]; b.off=[b.off[0]+ux*push,b.off[1]+uy*push];}
  }
  sitePins.forEach(p=>{p.leader=(p.off[0]||p.off[1])?el('line',{x1:p.x,y1:p.y,x2:p.x,y2:p.y,stroke:css('ink'),'stroke-width':1,'vector-effect':'non-scaling-stroke'},gSites):null; if(p.leader)gSites.insertBefore(p.leader,gSites.firstChild);});
})();
function recolour(){sitePins.forEach(p=>p.ring.setAttribute('fill',css(sc[p.r[colourBy]]||'na')));}

// ---- view / zoom (as V1) ----
const kNow=()=>LAND.W/VB.w;
function applyView(){
  const k=kNow();
  svg.setAttribute('viewBox',`${VB.x} ${VB.y} ${VB.w} ${VB.h}`);
  const s=1/k; const showSt=k>=2.2, showSite=k>=1.8;
  pins.forEach(p=>{p.g.setAttribute('transform',`translate(${p.x},${p.y}) scale(${s})`);p.lab.style.display=showSt?'':'none';
    p.g.style.display=(hidden.has(p.k)||(stMode==='key'&&!p.serving))?'none':''});
  sitePins.forEach(p=>{const f=Math.max(0,1-(k-1)/1.5); const ox=p.x+p.off[0]*s*f, oy=p.y+p.off[1]*s*f;
    p.g.setAttribute('transform',`translate(${ox},${oy}) scale(${s})`);
    if(p.leader){p.leader.setAttribute('x2',ox);p.leader.setAttribute('y2',oy);p.leader.style.display=f>0.05?'':'none'}
    p.lab.style.display=showSite?'':'none'});
  gLinks.querySelectorAll('[data-scale]').forEach(n=>n.setAttribute('transform',`translate(${n.dataset.x},${n.dataset.y}) scale(${s})`));
  document.getElementById('zlevel').textContent=`${k.toFixed(1)}×`;
  document.getElementById('zout').disabled=k<=1.001; document.getElementById('zin').disabled=k>=KMAX-0.001;
}
function setView(cx,cy,w){
  w=Math.min(LAND.W,Math.max(LAND.W/KMAX,w)); const h=w*LAND.H/LAND.W;
  let x=cx-w/2,y=cy-h/2; x=Math.max(0,Math.min(LAND.W-w,x)); y=Math.max(0,Math.min(LAND.H-h,y));
  VB.x=x;VB.y=y;VB.w=w;VB.h=h; applyView();
}
function zoomAt(f,px,py){const w=Math.min(LAND.W,Math.max(LAND.W/KMAX,VB.w/f)); const r=w/VB.w;
  const nx=px-(px-VB.x)*r, ny=py-(py-VB.y)*r; setView(nx+w/2,ny+w*LAND.H/LAND.W/2,w);}
function toMap(cx,cy){const b=svg.getBoundingClientRect();return [VB.x+(cx-b.left)/b.width*VB.w, VB.y+(cy-b.top)/b.height*VB.h]}
const centre=()=>[VB.x+VB.w/2,VB.y+VB.h/2];
document.getElementById('zin').onclick=()=>zoomAt(1.6,...centre());
document.getElementById('zout').onclick=()=>zoomAt(1/1.6,...centre());
document.getElementById('zreset').onclick=()=>setView(LAND.W/2,LAND.H/2,LAND.W);
const hintEl=document.getElementById('zhint'); let hintT;
svg.addEventListener('wheel',e=>{
  if(!(e.ctrlKey||e.metaKey)){hintEl.hidden=false;clearTimeout(hintT);hintT=setTimeout(()=>hintEl.hidden=true,1200);return}
  e.preventDefault(); zoomAt(Math.exp(-e.deltaY*0.0025),...toMap(e.clientX,e.clientY));
},{passive:false});
svg.addEventListener('dblclick',e=>{e.preventDefault();zoomAt(e.shiftKey?1/2:2,...toMap(e.clientX,e.clientY))});
const ptrs=new Map(); let dragMoved=false, start=null;
svg.addEventListener('pointerdown',e=>{ptrs.set(e.pointerId,{x:e.clientX,y:e.clientY}); dragMoved=false; start={vb:{...VB},pts:[...ptrs.values()].map(p=>({...p}))};});
svg.addEventListener('pointermove',e=>{
  if(!ptrs.has(e.pointerId))return; ptrs.set(e.pointerId,{x:e.clientX,y:e.clientY});
  const b=svg.getBoundingClientRect(), u=start.vb.w/b.width, pts=[...ptrs.values()];
  if(pts.length===1&&start.pts.length===1){
    const dx=pts[0].x-start.pts[0].x, dy=pts[0].y-start.pts[0].y;
    if(Math.hypot(dx,dy)>4){dragMoved=true; svg.setPointerCapture(e.pointerId); svg.classList.add('dragging')}
    if(dragMoved) setView(start.vb.x+start.vb.w/2-dx*u, start.vb.y+start.vb.h/2-dy*u, start.vb.w);
  } else if(pts.length===2&&start.pts.length===2){
    dragMoved=true;
    const d0=Math.hypot(start.pts[0].x-start.pts[1].x,start.pts[0].y-start.pts[1].y), d1=Math.hypot(pts[0].x-pts[1].x,pts[0].y-pts[1].y);
    const m0={x:(start.pts[0].x+start.pts[1].x)/2,y:(start.pts[0].y+start.pts[1].y)/2}, m1={x:(pts[0].x+pts[1].x)/2,y:(pts[0].y+pts[1].y)/2};
    const w=Math.min(LAND.W,Math.max(LAND.W/KMAX,start.vb.w*d0/d1));
    const ax=start.vb.x+(m0.x-b.left)*u, ay=start.vb.y+(m0.y-b.top)*u; const u1=w/b.width;
    setView(ax-(m1.x-b.left)*u1+w/2, ay-(m1.y-b.top)*u1+w*LAND.H/LAND.W/2, w);
  }
});
const endPtr=e=>{ptrs.delete(e.pointerId); svg.classList.remove('dragging'); start={vb:{...VB},pts:[...ptrs.values()].map(p=>({...p}))}; setTimeout(()=>{if(!ptrs.size)dragMoved=false},0)};
svg.addEventListener('pointerup',endPtr); svg.addEventListener('pointercancel',endPtr);
svg.addEventListener('keydown',e=>{const st=VB.w*0.15,[cx,cy]=centre();
  const m={ArrowLeft:[-st,0],ArrowRight:[st,0],ArrowUp:[0,-st],ArrowDown:[0,st]}[e.key];
  if(m){e.preventDefault();setView(cx+m[0],cy+m[1],VB.w)} else if(e.key==='+'||e.key==='='){zoomAt(1.6,cx,cy)} else if(e.key==='-'){zoomAt(1/1.6,cx,cy)}});
document.querySelectorAll('#segcol button').forEach(b=>b.onclick=()=>{colourBy=b.dataset.v; document.querySelectorAll('#segcol button').forEach(x=>x.classList.toggle('on',x===b)); recolour();});
document.querySelectorAll('#segst button').forEach(b=>b.onclick=()=>{stMode=b.dataset.v; document.querySelectorAll('#segst button').forEach(x=>x.classList.toggle('on',x===b)); applyView();});

function linkTargets(r){
  const t=[];
  KIND.forEach(([k])=>{if(k==='river'&&!r.river_relevant)return; const s=r[k]&&r[k][0]; if(s)t.push({lat:s.lat,lon:s.lon,col:k==='wl'?'tide':k==='wind'?'met':k,dash:'5 4',km:s.km});});
  ['wis','gtsm','cora'].forEach(k=>{const m=r.models[k]; if(m&&m.lat!=null)t.push({lat:m.lat,lon:m.lon,col:k==='gtsm'?'model-ext':'model',dash:'1.5 3',km:m.km,model:k});});
  return t;
}
function zoomToSite(n){
  const r=R.find(d=>d.num===n); const pts=[[r.lon,r.lat]];
  linkTargets(r).forEach(s=>{if(s.km<120)pts.push([s.lon,s.lat])});
  const xy=pts.map(p=>proj(...p)); const xs=xy.map(p=>p[0]), ys=xy.map(p=>p[1]);
  const w=Math.max(Math.max(...xs)-Math.min(...xs),(Math.max(...ys)-Math.min(...ys))*LAND.W/LAND.H)*1.35+40;
  setView((Math.max(...xs)+Math.min(...xs))/2,(Math.max(...ys)+Math.min(...ys))/2,w);
}

// station card
function showCard(s,k){
  const near=[...(nearestFor[s.id]||[])].sort((a,b)=>a-b);
  const c=document.getElementById('stcard');
  c.innerHTML=`<div class="stc-head"><span class="mono">${esc(s.id)}</span><button type="button" class="x" aria-label="Close station details">×</button></div>
    <div class="src">${esc(s.src)} · ${NAMES[k]}</div><div>${esc(s.name)}</div><small>${esc(s.record)}${s.years?` (${s.years} yr)`:''}</small>
    ${near.length?`<small>Nearest of its type for site ${near.join(', ')}</small>`:''}
    <a href="${gmap(s.lat,s.lon)}" target="_blank" rel="noopener">Open in Google Maps ↗</a>`;
  c.hidden=false; c.querySelector('.x').onclick=()=>c.hidden=true;
}

function select(n){
  const r=R.find(d=>d.num===n); if(!r)return; current=n;
  document.querySelectorAll('.site-mark').forEach(g=>g.classList.toggle('sel',+g.dataset.n===n));
  document.querySelectorAll('tr.site-row').forEach(t=>t.classList.toggle('sel',+t.dataset.n===n));
  gLinks.innerHTML='';
  const [sx,sy]=proj(r.lon,r.lat);
  linkTargets(r).forEach(s=>{const [x,y]=proj(s.lon,s.lat);
    el('line',{class:'link',x1:sx,y1:sy,x2:x,y2:y,stroke:css(s.col),'stroke-width':2,'stroke-dasharray':s.dash,'vector-effect':'non-scaling-stroke'},gLinks);
    if(s.model==='cora'){const g=el('g',{'data-scale':1,'data-x':x,'data-y':y},gLinks); el('circle',{r:3.5,fill:css('model'),stroke:css('panel'),'stroke-width':1},g);}});
  const e5=r.models.era5; if(e5&&e5.lat!=null){const [a,b]=proj(e5.lon-0.125,e5.lat+0.125),[c,d]=proj(e5.lon+0.125,e5.lat-0.125);
    el('rect',{x:a,y:b,width:c-a,height:d-b,fill:'none',stroke:css('met'),'stroke-dasharray':'2 3','stroke-width':1.2,'vector-effect':'non-scaling-stroke'},gLinks);}
  applyView();
  const rows=KIND.map(([k,lab])=>{const s=r[k]&&r[k][0], v=score(r,k);
    if(!s)return `<div class="row"><div class="k">${lab}</div><div class="v">no station in inventory</div><span class="pill p0">${LBL[-1]}</span></div>`;
    return `<div class="row"><div class="k">${lab}</div><div class="v"><span class="mono">${esc(s.id)}</span> · ${s.km} km <a class="gm" href="${gmap(s.lat,s.lon)}" target="_blank" rel="noopener" aria-label="Open ${esc(s.id)} in Google Maps">map ↗</a><small>${esc(s.src)} · ${esc(s.name)} · ${esc(s.record)} (${s.years} yr)</small></div><span class="pill ${pcls(v)}">${LBL[v]}</span></div>`}).join('');
  const mrows=MODELS.map(([k,lab,q])=>{const m=r.models[k]; const v=m.score;
    const where=v<0?`<small>${esc(m.why||'not available')}</small>`:
      `<span class="mono">${esc(m.id)}</span> · ${m.km} km${m.lat!=null?` <a class="gm" href="${gmap(m.lat,m.lon)}" target="_blank" rel="noopener">map ↗</a>`:''}<small>${q==='wl'?'water level':q==='wave'?'waves':'wind'}${k==='cora'?' + waves, '+(m.mesh==='shoreline'?'shoreline':'mesh')+' node':''}</small>`;
    return `<div class="row model"><div class="k">${lab}</div><div class="v">${where}</div><span class="pill ${pcls(v)}">${LBL[v]}</span></div>`}).join('');
  const eva=r.tide_eva?`<span class="mono">${esc(r.tide_eva.id)}</span> (${r.tide_eva.years} yr, ${r.tide_eva.km} km)`:'none with ≥20 yr within 40 km';
  document.getElementById('detail').innerHTML=
   `<div class="eyebrow">Site ${r.num} of ${R.length}</div>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><h2>${esc(r.name)}</h2><span class="pill ${pcls(r.overall)}">${LBL[r.overall]} overall</span><span class="pill ${pcls(r.overall_all)}">${LBL[r.overall_all]} incl. models</span></div>
    <div class="setting">${esc(r.title)}<br>${esc(r.setting)}</div>
    <div class="actions"><button type="button" class="btn" id="zsite">Zoom to site and stations</button><a class="btn" href="${gmap(r.lat,r.lon)}" target="_blank" rel="noopener">Open site in Google Maps ↗</a></div>
    <div class="subhead">Observations</div><div class="dl">${rows}</div>
    <div class="subhead">Models / hindcasts</div><div class="dl">${mrows}</div>
    <p class="note"><b>Water-level gauge for EVA:</b> ${eva}</p>
    ${r.note?`<p class="note">${esc(r.note)}</p>`:''}
    <p class="hint">Select a site on the map or in the table. Dashed lines: nearest station of each type; dotted lines: nearest WIS / GTSM point and CORA node; dotted box: ERA5 cell. Click any marker for details.</p>`;
  document.getElementById('zsite').onclick=()=>zoomToSite(n);
}
applyView();

// ---------------- matrix ----------------
(()=>{
  const t=document.getElementById('matrix');
  let h=`<thead><tr><th>Site</th>${KIND.map(k=>`<th>${k[1]}</th>`).join('')}<th>Overall</th>`+
        MODELS.map(([k,lab],i)=>`<th class="mcol${i===0?' sep':''}">${lab}</th>`).join('')+`<th>Incl. models</th></tr></thead><tbody>`;
  R.forEach(r=>{
    h+=`<tr class="site-row" data-n="${r.num}"><td class="sname">${r.num}. ${esc(r.name)}<small>${esc(r.setting)}</small></td>`+
      KIND.map(([k])=>{const s=r[k]&&r[k][0],v=score(r,k); return s?`<td class="cell ${ccls(v)}" title="${esc(s.label)} (${esc(s.src)})"><span class="id">${esc(s.id.replace(/^USGS-/,''))}</span><small>${s.km} km · ${s.years} yr</small></td>`:`<td class="cell cna">—</td>`}).join('')+
      `<td><span class="pill ${pcls(r.overall)}">${LBL[r.overall]}</span></td>`+
      MODELS.map(([k],i)=>{const m=r.models[k]; return m.score<0?`<td class="cell cna${i===0?' sep':''}">n/a<small>${esc(m.why||'')}</small></td>`:
        `<td class="cell ${ccls(m.score)}${i===0?' sep':''}" title="${esc(m.id)}"><span class="id">${esc(k==='era5'?m.txt:m.id)}</span><small>${m.km} km</small></td>`}).join('')+
      `<td><span class="pill ${pcls(r.overall_all)}">${LBL[r.overall_all]}</span></td></tr>`;
  });
  t.innerHTML=h+'</tbody>';
  t.querySelectorAll('tr.site-row').forEach(tr=>tr.addEventListener('click',()=>{select(+tr.dataset.n);document.getElementById('map').scrollIntoView({behavior:'smooth',block:'nearest'})}));
  const sn=[]; if(DATA.include_usgs_wl)sn.push('Water level uses NOAA tide gauges and USGS water-surface-elevation gauges (NAVD88).');
  sn.push(DATA.cora_mesh?`CORA distances use the cached ${DATA.cora_mesh==='native'?'full-mesh':'shoreline'} nodes.`:'CORA: mesh nodes not cached yet (run python -m loaders.cora --build).');
  sn.push('Distances are great-circle from the site pin. Record years run from first to last report (gaps inside a record are not known from the inventories).');
  document.getElementById('srcnote').textContent=sn.join(' ');
})();

// ---------------- coverage chart ----------------
(()=>{
  const groups=DATA.groups, models=DATA.models;
  groups.forEach(g=>{const ys=Object.keys(g.counts).map(Number); g.first=Math.min(...ys);
    const k=Math.min(5,g.n); g.y5=ys.filter(y=>g.counts[y]>=k).sort((a,b)=>a-b)[0]; g.k=k;});
  const svg=document.getElementById('coverage');
  const W=980,left=300,right=24,rowH=44,head=30,top=18;
  const nObs=groups.length,nMod=models.length;
  const yObs0=top+head, yMod0=yObs0+nObs*rowH+14+head, H=yMod0+nMod*rowH+34;
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  const firsts=groups.map(g=>g.first).concat(models.map(m=>(m.ext||m.r)[0]));
  const X0=Math.max(1850,Math.min(1900,Math.floor(Math.min(...firsts)/10)*10)),X1=Math.ceil((YNOW+4)/10)*10;
  const x=y=>left+(y-X0)/(X1-X0)*(W-left-right);
  const ov=DATA.overlap;
  if(ov){el('rect',{x:x(ov[0]),y:top,width:x(ov[1]+1)-x(ov[0]),height:H-top-30,fill:css('overlap')},svg);}
  for(let y=X0;y<=X1;y+=10){el('line',{x1:x(y),x2:x(y),y1:top,y2:H-30,stroke:css('line')},svg);const t=el('text',{x:x(y),y:H-12,'text-anchor':'middle','font-size':11,fill:css('muted')},svg);t.textContent=y;}
  el('line',{x1:x(NOW),x2:x(NOW),y1:top,y2:H-30,stroke:css('muted'),'stroke-dasharray':'4 4'},svg);
  const nowT=el('text',{x:x(NOW)-4,y:top+10,'text-anchor':'end','font-size':10.5,fill:css('muted')},svg); nowT.textContent=YNOW;
  const head1=(txt,y)=>{const t=el('text',{x:left-14,y,'text-anchor':'end','font-size':11,'font-weight':700,'letter-spacing':'.07em',fill:css('muted')},svg);t.textContent=txt};
  head1('IN-SITU OBSERVATIONS',yObs0-10);
  el('line',{x1:16,x2:W-right,y1:yMod0-head-6,y2:yMod0-head-6,stroke:css('line')},svg);
  head1('MODEL HINDCAST / REANALYSIS',yMod0-10);
  const rowLabel=(txt,y)=>{const t=el('text',{x:left-14,y:y+5,'text-anchor':'end','font-size':12.5,'font-weight':600,fill:css('ink')},svg);t.textContent=txt};
  const ann=(txt,xa,y)=>{const t=el('text',{x:xa,y:y-12,'font-size':11,fill:css('muted')},svg);t.textContent=txt};
  groups.forEach((g,i)=>{const yc=yObs0+i*rowH+rowH/2; rowLabel(g.label,yc);
    for(let y=g.first;y<=YNOW;y++){const c=g.counts[y]; if(!c)continue; const pct=Math.round(18+82*c/g.n);
      const wEnd=y===YNOW?NOW:y+1;
      const r=el('rect',{x:x(y),y:yc-8,width:Math.max(0.5,x(wEnd)-x(y)+0.4),height:16,fill:`color-mix(in srgb, var(--${g.col}) ${pct}%, var(--panel))`},svg);
      const tt=el('title',{},r); tt.textContent=`${y}: ${c} of ${g.n} stations`;}
    ann(`${g.first}–present`+(g.y5&&g.y5>g.first?` · ≥${g.k} active from ${g.y5}`:''),x(g.first),yc);
  });
  models.forEach((m,i)=>{const yc=yMod0+i*rowH+rowH/2; rowLabel(m.label,yc);
    if(m.ext) el('rect',{x:x(m.ext[0]),y:yc-8,width:x(m.ext[1])-x(m.ext[0]),height:16,rx:3,fill:css('model-ext')},svg);
    el('rect',{x:x(m.r[0]),y:yc-8,width:x(m.r[1])-x(m.r[0]),height:16,rx:3,fill:css('model')},svg);
    ann(m.txt,x(m.ext?m.ext[0]:m.r[0]),yc);
  });
  if(ov){const tx=el('text',{x:x((ov[0]+ov[1]+1)/2),y:H-36,'text-anchor':'middle','font-size':10,fill:css('muted')},svg); tx.textContent='overlap';}
  document.getElementById('covlegend').innerHTML=
   `<span><span class="sw" style="background:linear-gradient(90deg,color-mix(in srgb,var(--tide) 18%,var(--panel)),var(--tide))"></span>Stations active per year, few → all</span>`+
   `<span><span class="sw" style="background:var(--model)"></span>Model hindcast / reanalysis</span>`+
   `<span><span class="sw" style="background:var(--model-ext)"></span>Extended version</span>`+
   (ov?`<span><span class="sw" style="background:var(--overlap);outline:1px dashed var(--good)"></span>Common overlap ${ov[0]}–${ov[1]}</span>`:'');
})();

// ---------------- water-level gauge timeline ----------------
(()=>{
  const T=DATA.timeline;
  const svg=document.getElementById('timeline'); const W=960,rowH=30,top=26,left=270,right=215,H=top+Math.max(1,T.length)*rowH+24;
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  if(!T.length){const t=el('text',{x:W/2,y:top+10,'text-anchor':'middle',fill:css('muted')},svg);t.textContent='No water-level gauges in the inventory';return;}
  const X0=Math.min(1930,Math.floor(Math.min(...T.map(t=>t.s))/10)*10);
  const x=y=>left+(y-X0)/(NOW-X0)*(W-left-right);
  for(let y=X0;y<=YNOW;y+=10){el('line',{x1:x(y),x2:x(y),y1:top-6,y2:H-22,stroke:css('line')},svg);const t=el('text',{x:x(y),y:H-6,'text-anchor':'middle','font-size':11,fill:css('muted')},svg);t.textContent=y;}
  const x20=x(NOW-20); el('line',{x1:x20,x2:x20,y1:top-10,y2:H-22,stroke:css('accent'),'stroke-dasharray':'3 3'},svg);
  const tl=el('text',{x:x20+4,y:top-12,'font-size':10.5,fill:css('accent')},svg); tl.textContent=`20-yr EVA minimum (${Math.floor(NOW-20)} start)`;
  T.forEach((t,i)=>{const y=top+i*rowH+8;
    const n=el('text',{x:left-10,y:y+10,'text-anchor':'end','font-size':12,fill:css('ink'),'font-weight':600},svg); n.textContent=t.name.length>40?t.name.slice(0,39)+'…':t.name;
    el('rect',{x:x(t.s),y,width:Math.max(3,x(t.e)-x(t.s)),height:14,rx:2,fill:t.src==='USGS'?css('usgswl'):css('tide')},svg);
    const lab=el('text',{x:W-right+10,y:y+11,'font-size':11.5,fill:t.served.length?css('ink'):css('muted')},svg);
    lab.textContent=`${t.years} yr · `+(t.served.length?`nearest for site ${t.served.join(', ')}`:'nearest for no site');
  });
})();

if(current!==null)select(current);
