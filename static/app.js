(function () {
  'use strict';

  const form = document.getElementById('dl-form');
  const urlInput = document.getElementById('url-input');
  const pasteBtn = document.getElementById('paste-btn');
  const clearBtn = document.getElementById('clear-btn');
  const submitBtn = document.getElementById('submit-btn');
  const feedbackBox = document.getElementById('feedback-box');
  const loadingBox = document.getElementById('loading-box');
  const resultBox = document.getElementById('result-box');
  const sourceBadge = document.getElementById('source-badge');
  const videoTitle = document.getElementById('video-title');
  const videoDuration = document.getElementById('video-duration');
  const optionsList = document.getElementById('options-list');

  function formatDuration(secs) {
    const s = Math.max(1, Math.round(Number(secs) || 15));
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`;
  }

  function showMessage(msg) {
    feedbackBox.textContent = msg;
    feedbackBox.classList.remove('hidden');
  }

  function hideMessage() {
    feedbackBox.textContent = '';
    feedbackBox.classList.add('hidden');
  }

  async function processUrl(rawUrl) {
    const url = (rawUrl || '').trim();
    hideMessage();

    if (!url) {
      resultBox.classList.add('hidden');
      showMessage('Pega el enlace del post para identificar las opciones disponibles.');
      return;
    }

    resultBox.classList.add('hidden');
    loadingBox.classList.remove('hidden');
    submitBtn.disabled = true;

    try {
      const res = await fetch('/api/info', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url })
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || 'No se pudo analizar el enlace.');
      }
      renderOptions(data);
    } catch (err) {
      showMessage(err.message || 'Enlace no compatible.');
    } finally {
      loadingBox.classList.add('hidden');
      submitBtn.disabled = false;
    }
  }

  function renderOptions(data) {
    sourceBadge.textContent = data.source_label || data.platform;
    videoTitle.textContent = data.title || '';
    videoDuration.textContent = formatDuration(data.duration);
    optionsList.innerHTML = '';

    (data.options || []).forEach((opt) => {
      const row = document.createElement('div');
      row.className = 'option-row';

      const left = document.createElement('div');
      left.innerHTML = `
        <div class="option-label">${opt.label}</div>
        <div class="option-meta">${opt.meta}</div>
      `;

      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `dl-btn ${opt.primary ? 'primary' : ''}`;
      btn.textContent = 'Descargar';

      btn.addEventListener('click', () => {
        if (opt.direct_url) {
          window.open(opt.direct_url, '_blank', 'noopener,noreferrer');
        } else {
          const params = new URLSearchParams({ url: data.original_url, quality: opt.id });
          window.location.href = `/api/download?${params.toString()}`;
        }
      });

      row.appendChild(left);
      row.appendChild(btn);
      optionsList.appendChild(row);
    });

    resultBox.classList.remove('hidden');
  }

  urlInput.addEventListener('input', () => {
    const hasVal = urlInput.value.trim().length > 0;
    clearBtn.classList.toggle('hidden', !hasVal);
    pasteBtn.classList.toggle('hidden', hasVal);
    hideMessage();
  });

  urlInput.addEventListener('paste', () => {
    setTimeout(() => {
      const val = urlInput.value.trim();
      clearBtn.classList.toggle('hidden', !val);
      pasteBtn.classList.toggle('hidden', !!val);
      if (val) processUrl(val);
    }, 30);
  });

  pasteBtn.addEventListener('click', async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text && text.trim()) {
        urlInput.value = text.trim();
        urlInput.dispatchEvent(new Event('input'));
        processUrl(urlInput.value);
      } else {
        urlInput.focus();
      }
    } catch (_) {
      urlInput.focus();
    }
  });

  clearBtn.addEventListener('click', () => {
    urlInput.value = '';
    urlInput.dispatchEvent(new Event('input'));
    resultBox.classList.add('hidden');
    hideMessage();
    urlInput.focus();
  });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    processUrl(urlInput.value);
  });
})();
