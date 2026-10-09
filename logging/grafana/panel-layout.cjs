module.exports = function fitPanel(context) {
  const root = context.element;
  const panel = root.closest('.react-grid-item');
  const content = root.querySelector('.alert-list,.alert-history,.health-grid,.health-empty');
  if (!panel || !content) return;
  const grid = panel.parentElement;
  const key = Symbol.for('prodenv.contentLayout');
  if (!grid[key]) {
    const entries = new Map();
    const saved = new Map();
    const offsets = new Map();
    let frame = 0;
    const set = (element, property, value) => {
      if (!saved.has(element)) saved.set(element, new Map());
      const properties = saved.get(element);
      if (!properties.has(property)) properties.set(property, [element.style.getPropertyValue(property), element.style.getPropertyPriority(property)]);
      if (element.style.getPropertyValue(property) !== value || element.style.getPropertyPriority(property) !== 'important') element.style.setProperty(property, value, 'important');
    };
    const restore = () => {
      for (const [element, properties] of saved) for (const [property, [value, priority]] of properties) {
        if (value) element.style.setProperty(property, value, priority);
        else element.style.removeProperty(property);
      }
      saved.clear();
      offsets.clear();
    };
    const resize = () => {
      frame = 0;
      const siblings = Array.from(grid.children).filter(element => element.classList.contains('react-grid-item'));
      const geometry = new Map(siblings.map(element => [element, {
        top: element.getBoundingClientRect().top - (offsets.get(element) || 0),
        height: parseFloat(element.style.height) || 0,
        absolute: getComputedStyle(element).position === 'absolute',
      }]));
      const changes = [];
      for (const [element, entry] of entries) {
        if (!element.isConnected || !geometry.has(element)) continue;
        for (let ancestor = entry.root; ancestor && ancestor !== element; ancestor = ancestor.parentElement) {
          set(ancestor, 'height', 'auto');
          set(ancestor, 'max-height', 'none');
          set(ancestor, 'overflow', 'visible');
        }
        const height = Math.ceil(entry.content.getBoundingClientRect().bottom - element.getBoundingClientRect().top + 12);
        set(element, 'min-height', height + 'px');
        set(element, 'max-height', height + 'px');
        const original = geometry.get(element);
        if (original.absolute) changes.push({element, bottom: original.top + original.height, delta: height - original.height});
      }
      for (const [element, original] of geometry) {
        const offset = original.absolute ? changes.reduce((total, change) => total + (change.element !== element && original.top >= change.bottom - 1 ? change.delta : 0), 0) : 0;
        offsets.set(element, offset);
        set(element, 'translate', offset ? '0px ' + offset + 'px' : '0px');
      }
      const height = (parseFloat(grid.style.height) || 0) + changes.reduce((total, change) => total + change.delta, 0);
      set(grid, 'min-height', Math.max(0, height) + 'px');
      set(grid, 'max-height', 'none');
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(resize); };
    const observer = new ResizeObserver(schedule);
    const mutations = new MutationObserver(schedule);
    mutations.observe(grid, {childList: true, subtree: true, attributes: true, attributeFilter: ['style']});
    grid[key] = {entries, observer, schedule, remove(element) {
      observer.unobserve(entries.get(element).content);
      entries.delete(element);
      restore();
      if (entries.size) schedule();
      else {
        observer.disconnect();
        mutations.disconnect();
        if (frame) cancelAnimationFrame(frame);
        delete grid[key];
      }
    }};
  }
  const layout = grid[key];
  layout.entries.set(panel, {root, content});
  layout.observer.observe(content);
  const button = root.querySelector('.alert-toggle');
  const update = () => {
    root.querySelectorAll('.alert-details').forEach(details => { details.hidden = !this.expanded; });
    if (button) {
      button.textContent = this.expanded ? 'Collapse details' : 'Expand details';
      button.setAttribute('aria-expanded', String(Boolean(this.expanded)));
    }
    layout.schedule();
  };
  const toggle = () => { this.expanded = !this.expanded; update(); };
  if (button) button.addEventListener('click', toggle);
  update();
  return () => {
    if (button) button.removeEventListener('click', toggle);
    layout.remove(panel);
  };
};