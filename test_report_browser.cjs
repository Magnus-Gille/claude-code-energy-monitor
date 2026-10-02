/* Optional browser regression: PLAYWRIGHT_MODULE=/path/to/@playwright/test node test_report_browser.cjs report.html */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const playwright = require(process.env.PLAYWRIGHT_MODULE || '/usr/local/lib/node_modules/@playwright/test');
const zlib = require('node:zlib');
const L = {
  sv: {lang:'sv', locale:'sv-SE', h1:'Tokenanvändning', prompts:'Dyraste turerna', inputs:'Inmatningar', labels:['Titel','Första inmatningen','Plats','Slutrapport','Aktivitet','PR:er','Commits'], activity:'1 842 shell · 1 redigering · 21 webb', total:'Totalt', totalRe:/^Totalt( \(minst\))?$/, atLeast:/ \(minst\)$/,
    note:/^[\d\s\u00a0\u202f]+ anrop · [\d\s\u00a0\u202f]+ (?:sessioner|session)$/, cache:/^(—|\d+,\d % av all input)$/, selection:/ anrop · /, reasoning:/^varav reasoning |^inkl\. reasoning$/,
    unknown:'Okänt', short:['43,8 mdr','136,5 milj.',(12345).toLocaleString('sv-SE')], money:['$9,00','≥$3,00','$0,60','n/a'], credit:'≈ 15,0 krediter', creditFact:'≈ 15,0 krediter', toggleLabel:'Språk'},
  en: {lang:'en', locale:'en-US', h1:'Token usage', prompts:'Costliest turns', inputs:'Inputs', labels:['Title','Initiating input','Place','Final message','Activity','PRs','Commits'], activity:'1,842 shell · 1 edit · 21 web', total:'Total', totalRe:/^Total( \(at least\))?$/, atLeast:/ \(at least\)$/,
    note:/^[\d,]+ requests? · [\d,]+ sessions?$/, cache:/^(—|\d+\.\d% of all input)$/, selection:/ requests · /, reasoning:/^of which reasoning |^incl\. reasoning$/,
    unknown:'Unknown', short:['43.8B','136.5M','12.3K'], money:['$9.00','≥$3.00','$0.60','n/a'], credit:'≈ 15.0 credits', creditFact:'≈ 15.0 credits', toggleLabel:'Language'},
};
// Cost facts card (fixed fixture clock 2026-09-20): window texts, provenance badges and the figures of both windows.
const INS = {
  sv: {title:'Kostnadsfakta', w:['Senaste 30 dagarna','Hela historiken'], prov:['Beräknad','Uppmätt'], total:'$12,60', totalAll:'≥$21,72', spd:'Hastighet inte loggad, prissatt som standard (anrop: 2).', tier:'Servicenivå inte loggad, prissatt som standard (anrop: 1).', table:'ur den valda pristabellen (hämtad ', lower:'1 av anropen bakom det här faktat har ofullständiga tokenräknare', left:'Utelämnade: 1 anrop med osäker identitet',cplt:'Anrop med ofullständiga tokenräknare som utelämnas ur det här faktat: 1.', unpriced:'2 av 5 (40,0 %)', how:'Så räknas det', assume:'Antaganden', note:'följer inte filtren'},
  en: {title:'Cost facts', w:['Last 30 days','All history'], prov:['Computed','Measured'], total:'$12.60', totalAll:'≥$21.72', spd:'Speed not recorded, priced as standard (requests: 2).', tier:'Service tier not recorded, priced as standard (requests: 1).', table:'from the selected price table (retrieved on ', lower:'1 of the requests behind this fact have incomplete token counters', left:'Left out: 1 requests with an uncertain identity',cplt:'Requests with incomplete token counters left out of this fact: 1.', unpriced:'2 of 5 (40.0%)', how:'How it is computed', assume:'Assumptions', note:'do not follow the filters'},
};
const norm = x => x.replace(/[\s\u00a0\u202f]+/g, ' ');
const facts = page => page.evaluate(() => [...document.querySelectorAll('#ins-body .ins-fact')].map(a => ({id:a.dataset.fact, prov:a.querySelector('.ins-prov').textContent, rows:[...a.querySelectorAll('.ins-row')].map(r => [...r.children].map(c => c.textContent)),
  how:a.querySelector('.ins-how').textContent, assumptions:[...a.querySelectorAll('li')].map(l => l.textContent), markup:a.querySelectorAll('b,i,u,img,script').length})));
