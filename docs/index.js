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
  const res = await fetch('changelog.json');
  const entries = await res.json();
  if (!Array.isArray(entries) || entries.length === 0) return;

  const container = document.getElementById('changelog-list');
  container.innerHTML = '';

  for (const { version, date, summary } of entries) {
    const v = version.startsWith('v') ? version : `v${version}`;
    const formattedDate = date
      ? new Date(date).toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })
      : null;
    const item = document.createElement('div');
    item.className = 'changelog-item';
    item.innerHTML = `
      <span class="version">${v}</span>${formattedDate ? ` — <span class="changelog-date">${formattedDate}</span>` : ''}
      ${summary ? `<ul><li>${summary}</li></ul>` : ''}
    `;
    container.appendChild(item);
  }
}

function typeCommand() {
  const el = document.getElementById('typed-cmd');
  const text = 'pip install sudiviz[all]';
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
