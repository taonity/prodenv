function registerAlerts(context) {
  const identity = row => {
    const values = [
      ['Project', row.container_label_com_docker_compose_project || row.project],
      ['Container', row.name || row.container],
      ['Service', row.container_label_com_docker_compose_service || row.service],
      ['Replica', row.container_label_com_docker_compose_container_number],
      ['Filesystem', row.mountpoint],
    ].filter(([, value]) => value !== undefined && value !== '');
    if (!row.name && !row.container && row.instance) values.push(['Target', row.instance]);
    if (!values.length && row.job) values.push(['Monitor', row.job]);
    return values.map(([label, value]) => ({label, value: String(value)}));
  };
  context.handlebars.registerHelper('alertIdentity', identity);
  context.handlebars.registerHelper('historyIdentity', row => identity(row).filter(item => !(row.name || row.container) || !['Service', 'Replica'].includes(item.label)));
  context.handlebars.registerHelper('historyRange', () => {
    const range = context.grafana && context.grafana.timeRange || context.panelData && context.panelData.timeRange;
    const clock = value => new Date(Number(value.valueOf())).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
    return range ? {from: clock(range.from), to: clock(range.to)} : null;
  });
  context.handlebars.registerHelper('historyRows', () => {
    const data = context.panelData;
    const range = context.grafana && context.grafana.timeRange || data && data.timeRange;
    if (!range) return [];
    const from = Number(range.from.valueOf());
    const to = Number(range.to.valueOf());
    const duration = to - from;
    if (!(duration > 0)) return [];
    const clock = timestamp => new Date(timestamp).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    const rank = {critical: 0, error: 1, warning: 2, info: 3};
    const rows = [];
    for (const series of data.series || []) {
      const time = series.fields.find(field => field.type === 'time');
      if (!time) continue;
      const step = Number(data.request && data.request.intervalMs) || (time.values.length > 1 ? time.values[1] - time.values[0] : 60000);
      for (const field of series.fields.filter(field => field.type === 'number' && field.labels && field.labels.alertname)) {
        const intervals = [];
        let interval;
        for (let index = 0; index < time.values.length; index++) {
          const timestamp = Number(time.values[index]);
          if (field.values[index] !== 1 || timestamp < from || timestamp > to) { interval = undefined; continue; }
          if (!interval || timestamp > interval.stop + step / 2) {
            interval = {start: timestamp, stop: timestamp};
            intervals.push(interval);
          }
          interval.stop = Math.min(to, timestamp + step);
        }
        if (!intervals.length) continue;
        rows.push({...field.labels,
          identity: identity(field.labels),
          first: clock(intervals[0].start),
          last: clock(intervals[intervals.length - 1].stop),
          intervals: intervals.map(value => ({
            left: (100 * (value.start - from) / duration).toFixed(4),
            width: (100 * (value.stop - value.start) / duration).toFixed(4),
            label: new Date(value.start).toLocaleString() + ' - ' + new Date(value.stop).toLocaleString(),
          })),
          from: clock(from), to: clock(to),
        });
      }
    }
    const weight = row => Object.prototype.hasOwnProperty.call(rank, row.severity) ? rank[row.severity] : 4;
    return rows.sort((left, right) => weight(left) - weight(right) || left.alertname.localeCompare(right.alertname) || JSON.stringify(left.identity).localeCompare(JSON.stringify(right.identity)));
  });
}

