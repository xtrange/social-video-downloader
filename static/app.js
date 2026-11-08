(function () {
  'use strict';

  const form = document.getElementById('dl-form');
  const urlInput = document.getElementById('url-input');
  const pasteBtn = document.getElementById('paste-btn');
  const clearBtn = document.getElementById('clear-btn');
  const submitBtn = document.getElementById('submit-btn');
  const themeToggleBtn = document.getElementById('theme-toggle-btn');
  const themeIcon = document.getElementById('theme-icon');
  const feedbackBox = document.getElementById('feedback-box');
  const loadingBox = document.getElementById('loading-box');
  const resultBox = document.getElementById('result-box');
  const carouselTabs = document.getElementById('carousel-tabs');
  const sourceBadge = document.getElementById('source-badge');
  const videoUploader = document.getElementById('video-uploader');
  const videoTitle = document.getElementById('video-title');
  const toggleDescBtn = document.getElementById('toggle-desc-btn');
  const videoDuration = document.getElementById('video-duration');
  const copyDescBtn = document.getElementById('copy-desc-btn');
  const dlThumbBtn = document.getElementById('dl-thumb-btn');
  const thumbWrap = document.getElementById('thumb-wrap');
  const videoThumb = document.getElementById('video-thumb');
  const videoPreviewPlayer = document.getElementById('video-preview-player');
  const thumbPlaceholder = document.getElementById('thumb-placeholder');
  const thumbPlayOverlay = document.getElementById('thumb-play-overlay');
  const videoOptionsList = document.getElementById('video-options-list');
  const audioOptionsList = document.getElementById('audio-options-list');

  let isResetMode = false;
  let analyzedUrl = '';
  let currentDescription = '';
  let currentThumbUrl = '';
  let currentData = null;
  let currentCarouselIndex = 0;
  let currentPreviewPrimaryUrl = '';
  let currentPreviewFallbackUrl = '';
  let isPlayingPreview = false;

  // Mejora 9: Modo Oscuro / Claro automático con selector minimalista y memoria en localStorage
  function getActiveTheme() {
    const attr = document.documentElement.getAttribute('data-theme');
    if (attr === 'dark' || attr === 'light') return attr;
    try {
      const saved = localStorage.getItem('ddx_theme');
      if (saved === 'dark' || saved === 'light') return saved;
    } catch (_) {}
    return (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
  }

  function applyTheme(theme) {
    const resolved = theme === 'dark' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', resolved);
    if (themeIcon) {
      themeIcon.textContent = resolved === 'dark' ? '☀' : '☾';
    }
    if (themeToggleBtn) {
      themeToggleBtn.title = resolved === 'dark' ? 'Cambiar a modo claro' : 'Cambiar a modo oscuro';
    }
  }

  applyTheme(getActiveTheme());

  if (themeToggleBtn) {
    themeToggleBtn.addEventListener('click', () => {
      const nextTheme = getActiveTheme() === 'dark' ? 'light' : 'dark';
      try {
        localStorage.setItem('ddx_theme', nextTheme);
      } catch (_) {}
      applyTheme(nextTheme);
    });
  }

  // Mejora 4: Registrar Service Worker para PWA e integración con "Compartir -> DDX.cl" en móviles
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/static/sw.js').catch(() => {});
    });
  }

  // Mejora 6: Limpiador automático de enlaces "sucios" (extrae solo la URL si viene acompañada de texto)
  function extractCleanUrl(rawText) {
    if (!rawText) return '';
    const trimmed = String(rawText).trim();
    const httpMatch = trimmed.match(/https?:\/\/[^\s"'<>]+/i);
    if (httpMatch) {
      return httpMatch[0].replace(/[),.;!?]+$/, '');
    }
    const domainMatch = trimmed.match(/(?:www\.|vm\.|vt\.|m\.)?(?:instagram\.com|instagr\.am|tiktok\.com|facebook\.com|fb\.watch|x\.com|twitter\.com|youtube\.com|youtu\.be|reddit\.com|redd\.it|pinterest\.[a-z.]+|pin\.it|threads\.net|threads\.com|linkedin\.com|lnkd\.in|vimeo\.com|twitch\.tv|kick\.com|bsky\.app)\/[^\s"'<>]+/i);
    if (domainMatch) {
      const cleaned = domainMatch[0].replace(/[),.;!?]+$/, '');
      return /^https?:\/\//i.test(cleaned) ? cleaned : `https://${cleaned}`;
    }
    return trimmed;
  }

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

  function buildDdxFilename(platform, quality, ext, author, title, slideIndex) {
    const parts = ['ddx.cl', slugifyText(platform, 12) || 'video'];
    const authorSlug = slugifyText(author, 16);
    const titleSlug = slugifyText(title, 24);
    if (authorSlug) parts.push(authorSlug);
    if (titleSlug && titleSlug !== authorSlug) parts.push(titleSlug);
    if (typeof slideIndex === 'number' && slideIndex >= 0) {
      parts.push(`v${slideIndex + 1}`);
    }

    const qClean = String(quality || '')
      .replace(/^(xvar_|fbvar_|igvar_|tkvar_|rdvar_|fmt_|merge_|res_)/, '');
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

  function stopPreviewPlayer() {
    isPlayingPreview = false;
    if (thumbWrap) thumbWrap.classList.remove('playing');
    if (videoPreviewPlayer) {
      try {
        videoPreviewPlayer.pause();
      } catch (_) {}
      videoPreviewPlayer.removeAttribute('src');
      videoPreviewPlayer.classList.add('hidden');
    }
    if (videoDuration) videoDuration.classList.remove('hidden');
    if (currentThumbUrl && videoThumb) {
      videoThumb.classList.remove('hidden');
      if (thumbPlayOverlay && currentPreviewPrimaryUrl) {
        thumbPlayOverlay.classList.remove('hidden');
      }
    }
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
    stopPreviewPlayer();
    urlInput.value = '';
    analyzedUrl = '';
    currentDescription = '';
    currentThumbUrl = '';
    currentData = null;
    currentCarouselIndex = 0;
    currentPreviewPrimaryUrl = '';
    currentPreviewFallbackUrl = '';
    if (carouselTabs) {
      carouselTabs.innerHTML = '';
      carouselTabs.classList.add('hidden');
    }
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
    const url = extractCleanUrl(rawUrl);
    if (url && urlInput.value.trim() !== url) {
      urlInput.value = url;
      clearBtn.classList.remove('hidden');
      pasteBtn.classList.add('hidden');
    }
    hideMessage();
    stopPreviewPlayer();

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

  async function triggerFileDownload(opt, data, btn, shareBtn) {
    const originalLabel = 'Descargar';
    const url = data.original_url;
    const quality = opt.id;
    const ext = opt.ext || 'mp4';
    const platform = data.platform || 'video';
    const author = data.uploader || '';
    const title = data.description || data.title || '';
    const cleanQualityLabel = opt.label ? opt.label.replace(/^MP[34]\s*·\s*/i, '') : quality;
    const isCarousel = Array.isArray(data.carousel_items) && data.carousel_items.length > 1;
    const slideIdx = isCarousel ? currentCarouselIndex : undefined;

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
      let filename = buildDdxFilename(platform, cleanQualityLabel, ext, author, title, slideIdx);

      // PALANCA 1: Intento de descarga 100% directa Navegador <- CDN de origen (0 consumo en Vercel)
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
        } catch (_) {}
      }

      // FALLBACK AUTOMÁTICO: Si no hubo descarga directa por CORS o requiere unión de audio/conversión MP3
      if (!blob) {
        const activeVideoOpts = isCarousel && data.carousel_items[currentCarouselIndex]
          ? data.carousel_items[currentCarouselIndex].video_options
          : data.video_options;
        const fallbackCdnUrl = opt.direct_url || ((activeVideoOpts && activeVideoOpts[0] && activeVideoOpts[0].direct_url) || '');
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

        if (!isCarousel) {
          const disposition = response.headers.get('Content-Disposition');
          if (disposition) {
            const match = disposition.match(/filename="([^"]+)"/);
            if (match && match[1]) {
              filename = match[1];
            }
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
      setTimeout(() => URL.revokeObjectURL(blobUrl), 15000);

      // Mejora 5: Habilitar botón nativo "Compartir" en móviles (Web Share API) con el archivo descargado
      if (shareBtn && typeof navigator.share === 'function') {
        try {
          const mimeType = ext === 'mp3' ? 'audio/mpeg' : 'video/mp4';
          const fileObj = new File([blob], filename, { type: mimeType });
          if (!navigator.canShare || navigator.canShare({ files: [fileObj] })) {
            shareBtn.classList.remove('hidden');
            shareBtn.onclick = async () => {
              try {
                await navigator.share({
                  files: [fileObj],
                  title: filename,
                  text: 'Descargado en ddx.cl'
                });
              } catch (_) {}
            };
          }
        } catch (_) {}
      }

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

    const actionsWrap = document.createElement('div');
    actionsWrap.className = 'option-actions';

    const shareBtn = document.createElement('button');
    shareBtn.type = 'button';
    shareBtn.className = 'share-file-btn hidden';
    shareBtn.title = 'Compartir archivo en WhatsApp, Telegram o AirDrop';
    shareBtn.textContent = 'Compartir';

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `dl-btn ${opt.primary ? 'primary' : ''}`;
    btn.textContent = 'Descargar';

    btn.addEventListener('click', () => {
      triggerFileDownload(opt, data, btn, shareBtn);
    });

    actionsWrap.appendChild(shareBtn);
    actionsWrap.appendChild(btn);

    row.appendChild(left);
    row.appendChild(actionsWrap);
    return row;
  }

  function updateThumbnail(thumbUrl, hasPreviewVideo) {
    currentThumbUrl = thumbUrl || '';
    if (dlThumbBtn) {
      dlThumbBtn.textContent = 'Portada';
      dlThumbBtn.classList.remove('copied');
      dlThumbBtn.classList.toggle('hidden', !currentThumbUrl);
    }

    if (!videoThumb || !thumbPlaceholder) return;
    if (thumbUrl) {
      const proxied = `/api/thumbnail?url=${encodeURIComponent(thumbUrl)}`;
      videoThumb.onload = () => {
        if (!isPlayingPreview) {
          videoThumb.classList.remove('hidden');
          thumbPlaceholder.classList.add('hidden');
          if (thumbPlayOverlay && hasPreviewVideo) {
            thumbPlayOverlay.classList.remove('hidden');
          }
        }
      };
      videoThumb.onerror = () => {
        videoThumb.classList.add('hidden');
        thumbPlaceholder.classList.remove('hidden');
        if (thumbPlayOverlay) thumbPlayOverlay.classList.add('hidden');
      };
      videoThumb.src = proxied;
    } else {
      videoThumb.src = '';
      videoThumb.classList.add('hidden');
      thumbPlaceholder.classList.remove('hidden');
      if (thumbPlayOverlay) thumbPlayOverlay.classList.add('hidden');
    }
  }

  function applySlideToView(data, slideObj) {
    stopPreviewPlayer();

    const durationVal = slideObj ? slideObj.duration : data.duration;
    const thumbVal = (slideObj && slideObj.thumbnail) ? slideObj.thumbnail : data.thumbnail;
    const videoList = (slideObj && slideObj.video_options)
      ? slideObj.video_options
      : (data.video_options || (data.options || []).filter((o) => o.id !== 'audio'));
    const audioList = (slideObj && slideObj.audio_options)
      ? slideObj.audio_options
      : (data.audio_options || (data.options || []).filter((o) => o.id === 'audio'));

    videoDuration.textContent = formatDuration(durationVal);

    // Mejora 1: Configurar URLs para el mini-player al hacer clic en el screenshot
    const firstOpt = videoList[0];
    if (firstOpt) {
      const fallbackParams = new URLSearchParams({
        url: data.original_url,
        quality: firstOpt.id,
        cdn_url: firstOpt.direct_url || ''
      });
      currentPreviewFallbackUrl = `/api/download?${fallbackParams.toString()}`;
      currentPreviewPrimaryUrl = firstOpt.direct_url || currentPreviewFallbackUrl;
    } else {
      currentPreviewPrimaryUrl = '';
      currentPreviewFallbackUrl = '';
    }

    updateThumbnail(thumbVal, !!currentPreviewPrimaryUrl);

    if (videoOptionsList && audioOptionsList) {
      videoOptionsList.innerHTML = '';
      audioOptionsList.innerHTML = '';
      videoList.forEach((opt) => videoOptionsList.appendChild(createOptionRow(opt, data)));
      audioList.forEach((opt) => audioOptionsList.appendChild(createOptionRow(opt, data)));
    }
  }

  // Mejora 8: Cambiar de video activo dentro de un carrusel
  function selectCarouselSlide(slideIdx) {
    if (!currentData || !Array.isArray(currentData.carousel_items)) return;
    const slideObj = currentData.carousel_items[slideIdx];
    if (!slideObj) return;
    currentCarouselIndex = slideIdx;

    if (carouselTabs) {
      const btns = carouselTabs.querySelectorAll('.carousel-tab-btn');
      btns.forEach((b, i) => {
        const active = i === slideIdx;
        b.classList.toggle('active', active);
        b.setAttribute('aria-selected', active ? 'true' : 'false');
      });
    }

    applySlideToView(currentData, slideObj);
  }

  function renderOptions(data) {
    stopPreviewPlayer();
    currentData = data;
    currentCarouselIndex = 0;

    sourceBadge.textContent = data.source_label || data.platform;
    if (videoUploader) {
      videoUploader.textContent = data.uploader || '';
    }
    currentDescription = data.description || data.title || '';
    videoTitle.textContent = currentDescription;
    videoTitle.classList.remove('expanded');

    // Mejora 2: Mostrar botón "Ver más" si la descripción es extensa
    if (toggleDescBtn) {
      const isLong = currentDescription.length > 165 || currentDescription.split('\n').length > 4;
      toggleDescBtn.textContent = 'Ver más';
      toggleDescBtn.classList.toggle('hidden', !isLong);
    }

    if (copyDescBtn) {
      copyDescBtn.textContent = 'Copiar texto';
      copyDescBtn.classList.remove('copied');
      copyDescBtn.classList.toggle('hidden', !currentDescription);
    }

    // Mejora 8: Renderizar pestañas si la publicación es un carrusel con 2 o más videos
    const hasCarousel = Array.isArray(data.carousel_items) && data.carousel_items.length > 1;
    if (carouselTabs) {
      carouselTabs.innerHTML = '';
      if (hasCarousel) {
        const labelSpan = document.createElement('span');
        labelSpan.className = 'carousel-tabs-label';
        labelSpan.textContent = `Carrusel (${data.carousel_items.length} videos):`;
        carouselTabs.appendChild(labelSpan);

        data.carousel_items.forEach((item, idx) => {
          const tabBtn = document.createElement('button');
          tabBtn.type = 'button';
          tabBtn.role = 'tab';
          tabBtn.className = `carousel-tab-btn ${idx === 0 ? 'active' : ''}`;
          tabBtn.setAttribute('aria-selected', idx === 0 ? 'true' : 'false');
          tabBtn.textContent = item.label || `Video ${idx + 1}`;
          tabBtn.addEventListener('click', () => selectCarouselSlide(idx));
          carouselTabs.appendChild(tabBtn);
        });
        carouselTabs.classList.remove('hidden');
      } else {
        carouselTabs.classList.add('hidden');
      }
    }

    const firstSlide = hasCarousel ? data.carousel_items[0] : null;
    applySlideToView(data, firstSlide);

    resultBox.classList.remove('hidden');
  }

  // Mejora 1: Iniciar mini-player in-place al hacer clic sobre el screenshot
  if (thumbWrap && videoPreviewPlayer) {
    thumbWrap.addEventListener('click', () => {
      if (isPlayingPreview || !currentPreviewPrimaryUrl) return;
      isPlayingPreview = true;
      thumbWrap.classList.add('playing');
      videoThumb.classList.add('hidden');
      thumbPlaceholder.classList.add('hidden');
      if (thumbPlayOverlay) thumbPlayOverlay.classList.add('hidden');
      if (videoDuration) videoDuration.classList.add('hidden');

      videoPreviewPlayer.classList.remove('hidden');
      let triedFallback = false;
      videoPreviewPlayer.onerror = () => {
        if (!triedFallback && currentPreviewFallbackUrl && videoPreviewPlayer.src !== currentPreviewFallbackUrl) {
          triedFallback = true;
          videoPreviewPlayer.src = currentPreviewFallbackUrl;
          videoPreviewPlayer.play().catch(() => {});
        }
      };
      videoPreviewPlayer.src = currentPreviewPrimaryUrl;
      videoPreviewPlayer.play().catch(() => {});
    });
  }

  // Mejora 2: Expandir / Contraer descripción completa
  function toggleDescriptionExpand() {
    if (!videoTitle) return;
    const isExpanded = videoTitle.classList.toggle('expanded');
    if (toggleDescBtn) {
      toggleDescBtn.textContent = isExpanded ? 'Ver menos' : 'Ver más';
    }
  }

  if (toggleDescBtn) {
    toggleDescBtn.addEventListener('click', toggleDescriptionExpand);
  }
  if (videoTitle) {
    videoTitle.addEventListener('click', () => {
      if (toggleDescBtn && !toggleDescBtn.classList.contains('hidden')) {
        toggleDescriptionExpand();
      }
    });
  }

  // Mejora 3: Descargar imagen de portada / miniatura en alta resolución
  if (dlThumbBtn) {
    dlThumbBtn.addEventListener('click', async () => {
      if (!currentThumbUrl || !currentData) return;
      const prevText = dlThumbBtn.textContent;
      dlThumbBtn.textContent = 'Bajando...';
      try {
        const res = await fetch(`/api/thumbnail?url=${encodeURIComponent(currentThumbUrl)}`);
        if (!res.ok) throw new Error('Error');
        const blob = await res.blob();
        const isCarousel = Array.isArray(currentData.carousel_items) && currentData.carousel_items.length > 1;
        const filename = buildDdxFilename(
          currentData.platform || 'video',
          'portada',
          'jpg',
          currentData.uploader || '',
          currentDescription,
          isCarousel ? currentCarouselIndex : undefined
        );
        const blobUrl = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = blobUrl;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(blobUrl), 10000);
        dlThumbBtn.textContent = '✓ Portada';
        dlThumbBtn.classList.add('copied');
        setTimeout(() => {
          dlThumbBtn.textContent = 'Portada';
          dlThumbBtn.classList.remove('copied');
        }, 1800);
      } catch (_) {
        dlThumbBtn.textContent = prevText;
      }
    });
  }

  // Mejora 5 anterior: Copiar descripción del post
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

  // Mejora 3 anterior + Mejora 4 PWA Share Target: Detectar URL en barra de direcciones o compartida desde móvil
  function checkUrlPrefixOnLoad() {
    try {
      const searchParams = new URLSearchParams(window.location.search);
      const sharedCandidate = searchParams.get('url') || searchParams.get('text') || searchParams.get('title');
      if (sharedCandidate && sharedCandidate.trim()) {
        const cleanQueryUrl = extractCleanUrl(sharedCandidate);
        if (cleanQueryUrl && /^https?:\/\//i.test(cleanQueryUrl)) {
          urlInput.value = cleanQueryUrl;
          clearBtn.classList.remove('hidden');
          pasteBtn.classList.add('hidden');
          window.history.replaceState(null, '', '/');
          processUrl(cleanQueryUrl);
          return;
        }
      }

      const rawPath = window.location.pathname || '';
      if (rawPath.length > 1 && !rawPath.startsWith('/static/') && !rawPath.startsWith('/api/')) {
        let candidate = rawPath.slice(1) + (window.location.search || '') + (window.location.hash || '');
        try {
          candidate = decodeURIComponent(candidate);
        } catch (_) {}

        candidate = candidate.replace(/^(https?):\/([^/])/i, '$1://$2');

        if (!/^https?:\/\//i.test(candidate) && /^[a-z0-9.-]+\.[a-z]{2,}\//i.test(candidate)) {
          candidate = `https://${candidate}`;
        }

        const cleanedPrefixUrl = extractCleanUrl(candidate);
        if (/^https?:\/\//i.test(cleanedPrefixUrl)) {
          urlInput.value = cleanedPrefixUrl;
          clearBtn.classList.remove('hidden');
          pasteBtn.classList.add('hidden');
          window.history.replaceState(null, '', '/');
          processUrl(cleanedPrefixUrl);
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
      const cleanVal = extractCleanUrl(urlInput.value);
      urlInput.value = cleanVal;
      clearBtn.classList.toggle('hidden', !cleanVal);
      pasteBtn.classList.toggle('hidden', !!cleanVal);
      if (cleanVal) processUrl(cleanVal);
    }, 30);
  });

  // Mejora 10: Pegado instantáneo global (Ctrl+V / Cmd+V en cualquier parte de la pantalla) y tecla Escape
  document.addEventListener('paste', (e) => {
    if (document.activeElement === urlInput) return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || (document.activeElement && document.activeElement.isContentEditable)) {
      return;
    }
    const pastedText = (e.clipboardData || window.clipboardData)?.getData('text');
    if (!pastedText || !pastedText.trim()) return;
    const cleanVal = extractCleanUrl(pastedText);
    if (cleanVal) {
      e.preventDefault();
      urlInput.value = cleanVal;
      clearBtn.classList.remove('hidden');
      pasteBtn.classList.add('hidden');
      processUrl(cleanVal);
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (isPlayingPreview) {
        e.preventDefault();
        stopPreviewPlayer();
      } else if (urlInput.value.trim() || !resultBox.classList.contains('hidden')) {
        e.preventDefault();
        resetAll();
      }
    }
  });

  pasteBtn.addEventListener('click', async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text && text.trim()) {
        const cleanVal = extractCleanUrl(text);
        urlInput.value = cleanVal;
        urlInput.dispatchEvent(new Event('input'));
        processUrl(cleanVal);
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
