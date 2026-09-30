/* Optional browser regression: PLAYWRIGHT_MODULE=/path/to/@playwright/test node test_report_browser.cjs report.html */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const playwright = require(process.env.PLAYWRIGHT_MODULE || '/usr/local/lib/node_modules/@playwright/test');
(async()=>{
  const browserName=process.env.BROWSER || 'chromium';
  const screenshotDir=process.env.SCREENSHOT_DIR || os.tmpdir();
  const browser=await playwright[browserName].launch({headless:true});
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1080},offline:true,acceptDownloads:true});
    const page=await context.newPage(),errors=[],requests=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('request',r=>{if(/^https?:/.test(r.url()))requests.push(r.url())});
    await page.setContent(fs.readFileSync(process.argv[2],'utf8'),{waitUntil:'load'});
    // The page forbids eval (CSP), which waitForFunction's polling needs in WebKit; poll via evaluate instead.
    for(const deadline=Date.now()+60000;!(await page.evaluate(()=>window.reportReady===true));){
      if(Date.now()>deadline)throw new Error('report did not become ready: '+errors.join('; '));
      await page.waitForTimeout(50);
    }
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
     await page.screenshot({path:path.join(screenshotDir,'energy-report-desktop.png'),fullPage:true});
     const harnessOptions=await page.locator('#harness option').count(),harness=await page.locator('#harness option').nth(1).getAttribute('value'),cacheBefore=await page.locator('#cache-comparisons').innerText();
     if(harness){await page.selectOption('#harness',harness);assert.equal(await page.evaluate(()=>new Set(UsageReport.getSelected().map(r=>r.harness)).size),1);if(harnessOptions>2)assert.notEqual(await page.locator('#cache-comparisons').innerText(),cacheBefore)}
    await page.locator('.advanced summary').click();await page.fill('#search','this-match-does-not-exist-19042026');
    assert.equal(await page.evaluate(()=>UsageReport.getSelected().length),0);
    await page.locator('#chart-empty').isVisible().then(v=>assert.ok(v));
     await page.screenshot({path:path.join(screenshotDir,'energy-report-empty.png'),fullPage:true});
    await page.click('#reset');
    for(const gran of ['hour','minute','day']){await page.click('[data-gran="'+gran+'"]');assert.equal(await page.evaluate(()=>UsageReport.getSelected().length),check.records)}
    if(check.records){await page.locator('#chart .bar').first().click();assert.ok(await page.evaluate(()=>UsageReport.getSelected().length)>0);await page.click('#reset');await page.locator('.session summary').first().click();await page.locator('.turn summary').first().click();await page.locator('.turn table').first().waitFor();assert.ok(await page.locator('.turn table').count()>0)}
    const download=page.waitForEvent('download');await page.click('#export-json');const saved=await download;const file=await saved.path();const exportData=JSON.parse(fs.readFileSync(file,'utf8'));assert.equal(exportData.totals.known_tokens,check.expected);
    await page.setViewportSize({width:390,height:844});
     await page.screenshot({path:path.join(screenshotDir,'energy-report-mobile.png'),fullPage:false});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'mobile overflow');
    const texts=await page.evaluate(()=>['total-label','total','total-note','cache','cache-note','output','reasoning','selection','quality'].map(id=>[id,document.getElementById(id).textContent]));
    const T=Object.fromEntries(texts);assert.match(T['total-label'],/^Totalt( \(minst\))?$/);assert.match(T['total-note'],/^[\d\s\u00a0\u202f]+ anrop · [\d\s\u00a0\u202f]+ sessioner$/);assert.match(T['cache-note'],/^(—|\d+,\d % av all input)$/);assert.match(T.output,/./);assert.match(T.selection,/ anrop · /);assert.ok(/^varav reasoning |^inkl\. reasoning$/.test(T.reasoning));
    const cards=await page.evaluate(()=>[...document.querySelectorAll('.kpis .kpi')].map(k=>({label:k.querySelector('.label').textContent,title:k.querySelector('.value').title,text:k.querySelector('.value').textContent})));
    assert.deepEqual(cards.map(c=>c.label.replace(/ \(minst\)$/,'')),['Totalt','Input','Cache write','Cache read','Output']);
    const exact=t=>{const m=/^([\d\s\u00a0\u202f]+) tokens/.exec(t);assert.ok(m,'title '+t);return Number(m[1].replace(/\D/g,''))};
    const parts=cards.slice(1).map(c=>c.text==='Okänt'?0:exact(c.title));
    assert.equal(parts.reduce((x,y)=>x+y,0),exact(cards[0].title),'four categories sum to total');
    assert.equal(exact(cards[0].title),await page.evaluate(()=>UsageReport.aggregate(UsageReport.getSelected()).known_tokens));
    assert.deepEqual(await page.evaluate(()=>[...document.querySelectorAll('.legend span')].map(s=>s.textContent)),['Input','Cache write','Cache read','Output']);
    assert.equal(await page.evaluate(()=>[UsageReport.short(43812345678),UsageReport.short(136500000),UsageReport.short(12345)].join('|')),'43,8 mdr|136,5 milj.|'+(12345).toLocaleString('sv-SE'));
    // "Dyraste prompterna": the card renders the top prompts of the current selection (at most 5), previews only in private fixtures.
    const promptRows=await page.evaluate(()=>({card:!!document.getElementById('top-prompts'),rows:document.querySelectorAll('#top-prompts tr.prompt-row').length,expected:Math.min(5,UsageReport.topPrompts(UsageReport.getSelected()).length)}));
    assert.ok(promptRows.card);assert.equal(promptRows.rows,promptRows.expected);
    if(process.argv[3]){
      const page2=await context.newPage(),errors2=[];page2.on('pageerror',e=>errors2.push(e.message));
      await page2.setContent(fs.readFileSync(process.argv[3],'utf8'),{waitUntil:'load'});
      for(const deadline=Date.now()+60000;!(await page2.evaluate(()=>window.reportReady===true));){if(Date.now()>deadline)throw new Error('prompts fixture not ready: '+errors2.join('; '));await page2.waitForTimeout(50)}
      const card=await page2.evaluate(()=>({rows:[...document.querySelectorAll('#top-prompts tr.prompt-row')].map(r=>[...r.children].map(c=>c.textContent)),texts:[...document.querySelectorAll('#top-prompts tr.prompt-text')].map(r=>r.textContent),markup:document.querySelectorAll('#top-prompts tr.prompt-text b').length}));
      assert.equal(card.rows.length,4);assert.equal(card.rows[0][0],'1');
      assert.deepEqual(card.rows.map(r=>r[8].replace(/\u00a0/g,' ')),['$9,00','≥$3,00','$0,60','n/a']);assert.equal(card.rows[1][6],'1');assert.equal(card.rows[1][5],'2');
      assert.deepEqual(card.texts,['Refactor the importer','Fix the <b>failing</b> build']);assert.equal(card.markup,0,'preview must be text, not markup');
      await page2.screenshot({path:path.join(screenshotDir,'energy-report-prompts.png'),fullPage:true});
      assert.deepEqual(errors2,[]);
    }
    assert.deepEqual(errors,[]);assert.deepEqual(requests,[]);
    console.log(JSON.stringify({pass:true,browser:browserName,records:check.records,known_tokens:check.actual,network_requests:requests.length,console_errors:errors.length,checks:'summary cards, legend, totals, cache comparisons, bucket conservation, filters, empty state, zoom, drilldown, export, mobile overflow'}));
  } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