const identityContent = '<dl class="alert-identity">{{#each (alertIdentity this)}}<dt>{{label}}</dt><dd>{{value}}</dd>{{/each}}</dl>';
const originalTarget = '{{#if (alertTarget this)}}<p class="alert-target">{{alertTarget this}}</p>{{/if}}';
const identityStyles = ' .alert-identity { display:grid; grid-template-columns:max-content minmax(0,1fr); gap:2px 10px; margin:6px 0; } .alert-identity dt,.alert-identity dd { margin:0; min-width:0; font-size:12px; line-height:18px; overflow-wrap:anywhere; white-space:normal; } .alert-identity dt { opacity:.7; }';
const historyContent = '<div class="alert-history">{{#with (historyRange)}}<div class="alert-axis"><span>{{from}}</span><span>{{to}}</span></div>{{/with}}{{#each (historyRows)}}<article class="alert-item {{severityClass severity}}"><div class="history-heading"><h3>{{alertTitle alertname}}</h3><span class="history-severity">{{severityText severity}}</span>{{#if demo}}<span class="alert-sample">Sample</span>{{/if}}</div><div class="history-identity">{{#each (historyIdentity this)}}<span><span class="identity-label">{{label}}</span> {{value}}</span>{{/each}}</div><div class="alert-track" role="img" aria-label="Sampled firing intervals from {{from}} to {{to}}">{{#each intervals}}<span class="alert-interval" style="left:{{left}}%;width:{{width}}%" title="{{label}}"></span>{{/each}}</div></article>{{else}}<div class="health-empty">No firing samples returned</div>{{/each}}</div>';
const historyStyles = ' .alert-history { padding:4px 6px; } .alert-history .alert-item { padding:5px 7px; } .history-heading { display:flex; flex-wrap:wrap; align-items:baseline; gap:0 8px; } .alert-history .history-heading h3 { font-size:13px; line-height:18px; margin:0; } .history-severity,.alert-history .alert-sample { font-size:11px; line-height:16px; opacity:.8; } .history-identity { display:flex; flex-wrap:wrap; gap:0 12px; font-size:12px; line-height:17px; } .history-identity > span { min-width:0; overflow-wrap:anywhere; } .identity-label { opacity:.65; } .alert-track { position:relative; height:7px; margin-top:4px; background:rgba(127,127,127,.12); } .alert-interval { position:absolute; top:0; bottom:0; min-width:2px; max-width:100%; background:#f2495c; } .alert-axis { display:flex; justify-content:space-between; gap:8px; font-size:11px; line-height:18px; padding-bottom:3px; opacity:.7; }';

module.exports = {registerAlerts, identityContent, historyContent};

if (require.main === module) {
  const fs = require('fs');
  const path = require('path');
  const layout = 'return (' + require('./panel-layout.cjs').toString() + ').call(this, context);';
  const flatten = panels => panels.flatMap(panel => [panel, ...flatten(panel.panels || [])]);
  const registration = '(' + registerAlerts.toString() + ')(context);';
  for (const name of ['mobile-server', 'mobile-project', 'mobile-overview']) {
    const filename = path.join(__dirname, 'provisioning/dashboards', name + '.json');
    let source = fs.readFileSync(filename, 'utf8');
    const dashboard = JSON.parse(source);
    const panels = flatten(dashboard.panels);
    const alerts = panels.find(panel => (panel.options && panel.options.content || '').includes('alert-list'));
    const updated = {...alerts.options};
    const marker = '\n(function registerAlerts';
    const historyHelpers = updated.helpers.split(marker)[0] + '\n' + registration;
    if (name === 'mobile-server') {
      updated.content = updated.content.replace(identityContent, originalTarget);
      updated.helpers = updated.helpers.split(marker)[0];
      updated.styles = updated.styles.split(' .alert-identity')[0];
    } else {
      updated.content = updated.content.replace(originalTarget, identityContent);
      updated.helpers = historyHelpers;
      if (!updated.styles.includes('.alert-identity')) updated.styles += identityStyles;
    }
    updated.afterRender = layout;
    for (const property of ['content', 'helpers', 'styles', 'afterRender']) source = source.split(JSON.stringify(alerts.options[property])).join(JSON.stringify(updated[property]));
    for (const panel of panels.filter(panel => name === 'mobile-server' ? panel.id === 11 : panel.id === 40)) {
      source = source.split(JSON.stringify(panel.options.afterRender)).join(JSON.stringify(layout));
    }
    if (name === 'mobile-server') {
      const history = panels.find(panel => panel.id === 9);
      const replacement = {...history,
        type: 'marcusolsson-dynamictext-panel',
        fieldConfig: {defaults: {}, overrides: []},
        options: {...updated, renderMode: 'data', content: historyContent, helpers: historyHelpers,
          defaultContent: '<div class="alert-history">No firing samples returned</div>',
          styles: updated.styles + historyStyles},
        targets: [{refId: 'A', expr: alerts.targets[0].expr, legendFormat: '{{alertname}}', range: true}],
      };
      const parserStart = source.lastIndexOf('{', source.indexOf('"id": 9'));
      let depth = 0, quoted = false, escaped = false, end = parserStart;
      for (; end < source.length; end++) {
        const character = source[end];
        if (quoted) { if (escaped) escaped = false; else if (character === '\\') escaped = true; else if (character === '"') quoted = false; }
        else if (character === '"') quoted = true;
        else if (character === '{') depth++;
        else if (character === '}' && --depth === 0) { end++; break; }
      }
      source = source.slice(0, parserStart) + JSON.stringify(replacement, null, '\t').replace(/\n/g, '\n\t\t') + source.slice(end);
    }
    fs.writeFileSync(filename, source);
  }
}