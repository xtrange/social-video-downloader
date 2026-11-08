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
  const videoUploader = document.getElementById('video-uploader');
  const videoTitle = document.getElementById('video-title');
  const videoDuration = document.getElementById('video-duration');
  const copyDescBtn = document.getElementById('copy-desc-btn');
  const videoThumb = document.getElementById('video-thumb');
  const thumbPlaceholder = document.getElementById('thumb-placeholder');
  const videoOptionsList = document.getElementById('video-options-list');
  const audioOptionsList = document.getElementById('audio-options-list');

  let isResetMode = false;
  let analyzedUrl = '';
  let currentDescription = '';

  function slugifyText(text, maxLen) {
    if (!text) return '';
    let clean = String(text).replace(/^@+/, '').trim();
    clean = clean.normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    clean = clean.replace(/https?:\/\/\S+/gi, '');
    clean = clean.replace(/[^a-zA-Z0-9]+/g, '-').replace(/^-+|-+$/g, '').toLowerCase();
    if (maxLen && clean.length > maxLen) {
      clean = clean.slice(0, maxLen).replace(/-+$/g, '');
    }
    return clean;
  }

  function buildDdxFilename(platform, quality, ext, author, title) {
    const parts = ['ddx.cl', slugifyText(platform, 12) || 'video'];
    const authorSlug = slugifyText(author, 16);
    const titleSlug = slugifyText(title, 24);
    if (authorSlug) parts.push(authorSlug);
    if (titleSlug && titleSlug !== authorSlug) parts.push(titleSlug);

    const qClean = String(quality || '')
      .replace(/^(xvar_|fbvar_|fmt_|merge_|res_)/, '');
    const qSlug = slugifyText(qClean, 10);
    if (qSlug) parts.push(qSlug);

    const safeExt = String(ext || 'mp4').toLowerCase().replace(/[^a-z0-9]/g, '') || 'mp4';
    return `${parts.join('-')}.${safeExt}`;
  }

  function parseEstimatedBytes(metaStr) {
    if (!metaStr) return 0;
    const mbMatch = String(metaStr).match(/([\d.]+)\s*MB/i);
    if (mbMatch) {
      return Math.round(parseFloat(mbMatch[1]) * 1024 * 1024);
    }
    const kbMatch = String(metaStr).match(/([\d.]+)\s*KB/i);
    if (kbMatch) {
      return Math.round(parseFloat(kbMatch[1]) * 1024);
    }
    return 0;
  }

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
    currentDescription = '';
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

  async function readResponseWithProgress(response, metaStr, minPct, btn, ext) {
    const contentLengthHeader = response.headers.get('Content-Length');
    const totalBytes = (contentLengthHeader ? parseInt(contentLengthHeader, 10) : 0) || parseEstimatedBytes(metaStr);
    const contentType = response.headers.get('Content-Type') || (ext === 'mp3' ? 'audio/mpeg' : 'video/mp4');

    if (response.body && typeof response.body.getReader === 'function') {
      const reader = response.body.getReader();
      const chunks = [];
      let receivedBytes = 0;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value) {
          chunks.push(value);
          receivedBytes += value.byteLength;
          if (totalBytes > 0) {
            const pct = Math.min(99, Math.max(minPct, Math.round((receivedBytes / totalBytes) * 100)));
            btn.style.setProperty('--dl-progress', `${pct}%`);
            btn.textContent = `Descargando ${pct}%`;
          } else {
            const mb = (receivedBytes / (1024 * 1024)).toFixed(1);
            const pseudoPct = Math.min(95, minPct + Math.round((receivedBytes / (3 * 1024 * 1024)) * 70));
            btn.style.setProperty('--dl-progress', `${pseudoPct}%`);
            btn.textContent = `Descargando ${mb} MB`;
          }
        }
      }
      return new Blob(chunks, { type: contentType });
    }
    return await response.blob();
  }

  async function triggerFileDownload(opt, data, btn) {
    const originalLabel = 'Descargar';
    const url = data.original_url;
    const quality = opt.id;
    const ext = opt.ext || 'mp4';
    const platform = data.platform || 'video';
    const author = data.uploader || '';
    const title = data.description || data.title || '';
    const cleanQualityLabel = opt.label ? opt.label.replace(/^MP[34]\s*·\s*/i, '') : quality;

    btn.disabled = true;
    btn.classList.remove('done');
    btn.classList.add('downloading');
    hideMessage();

    let prepPct = 4;
    btn.style.setProperty('--dl-progress', `${prepPct}%`);
    btn.textContent = `Descargando ${prepPct}%`;

    const prepTimer = setInterval(() => {
      if (prepPct < 24) {
        prepPct += 3;
        btn.style.setProperty('--dl-progress', `${prepPct}%`);
        btn.textContent = `Descargando ${prepPct}%`;
      }
    }, 220);

    try {
      let blob = null;
      let filename = buildDdxFilename(platform, cleanQualityLabel, ext, author, title);

      // PALANCA 1: Intento de descarga 100% directa Navegador <- CDN de origen (0 consumo de tráfico/CPU/disco en ddx.cl)
      if (opt.direct_url && quality !== 'audio') {
        try {
          const directResp = await fetch(opt.direct_url, {
            method: 'GET',
            mode: 'cors',
            credentials: 'omit',
            referrerPolicy: 'no-referrer'
          });
          if (directResp.ok) {
            clearInterval(prepTimer);
            const candidateBlob = await readResponseWithProgress(directResp, opt.meta, prepPct, btn, ext);
            if (candidateBlob && candidateBlob.size > 1024) {
              blob = candidateBlob;
            }
          }
        } catch (_) {
          // Si el CDN bloquea CORS en el navegador, pasa automáticamente en milisegundos al fallback de /api/download
        }
      }

      // FALLBACK AUTOMÁTICO: Si no hubo descarga directa por CORS o requiere unión de audio/conversión MP3
      if (!blob) {
        const fallbackCdnUrl = opt.direct_url || ((data.video_options && data.video_options[0] && data.video_options[0].direct_url) || '');
        const params = new URLSearchParams({
          url,
          quality,
          author: author.slice(0, 60),
          title: title.slice(0, 80),
          cdn_url: fallbackCdnUrl
        });
        const response = await fetch(`/api/download?${params.toString()}`);
        clearInterval(prepTimer);

        if (!response.ok) {
          let detail = 'No se pudo descargar el archivo.';
          try {
            const errData = await response.json();
            if (errData.detail) detail = errData.detail;
          } catch (_) {}
          throw new Error(detail);
        }

        const disposition = response.headers.get('Content-Disposition');
        if (disposition) {
          const match = disposition.match(/filename="([^"]+)"/);
          if (match && match[1]) {
            filename = match[1];
          }
        }

        blob = await readResponseWithProgress(response, opt.meta, prepPct, btn, ext);
      }

      btn.style.setProperty('--dl-progress', '100%');
      btn.textContent = 'Descargando 100%';

      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = blobUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(blobUrl), 10000);

      btn.classList.remove('downloading');
      btn.classList.add('done');
      btn.style.removeProperty('--dl-progress');
      btn.textContent = '✓ Guardado';

      setTimeout(() => {
        btn.classList.remove('done');
        btn.disabled = false;
        btn.textContent = originalLabel;
      }, 2400);
    } catch (err) {
      clearInterval(prepTimer);
      btn.classList.remove('downloading', 'done');
      btn.style.removeProperty('--dl-progress');
      btn.disabled = false;
      btn.textContent = originalLabel;
      showMessage(err.message || 'Error al descargar el archivo.');
    }
  }

  function createOptionRow(opt, data) {
    const row = document.createElement('div');
    row.className = 'option-row';

    const left = document.createElement('div');
    const recBadgeHtml = opt.recommended ? '<span class="rec-badge">Recomendado</span>' : '';
    left.innerHTML = `
      <div class="option-title-row">
        <span class="option-label">${opt.label}</span>
        ${recBadgeHtml}
      </div>
      <div class="option-meta">${opt.meta}</div>
    `;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `dl-btn ${opt.primary ? 'primary' : ''}`;
    btn.textContent = 'Descargar';

    btn.addEventListener('click', () => {
      triggerFileDownload(opt, data, btn);
    });

    row.appendChild(left);
    row.appendChild(btn);
    return row;
  }

  function updateThumbnail(thumbUrl) {
    if (!videoThumb || !thumbPlaceholder) return;
    if (thumbUrl) {
      const proxied = `/api/thumbnail?url=${encodeURIComponent(thumbUrl)}`;
      videoThumb.onload = () => {
        videoThumb.classList.remove('hidden');
        thumbPlaceholder.classList.add('hidden');
      };
      videoThumb.onerror = () => {
        videoThumb.classList.add('hidden');
        thumbPlaceholder.classList.remove('hidden');
      };
      videoThumb.src = proxied;
    } else {
      videoThumb.src = '';
      videoThumb.classList.add('hidden');
      thumbPlaceholder.classList.remove('hidden');
    }
  }

  function renderOptions(data) {
    sourceBadge.textContent = data.source_label || data.platform;
    if (videoUploader) {
      videoUploader.textContent = data.uploader || '';
    }
    currentDescription = data.description || data.title || '';
    videoTitle.textContent = currentDescription;
    videoDuration.textContent = formatDuration(data.duration);
    updateThumbnail(data.thumbnail);

    if (copyDescBtn) {
      copyDescBtn.textContent = 'Copiar texto';
      copyDescBtn.classList.remove('copied');
      copyDescBtn.classList.toggle('hidden', !currentDescription);
    }

    const videoList = data.video_options || (data.options || []).filter((o) => o.id !== 'audio');
    const audioList = data.audio_options || (data.options || []).filter((o) => o.id === 'audio');

    if (videoOptionsList && audioOptionsList) {
      videoOptionsList.innerHTML = '';
      audioOptionsList.innerHTML = '';
      videoList.forEach((opt) => videoOptionsList.appendChild(createOptionRow(opt, data)));
      audioList.forEach((opt) => audioOptionsList.appendChild(createOptionRow(opt, data)));
    }

    resultBox.classList.remove('hidden');
  }

  if (copyDescBtn) {
    copyDescBtn.addEventListener('click', async () => {
      if (!currentDescription) return;
      try {
        await navigator.clipboard.writeText(currentDescription);
        copyDescBtn.textContent = '✓ Copiado';
        copyDescBtn.classList.add('copied');
        setTimeout(() => {
          copyDescBtn.textContent = 'Copiar texto';
          copyDescBtn.classList.remove('copied');
        }, 1800);
      } catch (_) {}
    });
  }

  function checkUrlPrefixOnLoad() {
    try {
      const searchParams = new URLSearchParams(window.location.search);
      const queryUrl = searchParams.get('url');
      if (queryUrl && queryUrl.trim()) {
        const cleanQueryUrl = queryUrl.trim();
        urlInput.value = cleanQueryUrl;
        clearBtn.classList.remove('hidden');
        pasteBtn.classList.add('hidden');
        window.history.replaceState(null, '', '/');
        processUrl(cleanQueryUrl);
        return;
      }

      const rawPath = window.location.pathname || '';
      if (rawPath.length > 1 && !rawPath.startsWith('/static/') && !rawPath.startsWith('/api/')) {
        let candidate = rawPath.slice(1) + (window.location.search || '') + (window.location.hash || '');
        try {
          candidate = decodeURIComponent(candidate);
        } catch (_) {}

        // Fix browser-collapsed single slash after scheme: https:/example.com -> https://example.com
        candidate = candidate.replace(/^(https?):\/([^/])/i, '$1://$2');

        // If user wrote ddx.cl/www.instagram.com/... or ddx.cl/instagram.com/... without scheme
        if (!/^https?:\/\//i.test(candidate) && /^[a-z0-9.-]+\.[a-z]{2,}\//i.test(candidate)) {
          candidate = `https://${candidate}`;
        }

        if (/^https?:\/\//i.test(candidate)) {
          urlInput.value = candidate;
          clearBtn.classList.remove('hidden');
          pasteBtn.classList.add('hidden');
          window.history.replaceState(null, '', '/');
          processUrl(candidate);
        }
      }
    } catch (_) {}
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

  checkUrlPrefixOnLoad();
})();
