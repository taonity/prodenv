const assert = require('assert').strict;
const os = require('os');
const path = require('path');

function sampleResponse(request, empty = false) {
  const table = (refId, rows) => rows.map(({Value, ...labels}) => ({schema: {refId, meta: {type: 'numeric-multi', typeVersion: [0, 1], custom: {resultType: 'vector'}}, fields: [
    {name: 'Time', type: 'time'}, {name: 'Value', type: 'number', labels},
  ]}, data: {values: [[Date.now()], [Value]]}}));
  const labels = index => ({alertname: 'ContainerWithoutMemoryLimit', severity: index === 0 ? 'critical' : 'warning', demo: 'sample', container_label_com_docker_compose_project: `sample-project-${index}-stage`, container_label_com_docker_compose_service: 'backend', container_label_com_docker_compose_container_number: '1', name: `sample-project-${index}-stage-backend-1`});
    const results = {};
    const pending = [];
    for (const query of request.queries || []) {
      const expression = query.expr || '';
      let frames;
      if (expression.startsWith('ALERTS{')) {
        if (empty) frames = [];
        else if (query.instant) frames = table(query.refId, Array.from({length: 24}, (_, index) => ({...labels(index), Value: 1})));
        else {
          const start = Number(request.from), end = Number(request.to), step = query.intervalMs || 60000;
          const samples = Math.floor((end - start) / step) + 1;
          frames = Array.from({length: 24}, (_, index) => ({schema: {refId: query.refId, meta: {type: 'timeseries-multi', typeVersion: [0, 1], custom: {resultType: 'matrix'}}, fields: [
            {name: 'Time', type: 'time', config: {interval: step}},
            {name: 'Value', type: 'number', labels: labels(index)},
          ]}, data: {values: [Array.from({length: samples}, (_, time) => start + time * step), Array.from({length: samples}, (_, time) => Math.floor(time / 30) % 5 === 2 ? null : 1)]}}));
        }
      } else if (expression.includes('node_filesystem_')) {
        frames = empty ? [] : table(query.refId, Array.from({length: 12}, (_, index) => ({mountpoint: `/data/project-${index}`, Value: query.refId === 'A' ? 1073741824 * (index + 1) : 65})));
      } else if (expression.includes('prodenv_health_container_')) {
        frames = empty ? [] : table(query.refId, Array.from({length: 14}, (_, index) => ({name: `fullstack-starter-prod-worker-${index}`, Value: query.refId === 'D' ? 0 : 1})));
      }
      if (frames) results[query.refId] = {status: 200, frames};
      else pending.push(query);
    }
    return {results, pending};
}

