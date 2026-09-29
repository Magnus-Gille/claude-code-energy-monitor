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
    await page.waitForFunction(()=>window.reportReady);
    const check=await page.evaluate(()=>{
      const d=JSON.parse(document.getElementById('report-data').textContent);
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
    assert.deepEqual(errors,[]);assert.deepEqual(requests,[]);
    console.log(JSON.stringify({pass:true,browser:browserName,records:check.records,known_tokens:check.actual,network_requests:requests.length,console_errors:errors.length,checks:'totals, cache comparisons, bucket conservation, filters, empty state, zoom, drilldown, export, mobile overflow'}));
  } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
