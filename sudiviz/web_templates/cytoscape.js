/*
 * sudiviz/web_templates/cytoscape.js
 *
 * Reusable Cytoscape.js stylesheet + layout config for sudiviz.
 *
 * The main index.html inlines its own copy for self-contained-export builds,
 * but this file is what the FastAPI server ships under /static/cytoscape.js
 * so external tools (e.g. embedded dashboards) can load just the stylesheet
 * without the rest of the page.
 *
 * Exports a global `SudivizGraph` with three helpers:
 *   buildStylesheet()        → array of Cytoscape style rules
 *   layoutOptions(direction) → dagre layout options
 *   applyOrphanFilter(cy, on)→ toggle orphan-only view
 */
(function (root) {
  const COLORS = {
    healthy: '#22c55e',
    warning: '#eab308',
    unhealthy: '#ef4444',
    unreachable: '#6b7280',
    orphan: '#dc2626',
  };

  function buildStylesheet() {
    return [
      {
        selector: 'node',
        style: {
          label: 'data(label)',
          'text-valign': 'center',
          'text-halign': 'center',
          'background-color': '#ffffff',
          'border-width': 2,
          'border-color': '#374151',
          color: '#111827',
          'font-size': 11,
          'text-wrap': 'wrap',
          'text-max-width': 140,
          padding: '12px',
          shape: 'round-rectangle',
          width: 'label',
          height: 'label',
        },
      },
      { selector: 'node.alb',            style: { shape: 'cut-rectangle',   'background-color': '#dbeafe' } },
      { selector: 'node.target_group',   style: { shape: 'round-rectangle', 'background-color': '#ecfeff' } },
      { selector: 'node.instance',       style: { shape: 'round-rectangle', 'background-color': '#f5f3ff' } },
      { selector: 'node.security_group', style: { shape: 'diamond',         'background-color': '#fef3c7' } },
      { selector: 'node.vpc',            style: { shape: 'rectangle',       'background-color': '#f3f4f6' } },
      { selector: 'node.healthy',        style: { 'border-color': COLORS.healthy } },
      { selector: 'node.unhealthy',      style: { 'border-color': COLORS.unhealthy } },
      { selector: 'node.warning, node.initial, node.draining', style: { 'border-color': COLORS.warning } },
      { selector: 'node.unknown, node.unused', style: { 'border-color': COLORS.unreachable } },
      {
        selector: 'node.orphan',
        style: {
          'border-color': COLORS.orphan,
          'border-style': 'dashed',
          'border-width': 3,
          'background-color': '#fee2e2',
        },
      },
      {
        selector: 'edge',
        style: {
          'curve-style': 'bezier',
          'target-arrow-shape': 'triangle',
          width: 2,
          'line-color': '#9ca3af',
          'target-arrow-color': '#9ca3af',
          label: 'data(relation)',
          'font-size': 9,
          color: '#6b7280',
        },
      },
      {
        selector: 'edge.orphan',
        style: {
          'line-style': 'dashed',
          'line-color': COLORS.orphan,
          'target-arrow-color': COLORS.orphan,
          width: 3,
        },
      },
      { selector: 'node:selected', style: { 'border-width': 4, 'border-color': '#2563eb' } },
    ];
  }

  function layoutOptions(direction) {
    return {
      name: 'dagre',
      rankDir: direction || 'LR',
      nodeSep: 60,
      rankSep: 120,
    };
  }

  function applyOrphanFilter(cy, on) {
    if (!cy) return;
    cy.elements().removeClass('hidden');
    if (on) cy.elements().not('.orphan').addClass('hidden');
  }

  root.SudivizGraph = { buildStylesheet, layoutOptions, applyOrphanFilter, COLORS };
})(typeof window !== 'undefined' ? window : globalThis);
