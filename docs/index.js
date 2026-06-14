const REPO = 'pydevsg/sudiviz';

async function loadPypiVersion() {
  const res = await fetch('https://pypi.org/pypi/sudiviz/json');
  const data = await res.json();
  const version = data.info.version;
  const released = data.urls?.[0]?.upload_time;
  document.getElementById('pypi-version').textContent = `v${version}`;
  if (released) {
    const date = new Date(released).toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' });
    document.getElementById('pypi-date').textContent = `Released ${date}`;
  }
}

async function loadChangelog() {
  const res = await fetch(`https://api.github.com/repos/${REPO}/tags?per_page=10`);
  const tags = await res.json();
  if (!Array.isArray(tags) || tags.length === 0) return;

  const container = document.getElementById('changelog-list');
  container.innerHTML = '';

  const withDetails = await Promise.all(tags.map(async tag => {
    try {
      const r = await fetch(tag.commit.url);
      const c = await r.json();
      const date = c.commit?.author?.date
        ? new Date(c.commit.author.date).toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })
        : null;
      const lines = (c.commit?.message || '')
        .split('\n')
        .map(l => l.replace(/^[\s*\-]+/, '').trim())
        .filter(l => l.length > 0 && !/^co-authored-by/i.test(l));
      // Use only the first line as the single bullet
      const firstLine = lines[0] || '';
      // Strip leading version prefix like "v1.0.0 - " from the line
      const cleaned = firstLine.replace(/^v?\d+\.\d+[\.\d]*\s*[-–]\s*/i, '').trim();
      const bullets = cleaned ? [cleaned] : [];
      return { name: tag.name, date, bullets };
    } catch {
      return { name: tag.name, date: null, bullets: [] };
    }
  }));

  for (const { name, date, bullets } of withDetails) {
    const version = name.startsWith('v') ? name : `v${name}`;
    const li = bullets.map(b => `<li>${b}</li>`).join('');
    const item = document.createElement('div');
    item.className = 'changelog-item';
    item.innerHTML = `
      <span class="version">${version}</span>${date ? ` — <span class="changelog-date">${date}</span>` : ''}
      ${li ? `<ul>${li}</ul>` : ''}
    `;
    container.appendChild(item);
  }
}

function typeCommand() {
  const el = document.getElementById('typed-cmd');
  const text = 'pip install sudiviz';
  let i = 0;
  el.textContent = '';
  const typing = setInterval(() => {
    el.textContent = text.slice(0, ++i);
    if (i === text.length) {
      clearInterval(typing);
      setTimeout(() => {
      const erasing = setInterval(() => {
        el.textContent = el.textContent.slice(0, -1);
        if (el.textContent.length === 0) {
          clearInterval(erasing);
          typeCommand();
        }
      }, 30);
      }, 10000);
    }
  }, 60);
}

loadPypiVersion().catch(() => {});
loadChangelog().catch(() => {});
typeCommand();