module.exports = async function checkMonitoringLayout(page, origin) {
  const reports = [];
  let empty = false;
  const intercept = async route => {
    const request = route.request().postDataJSON();
    const {results, pending} = sampleResponse(request, empty);
    if (pending.length) {
      const response = await route.fetch({postData: JSON.stringify({...request, queries: pending})});
      Object.assign(results, (await response.json()).results);
    }
    await route.fulfill({json: {results}});
  };
  await page.route('**/api/ds/query*', intercept);
  const panel = id => page.locator(`[data-griditem-key="grid-item-${id}"]`);
  const inspect = async (id, selector, count) => {
    await panel(id).scrollIntoViewIfNeeded();
    try {
      await page.waitForFunction(({id, selector, count}) => document.querySelectorAll(`[data-griditem-key="grid-item-${id}"] ${selector}`).length === count, {id, selector, count});
    } catch (error) {
      throw new Error(`Panel ${id}: ${await panel(id).innerText()}; ${error.message}`);
    }
    await page.waitForFunction(id => {
      const element = document.querySelector(`[data-griditem-key="grid-item-${id}"]`);
      const content = element.querySelector('.alert-history,.alert-list,.health-grid,.health-empty');
      return content && content.getBoundingClientRect().bottom <= element.getBoundingClientRect().bottom;
    }, id);
    return panel(id).evaluate(element => {
      const bounds = element.getBoundingClientRect();
      const horizontalOverflow = element.scrollWidth > element.clientWidth + 2;
      const scrollbars = Array.from(element.querySelectorAll('*')).filter(child => ['auto', 'scroll'].includes(getComputedStyle(child).overflowY) && child.clientHeight > 0 && child.scrollHeight > child.clientHeight + 2).map(child => child.className);
      const overlap = Array.from(element.parentElement.children).filter(sibling => sibling !== element && sibling.classList.contains('react-grid-item')).some(sibling => {
        const other = sibling.getBoundingClientRect();
        return bounds.left < other.right - 2 && bounds.right > other.left + 2 && bounds.top < other.bottom - 2 && bounds.bottom > other.top + 2;
      });
      return {height: Math.round(bounds.height), scrollbars, overlap, horizontalOverflow};
    });
  };
  try {
    for (const width of [1440, 360, 320]) {
      await page.setViewportSize({width, height: 960});
      await page.goto(`${origin}/d/mobile-server?from=now-24h&to=now`);
      const firing = await inspect(7, '.alert-item', 24);
      assert((await panel(7).innerText()).includes('sample-project-23-stage-backend-1'));
      const history = await inspect(9, '.alert-item', 24);
      assert((await panel(9).innerText()).includes('sample-project-23-stage'));
      assert.equal(await panel(9).locator('.alert-axis').count(), 1);
      assert(history.height < (width >= 1000 ? 1800 : 3200), 'Alert history must stay compact while showing all 24 identities');
      await panel(9).screenshot({path: path.join(os.tmpdir(), `monitoring-alerts-${width}.png`)});
      const filesystems = await inspect(11, '.health-grid article', 12);
      await panel(11).screenshot({path: path.join(os.tmpdir(), `monitoring-filesystems-${width}.png`)});
      assert.deepEqual(firing.scrollbars, []);
      assert.deepEqual(history.scrollbars, []);
      assert.deepEqual(filesystems.scrollbars, []);
      assert(!firing.overlap && !history.overlap && !filesystems.overlap, 'Expanded panels must not overlap');
      assert(!firing.horizontalOverflow && !history.horizontalOverflow && !filesystems.horizontalOverflow);
      for (const dashboard of ['mobile-project', 'mobile-overview']) {
        await page.goto(`${origin}/d/${dashboard}?var-project=fullstack-starter-prod`);
        await page.locator(`[data-griditem-key="panel-${dashboard === 'mobile-project' ? 6 : 9}"]`).scrollIntoViewIfNeeded();
        await page.getByText('Diagnostics', {exact: true}).click();
        const health = await inspect(40, '.health-grid article', 14);
        assert.deepEqual(health.scrollbars, []);
        assert(!health.overlap);
        assert(!health.horizontalOverflow);
        await panel(40).screenshot({path: path.join(os.tmpdir(), `monitoring-${dashboard}-health-${width}.png`)});
        reports.push({width, dashboard, health});
      }
      reports.push({width, firing, history, filesystems});
    }
    empty = true;
    await page.goto(`${origin}/d/mobile-server`);
    const emptyHistory = await inspect(9, '.alert-history', 1);
    assert((await panel(9).innerText()).includes('No firing samples returned'));
    assert.deepEqual(emptyHistory.scrollbars, []);
    return reports;
  } finally {
    await page.unrouteAll({behavior: 'wait'});
  }
};

if (require.main === module) {
  const http = require('http');
  const upstream = new URL(process.env.MONITORING_PREVIEW_UPSTREAM || 'http://127.0.0.1:32774');
  const server = http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url, upstream);
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const body = Buffer.concat(chunks);
      const headers = {...request.headers, host: upstream.host, origin: upstream.origin};
      delete headers['content-length'];
      delete headers['accept-encoding'];
      const query = url.pathname === '/api/ds/query' ? JSON.parse(body.toString()) : null;
      const sample = query && sampleResponse(query);
      if (sample && !sample.pending.length) {
        response.writeHead(200, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
        response.end(JSON.stringify({results: sample.results}));
        return;
      }
      const result = await fetch(url, {
        method: request.method, headers, redirect: 'manual',
        body: ['GET', 'HEAD'].includes(request.method) ? undefined : sample ? JSON.stringify({...query, queries: sample.pending}) : body,
      });
      const outputHeaders = Object.fromEntries(result.headers);
      const cookies = result.headers.getSetCookie();
      if (cookies.length) outputHeaders['set-cookie'] = cookies;
      delete outputHeaders['content-encoding'];
      delete outputHeaders['content-length'];
      delete outputHeaders['transfer-encoding'];
      if (outputHeaders.location && outputHeaders.location.startsWith(upstream.origin)) outputHeaders.location = outputHeaders.location.slice(upstream.origin.length) || '/';
      let output;
      if (sample) {
        const original = await result.json();
        output = JSON.stringify({...original, results: {...original.results, ...sample.results}});
      } else if (url.pathname.startsWith('/api/dashboards/uid/mobile-') && result.ok) {
        const dashboard = await result.json();
        dashboard.dashboard.title += ' (Sample preview)';
        output = JSON.stringify(dashboard);
      } else output = Buffer.from(await result.arrayBuffer());
      response.writeHead(result.status, outputHeaders);
      response.end(output);
    } catch (error) {
      response.writeHead(502, {'Content-Type': 'text/plain'});
      response.end('Local sample preview unavailable: ' + error.message);
    }
  });
  server.listen(Number(process.env.MONITORING_PREVIEW_PORT || 0), '127.0.0.1', () => {
    console.log(`Sample preview: http://127.0.0.1:${server.address().port}/d/mobile-server`);
    console.log('Synthetic alerts, filesystems and container health only; upstream production settings are unchanged.');
  });
}