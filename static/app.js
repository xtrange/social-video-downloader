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

  let isResetMode = false;
  let analyzedUrl = '';

  function setButtonMode(mode) {
    if (mode === 'loading') {
      isResetMode = false;
      submitBtn.disabled = true;
      submitBtn.classList.remove('secondary-mode');
      submitBtn.textContent = 'Analizando...';
    } else if (mode === 'reset') {
      isResetMode = true;
      submitBtn.disabled = false;
      submitBtn.classList.add('secondary-mode');
      submitBtn.textContent = 'Probar otro enlace';
    } else {
      isResetMode = false;
      submitBtn.disabled = false;
      submitBtn.classList.remove('secondary-mode');
      submitBtn.textContent = 'Continuar';
    }
  }

  function resetAll() {
    urlInput.value = '';
    analyzedUrl = '';
    clearBtn.classList.add('hidden');
    pasteBtn.classList.remove('hidden');
    resultBox.classList.add('hidden');
    loadingBox.classList.add('hidden');
    hideMessage();
    setButtonMode('continue');
    urlInput.focus();
  }

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
      setButtonMode('continue');
      showMessage('Pega el enlace del post para identificar las opciones disponibles.');
      return;
    }

    resultBox.classList.add('hidden');
    loadingBox.classList.remove('hidden');
    setButtonMode('loading');

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
      analyzedUrl = url;
      renderOptions(data);
      setButtonMode('reset');
    } catch (err) {
      setButtonMode('continue');
      showMessage(err.message || 'Enlace no compatible.');
    } finally {
      loadingBox.classList.add('hidden');
    }
  }

  async function triggerFileDownload(url, quality, ext, platform, btn) {
    const originalLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Descargando...';
    hideMessage();

    try {
      const params = new URLSearchParams({ url, quality });
      const response = await fetch(`/api/download?${params.toString()}`);
      if (!response.ok) {
        let detail = 'No se pudo descargar el archivo.';
        try {
          const errData = await response.json();
          if (errData.detail) detail = errData.detail;
        } catch (_) {}
        throw new Error(detail);
      }

      const blob = await response.blob();
      const filename = `video_${platform || 'social'}_${quality}.${ext || 'mp4'}`;
      const blobUrl = URL.createObjectURL(blob);

      const a = document.createElement('a');
      a.href = blobUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(blobUrl), 10000);
    } catch (err) {
      showMessage(err.message || 'Error al descargar el video.');
    } finally {
      btn.disabled = false;
      btn.textContent = originalLabel;
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
        triggerFileDownload(data.original_url, opt.id, opt.ext, data.platform, btn);
      });

      row.appendChild(left);
      row.appendChild(btn);
      optionsList.appendChild(row);
    });

    resultBox.classList.remove('hidden');
  }

  urlInput.addEventListener('input', () => {
    const val = urlInput.value.trim();
    const hasVal = val.length > 0;
    clearBtn.classList.toggle('hidden', !hasVal);
    pasteBtn.classList.toggle('hidden', hasVal);
    hideMessage();
    if (val !== analyzedUrl) {
      setButtonMode('continue');
    } else if (analyzedUrl && !resultBox.classList.contains('hidden')) {
      setButtonMode('reset');
    }
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
    resetAll();
  });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (isResetMode) {
      resetAll();
    } else {
      processUrl(urlInput.value);
    }
  });
})();