// Re-encode a report with an explicit payload language (the page only reads it after decoding).
function withLang(html, lang) {
  return html.replace(/(<script id="report-data" type="application\/octet-stream\+base64">)([A-Za-z0-9+\/=]+)(<\/script>)/, (_, a, b64, c) => {
    const data = JSON.parse(zlib.gunzipSync(Buffer.from(b64, 'base64')).toString('utf8'));
    data.lang = lang;
    return a + zlib.gzipSync(Buffer.from(JSON.stringify(data), 'utf8')).toString('base64') + c;
  });
}
async function ready(page, errors, what = 'report') {
  // The page forbids eval (CSP), which waitForFunction's polling needs in WebKit; poll via evaluate instead.
  for (const deadline = Date.now() + 60000; !(await page.evaluate(() => window.reportReady === true));) {
    if (Date.now() > deadline) throw new Error(what + ' did not become ready: ' + errors.join('; '));
    await page.waitForTimeout(50);
  }
}
(async()=>{
  const browserName=process.env.BROWSER || 'chromium';
  const screenshotDir=process.env.SCREENSHOT_DIR || os.tmpdir();
  const smoke=fs.readFileSync(process.argv[2],'utf8'),fixture=process.argv[3]?fs.readFileSync(process.argv[3],'utf8'):null,sharedFixture=process.argv[4]?fs.readFileSync(process.argv[4],'utf8'):null;if(process.env.REQUIRE_FIXTURES==='1'&&!(fixture&&sharedFixture))throw new Error('REQUIRE_FIXTURES: the prompts and shared fixtures must both be given');
  const browser=await playwright[browserName].launch({headless:true});
  const summary={};
  const newPage=async(opts,html,init)=>{
    const context=await browser.newContext({viewport:{width:1440,height:1080},offline:true,acceptDownloads:true,...opts});
    if(init)await context.addInitScript(init);
    const page=await context.newPage(),errors=[],requests=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('request',r=>{if(/^https?:/.test(r.url()))requests.push(r.url())});
    await page.setContent(html,{waitUntil:'load'});await ready(page,errors);
    return {context,page,errors,requests};
  };
  // The full report regression, once per language (the browser locale picks it: lang "auto").
  async function suite(T) {
    const {context,page,errors,requests}=await newPage({locale:T.locale},smoke);
    assert.equal(await page.evaluate(()=>document.documentElement.lang),T.lang);
    const check=await page.evaluate(()=>{
      const d={records:window.UsageReport.all};
      const total=d.records.filter(r=>!r.id_synthetic).reduce((n,r)=>n+['fresh_input','cache_read','cache_write','output'].reduce((s,k)=>s+(r.tokens[k]??0),0),0);
      const a=window.UsageReport.aggregate(d.records);
       const sample=(dimension,name,tokens,extra={})=>({session:'fixed-session',harness:'fixed-harness',model:'fixed-model',project_id:'fixed-project',id_synthetic:false,tokens,[dimension]:name,...extra});
       const comparisons={};
       for(const dimension of ['session','harness','model','project_id']){
         const rows=[
           sample(dimension,dimension+'-high',{fresh_input:40,cache_read:50,cache_write:10,output:1}),
           sample(dimension,dimension+'-high',{fresh_input:40,cache_read:50,cache_write:10,output:1}),
           sample(dimension,dimension+'-low',{fresh_input:70,cache_read:20,cache_write:10,output:1}),
           sample(dimension,dimension+'-missing',{fresh_input:10,cache_read:null,cache_write:0,output:1}),
           sample(dimension,dimension+'-synthetic',{fresh_input:10,cache_read:90,cache_write:0,output:1},{id_synthetic:true}),
           sample(dimension,null,{fresh_input:10,cache_read:90,cache_write:0,output:1}),
         ];
         comparisons[dimension]=window.UsageReport.cacheComparison(rows,dimension);
       }
      return {expected:total,actual:a.known_tokens,records:d.records.length,buckets:window.UsageReport.groupBuckets(d.records,'hour').reduce((n,b)=>n+b.known_tokens,0),csv:window.UsageReport.csvCell('=1+1'),comparisons};
    });
    assert.equal(check.actual,check.expected);assert.equal(check.buckets,check.expected);assert.ok(check.csv.startsWith('"\''));
     for(const [dimension,result] of Object.entries(check.comparisons)){
       assert.equal(result.total_groups,5);assert.equal(result.compared_groups,2);assert.equal(result.unknown_groups,3);assert.equal(result.compared_input_tokens,300);
       assert.deepEqual(result.best,{name:dimension+'-high',cache_ratio:.5,observations:2,input_tokens:200});
       assert.deepEqual(result.worst,{name:dimension+'-low',cache_ratio:.2,observations:1,input_tokens:100});
     }
    assert.equal(await page.locator('#cache-comparisons [data-cache-dimension]').count(),4);
    assert.equal(await page.locator('h1').innerText(),T.h1);
    assert.equal(await page.locator('#prompts h2').innerText(),T.prompts);
    {
      const I=INS[T.lang];assert.equal(await page.locator('#cost-facts h2').innerText(),I.title);
      assert.ok(norm(await page.locator('#cost-facts > .panel-top p').first().innerText()).includes(I.note));
      assert.deepEqual(await page.locator('#cost-facts [data-win]').allInnerTexts(),I.w);
      const own=await facts(page);  // the smoke data may have no listed prices: facts are then omitted by design; the fixture below asserts them exactly
      for(const f of own){assert.ok(I.prov.includes(f.prov),'provenance badge '+f.prov);assert.equal(f.markup,0);assert.ok(f.how.startsWith(I.how+': '));assert.ok(f.assumptions.length>=1)}
      const cs=own.find(f=>f.id==='context_size'),ms=own.find(f=>f.id==='model_share');if(cs)assert.equal(cs.prov,I.prov[1]);if(ms)assert.equal(ms.prov,I.prov[0]);
      // the card is computed server-side: the filters above do not touch it
      const before=await page.locator('#ins-body').innerText();await page.selectOption('#harness',{index:1});assert.equal(await page.locator('#ins-body').innerText(),before);await page.selectOption('#harness',{index:0});
      assert.equal(await page.locator('#cost-facts [data-win="30d"]').getAttribute('aria-pressed'),'true');
    }
    assert.equal(await page.evaluate(()=>document.title.startsWith('TokenAtlas')),true);
     await page.screenshot({path:path.join(screenshotDir,'energy-report-desktop-'+T.lang+'.png'),fullPage:true});
     const harnessOptions=await page.locator('#harness option').count(),harness=await page.locator('#harness option').nth(1).getAttribute('value'),cacheBefore=await page.locator('#cache-comparisons').innerText();
     if(harness){await page.selectOption('#harness',harness);assert.equal(await page.evaluate(()=>new Set(UsageReport.getSelected().map(r=>r.harness)).size),1);if(harnessOptions>2)assert.notEqual(await page.locator('#cache-comparisons').innerText(),cacheBefore)}
    await page.locator('.advanced summary').click();await page.fill('#search','this-match-does-not-exist-19042026');
    assert.equal(await page.evaluate(()=>UsageReport.getSelected().length),0);
    await page.locator('#chart-empty').isVisible().then(v=>assert.ok(v));
     await page.screenshot({path:path.join(screenshotDir,'energy-report-empty-'+T.lang+'.png'),fullPage:true});
    await page.click('#reset');
    for(const gran of ['hour','minute','day']){await page.click('[data-gran="'+gran+'"]');assert.equal(await page.evaluate(()=>UsageReport.getSelected().length),check.records)}
    if(check.records){await page.locator('#chart .bar').first().click();assert.ok(await page.evaluate(()=>UsageReport.getSelected().length)>0);await page.click('#reset');await page.locator('.session summary').first().click();await page.locator('.turn summary').first().click();await page.locator('.turn table').first().waitFor();assert.ok(await page.locator('.turn table').count()>0)}
    const download=page.waitForEvent('download');await page.click('#export-json');const saved=await download;const file=await saved.path();const exportData=JSON.parse(fs.readFileSync(file,'utf8'));assert.equal(exportData.totals.known_tokens,check.expected);
    await page.setViewportSize({width:390,height:844});
     await page.screenshot({path:path.join(screenshotDir,'energy-report-mobile-'+T.lang+'.png'),fullPage:false});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'mobile overflow');
    const texts=await page.evaluate(()=>['total-label','total','total-note','cache','cache-note','output','reasoning','selection','quality'].map(id=>[id,document.getElementById(id).textContent]));
    const Tx=Object.fromEntries(texts);assert.match(Tx['total-label'],T.totalRe);assert.match(Tx['total-note'],T.note);assert.match(Tx['cache-note'],T.cache);assert.match(Tx.output,/./);assert.match(Tx.selection,T.selection);assert.ok(T.reasoning.test(Tx.reasoning),Tx.reasoning);
    const cards=await page.evaluate(()=>[...document.querySelectorAll('.kpis .kpi')].map(k=>({label:k.querySelector('.label').textContent,title:k.querySelector('.value').title,text:k.querySelector('.value').textContent})));
    assert.deepEqual(cards.map(c=>c.label.replace(T.atLeast,'')),[T.total,'Input','Cache write','Cache read','Output']);
    const exact=t=>{const m=/^([\d\s,.  ]+) tokens/.exec(t);assert.ok(m,'title '+t);return Number(m[1].replace(/\D/g,''))};
    const parts=cards.slice(1).map(c=>c.text===T.unknown?0:exact(c.title));
    assert.equal(parts.reduce((x,y)=>x+y,0),exact(cards[0].title),'four categories sum to total');
    assert.equal(exact(cards[0].title),await page.evaluate(()=>UsageReport.aggregate(UsageReport.getSelected()).known_tokens));
    assert.deepEqual(await page.evaluate(()=>[...document.querySelectorAll('.legend > span')].map(s=>s.textContent)),['Input','Cache write','Cache read','Output']);
    assert.equal(await page.evaluate(()=>[UsageReport.short(43812345678),UsageReport.short(136500000),UsageReport.short(12345)].join('|')),T.short.join('|'));
    // The toggle marks the active language; the export buttons and filter labels are localized too.
    assert.equal(await page.locator('.langtoggle').getAttribute('aria-label'),T.toggleLabel);
    assert.equal(await page.locator('[data-lang="'+T.lang+'"]').getAttribute('aria-pressed'),'true');
    // "Dyraste prompterna" / "Costliest prompts": the card renders the top prompts of the current selection (at most 10), previews only in private fixtures.
    const promptRows=await page.evaluate(()=>({card:!!document.getElementById('top-prompts'),rows:document.querySelectorAll('#top-prompts tr.prompt-row').length,expected:Math.min(10,UsageReport.topPrompts(UsageReport.getSelected()).length)}));
    assert.ok(promptRows.card);assert.equal(promptRows.rows,promptRows.expected);
    assert.deepEqual(errors,[]);assert.deepEqual(requests,[]);
    summary[T.lang]={records:check.records,known_tokens:check.actual,network_requests:requests.length,console_errors:errors.length};
    await context.close();
    if(fixture){
      const {context:c2,page:p2,errors:errors2}=await newPage({locale:T.locale},fixture);
      const card=await p2.evaluate(()=>({rows:[...document.querySelectorAll('#top-prompts tr.prompt-row')].map(r=>[...r.children].map(c=>c.firstChild.textContent)),credits:[...document.querySelectorAll('#top-prompts tr.prompt-row')].map(r=>{const x=r.children[9].querySelector('.cr');return x&&x.textContent}),texts:[...document.querySelectorAll('#top-prompts tr.prompt-text')].map(r=>r.textContent),markup:document.querySelectorAll('#top-prompts tr.prompt-text b').length}));
      assert.equal(card.rows.length,4);assert.equal(card.rows[0][0],'1');assert.deepEqual(card.credits,[null,null,T.credit,null],'credits only for the Codex turn whose requests all have a rate');
      assert.deepEqual(card.rows.map(r=>r[9].replace(/ /g,' ')),T.money);assert.equal(card.rows[1][6],'1');assert.equal(card.rows[1][5],'2');assert.deepEqual(card.rows.map(r=>r[7]),['3','14','–','–'],'inputs column: stored counts, – when unknown');
      assert.equal(await p2.locator('#top-prompts th').nth(9).innerText(),T.lang==='sv'?'Kostnad':'Cost');assert.equal(await p2.locator('#top-prompts th').nth(7).innerText(),T.inputs);
      {// credits in the costliest-turns card: desktop table, then the 390 px layout (the table scrolls inside its wrapper, the page must not)
        const cr=p2.locator('#top-prompts tr.prompt-row .cr');assert.equal(await cr.count(),1);assert.equal((await cr.first().innerText()).trim(),T.credit);assert.equal(await cr.first().isVisible(),true);
        await p2.locator('#prompts').screenshot({path:path.join(screenshotDir,'credits-turns-desktop-'+T.lang+'.png')});
        const vp=p2.viewportSize();await p2.setViewportSize({width:390,height:844});
        await cr.first().scrollIntoViewIfNeeded();assert.equal(await cr.first().isVisible(),true);assert.equal((await cr.first().innerText()).trim(),T.credit,'credits at 390 px');
        const fits=await p2.evaluate(()=>{const c=document.querySelector('#top-prompts tr.prompt-row .cr').getBoundingClientRect(),w=document.querySelector('#top-prompts .table-wrap').getBoundingClientRect();return {inside:c.left>=w.left-1&&c.right<=w.right+1,page:document.documentElement.scrollWidth<=innerWidth+1}});
        assert.ok(fits.page,'390 px: the page must not overflow');
        await p2.locator('#prompts').screenshot({path:path.join(screenshotDir,'credits-turns-390-'+T.lang+'.png')});
        await p2.setViewportSize(vp);
      }
      assert.deepEqual(card.texts,['Refactor the importer','Fix the <b>failing</b> build']);assert.equal(card.markup,0,'preview must be text, not markup');
      await p2.screenshot({path:path.join(screenshotDir,'energy-report-prompts-'+T.lang+'.png'),fullPage:true});
      // Private context: a collapsed details block per stored turn; opening shows title, place, final message and activity, all as text.
      const det=p2.locator('#top-prompts tr.prompt-ctx details.tc');assert.equal(await det.count(),2);
      const costly=det.nth(1);assert.equal(await costly.locator('.tc-row').first().isVisible(),false,'collapsed until opened');
      const tableWidth=()=>p2.evaluate(()=>{const tb=document.querySelector('#top-prompts table')||document.querySelector('#top-prompts');return {table:tb.scrollWidth,box:tb.parentElement.clientWidth}});const before=await tableWidth();await costly.locator('summary').click();assert.equal(await costly.locator('.tc-row').first().isVisible(),true);const after=await tableWidth();assert.ok(after.table<=Math.max(before.table,after.box)+1,'opening context must not widen the table: '+JSON.stringify({before,after}));
      const body=(await costly.innerText()).replace(/\s/g,' ');
      for(const x of [...T.labels,'Fix the <i>build</i> pipeline','Fix the <b>failing</b> build','also <u>lint</u>','and tests','Done: <b>all green</b>','https://example.test/o/app.git · feat/x · /w/app','#16','Fix <b>build</b> order','Add lint',T.activity])assert.ok(body.includes(x),x+' in '+body);
      assert.equal(await p2.locator('#top-prompts details.tc :is(b,i,u)').count(),0,'context must be text, not markup');
      const sparse=det.nth(0);await sparse.locator('summary').click();const sparseText=await sparse.innerText();
      assert.ok(sparseText.includes('Refactor the importer'));for(const x of [T.labels[0],T.labels[2],T.labels[3],T.labels[4]])assert.ok(!sparseText.includes(x),'unknown parts are omitted: '+x);
      await p2.locator('#prompts').screenshot({path:path.join(screenshotDir,'energy-report-context-'+T.lang+'.png')});
      {
        // Section order (#73): the costliest turns come right after the totals, then cost facts and energy; turns are numbered 01
        {const pos=await p2.evaluate(()=>['prompts','cost-facts','energy','sessions','coverage'].map(id=>document.getElementById(id).getBoundingClientRect().top+window.scrollY));
         assert.ok(pos.every((v,i)=>i===0||pos[i-1]<v),'section order prompts < cost facts < energy < sessions < coverage: '+pos);
         const e4=norm(await p2.locator('#prompts [data-t="e4"]').innerText());assert.ok(e4.startsWith('01'),'turns eyebrow: '+e4);}
        // Energy card (#61): an order-of-magnitude estimate that follows the filters, with its range, the unweighted count and the proxy note
        const e=await p2.evaluate(()=>({title:document.querySelector('#energy h2').textContent,value:document.getElementById('energy-value').textContent,range:document.getElementById('energy-range').textContent,unw:document.getElementById('energy-unweighted').textContent,proxy:document.getElementById('energy-proxy').textContent,calc:UsageReport.energyOf(UsageReport.getSelected())}));
        assert.equal(norm(e.title),T.lang==='sv'?'Energi (uppskattning)':'Energy (estimate)');
        assert.ok(e.value.includes('~')&&e.value!=='—','energy value: '+e.value);
        assert.ok(e.range.includes('÷3')&&e.range.includes('×3'),'energy range: '+e.range);
        assert.ok(e.calc.mid>0&&e.calc.unweighted>0&&norm(e.unw).length>0,'unweighted requests are counted and shown: '+e.unw);
        assert.ok(e.proxy.includes('README'),'proxy note: '+e.proxy);
        assert.equal(await p2.locator('#cost-facts article[data-fact="energy"]').count(),0,'energy is not shown as a cost fact');
        // parity: the page's computation over all rows equals the Python energy fact for all history (same rows, constants and factors)
        const par=await p2.evaluate(()=>{const e=UsageReport.energyOf(UsageReport.all),f=UsageReport.data.insights.windows.find(w=>w.id==='all').facts.find(x=>x.id==='energy').values;return {page:e.mid,py:f.mid_mwh,rows:[e.rows,f.requests],unw:[e.unweighted,f.unweighted_requests],inc:[e.incomplete,f.lower_bound_requests],parts:f.parts.map(x=>[x.part,e.parts[x.part],x.mwh])}});
        assert.ok(Math.abs(par.page-par.py)<=1e-9*Math.max(1,par.py),'page '+par.page+' vs python '+par.py);assert.deepEqual(par.rows[0],par.rows[1]);assert.deepEqual(par.unw[0],par.unw[1]);
        assert.deepEqual(par.inc[0],par.inc[1]);assert.ok(par.inc[0]>0,'the fixture has an incomplete row');for(const [k,pg,py] of par.parts)assert.ok(Math.abs(pg-py)<=1e-9*Math.max(1,py),k+': page '+pg+' vs python '+py);
      }
      {
        const I=INS[T.lang],money=async()=>{const f=(await facts(p2)).find(x=>x.id==='model_share');return {ids:(await facts(p2)).map(x=>x.id),total:norm(f.rows.find(r=>r[0]===(T.lang==='sv'?'Prissatt kostnad':'Priced cost'))[1]),unpriced:norm(f.rows.at(-1)[1])}};
        const first=(await facts(p2)).find(x=>x.id==='model_share').assumptions.join(' | ');
        assert.ok(first.includes(I.spd)&&first.includes(I.tier)&&first.includes(I.table),'pricing assumptions with counts and the table date: '+first);assert.ok(!first.includes(I.lower),'the 30-day window has no incomplete request');
        {const cf=(await facts(p2)).find(x=>x.id==='credits');assert.ok(cf,'credits fact');
         assert.ok(cf.rows.some(r=>norm(r[1]).includes(T.creditFact)),'credit equivalent: '+JSON.stringify(cf.rows));
         const at=cf.assumptions.join(' | ');for(const x of (T.lang==='sv'?['motsvarar','inte vad som dragits','standardhastighet','äldre kreditprislista','dollar']:['corresponds to','not what was drawn','standard speed','legacy rate card','not dollars']))assert.ok(at.includes(x),'credits assumption '+x+': '+at);
         await p2.locator('#cost-facts article[data-fact="credits"]').screenshot({path:path.join(screenshotDir,'credits-fact-'+T.lang+'.png')})}
        const a=await money();assert.deepEqual(a.ids,['model_share','price_comparison','cost_parts','context_size','credits']);assert.equal(a.total,I.total);assert.equal(a.unpriced,I.unpriced);
        {const per=norm(await p2.locator('#ins-period').innerText());assert.ok(per.includes('2026-08-21 – 2026-09-20'));assert.ok(!per.includes(I.left),'no exclusion note without excluded requests: '+per)}
        await p2.click('#cost-facts [data-win="all"]');
        const b=await money();assert.deepEqual(b.ids,['model_share','price_comparison','cost_parts','context_size','long_context_premium','subagent_share','credits']);assert.equal(b.total,I.totalAll,'the window toggle switches the values');
        {const ms=(await facts(p2)).find(x=>x.id==='model_share'),txt=ms.assumptions.join(' | ');assert.ok(txt.includes(I.lower)&&txt.includes(I.left),'lower bound and left-out disclosures: '+txt);assert.ok(ms.rows.some(r=>r[1].startsWith('≥')),'amounts marked as lower bounds');
         const ctx=(await facts(p2)).find(x=>x.id==='context_size');assert.ok(ctx.rows[0][1].includes('≥'));
         const lc=(await facts(p2)).find(x=>x.id==='long_context_premium');assert.ok(!lc.assumptions.join(' | ').includes(I.cplt),'the incomplete request is of a model without a long-context tier: nothing to leave out: '+lc.assumptions.join(' | '));assert.ok(!lc.assumptions.join(' | ').includes(I.lower));assert.ok(lc.rows.every(r=>!r[1].includes('≥')),'exact, not a lower bound: '+JSON.stringify(lc.rows))}
        assert.equal(await p2.locator('#cost-facts [data-win="all"]').getAttribute('aria-pressed'),'true');assert.equal(await p2.locator('#cost-facts [data-win="30d"]').getAttribute('aria-pressed'),'false');
        {const per=norm(await p2.locator('#ins-period').innerText());assert.ok(per.includes(T.lang==='sv'?'första anropet – 2026-09-20':'the first request – 2026-09-20'));assert.ok(per.includes(I.left),'the window header discloses left-out requests even without facts: '+per)}
        const all=await facts(p2);assert.deepEqual(all.map(f=>f.prov),all.map(f=>f.id==='context_size'?I.prov[1]:I.prov[0]));assert.ok(all.every(f=>f.markup===0));
        // price ladder: the same tokens at every same-provider model, cost descending, the model used marked, no 'cheapest' framing
        const cmp=all.find(f=>f.id==='price_comparison'),marker=T.lang==='sv'?'(använd modell)':'(model used)',costs=cmp.rows.map(r=>parseFloat(norm(r[1]).replace('$','').replace(/\s/g,'').replace(',','.')));
        assert.ok(cmp.rows.length>=2);assert.equal(cmp.rows.filter(r=>r[0].endsWith(marker)).length,1);assert.deepEqual(costs,[...costs].sort((x,y)=>y-x),'cost descending');
        assert.ok(!/cheap|saving|billig|besparing/i.test(await p2.locator('[data-fact="price_comparison"]').innerText()));
        assert.equal(await p2.locator('[data-fact="price_comparison"] h3 span').first().innerText(),T.lang==='sv'?'Samma tokens till listpris för andra modeller från samma leverantör':"The same tokens at other models' list prices (same provider)");
        await p2.locator('#cost-facts').screenshot({path:path.join(screenshotDir,'energy-report-costfacts-'+T.lang+'.png')});
        await p2.click('[data-lang="'+(T.lang==='sv'?'en':'sv')+'"]');assert.equal(await p2.locator('#cost-facts h2').innerText(),INS[T.lang==='sv'?'en':'sv'].title);assert.equal(await p2.locator('#cost-facts [data-win="all"]').getAttribute('aria-pressed'),'true','the window survives a language switch');
      }
      {
        // Mobile (#80): the costliest turns become cards at 390 px (cost and prompt text on screen, no sideways scroll); the desktop table is unchanged.
        await p2.setViewportSize({width:390,height:844});
        const m=await p2.evaluate(()=>{const box=document.getElementById('top-prompts'),w=box.querySelector('.table-wrap'),W=window.innerWidth,over=e=>e.scrollWidth<=e.clientWidth+1,fit=e=>e.getBoundingClientRect().right<=W&&over(e);
          return {box:over(box),wrap:over(w),doc:document.documentElement.scrollWidth<=W+1,rows:box.querySelectorAll('tr.prompt-row').length,costs:[...box.querySelectorAll('tr.prompt-row td:last-child')].map(fit),texts:[...box.querySelectorAll('tr.prompt-text td')].map(fit),labels:[...box.querySelectorAll('tr.prompt-row')].every(r=>[...r.children].every(c=>c.dataset.label))}});
        assert.ok(m.rows>0&&m.costs.length===m.rows&&m.texts.length>0,'prompt rows with texts at 390 px');
        assert.ok(m.box&&m.wrap&&m.doc,'no horizontal overflow at 390 px: '+JSON.stringify(m));assert.ok(m.costs.every(Boolean),'every cost cell is on screen at 390 px');assert.ok(m.texts.every(Boolean),'every prompt text fits at 390 px');assert.ok(m.labels,'cells carry their localized column label');
        assert.equal(await p2.evaluate(()=>getComputedStyle(document.querySelector('#top-prompts tr.prompt-row')).borderTopWidth),'0px','no rule above the first card');
        await p2.locator('#prompts').screenshot({path:path.join(screenshotDir,'energy-report-prompts-390-'+T.lang+'.png')});
        // A narrow printed page keeps the table: the cards are a screen layout only.
        await p2.emulateMedia({media:'print'});
        assert.ok((await p2.evaluate(()=>[...document.querySelectorAll('#top-prompts th')].map(h=>getComputedStyle(h).display))).every(x=>x!=='none'),'table header kept in print at 390 px');
        await p2.emulateMedia({media:'screen'});
        await p2.setViewportSize({width:1366,height:900});
        const d=await p2.evaluate(()=>[...document.querySelectorAll('#top-prompts th')].map(h=>getComputedStyle(h).display));
        assert.equal(d.length,10);assert.ok(d.every(x=>x!=='none'),'table header visible on desktop: '+d);
        await p2.setViewportSize({width:1440,height:1080});
      }
      assert.deepEqual(errors2,[]);await c2.close();
    }
    if(sharedFixture){
      // Shared: no side-file data at all (the Inputs column is all unknown); no details block, no context text of any kind.
      const {context:c3,page:p3,errors:errors3}=await newPage({locale:T.locale},sharedFixture);
      const shared=await p3.evaluate(()=>({rows:[...document.querySelectorAll('#top-prompts tr.prompt-row')].map(r=>[...r.children].map(c=>c.textContent)),details:document.querySelectorAll('#top-prompts details, #top-prompts tr.prompt-ctx, #top-prompts tr.prompt-text').length,page:document.body.innerText,data:Object.keys(window.UsageReport.data)}));
      assert.deepEqual(shared.rows.map(r=>r[7]),['–','–','–','–']);assert.equal(shared.details,0);assert.equal(await p3.locator('#top-prompts th').nth(7).innerText(),T.inputs);
      for(const x of ['feat/x','Fix the','pipeline','all green','example.test','#16','Add lint'])assert.ok(!shared.page.includes(x),'shared page must not show '+x);
      assert.ok(!shared.data.includes('prompt_inputs')&&!shared.data.includes('prompt_context')&&!shared.data.includes('prompt_texts'));
      {
        const card=await p3.locator('#cost-facts').innerText();for(const x of ['mystery','/w/','secret','s1'])assert.ok(!card.includes(x),'shared cost facts must not show '+x);
        await p3.click('#cost-facts [data-win="all"]');assert.ok((await facts(p3)).length>=5);
      }
      assert.deepEqual(errors3,[]);await c3.close();
    }
  }
  try {
    await suite(L.sv);
    await suite(L.en);
    // Toggle: click EN then SV live, without a reload; <html lang>, labels and number formats follow.
    {
      const {context,page,errors}=await newPage({locale:'sv-SE'},smoke);
      assert.equal(await page.locator('h1').innerText(),L.sv.h1);
      await page.selectOption('#harness',{index:1});
      const before=await page.evaluate(()=>UsageReport.getSelected().length);
      await page.click('[data-lang="en"]');
      assert.equal(await page.locator('h1').innerText(),L.en.h1);
      assert.equal(await page.evaluate(()=>document.documentElement.lang),'en');
      assert.equal(await page.locator('#prompts h2').innerText(),L.en.prompts);
      assert.equal(await page.locator('[data-lang="en"]').getAttribute('aria-pressed'),'true');
      assert.equal(await page.locator('[data-lang="sv"]').getAttribute('aria-pressed'),'false');
      assert.equal(await page.evaluate(()=>UsageReport.short(136500000)),'136.5M');
      assert.equal(await page.locator('#harness option').first().innerText(),'All');
      assert.equal(await page.evaluate(()=>UsageReport.getSelected().length),before,'the filter selection survives a language switch');
      await page.click('[data-lang="sv"]');
      assert.equal(await page.locator('h1').innerText(),L.sv.h1);assert.equal(await page.evaluate(()=>document.documentElement.lang),'sv');
      assert.equal(await page.locator('#harness option').first().innerText(),'Alla');
      assert.deepEqual(errors,[]);await context.close();
    }
    // Explicit payload language beats the browser locale; the toggle still works on top of it.
    {
      const {context,page,errors}=await newPage({locale:'sv-SE'},withLang(smoke,'en'));
      assert.equal(await page.locator('h1').innerText(),L.en.h1);
      await page.click('[data-lang="sv"]');assert.equal(await page.locator('h1').innerText(),L.sv.h1);
      assert.deepEqual(errors,[]);await context.close();
      const other=await newPage({locale:'en-US'},withLang(smoke,'sv'));
      assert.equal(await other.page.locator('h1').innerText(),L.sv.h1);assert.deepEqual(other.errors,[]);await other.context.close();
    }
    // The choice is remembered (file:// origin; storage may legitimately be unavailable, e.g. in WebKit).
    {
      const file=path.join(fs.mkdtempSync(path.join(os.tmpdir(),'tokenatlas-lang-')),'report.html');fs.writeFileSync(file,smoke);
      const context=await browser.newContext({locale:'sv-SE'});const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
      await page.goto('file://'+file);await ready(page,errors);
      await page.click('[data-lang="en"]');
      const stored=await page.evaluate(()=>{try{return localStorage.getItem('tokenatlas-lang')}catch(e){return 'unavailable'}});
      if(stored==='en'){await page.goto('file://'+file);await ready(page,errors);assert.equal(await page.locator('h1').innerText(),L.en.h1,'remembered choice')}
      summary.remembered=stored;assert.deepEqual(errors,[]);await context.close();
    }
    // Storage that throws (blocked cookies, privacy modes) must not break the report or the toggle.
    for(const init of [
      ()=>{Storage.prototype.getItem=function(){throw new Error('blocked')};Storage.prototype.setItem=function(){throw new Error('blocked')}},
      ()=>{Object.defineProperty(window,'localStorage',{configurable:true,get(){throw new DOMException('denied','SecurityError')}})},
    ]){
      const {context,page,errors}=await newPage({locale:'sv-SE'},smoke,init);
      assert.equal(await page.locator('h1').innerText(),L.sv.h1);
      await page.click('[data-lang="en"]');assert.equal(await page.locator('h1').innerText(),L.en.h1);
      assert.equal(await page.evaluate(()=>document.documentElement.lang),'en');
      assert.deepEqual(errors,[]);await context.close();
    }
    console.log(JSON.stringify({pass:true,browser:browserName,prompts_fixture:!!fixture,shared_fixture:!!sharedFixture,...summary,checks:'both languages (summary cards, legend, totals, K/M/B vs mdr/milj., money, cache comparisons, bucket conservation, filters, empty state, zoom, drilldown, export, mobile overflow, prompts card), live toggle, explicit payload language, remembered choice, throwing storage'}));
  } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
