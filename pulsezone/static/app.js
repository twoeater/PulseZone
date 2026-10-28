'use strict';

// Apply the saved theme before the stylesheet and page body can paint.
const themeStorageKey = 'pulsezone.theme';
const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
const themeModes = new Set(['system', 'light', 'dark']);
let themeMode = 'system';
try {
  const saved = window.localStorage.getItem(themeStorageKey);
  if (themeModes.has(saved)) themeMode = saved;
} catch (_) {
  // Theme switching still works when browser storage is unavailable.
}
function applyTheme() {
  document.documentElement.dataset.theme = themeMode === 'system'
    ? (systemTheme.matches ? 'dark' : 'light') : themeMode;
}
applyTheme();
systemTheme.addEventListener('change', () => {
  if (themeMode === 'system') applyTheme();
});
window.addEventListener('storage', event => {
  if (event.key !== themeStorageKey && event.key !== null) return;
  themeMode = themeModes.has(event.newValue) ? event.newValue : 'system';
  applyTheme();
  const selector = document.getElementById('theme-mode');
  if (selector) selector.value = themeMode;
});

document.addEventListener('DOMContentLoaded', () => {
const $ = id => document.getElementById(id);
$('theme-mode').value = themeMode;
$('theme-mode').addEventListener('change', event => {
  themeMode = event.target.value;
  applyTheme();
  try { window.localStorage.setItem(themeStorageKey, themeMode); } catch (_) {}
});
const make = (tag, text, cls) => { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; };
let catalog = {zones:[],defaults:{},revision:-1}, latest = {records:{}}, editing = null, polling = false;
const selected = new Set(), statusNodes = new Map();
const zonePanels = new Map();
const collapsedStorageKey = 'pulsezone.collapsedZones';
const collapsedZones = new Set();
try {
  const saved = JSON.parse(window.localStorage.getItem(collapsedStorageKey) || '[]');
  if (Array.isArray(saved)) {
    for (const path of saved) if (typeof path === 'string') collapsedZones.add(path);
  }
} catch (_) {}
const labels={display_name:'표시 이름',domain:'접속 도메인',record:'레코드 (@ = apex / 빈 값 = IP 직접)',ip:'IPv4',alias:'레코드 표시 이름',interval:'interval · 초',timeout:'timeout · 초',retry:'retry · 실패 횟수',retry_delay:'retry_delay · 초',enabled:'on/off',protocol:'프로토콜',https_insecure:'HTTPS 인증서 검증 생략 (https_insecure)',maintenance:'maintenance · 같은 날 중복 알림 생략',description:'설명',tip:'마우스오버 tip'};
const inherited=['interval','timeout','retry','retry_delay','protocol','https_insecure','maintenance'];
async function api(path, method='GET', body) {
  const options={method,cache:'no-store'};
  if(body!==undefined){options.headers={'Content-Type':'application/json'};options.body=JSON.stringify(body);}
  const response=await fetch(path,options);
  const data=await response.json();
  if(!response.ok)throw new Error(data.error||`HTTP ${response.status}`);
  return data;
}
function inform(text){$('notice').textContent=text;}
function button(text, action, cls='link'){const b=make('button',text,cls);b.type='button';b.addEventListener('click',action);return b;}
function selection(key,label){const box=make('input');box.type='checkbox';box.checked=selected.has(key);box.setAttribute('aria-label',label);box.addEventListener('change',()=>{if(box.checked)selected.add(key);else selected.delete(key);updateSelection();});return box;}
function updateSelection(){
  $('bulk').disabled=!selected.size;$('bulk').textContent=`선택 항목 설정 (${selected.size})`;
  $('bulk-maintenance').disabled=!('maintenance' in catalog.defaults);
  if($('bulk-maintenance').disabled&&$('bulk-field').value==='maintenance')$('bulk-field').value='interval';
}
function date(value){return value?new Date(value*1000).toLocaleString():'—';}
function saveCollapsedZones() {
  try { window.localStorage.setItem(collapsedStorageKey, JSON.stringify([...collapsedZones])); } catch (_) {}
}
function updateZonePanel(view) {
  const collapsed = collapsedZones.has(view.zone.relative_path);
  view.panel.hidden = collapsed;
  view.section.classList.toggle('is-collapsed', collapsed);
  view.toggle.setAttribute('aria-expanded', String(!collapsed));
  view.toggle.setAttribute('aria-label', `${view.zone.relative_path} ${collapsed ? '펼치기' : '접기'}`);
  view.toggle.title = collapsed ? '레코드 목록 펼치기' : '레코드 목록 접기';
  view.arrow.textContent = collapsed ? '▸' : '▾';
}
function updateFoldButton() {
  const allCollapsed = catalog.zones.length > 0 && catalog.zones.every(z => collapsedZones.has(z.relative_path));
  $('toggle-all-zones').textContent = allCollapsed ? '모두 펼치기' : '모두 접기';
  $('toggle-all-zones').disabled = catalog.zones.length === 0;
}
function toggleZone(path) {
  if (collapsedZones.has(path)) collapsedZones.delete(path); else collapsedZones.add(path);
  saveCollapsedZones();
  for (const view of zonePanels.values()) if (view.zone.relative_path === path) updateZonePanel(view);
  updateFoldButton();
  renderStatus();
}
function render(){
  const fragment=document.createDocumentFragment(), q=$('search').value.trim().toLowerCase(), show=$('show-missing').checked;
  statusNodes.clear();
  zonePanels.clear();
  for(const zone of catalog.zones){
    const zoneMatch=[zone.display_name,zone.domain,zone.filename,zone.relative_path,zone.description].join(' ').toLowerCase().includes(q);
    const rows=zone.records.filter(r=>(show||r.effective.present)&&(zoneMatch||[r.record,r.ip,r.alias,r.description].join(' ').toLowerCase().includes(q)));
    if(!rows.length&&(!zoneMatch||(!zone.present&&!show)))continue;
    const section=make('section',undefined,'zone'), head=make('div',undefined,'zone-head'), heading=button('',()=>openZone(zone.id),'zone-heading');
    const toggle=button('',()=>toggleZone(zone.relative_path),'zone-toggle'), arrow=make('span','▾','zone-arrow');
    arrow.setAttribute('aria-hidden','true');
    const stats=make('span',undefined,'zone-stats status-counts'), panel=make('div',undefined,'zone-body');
    panel.id=`zone-body-${zone.id}`;stats.id=`zone-stats-${zone.id}`;
    toggle.setAttribute('aria-controls',panel.id);toggle.setAttribute('aria-describedby',stats.id);
    const alias=make('span',zone.display_name||zone.filename,'zone-title'), domain=make('span',zone.domain,'zone-domain');
    domain.append(make('span',`(${zone.filename})`,'path'));
    alias.title=[zone.tip,zone.display_name||zone.filename].filter(Boolean).join('\n');
    domain.title=[zone.tip,`${zone.domain}(${zone.filename})`,zone.relative_path].filter(Boolean).join('\n');
    heading.title=zone.tip||'도메인 설정';heading.setAttribute('aria-label',`${zone.domain} 도메인 설정`);
    heading.append(alias,domain,stats);
    toggle.append(arrow);
    const actions=make('div',undefined,'zone-actions'), zoneSelection=selection(`z:${zone.id}`,`${zone.domain} 도메인 선택`);
    zoneSelection.title='도메인 일괄 설정 선택';
    actions.append(zoneSelection,button('레코드 관리',()=>openRecords(zone.id),'zone-manage'));
    const description=make('p',zone.description||'도메인 설명 없음','zone-desc');
    if(!zone.enabled)description.append(make('span','도메인 OFF','badge'));
    head.append(toggle,heading,actions,description);section.append(head);
    const view={zone,section,panel,toggle,arrow,stats};zonePanels.set(zone.id,view);updateZonePanel(view);
    const wrap=make('div',undefined,'table-wrap'), table=make('table'), thead=make('thead'), trh=make('tr');
    for(const text of ['레코드 (IP)','레코드 설명','점검 결과','최근 이력 →'])trh.append(make('th',text));
    thead.append(trh);table.append(thead);const tbody=make('tbody');
    const displayRows=rows.length?rows:[null];
    displayRows.forEach(row=>{
      const tr=make('tr');
      if(!row){const td=make('td','표시할 A 레코드가 없습니다. 레코드 관리에서 직접 추가할 수 있습니다.','empty');td.colSpan=4;tr.append(td);}
      else {
        const key=`${zone.id}:${row.id}`, name=make('td',undefined,'record-cell mono');
        const label=row.alias||row.record||'IP 직접';const link=button(`${label} (${row.ip})`,()=>openRecords(zone.id,row.id));
        link.title=[row.tip,`요청 호스트: ${row.effective.host}`,`적용: interval=${row.effective.interval}, timeout=${row.effective.timeout}, retry=${row.effective.retry}, delay=${row.effective.retry_delay}, ${row.effective.protocol}, https_insecure=${row.effective.https_insecure??false}`].filter(Boolean).join('\n');
        name.append(selection(`r:${key}`,`${label} ${row.ip} 레코드 선택`),document.createTextNode(' '),link);
        if(row.source==='manual')name.append(make('span','수동','badge'));
        const state=make('td',undefined,'state'), history=make('div',undefined,'history'), historyCell=make('td');historyCell.append(history);
        tr.append(name,make('td',row.description,'description'),state,historyCell);statusNodes.set(key,{state,history,panel});
      }
      tbody.append(tr);
    });
    table.append(tbody);wrap.append(table);panel.append(wrap);section.append(panel);fragment.append(section);
  }
  if(!fragment.childNodes.length)fragment.append(make('div','표시할 존이 없습니다. ZONE_PATH와 스캔 오류를 확인하세요.','empty'));
  $('bulk-https-insecure').disabled=!('https_insecure' in catalog.defaults);
  $('groups').replaceChildren(fragment);updateSelection();updateFoldButton();renderStatus();
}
function statusCounts(totals){
  const parts=[
    make('span',`UP ${totals.UP||0}`,'UP'),
    make('span',`DOWN ${totals.DOWN||0}`,'DOWN'),
    make('span',totals.OFF?`OFF ${totals.OFF}`:'','OFF'),
  ];
  for(const state of ['RETRYING','MISSING','UNKNOWN'])if(totals[state])parts.push(make('span',`${state} ${totals[state]}`,state));
  return parts;
}
function renderStatus(){
  const counts={};
  for(const [key,value] of Object.entries(latest.records||{})){
    counts[value.status]=(counts[value.status]||0)+1;const nodes=statusNodes.get(key);if(!nodes||nodes.panel.hidden)continue;
    const current=value.latest;nodes.state.replaceChildren(make('span',value.status,value.status));
    nodes.state.title=current?`${date(current.timestamp)}\n${current.error||'정상'}`:'';
    if(current){nodes.state.append(make('span',`${current.protocol.toUpperCase()} ${current.status||'—'} · ${current.elapsed_ms.toFixed(0)}ms`,'duration'));}
    if(value.maintenance)nodes.state.append(make('span','MAINT','badge'));
    nodes.state.title+=`\n마지막 알림 발생: ${value.alerted_at||'없음'}`;
    if(value.failures)nodes.state.append(make('span',`${value.failures}회 실패 · ${value.duration.toFixed(0)}초`,'duration'));
    const history=document.createDocumentFragment();for(const point of value.history){const tick=make('span',point.ok?'v':'X',`tick ${point.ok?'ok':'fail'}`);tick.title=`${date(point.timestamp)}\n${point.protocol} ${point.status||'—'} · ${point.elapsed_ms.toFixed(1)}ms\n${point.error||'정상'}`;history.append(tick);}nodes.history.replaceChildren(history);
  }
  for(const view of zonePanels.values()){
    const totals={};
    for(const row of view.zone.records){const state=latest.records?.[`${view.zone.id}:${row.id}`]?.status||'UNKNOWN';totals[state]=(totals[state]||0)+1;}
    view.stats.replaceChildren(make('span',`총 ${view.zone.records.length}개`,'zone-total'),...statusCounts(totals));
  }
  $('summary').replaceChildren(...(Object.keys(counts).length?statusCounts(counts):[make('span','등록된 점검 대상 없음')]));
  $('scan-time').textContent=`마지막 존 스캔: ${date(latest.last_scan)}`;
  const errors=Object.entries(latest.scan_errors||{}).map(([path,msg])=>`${path}: ${msg}`);
  for(const [channel,delivery] of Object.entries(latest.alerts||{})){if(!delivery.ok)errors.push(`${channel} 알림 전송 실패: ${delivery.error}`);}
  if(latest.dropped_alerts)errors.push(`알림 대기열 초과: ${latest.dropped_alerts}건`);
  $('errors').textContent=errors.join('\n');
  updateAlertInfo();
}
async function load(){catalog=await api('/api/zones');render();}
async function poll(){if(polling)return;polling=true;try{latest=await api('/api/status');if(latest.revision!==catalog.revision)await load();else renderStatus();}catch(error){inform(`연결 오류: ${error.message}`);}finally{polling=false;}}
function zoneById(id){return catalog.zones.find(z=>z.id===id);}
function createInput(key,value,zoneMode){
  let input;
  if(key==='enabled'||key==='protocol'||key==='https_insecure'||key==='maintenance'){
    input=make('select');const choices={
      enabled:[['','상속 (도메인 설정)'],['1','on'],['0','off']],
      protocol:[['','상속'],['auto','HTTPS 우선 / HTTP 대체'],['http','HTTP'],['https','HTTPS']],
      https_insecure:[['','상속'],['false','false · 인증서 검증'],['true','true · 검증 생략 (curl -k)']],
      maintenance:[['','상속'],['false','false · 일반 알림'],['true','true · 같은 날 중복 알림 생략']],
    }[key];
    for(const [val,text] of choices){if(zoneMode&&key==='enabled'&&val==='')continue;const option=make('option',text);option.value=val;input.append(option);}
  }else if(key==='description'||key==='tip'){input=make('textarea');input.maxLength=4000;}
  else {input=make('input');input.type=inherited.includes(key)?'number':'text';if(input.type==='number'){input.min=key==='retry'?'1':'0.001';input.step=key==='retry'?'1':'any';input.placeholder='비워두면 상속';}else input.maxLength=4000;}
  input.name=key;input.value=value===null||value===undefined?'':String(value);return input;
}
function fields(item,zoneMode,zone){
  $('fields').replaceChildren();
  const keys=zoneMode?['display_name','domain',...inherited.slice(0,4),'enabled',...inherited.slice(4),'description','tip']:['record','ip','alias',...inherited.slice(0,4),'enabled',...inherited.slice(4),'description','tip'];
  for(const key of keys){
    if(key==='https_insecure'&&!('https_insecure' in catalog.defaults))continue;
    if(key==='maintenance'&&!('maintenance' in catalog.defaults))continue;
    const label=make('label',labels[key]);const input=createInput(key,item[key],zoneMode);input.id=`edit-${key}`;if(key==='domain'||key==='ip')input.required=true;label.append(input);
    if(inherited.includes(key)){const parent=zoneMode?catalog.defaults[key]:zone.effective[key];label.append(make('small',`상속값: ${parent} / 적용값: ${item[key]??parent}`));}
    if(key==='enabled'&&!zoneMode)label.append(make('small',`상위 도메인: ${zone.enabled?'on':'off (모든 레코드 중지)'}`));
    if(key==='maintenance')label.append(make('small','점검은 계속합니다. 오늘 13시 이후 마지막 알림이 오늘 13시 전인 레코드는 false로 고정됩니다 (서버 시간).'));
    $('fields').append(label);
  }
  $('form-error').textContent='';
}
function updateAlertInfo(){
  const info=$('record-alert-info');info.hidden=editing?.type!=='record'||editing.recordId===null;
  if(info.hidden)return;
  const row=latest.records?.[`${editing.zoneId}:${editing.recordId}`]||zoneById(editing.zoneId)?.records.find(r=>r.id===editing.recordId);
  info.textContent=`마지막 알림 발생 (alerted_at): ${row?.alerted_at||'없음'} · 현재 maintenance: ${(row?.effective?.maintenance??row?.maintenance)?'true':'false'}`;
}
function showEditor(){updateAlertInfo();if(!$('editor').open)$('editor').showModal();}
function openZone(id){const zone=zoneById(id);editing={zoneId:id,type:'zone'};$('editor-title').textContent=`도메인 설정 · ${zone.filename}`;$('editor-help').textContent='빈 점검 설정은 환경변수를 따릅니다. domain은 실제 HTTP Host와 HTTPS SNI에 사용됩니다.';$('record-picker').replaceChildren();fields(zone,true,zone);showEditor();}
function openRecords(id,rowId=null){
  const zone=zoneById(id);const row=rowId===null?{}:zone.records.find(r=>r.id===rowId);
  editing={zoneId:id,recordId:rowId,type:'record'};$('editor-title').textContent=`레코드 관리 · ${zone.filename}`;
  $('editor-help').textContent='레코드명·IP를 바꾸면 수동 항목이 됩니다. 다음 스캔에서 파일의 원본이 다시 등록됩니다. @는 apex, 빈 레코드명은 IP 직접 점검입니다.';
  const picker=$('record-picker');picker.replaceChildren(button('+ 새 레코드 추가',()=>openRecords(id),''));
  for(const r of zone.records){const line=make('div',undefined,`pick-row ${r.id===rowId?'active':''}`);line.append(button(`${r.alias||r.record||'IP 직접'} (${r.ip})`,()=>openRecords(id,r.id)),make('small',`${r.source==='manual'?'수동':'파일'}${r.effective.present?'':' · 원본 소실'}`));picker.append(line);}
  fields(row,false,zone);showEditor();
}
function settingValue(key,value){return value===''?null:key==='protocol'?value:(key==='https_insecure'||key==='maintenance')?value==='true':Number(value);}
function formValue(form){const patch={};for(const [key,value] of new FormData(form)){if(inherited.includes(key)||key==='enabled'||key==='maintenance'){patch[key]=settingValue(key,value);}else patch[key]=value;}return patch;}
$('edit-form').addEventListener('submit',async event=>{
  event.preventDefault();const submit=event.submitter;submit.disabled=true;
  try{const patch=formValue(event.target), zonePath=`/api/zones/${editing.zoneId}`;const current={...editing};
    if(current.type==='zone')await api(zonePath,'PATCH',patch);
    else if(current.recordId===null)await api(`${zonePath}/records`,'POST',patch);
    else await api(`${zonePath}/records/${current.recordId}`,'PATCH',patch);
    await load();await poll();inform('저장했습니다. 점검 설정을 적용했습니다.');
    if(current.type==='zone')$('editor').close();else openRecords(current.zoneId,current.recordId);
  }catch(error){$('form-error').textContent=error.message;}finally{submit.disabled=false;}
});
$('inherit').addEventListener('click',()=>{for(const key of inherited){const input=$(`edit-${key}`);if(input)input.value='';}if(editing.type==='record')$('edit-enabled').value='';});
$('close-editor').addEventListener('click',()=>$('editor').close());
$('search').addEventListener('input',render);$('show-missing').addEventListener('change',render);
$('clear-selection').addEventListener('click',()=>{selected.clear();render();});
$('toggle-all-zones').addEventListener('click',()=>{
  const collapse=catalog.zones.some(zone=>!collapsedZones.has(zone.relative_path));
  for(const zone of catalog.zones){if(collapse)collapsedZones.add(zone.relative_path);else collapsedZones.delete(zone.relative_path);}
  saveCollapsedZones();
  for(const view of zonePanels.values())updateZonePanel(view);
  updateFoldButton();renderStatus();
});
$('refresh').addEventListener('click',async()=>{const b=$('refresh');b.disabled=true;try{latest=await api('/api/refresh','POST',{});await load();inform('존 파일을 다시 읽었습니다.');}catch(e){inform(e.message);}finally{b.disabled=false;}});
function bulkValue(){const key=$('bulk-field').value;const zoneMode=Array.from(selected).some(k=>k.startsWith('z:'));const input=createInput(key,key==='enabled'?1:null,zoneMode);input.id='bulk-input';$('bulk-value').replaceChildren(input);}
$('bulk').addEventListener('click',()=>{$('bulk-error').textContent='';bulkValue();$('bulk-editor').showModal();});
$('bulk-field').addEventListener('change',bulkValue);$('close-bulk').addEventListener('click',()=>$('bulk-editor').close());
$('bulk-form').addEventListener('submit',async event=>{event.preventDefault();const submit=event.submitter;submit.disabled=true;try{const key=$('bulk-field').value,value=$('bulk-input').value;const patch={[key]:settingValue(key,value)};const operations=Array.from(selected).map(value=>{const [type,zoneId,rowId]=value.split(':');return {...(type==='r'?{record_id:Number(rowId)}:{}),zone_id:Number(zoneId),patch};});await api('/api/bulk-update','POST',{operations});await load();await poll();$('bulk-editor').close();inform(`${operations.length}개 항목을 저장했습니다.`);}catch(error){$('bulk-error').textContent=error.message;}finally{submit.disabled=false;}});
poll();setInterval(poll,2000);
});
