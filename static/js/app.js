const API_BASE = '';
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

function getAuthHeaders(extraHeaders) {
    const token = localStorage.getItem('cc_token');
    const headers = Object.assign({}, extraHeaders || {});
    if (token) headers.Authorization = 'Bearer ' + token;
    return headers;
}

// 安全注意：Token 存储在 localStorage 有 XSS 窃取风险
// 生产环境建议改用 HttpOnly Cookie（后端已设置）
// 前端通过 credentials:'include' 自动携带 Cookie

const ALGO_NAMES = {
    reinhard: '经典追色-快速', histogram: '直方图追色-精确',
    luminance_partition: '亮度分区追色-自然', neural_preset: '神经预设追色-需训练',
    modflows: 'AI全局追色-高保真', regional_modflows: 'AI分区追色-皮肤保护',
    modflows_b0: 'AI全局追色-B0快速模式',
    regional_luminance: '快速分区追色-皮肤保护', dncm_lut: '神经预设追色-精确LUT',
    ai_portrait: 'AI人像追色-肤色保护',
};

const STYLE_TAGS = {
    'ai_portrait': 'Portrait',
    'modflows': 'AI',
    'modflows_b0': 'AI',
    'regional_modflows': 'Regional',
    'reinhard': 'Reinhard',
    'histogram': 'Histogram',
    'luminance_partition': 'LumPart',
    'neural_preset': 'Neural',
    'import_lut': 'LUT',
    'orange_bw': 'B&W',
    'warm': 'Warm',
    'cool': 'Cool',
    'standard': 'Original',
};

const BUILTIN_PROFILES = ['bw', 'warm', 'cool', 'orange_bw'];

let targetImages = [];
window.targetImages = targetImages;
let currentTargetIndex = -1;
let refFile = null, refDataUrl = null;
let isProcessing = false;

let lutAI = null, lutProfile = null;
let _profileBuiltin = null, _profileFile = null;
let _lastSessionId = null;
let _mergedSessionId = null;
let _profileSessionId = null;
let _modelStatusCache = null;
let _modelStatusPromise = null;
let _originalImageData = null, _stylizedImageData = null;
let _origCanvasDataUrl = null, _resultCanvasDataUrl = null;
let _refDataUrl = null;
let _subjectMaskPath = '';
let _subjectMaskUrl = '';
let _subjectMaskPoints = [];
let _subjectMaskMode = 'protect_subject';
let _depthLayerPath = '';
let _depthLayerUrl = '';
let _semanticMatchMeta = null;

let activeTab = 'ai';
let currentViewMode = 'single';
let _dividerPos = 50;
let _prevViewMode = null;

let _selectedIndices = [];
let _lastClickedIndex = null;
let _galleryLongPressTimer = null;
let _galleryLongPressTriggered = false;
let _galleryDeleteDropzone = null;
let _galleryDragImageId = '';
let _galleryPendingDeleteImageId = '';
let _galleryDeleteHover = false;
window.currentProjectId = 0;
window.currentProjectType = 'image';

function deferIdle(fn) {
    if (window.requestIdleCallback) {
        return window.requestIdleCallback(fn, { timeout: 1200 });
    }
    return window.setTimeout(fn, 0);
}

function createPerfTrace(scope, fields) {
    var start = performance.now();
    var last = start;
    return function(label, extra) {
        var now = performance.now();
        var payload = Object.assign({}, fields || {}, extra || {}, {
            total_ms: Math.round(now - start),
            delta_ms: Math.round(now - last),
        });
        last = now;
        console.log('[PERF][' + scope + '] ' + label, payload);
    };
}

function getReferenceUploadFile() {
    if (refFile && refFile.size > 0) return refFile;
    if (_refDataUrl && _refDataUrl.startsWith('data:')) {
        return dataURLtoBlob(_refDataUrl);
    }
    return null;
}

function normalizeProjectAssetUrl(value, projectId) {
    var raw = String(value || '').trim();
    if (!raw) return '';
    if (/^\/assets\/local_user\//i.test(raw)) {
        return '/api/user_assets/' + raw.replace(/^\/assets\/local_user\//i, '');
    }
    if (/^(data:|blob:|https?:\/\/|\/api\/project_assets\/|\/api\/user_assets\/|\/assets\/|\/videos\/|\/styles\/)/i.test(raw)) {
        return raw;
    }
    var pid = Number(projectId || window.currentProjectId || 0);
    var normalized = raw.replace(/\\/g, '/').split('?')[0];
    var marker = '/user_assets/projects/';
    var markerIndex = normalized.toLowerCase().indexOf(marker);
    if (markerIndex >= 0) {
        var rest = normalized.slice(markerIndex + marker.length).replace(/^\/+/, '');
        var parts = rest.split('/');
        if (parts.length >= 3 && (!pid || Number(parts[0]) === pid)) {
            return '/api/project_assets/' + parts[0] + '/' + parts.slice(1).map(encodeURIComponent).join('/');
        }
    }
    // 识别迁移后的 storage/projects/assets/{pid}/... 本地绝对路径，转成 HTTP URL
    var marker2 = '/storage/projects/assets/';
    var marker2Index = normalized.toLowerCase().indexOf(marker2);
    if (marker2Index >= 0) {
        var rest2 = normalized.slice(marker2Index + marker2.length).replace(/^\/+/, '');
        var parts2 = rest2.split('/');
        if (parts2.length >= 2) {
            var pathPid = Number(parts2[0]);
            if (!pid || pathPid === pid) {
                return '/api/project_assets/' + parts2[0] + '/' + parts2.slice(1).map(encodeURIComponent).join('/');
            }
        }
    }
    return '';
}

function getImageReferenceSrc(img) {
    if (!img) return '';
    // localReferencePath 在旧快照里可能存本地路径，过 normalize 转 HTTP URL
    return img.refDataUrl ||
        normalizeProjectAssetUrl(img.localReferencePath, window.currentProjectId) ||
        normalizeProjectAssetUrl(img.refSavedPath, window.currentProjectId) ||
        normalizeProjectAssetUrl(window._refSavedPath, window.currentProjectId) ||
        '';
}

function getImageResultSrc(img, fallbackSrc) {
    if (!img) return fallbackSrc || '';
    // localResultPath 在旧快照里可能存本地绝对路径，统一过 normalize 转 HTTP URL
    return normalizeProjectAssetUrl(img.localResultPath, window.currentProjectId) ||
        img.resultDataUrl ||
        normalizeProjectAssetUrl(img.resultSavedPath, window.currentProjectId) ||
        fallbackSrc ||
        '';
}

function hasImageResult(img) {
    return !!getImageResultSrc(img, '');
}

function isSelected(index) {
    return _selectedIndices.includes(index);
}

function getSelectedImages() {
    return _selectedIndices.map(function(i) { return targetImages[i]; });
}

function getSelectedCount() {
    return _selectedIndices.length;
}

function ensureGalleryDeleteDropzone() {
    if (_galleryDeleteDropzone) return _galleryDeleteDropzone;
    var dropzone = document.createElement('div');
    dropzone.className = 'gallery-delete-dropzone';
    dropzone.innerHTML = '<div class="gallery-delete-dropzone-icon">🗑</div>' +
        '<div class="gallery-delete-dropzone-copy">' +
        '<div class="gallery-delete-dropzone-title">拖入此处删除</div>' +
        '<div class="gallery-delete-dropzone-subtitle">仅删除当前项目内图片</div>' +
        '</div>';
    dropzone.addEventListener('dragover', function(e) {
        e.preventDefault();
        _galleryDeleteHover = true;
        dropzone.classList.add('drag-over');
        if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
    });
    dropzone.addEventListener('dragleave', function() {
        _galleryDeleteHover = false;
        dropzone.classList.remove('drag-over');
    });
    dropzone.addEventListener('drop', function(e) {
        e.preventDefault();
        e.stopPropagation();
        _galleryDeleteHover = true;
    });
    document.body.appendChild(dropzone);
    _galleryDeleteDropzone = dropzone;
    return dropzone;
}

function showGalleryDeleteDropzone() {
    var dropzone = ensureGalleryDeleteDropzone();
    _galleryDeleteHover = false;
    dropzone.classList.add('visible');
}

function hideGalleryDeleteDropzone() {
    if (!_galleryDeleteDropzone) return;
    _galleryDeleteDropzone.classList.remove('visible');
    _galleryDeleteDropzone.classList.remove('drag-over');
    _galleryDeleteHover = false;
    _galleryDragImageId = '';
}

function isEditableElement(node) {
    if (!node) return false;
    var tag = node.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    return !!(node.isContentEditable || (node.closest && node.closest('[contenteditable="true"]')));
}

function updateSelectionUI() {
    var bar = $('#batch-select-bar');
    var toggleBtn = $('#toggle-select-btn');
    var countBar = $('#selection-count-text');
    var count = getSelectedCount();
    var total = targetImages.length;

    if (total === 0) {
        bar.hidden = true;
        countBar.textContent = '未选择图片';
    } else {
        bar.hidden = false;
        if (count === 0) {
            countBar.textContent = '未选择图片';
            toggleBtn.textContent = '\u2610';
            toggleBtn.title = '\u5168\u9009';
        } else if (count === total) {
            countBar.textContent = '\u5DF2\u9009 ' + count + '/' + total;
            toggleBtn.textContent = '\u2611';
            toggleBtn.title = '\u53D6\u6D88\u5168\u9009';
        } else {
            countBar.textContent = '\u5DF2\u9009 ' + count + '/' + total;
            toggleBtn.textContent = '\u2610';
            toggleBtn.title = '\u5168\u9009';
        }
    }
    updateRatingUI();
}

function toggleSelectAll() {
    var total = targetImages.length;
    if (getSelectedCount() === total) {
        deselectAll();
    } else {
        selectAll();
    }
}

function selectAll() {
    _selectedIndices = [];
    for (var i = 0; i < targetImages.length; i++) {
        _selectedIndices.push(i);
    }
    renderGallery();
    updateSelectionUI();
    updateAllButtons();
}

function deselectAll() {
    _selectedIndices = [];
    _lastClickedIndex = null;
    renderGallery();
    updateSelectionUI();
    updateAllButtons();
}

function setRating(index, rating) {
    if (index < 0 || index >= targetImages.length) return;
    targetImages[index].rating = rating;
    syncRatingToBackend(targetImages[index].name, targetImages[index].rating);
    refreshGalleryWarnings();
    updateRatingUI();
}

function updateRatingUI() {
    if (currentTargetIndex < 0 || currentTargetIndex >= targetImages.length) {
        $('#batch-star-rating').innerHTML = '';
        return;
    }
    var rating = targetImages[currentTargetIndex].rating || 0;
    var html = '';
    for (var s = 1; s <= 5; s++) {
        html += '<span class="star' + (s <= rating ? ' active' : '') + '" data-star="' + s + '">\u2605</span>';
    }
    $('#batch-star-rating').innerHTML = html;
}

function syncRatingToBackend(fileName, rating) {
    var pid = window.currentProjectId;
    if (!pid || pid === 0) return;
    var token = localStorage.getItem('cc_token');
    if (!token) return;
    fetch('/api/projects/' + pid + '/rate_asset', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + token,
        },
        body: JSON.stringify({ file_name: fileName, rating: rating })
    }).catch(function() {});
}

function refreshGalleryWarnings() {
    var items = document.querySelectorAll('.gallery-item');
    items.forEach(function(el, i) {
        var img = targetImages[i];
        var existingBadge = el.querySelector('.unrated-badge');
        if (img && (!img.rating || img.rating === 0)) {
            el.classList.add('unrated-warning');
            if (!existingBadge) {
                var badge = document.createElement('span');
                badge.className = 'unrated-badge';
                badge.textContent = '\u26A0';
                el.appendChild(badge);
            }
        } else {
            el.classList.remove('unrated-warning');
            if (existingBadge) existingBadge.remove();
        }
    });
}

function checkRatingsBeforeExport(images) {
    var unrated = [];
    images.forEach(function(img) {
        if (!img.rating || img.rating === 0) unrated.push(img);
    });
    if (unrated.length === 0) return true;

    var modal = document.getElementById('rating-intercept-modal');
    if (modal) modal.style.display = 'flex';

    refreshGalleryWarnings();
    return false;
}

function nextId() {
    let n = 0;
    while (targetImages.some(img => img.id === 'img_' + n)) n++;
    return 'img_' + n;
}

/* ---------- helpers ---------- */
function showToast(msg, dur = 3000) {
    const t = $('#toast');
    if (typeof msg === 'string') {
        t.innerHTML = '';
        t.textContent = msg;
    } else {
        t.innerHTML = '';
        t.appendChild(msg);
    }
    t.hidden = false;
    t.onclick = null;
    clearTimeout(t._toastTimer);
    t._toastTimer = setTimeout(() => { t.hidden = true; }, dur);
}

function openLightbox(src) {
    $('#lightbox-img').src = src; $('#lightbox').hidden = false;
    document.body.style.overflow = 'hidden';
}
function closeLightbox() { $('#lightbox').hidden = true; document.body.style.overflow = ''; }
window.openLightbox = openLightbox;
window.closeLightbox = closeLightbox;
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeLightbox(); });
document.addEventListener('keydown', function(e) {
    if (isEditableElement(document.activeElement)) return;
    if (e.key === 'e' || e.key === 'E') {
        e.preventDefault();
        if (currentViewMode !== 'single') setViewMode('single');
    } else if (e.key === 'y' || e.key === 'Y') {
        e.preventDefault();
        var hasResult = !!_resultCanvasDataUrl;
        var hasRef = !!getImageReferenceSrc(getCurrentImage());
        if (e.shiftKey) {
            if (hasResult && hasRef && currentViewMode !== 'reference') setViewMode('reference');
        } else {
            if (hasResult && currentViewMode !== 'compare') setViewMode('compare');
        }
    }
});

document.addEventListener('keydown', function(e) {
    if (isEditableElement(document.activeElement)) return;
    if (e.key !== 'Backspace' && e.key !== 'Delete') return;
    if (!targetImages.length || !getSelectedCount()) return;
    e.preventDefault();
    deleteSelectedProjectImages();
});

function getCurrentImage() {
    if (currentTargetIndex < 0 || currentTargetIndex >= targetImages.length) return null;
    return targetImages[currentTargetIndex];
}

function getCurrentId() {
    const img = getCurrentImage();
    return img ? img.id : null;
}

/* ---------- State Save / Restore ---------- */
function saveCurrentState() {
    const img = getCurrentImage();
    if (!img) return;

    img.sessionId = _lastSessionId;
    img.mergedSessionId = _mergedSessionId;
    if (_profileBuiltin) img.profileId = _profileBuiltin;
    else if (_profileSessionId) img.profileId = _profileSessionId;
    else img.profileId = null;

    img.params = getAdjustParams();
    if (!img.resultDataUrl && _resultCanvasDataUrl) {
        img.resultDataUrl = _resultCanvasDataUrl;
    }
    if (_refDataUrl) img.refDataUrl = _refDataUrl;
    if (window._refSavedPath) img.refSavedPath = window._refSavedPath;
    img.subjectMaskPath = _subjectMaskPath || '';
    img.subjectMaskUrl = _subjectMaskUrl || '';
    img.subjectMaskMode = _subjectMaskMode || 'protect_subject';
    img.subjectMaskPoints = (_subjectMaskPoints || []).slice();
    img.depthLayerPath = _depthLayerPath || '';
    img.depthLayerUrl = _depthLayerUrl || '';
    img.semanticMatchMeta = _semanticMatchMeta || null;
    img.status = hasImageResult(img) ? 'done' : 'pending';
}

function restoreCurrentState() {
    const img = getCurrentImage();
    if (!img) return;

    _lastSessionId = img.sessionId || null;
    _mergedSessionId = img.mergedSessionId || null;
    lutAI = img.sessionId || null;

    if (img.profileId) {
        if (BUILTIN_PROFILES.includes(img.profileId)) {
            _profileBuiltin = img.profileId;
            _profileFile = null;
            _profileSessionId = null;
            lutProfile = { type: 'builtin', name: img.profileId };
        } else {
            _profileBuiltin = null;
            _profileSessionId = img.profileId;
            lutProfile = img.profileId;
        }
    } else {
        _profileBuiltin = null;
        _profileFile = null;
        _profileSessionId = null;
        lutProfile = null;
    }

    _resultCanvasDataUrl = getImageResultSrc(img, '');
    _origCanvasDataUrl = img.thumbnailUrl || img.sourcePath;
    _refDataUrl = img.refDataUrl || null;
    window._refSavedPath = img.refSavedPath || '';
    _subjectMaskPath = img.subjectMaskPath || '';
    _subjectMaskUrl = img.subjectMaskUrl || '';
    _subjectMaskMode = img.subjectMaskMode || 'protect_subject';
    _subjectMaskPoints = Array.isArray(img.subjectMaskPoints) ? img.subjectMaskPoints.slice() : [];
    _depthLayerPath = img.depthLayerPath || '';
    _depthLayerUrl = img.depthLayerUrl || '';
    _semanticMatchMeta = img.semanticMatchMeta || null;

    var refPreview = $('#ref-preview');
    var refPlaceholder = $('#ref-placeholder');
    var refClear = $('#ref-clear');
    var refInput = $('#ref-input');
    var refSrc = getImageReferenceSrc(img);
    if (refPreview) {
        refPreview.src = refSrc || '';
        refPreview.hidden = !refSrc;
    }
    if (refPlaceholder) refPlaceholder.hidden = !!refSrc;
    if (refClear) refClear.hidden = !refSrc;
    if (refInput && !refSrc) refInput.value = '';
    refFile = refSrc ? (refFile || new File([], 'reference.jpg')) : null;

    $('#profile-select').value = img.profileId && BUILTIN_PROFILES.includes(img.profileId) ? img.profileId : 'standard';
    if (img.profileId && !BUILTIN_PROFILES.includes(img.profileId)) {
        const select = $('#profile-select');
        const existing = select.querySelector('option[value^="custom_"]');
        if (existing) existing.remove();
        const opt = document.createElement('option');
        opt.value = 'custom_current';
        opt.textContent = '自定义: ' + img.profileId;
        select.appendChild(opt);
        select.value = 'custom_current';
    }
    $('#profile-status-text').textContent = img.profileId ?
        (BUILTIN_PROFILES.includes(img.profileId) ? '预设: ' + img.profileId : '自定义 LUT') :
        '未加载配置文件';

    if (img.params) {
        setAdjustValue(ADJUST_PARAMS[0], img.params.intensity);
        setAdjustValue(ADJUST_PARAMS[1], img.params.exposure);
        setAdjustValue(ADJUST_PARAMS[2], img.params.contrast);
        setAdjustValue(ADJUST_PARAMS[3], img.params.highlight);
        setAdjustValue(ADJUST_PARAMS[4], img.params.shadow);
        setAdjustValue(ADJUST_PARAMS[5], img.params.vibrance);
    } else {
        resetAdjustSliders();
    }
    updateMaskUI();
    updateDepthUI();
    updateSemanticUI();
}

/* ---------- Switch Target ---------- */
function loadSwitchImageData() {
    const img = getCurrentImage();
    // 优先用持久化的原始追色结果(img.resultDataUrl)，避免用当前画布(可能已是调整后)作为基准导致叠加
    // localResultPath 在旧快照里可能是本地路径，过 normalize 转 HTTP URL
    const resultSrc = (img && (img.resultDataUrl || normalizeProjectAssetUrl(img.localResultPath || '', window.currentProjectId))) || $('#canvas-result').src;
    if (!resultSrc) return;

    const loadImg = new Image();
    loadImg.onload = () => {
        _stylizedImageData = imageToImageData(loadImg);

        const orig = new Image();
        orig.onload = () => {
            const c = document.createElement('canvas');
            c.width = _stylizedImageData.width;
            c.height = _stylizedImageData.height;
            const ctx = c.getContext('2d');
            ctx.drawImage(orig, 0, 0, c.width, c.height);
            _originalImageData = ctx.getImageData(0, 0, c.width, c.height);
            requestWorkerAdjust();
        };
        orig.onerror = () => {
            _originalImageData = new ImageData(
                new Uint8ClampedArray(_stylizedImageData.data),
                _stylizedImageData.width,
                _stylizedImageData.height
            );
            requestWorkerAdjust();
        };
        orig.src = _origCanvasDataUrl;
    };
    loadImg.onerror = () => {};
    loadImg.src = resultSrc;
}

function imageToImageData(img) {
    const c = document.createElement('canvas');
    c.width = img.naturalWidth;
    c.height = img.naturalHeight;
    const ctx = c.getContext('2d');
    ctx.drawImage(img, 0, 0);
    return ctx.getImageData(0, 0, c.width, c.height);
}

function getImageById(id) {
    return targetImages.find(img => img.id === id) || null;
}

function deleteSelectedProjectImageById(imageId) {
    if (!imageId) return;
    var index = targetImages.findIndex(function(item) { return item && item.id === imageId; });
    if (index >= 0) {
        deleteSelectedProjectImages([index]);
    }
}

function getProjectImageDeletionPaths(images) {
    var paths = [];
    var seen = new Set();
    (images || []).forEach(function(img) {
        ['savedPath', 'sourcePath', 'thumbnailUrl'].forEach(function(key) {
            var value = img && img[key] ? String(img[key]).trim() : '';
            if (!value) return;
            if (!(value.startsWith('/assets/projects/') || value.startsWith('/uploaded/projects/'))) return;
            var normalized = value.split('?')[0];
            if (seen.has(normalized)) return;
            seen.add(normalized);
            paths.push(normalized);
        });
    });
    return paths;
}

function resetWorkspaceAfterDeletion() {
    currentTargetIndex = -1;
    _lastClickedIndex = null;
    _selectedIndices = [];
    _lastSessionId = null;
    _mergedSessionId = null;
    _profileSessionId = null;
    _profileBuiltin = null;
    _profileFile = null;
    lutAI = null;
    lutProfile = null;
    _originalImageData = null;
    _stylizedImageData = null;
    _origCanvasDataUrl = null;
    _resultCanvasDataUrl = null;
    _refDataUrl = null;
    _subjectMaskPath = '';
    _subjectMaskUrl = '';
    _subjectMaskPoints = [];
    _subjectMaskMode = 'protect_subject';
    _depthLayerPath = '';
    _depthLayerUrl = '';
    _semanticMatchMeta = null;
    refFile = null;
    window._refSavedPath = '';
    var canvasPlaceholder = $('#canvas-placeholder');
    var canvasStack = $('#canvas-stack');
    if (canvasPlaceholder) canvasPlaceholder.hidden = false;
    if (canvasStack) canvasStack.hidden = true;
    if ($('#canvas-original')) $('#canvas-original').src = '';
    if ($('#canvas-result')) $('#canvas-result').src = '';
    if ($('#canvas-depth-preview')) { $('#canvas-depth-preview').src = ''; $('#canvas-depth-preview').hidden = true; }
    if ($('#canvas-mask-preview')) { $('#canvas-mask-preview').src = ''; $('#canvas-mask-preview').hidden = true; }
    if ($('#canvas-reference')) $('#canvas-reference').src = '';
    if ($('#canvas-filename')) $('#canvas-filename').textContent = '';
    if ($('#canvas-resolution')) $('#canvas-resolution').textContent = '';
    if ($('#ref-preview')) $('#ref-preview').hidden = true;
    if ($('#ref-placeholder')) $('#ref-placeholder').hidden = false;
    if ($('#ref-clear')) $('#ref-clear').hidden = true;
    if ($('#ref-input')) $('#ref-input').value = '';
    setViewMode('single');
}

function reindexSelectionAfterDeletion(removedIndices) {
    var removed = Array.isArray(removedIndices) ? removedIndices.slice().sort(function(a, b) { return a - b; }) : [];
    var removedSet = new Set(removed);
    if (removedSet.has(currentTargetIndex)) {
        if (targetImages.length === 0) {
            resetWorkspaceAfterDeletion();
        } else {
            var nextIndex = removed[0];
            if (nextIndex >= targetImages.length) nextIndex = targetImages.length - 1;
            currentTargetIndex = -1;
            switchTarget(nextIndex);
        }
    } else if (targetImages.length > 0) {
        var shiftCurrent = removed.filter(function(idx) { return idx < currentTargetIndex; }).length;
        currentTargetIndex = Math.max(0, currentTargetIndex - shiftCurrent);
        restoreCurrentState();
        renderGallery();
    }
    _selectedIndices = _selectedIndices
        .filter(function(idx) { return !removedSet.has(idx); })
        .map(function(idx) {
            var shift = removed.filter(function(removedIndex) { return removedIndex < idx; }).length;
            return idx - shift;
        });
    _lastClickedIndex = _selectedIndices.length ? _selectedIndices[_selectedIndices.length - 1] : null;
}

async function deleteSelectedProjectImages(explicitIndices) {
    hideGalleryDeleteDropzone();
    _galleryPendingDeleteImageId = '';
    if (!window.currentProjectId) {
        showToast('请先进入项目后再删除图片');
        return;
    }
    if (isProcessing) {
        showToast('请等待当前处理完成后再删除图片');
        return;
    }
    var indices = Array.isArray(explicitIndices) && explicitIndices.length
        ? explicitIndices.slice()
        : _selectedIndices.slice();
    indices = indices
        .filter(function(idx) { return Number.isInteger(idx) && idx >= 0 && idx < targetImages.length; })
        .sort(function(a, b) { return a - b; });
    if (!indices.length) return;

    var images = indices.map(function(idx) { return targetImages[idx]; }).filter(Boolean);
    var confirmMsg = indices.length === 1 ? '确定删除这张图片吗？' : '确定删除选中的 ' + indices.length + ' 张图片吗？';
    if (!window.confirm(confirmMsg)) return;

    var paths = getProjectImageDeletionPaths(images);
    if (paths.length) {
        try {
            var resp = await fetch('/api/projects/' + window.currentProjectId + '/assets', {
                method: 'DELETE',
                headers: getAuthHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ paths: paths }),
            });
            var data = await resp.json().catch(function() { return {}; });
            if (!resp.ok) {
                showToast((data && data.detail) || '删除失败');
                return;
            }
        } catch (err) {
            showToast('删除失败: ' + err.message);
            return;
        }
    }

    for (var i = indices.length - 1; i >= 0; i--) {
        targetImages.splice(indices[i], 1);
    }
    reindexSelectionAfterDeletion(indices);
    renderGallery();
    updateSelectionUI();
    updateAllButtons();
    saveSnapshot(window.currentProjectId);
    showToast(indices.length === 1 ? '图片已删除' : '已删除 ' + indices.length + ' 张图片');
}

function switchTarget(index) {
    if (index < 0 || index >= targetImages.length) return;
    if (index === currentTargetIndex) return;

    if (isProcessing) {
        showToast('请等待当前处理完成后再切换');
        return;
    }

    saveCurrentState();

    currentTargetIndex = index;
    const img = getCurrentImage();
    if (!img) return;

    if (window.fitToView) window.fitToView();

    restoreCurrentState();

    renderGallery();

    $('#canvas-filename').textContent = img.name;
    $('#canvas-resolution').textContent = img.meta || '';

    $('#canvas-placeholder').hidden = true;
    $('#canvas-stack').hidden = false;

    const thumbSrc = img.thumbnailUrl || img.sourcePath;
    $('#canvas-original').src = thumbSrc;
    $('#canvas-result').src = getImageResultSrc(img, _resultCanvasDataUrl || thumbSrc);
    $('#canvas-reference').src = getImageReferenceSrc(img);
    _origCanvasDataUrl = thumbSrc;

    _originalImageData = null;
    _stylizedImageData = null;

    if (hasImageResult(img)) {
        loadSwitchImageData();
    }

    currentViewMode = 'single';
    _dividerPos = 50;
    setViewMode('single');

    updateBatchApplyButton();
    updateAllButtons();
    $('#adjust-sliders').hidden = !(_lastSessionId || _profileSessionId || img.profileId);
}

function updateAIButton() {
    const img = getCurrentImage();
    $('#ai-transfer-btn').disabled = !img || !refFile || isProcessing;
}

function updateProfileApplyButton() {
    var img = getCurrentImage();
    $('#apply-profile-btn').disabled = !img || isProcessing;
}

function updateExportButton() {
    var exportBtn = $('#export-btn');
    var folderBtn = $('#export-folder-btn');
    var quickExportBtn = $('#quick-export-btn');
    var quickFolderBtn = $('#quick-export-folder-btn');
    var hasFolder = !!exportFolderName;
    var downloadable = getSelectedImages().filter(function(img) { return hasImageResult(img); });
    var hasDownloadable = downloadable.length > 0;
    var count = downloadable.length;

    if (isProcessing) {
        exportBtn.disabled = true;
        exportBtn.textContent = '\u23F3 \u5BFC\u51FA\u4E2D...';
        exportBtn.className = 'panel-action-btn-download exporting';
        folderBtn.disabled = true;
        folderBtn.textContent = '\u23F3 \u5BFC\u51FA\u4E2D...';
        if (quickExportBtn) quickExportBtn.disabled = true;
        if (quickFolderBtn) quickFolderBtn.disabled = true;
        return;
    }

    if (!hasDownloadable) {
        exportBtn.disabled = true;
        exportBtn.title = '\u8BF7\u5148\u9009\u62E9\u56FE\u7247';
        exportBtn.className = 'panel-action-btn-download';
    } else if (!hasFolder) {
        exportBtn.disabled = true;
        exportBtn.title = '\u8BF7\u5148\u9009\u62E9\u5BFC\u51FA\u4F4D\u7F6E';
        exportBtn.className = 'panel-action-btn-download';
    } else {
        exportBtn.disabled = false;
        exportBtn.title = '';
        exportBtn.className = 'panel-action-btn-download';
    }
    exportBtn.textContent = '\uD83D\uDCBE \u5BFC\u51FA\u56FE\u7247';

    folderBtn.disabled = !hasDownloadable || isProcessing;
    folderBtn.textContent = hasDownloadable ? '\uD83D\uDCC1 \u5BFC\u51FA ' + count + ' \u5F20\u56FE\u7247' : '\uD83D\uDCC1 \u4E0B\u8F7D\u5230\u6587\u4EF6\u5939';

    if (quickExportBtn) quickExportBtn.disabled = !hasDownloadable || isProcessing;
    if (quickFolderBtn) quickFolderBtn.disabled = !hasDownloadable || isProcessing;
}

function updateViewButtons() {
    const hasResult = !!_resultCanvasDataUrl && _resultCanvasDataUrl !== '';
    const hasRef = !!getImageReferenceSrc(getCurrentImage());
    $('#view-btn-compare').disabled = !hasResult;
    $('#view-btn-reference').disabled = !hasResult || !hasRef;
    $('#view-press-btn').disabled = !hasResult;
}

function updateBatchApplyButton() {
    var btns = $$('.batch-apply-btn');
    var img = getCurrentImage();
    var selectedOthers = img ? getSelectedImages().filter(function(t) { return t.id !== img.id; }) : [];
    var hasCheckedOthers = img && selectedOthers.length > 0;
    var hasAnyStyle = img && (img.sessionId || img.profileId || hasImageResult(img));
    btns.forEach(function(btn) {
        if (hasCheckedOthers && hasAnyStyle) {
            btn.disabled = false;
            btn.title = '打开弹窗选择要批量应用的模块';
        } else {
            btn.disabled = true;
            if (!hasCheckedOthers) {
                btn.title = '需要选中其他图片才能批量应用';
            } else if (!hasAnyStyle) {
                btn.title = '需要对当前图片完成追色后才能批量应用';
            }
        }
    });
}

function updateAllButtons() {
    updateAIButton();
    updateProfileApplyButton();
    updateExportButton();
    updateViewButtons();
    updateBatchApplyButton();
}

/* ---------- view modes ---------- */
function setViewMode(mode) {
    currentViewMode = mode;
    var stack = $('#canvas-stack');
    var leftPane = $('#compare-left');
    var rightPane = $('#compare-right');
    var divider = $('#canvas-divider-fixed');

    $$('.view-btn').forEach(function(b) { b.classList.remove('active'); });
    var activeBtn = document.querySelector('.view-btn[data-view="' + mode + '"]');
    if (activeBtn) activeBtn.classList.add('active');

    if (mode === 'single') {
        stack.hidden = false;
        leftPane.hidden = true;
        rightPane.hidden = true;
        divider.hidden = true;
        $('#pane-result').style.display = '';
        if (window.fitToView) window.fitToView();
    } else if (mode === 'compare') {
        stack.hidden = true;
        leftPane.hidden = false;
        rightPane.hidden = false;
        divider.hidden = false;
        _dividerPos = 50;
        updateDividerLayout();
        $('#compare-left-img').src = $('#canvas-original').src;
        $('#compare-right-img').src = $('#canvas-result').src;
        var loaded = 0;
        function onLoad() { loaded++; if (loaded === 2) { window.fitCompareToView(); } }
        $('#compare-left-img').onload = onLoad;
        $('#compare-right-img').onload = onLoad;
        if ($('#compare-left-img').complete && $('#compare-left-img').naturalWidth) onLoad();
        if ($('#compare-right-img').complete && $('#compare-right-img').naturalWidth) onLoad();
        if (loaded === 2) window.fitCompareToView();
    } else if (mode === 'reference') {
        stack.hidden = true;
        leftPane.hidden = false;
        rightPane.hidden = false;
        divider.hidden = false;
        _dividerPos = 50;
        updateDividerLayout();
        $('#compare-left-img').src = $('#canvas-reference').src;
        $('#compare-right-img').src = $('#canvas-result').src;
        var loadedR = 0;
        function onRLoad() { loadedR++; if (loadedR === 2) { window.fitCompareToView(); } }
        $('#compare-left-img').onload = onRLoad;
        $('#compare-right-img').onload = onRLoad;
        if ($('#compare-left-img').complete && $('#compare-left-img').naturalWidth) onRLoad();
        if ($('#compare-right-img').complete && $('#compare-right-img').naturalWidth) onRLoad();
        if (loadedR === 2) window.fitCompareToView();
    }
}

function updateDividerLayout() {
    var pct = _dividerPos;
    var leftPane = $('#compare-left');
    var rightPane = $('#compare-right');
    var divider = $('#canvas-divider-fixed');
    leftPane.style.width = pct + '%';
    rightPane.style.left = pct + '%';
    rightPane.style.right = '0';
    divider.style.left = pct + '%';
}

/* ---------- long press compare ---------- */
function setupPressCompare() {
    var btn = $('#view-press-btn');
    var pressTimer = null;
    var savedResultSrc = null;
    var savedCompareRightSrc = null;

    function onPressStart(e) {
        e.preventDefault();
        if (btn.disabled || isProcessing) return;
        _prevViewMode = currentViewMode;
        btn.classList.add('pressing');

        if (currentViewMode === 'compare') {
            savedCompareRightSrc = $('#compare-right-img').src;
            $('#compare-right-img').src = $('#compare-left-img').src;
        } else if (currentViewMode === 'reference') {
            savedCompareRightSrc = $('#compare-right-img').src;
            $('#compare-right-img').src = $('#canvas-original').src;
        } else {
            savedResultSrc = $('#canvas-result').src;
            if (_origCanvasDataUrl) $('#canvas-result').src = _origCanvasDataUrl;
        }
    }

    function onPressEnd(e) {
        e.preventDefault();
        btn.classList.remove('pressing');
        if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }

        if (currentViewMode === 'compare' || currentViewMode === 'reference') {
            if (savedCompareRightSrc) { $('#compare-right-img').src = savedCompareRightSrc; }
            savedCompareRightSrc = null;
        } else {
            if (savedResultSrc) $('#canvas-result').src = savedResultSrc;
            savedResultSrc = null;
        }
    }

    btn.addEventListener('mousedown', onPressStart);
    btn.addEventListener('mouseup', onPressEnd);
    btn.addEventListener('mouseleave', onPressEnd);
    btn.addEventListener('touchstart', onPressStart, { passive: false });
    btn.addEventListener('touchend', onPressEnd);
    btn.addEventListener('touchcancel', onPressEnd);
}

/* ---------- divider drag ---------- */
function setupDividerDrag() {
    var divider = $('#canvas-divider-fixed');
    var dragging = false;

    function onDragStart(e) {
        if (currentViewMode === 'single') return;
        dragging = true;
        divider.style.transition = 'none';
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
        e.preventDefault();
    }

    function onDragMove(e) {
        if (!dragging) return;
        var area = $('#canvas-area');
        var rect = area.getBoundingClientRect();
        var clientX = e.touches ? e.touches[0].clientX : e.clientX;
        var pct = ((clientX - rect.left) / rect.width) * 100;
        pct = Math.max(15, Math.min(85, pct));
        _dividerPos = pct;
        updateDividerLayout();
    }

    function onDragEnd(e) {
        if (!dragging) return;
        dragging = false;
        divider.style.transition = '';
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
    }

    divider.addEventListener('mousedown', onDragStart);
    document.addEventListener('mousemove', onDragMove);
    document.addEventListener('mouseup', onDragEnd);
    divider.addEventListener('touchstart', onDragStart, { passive: false });
    document.addEventListener('touchmove', onDragMove, { passive: false });
    document.addEventListener('touchend', onDragEnd);
}

/* ---------- wheel delta normalization ---------- */
function normalizeWheelDelta(e) {
    var delta = e.deltaY;
    if (e.deltaMode === 1) delta *= 40;
    else if (e.deltaMode === 2) delta *= 800;
    delta = Math.max(-300, Math.min(300, delta));
    return delta;
}

/* ---------- Zoom + Pan System (Lightroom dual-viewport) ---------- */
var zoomState = {
    scale: 1.0,
    offsetX: 0,
    offsetY: 0,
    minScale: 0.1,
    maxScale: 32.0,
    isDragging: false,
    startX: 0,
    startY: 0,
    wheelAccum: 0,
    lastWheelTime: 0,
    WHEEL_THRESHOLD: 50,
    isAnimating: false,
    targetScale: 1.0,
    targetOffsetX: 0,
    targetOffsetY: 0,
};
var compareState = {
    leftScale: 1.0,
    rightScale: 1.0,
    leftOffsetX: 0,
    leftOffsetY: 0,
    rightOffsetX: 0,
    rightOffsetY: 0,
    minScale: 0.1,
    maxScale: 32.0,
    wheelAccum: 0,
    lastWheelTime: 0,
    WHEEL_THRESHOLD: 50,
    isAnimating: false,
    targetLeftScale: 1.0,
    targetRightScale: 1.0,
    targetLeftOffsetX: 0,
    targetLeftOffsetY: 0,
    targetRightOffsetX: 0,
    targetRightOffsetY: 0,
};
var _comparePanState = {
    isPanning: false,
    side: null,
    startX: 0,
    startY: 0,
    startOffsetX: 0,
    startOffsetY: 0,
};
var _fitScale = 1.0;
var _compareFitScale = 1.0;
var _zoomSetupDone = false;

function setupZoom() {
    if (_zoomSetupDone) return;
    _zoomSetupDone = true;

    var canvasArea = $('#canvas-area');
    var canvasStack = $('#canvas-stack');
    var fitBtn = $('#fit-btn');

    function updateTransform() {
        canvasStack.style.transform =
            'translate(' + zoomState.offsetX + 'px, ' + zoomState.offsetY + 'px) scale(' + zoomState.scale + ')';
        var pct = Math.round(zoomState.scale * 100);
        $('#zoom-indicator').textContent = (pct === Math.round(_fitScale * 100)) ? 'Fit' : pct + '%';
    }

    function updateCompareTransform() {
        var leftInner = $('#compare-left-inner');
        var rightInner = $('#compare-right-inner');
        leftInner.style.transform = 'translate(' + compareState.leftOffsetX + 'px, ' + compareState.leftOffsetY + 'px) scale(' + compareState.leftScale + ')';
        rightInner.style.transform = 'translate(' + compareState.rightOffsetX + 'px, ' + compareState.rightOffsetY + 'px) scale(' + compareState.rightScale + ')';
        var pct = Math.round(compareState.leftScale * 100);
        $('#zoom-indicator').textContent = (pct === Math.round(_compareFitScale * 100)) ? 'Fit' : pct + '%';
    }

    function fitToView() {
        var resultImg = $('#canvas-result');
        var areaW = canvasArea.clientWidth;
        var areaH = canvasArea.clientHeight;
        var imgW = resultImg.naturalWidth || areaW;
        var imgH = resultImg.naturalHeight || areaH;

        _fitScale = Math.min(areaW / imgW, areaH / imgH, 1.0);
        if (_fitScale <= 0) _fitScale = 1.0;

        canvasStack.style.width = imgW + 'px';
        canvasStack.style.height = imgH + 'px';

        zoomState.scale = _fitScale;
        zoomState.targetScale = _fitScale;
        zoomState.offsetX = (areaW - imgW * _fitScale) / 2;
        zoomState.offsetY = (areaH - imgH * _fitScale) / 2;
        zoomState.targetOffsetX = zoomState.offsetX;
        zoomState.targetOffsetY = zoomState.offsetY;
        zoomState.isAnimating = false;
        updateTransform();
    }

    function fitCompareToView() {
        var areaW = canvasArea.clientWidth;
        var areaH = canvasArea.clientHeight;
        var dividerX = areaW * _dividerPos / 100;
        var leftPaneW = dividerX;
        var rightPaneW = areaW - dividerX;

        var leftImg = $('#compare-left-img');
        var rightImg = $('#compare-right-img');
        var leftW = leftImg.naturalWidth || 1;
        var leftH = leftImg.naturalHeight || 1;
        var rightW = rightImg.naturalWidth || 1;
        var rightH = rightImg.naturalHeight || 1;

        var leftFit = Math.min(leftPaneW / leftW, areaH / leftH, 1.0);
        var rightFit = Math.min(rightPaneW / rightW, areaH / rightH, 1.0);
        _compareFitScale = Math.min(leftFit, rightFit);
        if (_compareFitScale <= 0) _compareFitScale = 1.0;

        compareState.leftScale = _compareFitScale;
        compareState.rightScale = _compareFitScale;
        compareState.targetLeftScale = _compareFitScale;
        compareState.targetRightScale = _compareFitScale;

        compareState.leftOffsetX = leftPaneW - leftW * _compareFitScale;
        compareState.leftOffsetY = (areaH - leftH * _compareFitScale) / 2;
        compareState.targetLeftOffsetX = compareState.leftOffsetX;
        compareState.targetLeftOffsetY = compareState.leftOffsetY;

        compareState.rightOffsetX = 0;
        compareState.rightOffsetY = (areaH - rightH * _compareFitScale) / 2;
        compareState.targetRightOffsetX = compareState.rightOffsetX;
        compareState.targetRightOffsetY = compareState.rightOffsetY;
        compareState.isAnimating = false;

        updateCompareTransform();
    }

    window.fitToView = function() {
        requestAnimationFrame(function() { requestAnimationFrame(fitToView); });
    };
    window.fitCompareToView = function() {
        requestAnimationFrame(function() { requestAnimationFrame(fitCompareToView); });
    };

    canvasArea.addEventListener('wheel', function(e) {
        e.preventDefault();

        var rawDelta = normalizeWheelDelta(e);
        var now = Date.now();

        if (currentViewMode === 'single') {
            if (now - zoomState.lastWheelTime > 200) zoomState.wheelAccum = 0;
            zoomState.lastWheelTime = now;
            zoomState.wheelAccum += rawDelta;
            if (Math.abs(zoomState.wheelAccum) < zoomState.WHEEL_THRESHOLD) return;

            var steps = Math.floor(Math.abs(zoomState.wheelAccum) / zoomState.WHEEL_THRESHOLD);
            var direction = zoomState.wheelAccum > 0 ? -1 : 1;
            zoomState.wheelAccum = zoomState.wheelAccum % zoomState.WHEEL_THRESHOLD;

            var rect = canvasArea.getBoundingClientRect();
            var mx = e.clientX - rect.left;
            var my = e.clientY - rect.top;
            var oldScale = zoomState.targetScale;
            var zoomFactor = Math.pow(1.15, steps * direction);
            var newScale = Math.max(zoomState.minScale, Math.min(zoomState.maxScale, oldScale * zoomFactor));

            zoomState.targetOffsetX = mx - (mx - zoomState.targetOffsetX) * (newScale / oldScale);
            zoomState.targetOffsetY = my - (my - zoomState.targetOffsetY) * (newScale / oldScale);
            zoomState.targetScale = newScale;

            if (!zoomState.isAnimating) {
                zoomState.isAnimating = true;
                requestAnimationFrame(animateZoom);
            }
        } else if (currentViewMode === 'compare') {
            if (now - compareState.lastWheelTime > 200) compareState.wheelAccum = 0;
            compareState.lastWheelTime = now;
            compareState.wheelAccum += rawDelta;
            if (Math.abs(compareState.wheelAccum) < compareState.WHEEL_THRESHOLD) return;

            var cSteps = Math.floor(Math.abs(compareState.wheelAccum) / compareState.WHEEL_THRESHOLD);
            var cDirection = compareState.wheelAccum > 0 ? -1 : 1;
            compareState.wheelAccum = compareState.wheelAccum % compareState.WHEEL_THRESHOLD;

            var rectC = canvasArea.getBoundingClientRect();
            var paneX = e.clientX - rectC.left;
            var paneY = e.clientY - rectC.top;
            var areaW = rectC.width;
            var dividerX = areaW * _dividerPos / 100;
            var leftPaneW = dividerX;
            var rightPaneW = areaW - dividerX;

            var oldScaleC = compareState.targetLeftScale;
            var zoomFactorC = Math.pow(1.15, cSteps * cDirection);
            var newScaleC = Math.max(compareState.minScale, Math.min(compareState.maxScale, oldScaleC * zoomFactorC));

            var leftMX, rightMX;
            if (paneX < dividerX) {
                leftMX = paneX;
                rightMX = (paneX / leftPaneW) * rightPaneW;
            } else {
                leftMX = ((paneX - dividerX) / rightPaneW) * leftPaneW;
                rightMX = paneX - dividerX;
            }

            compareState.targetLeftOffsetX = leftMX - (leftMX - compareState.targetLeftOffsetX) * (newScaleC / oldScaleC);
            compareState.targetLeftOffsetY = paneY - (paneY - compareState.targetLeftOffsetY) * (newScaleC / oldScaleC);
            compareState.targetLeftScale = newScaleC;

            compareState.targetRightOffsetX = rightMX - (rightMX - compareState.targetRightOffsetX) * (newScaleC / oldScaleC);
            compareState.targetRightOffsetY = paneY - (paneY - compareState.targetRightOffsetY) * (newScaleC / oldScaleC);
            compareState.targetRightScale = newScaleC;

            if (!compareState.isAnimating) {
                compareState.isAnimating = true;
                requestAnimationFrame(animateCompareZoom);
            }
        } else if (currentViewMode === 'reference') {
            if (now - compareState.lastWheelTime > 200) compareState.wheelAccum = 0;
            compareState.lastWheelTime = now;
            compareState.wheelAccum += rawDelta;
            if (Math.abs(compareState.wheelAccum) < compareState.WHEEL_THRESHOLD) return;

            var rSteps = Math.floor(Math.abs(compareState.wheelAccum) / compareState.WHEEL_THRESHOLD);
            var rDirection = compareState.wheelAccum > 0 ? -1 : 1;
            compareState.wheelAccum = compareState.wheelAccum % compareState.WHEEL_THRESHOLD;

            var rectR = canvasArea.getBoundingClientRect();
            var rX = e.clientX - rectR.left;
            var rY = e.clientY - rectR.top;
            var dividerXR = rectR.width * _dividerPos / 100;

            if (rX < dividerXR) {
                var oldLS = compareState.targetLeftScale;
                var newLS = Math.max(compareState.minScale, Math.min(compareState.maxScale, oldLS * Math.pow(1.15, rSteps * rDirection)));
                compareState.targetLeftOffsetX = rX - (rX - compareState.targetLeftOffsetX) * (newLS / oldLS);
                compareState.targetLeftOffsetY = rY - (rY - compareState.targetLeftOffsetY) * (newLS / oldLS);
                compareState.targetLeftScale = newLS;
            } else {
                var oldRS = compareState.targetRightScale;
                var newRS = Math.max(compareState.minScale, Math.min(compareState.maxScale, oldRS * Math.pow(1.15, rSteps * rDirection)));
                var rightXR = rX - dividerXR;
                compareState.targetRightOffsetX = rightXR - (rightXR - compareState.targetRightOffsetX) * (newRS / oldRS);
                compareState.targetRightOffsetY = rY - (rY - compareState.targetRightOffsetY) * (newRS / oldRS);
                compareState.targetRightScale = newRS;
            }

            if (!compareState.isAnimating) {
                compareState.isAnimating = true;
                requestAnimationFrame(animateCompareZoom);
            }
        }
    }, { passive: false });

    function animateZoom() {
        var EASE = 0.15;

        zoomState.scale += (zoomState.targetScale - zoomState.scale) * EASE;
        zoomState.offsetX += (zoomState.targetOffsetX - zoomState.offsetX) * EASE;
        zoomState.offsetY += (zoomState.targetOffsetY - zoomState.offsetY) * EASE;

        updateTransform();

        var ds = Math.abs(zoomState.targetScale - zoomState.scale);
        var dx = Math.abs(zoomState.targetOffsetX - zoomState.offsetX);
        var dy = Math.abs(zoomState.targetOffsetY - zoomState.offsetY);

        if (ds < 0.001 && dx < 0.5 && dy < 0.5) {
            zoomState.scale = zoomState.targetScale;
            zoomState.offsetX = zoomState.targetOffsetX;
            zoomState.offsetY = zoomState.targetOffsetY;
            updateTransform();
            zoomState.isAnimating = false;
        } else {
            requestAnimationFrame(animateZoom);
        }
    }

    function animateCompareZoom() {
        var EASE = 0.15;

        compareState.leftScale += (compareState.targetLeftScale - compareState.leftScale) * EASE;
        compareState.rightScale += (compareState.targetRightScale - compareState.rightScale) * EASE;
        compareState.leftOffsetX += (compareState.targetLeftOffsetX - compareState.leftOffsetX) * EASE;
        compareState.leftOffsetY += (compareState.targetLeftOffsetY - compareState.leftOffsetY) * EASE;
        compareState.rightOffsetX += (compareState.targetRightOffsetX - compareState.rightOffsetX) * EASE;
        compareState.rightOffsetY += (compareState.targetRightOffsetY - compareState.rightOffsetY) * EASE;

        updateCompareTransform();

        var dls = Math.abs(compareState.targetLeftScale - compareState.leftScale);
        var drs = Math.abs(compareState.targetRightScale - compareState.rightScale);
        var dlx = Math.abs(compareState.targetLeftOffsetX - compareState.leftOffsetX);
        var dly = Math.abs(compareState.targetLeftOffsetY - compareState.leftOffsetY);
        var drx = Math.abs(compareState.targetRightOffsetX - compareState.rightOffsetX);
        var dry = Math.abs(compareState.targetRightOffsetY - compareState.rightOffsetY);

        if (dls < 0.001 && drs < 0.001 && dlx < 0.5 && dly < 0.5 && drx < 0.5 && dry < 0.5) {
            compareState.leftScale = compareState.targetLeftScale;
            compareState.rightScale = compareState.targetRightScale;
            compareState.leftOffsetX = compareState.targetLeftOffsetX;
            compareState.leftOffsetY = compareState.targetLeftOffsetY;
            compareState.rightOffsetX = compareState.targetRightOffsetX;
            compareState.rightOffsetY = compareState.targetRightOffsetY;
            updateCompareTransform();
            compareState.isAnimating = false;
        } else {
            requestAnimationFrame(animateCompareZoom);
        }
    }

    canvasArea.addEventListener('mousedown', function(e) {
        if (e.button !== 0) return;

        if (currentViewMode === 'single') {
            zoomState.isDragging = true;
            zoomState.startX = e.clientX - zoomState.offsetX;
            zoomState.startY = e.clientY - zoomState.offsetY;
        } else if (currentViewMode === 'compare' || currentViewMode === 'reference') {
            var rect = canvasArea.getBoundingClientRect();
            var x = e.clientX - rect.left;
            var areaW = rect.width;
            var dividerX = areaW * _dividerPos / 100;

            if (x < dividerX) {
                _comparePanState.isPanning = true;
                _comparePanState.side = 'left';
                _comparePanState.startX = e.clientX;
                _comparePanState.startY = e.clientY;
                _comparePanState.startOffsetX = compareState.leftOffsetX;
                _comparePanState.startOffsetY = compareState.leftOffsetY;
            } else {
                _comparePanState.isPanning = true;
                _comparePanState.side = 'right';
                _comparePanState.startX = e.clientX;
                _comparePanState.startY = e.clientY;
                _comparePanState.startOffsetX = compareState.rightOffsetX;
                _comparePanState.startOffsetY = compareState.rightOffsetY;
            }
        }
    });

    window.addEventListener('mousemove', function(e) {
        if (zoomState.isDragging) {
            zoomState.offsetX = e.clientX - zoomState.startX;
            zoomState.offsetY = e.clientY - zoomState.startY;
            updateTransform();
        } else if (_comparePanState.isPanning) {
            var dx = e.clientX - _comparePanState.startX;
            var dy = e.clientY - _comparePanState.startY;
            var areaW = canvasArea.clientWidth;
            var dividerXPan = areaW * _dividerPos / 100;

            if (_comparePanState.side === 'left') {
                var leftImg = $('#compare-left-img');
                var lW = leftImg.naturalWidth || 1;
                var newX = _comparePanState.startOffsetX + dx;
                newX = Math.min(newX, dividerXPan - lW * compareState.leftScale);
                compareState.leftOffsetX = newX;
                compareState.leftOffsetY = _comparePanState.startOffsetY + dy;
            } else {
                var newXR = _comparePanState.startOffsetX + dx;
                newXR = Math.max(newXR, 0);
                compareState.rightOffsetX = newXR;
                compareState.rightOffsetY = _comparePanState.startOffsetY + dy;
            }
            updateCompareTransform();
        }
    });

    window.addEventListener('mouseup', function() {
        zoomState.isDragging = false;
        _comparePanState.isPanning = false;
    });

    fitBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        if (currentViewMode === 'single') fitToView();
        else fitCompareToView();
    });

    canvasArea.addEventListener('dblclick', function(e) {
        if (e.button !== 0) return;
        e.preventDefault();
        if (currentViewMode === 'single') fitToView();
        else fitCompareToView();
    });

    $('#canvas-result').addEventListener('load', function() {
        if (!$('#canvas-stack').hidden) {
            requestAnimationFrame(function() { requestAnimationFrame(fitToView); });
        }
    });
}

/* ---------- left sidebar: gallery ---------- */
function renderGallery() {
    var list = $('#gallery-list');
    var empty = $('#gallery-empty');
    list.querySelectorAll('.gallery-item').forEach(function(el) { el.remove(); });

    if (targetImages.length === 0) {
        empty.hidden = false;
        updateSelectionUI();
        return;
    }
    empty.hidden = true;

    for (var i = 0; i < targetImages.length; i++) {
        (function(index) {
            var img = targetImages[index];
            var isCurrentEdit = index === currentTargetIndex;
            var selected = isSelected(index);

            var div = document.createElement('div');
            var cls = 'gallery-item';
            if (selected) cls += ' selected';
            if (isCurrentEdit) cls += ' current-edit';
            div.className = cls;

            if (isCurrentEdit) {
                var marker = document.createElement('div');
                marker.className = 'current-edit-marker';
                div.appendChild(marker);
            }

            var thumb = document.createElement('img');
            thumb.src = img.thumbnailUrl || img.sourcePath || '';
            thumb.alt = img.name || '';
            thumb.className = 'gallery-thumb';
            thumb.draggable = false;
            div.appendChild(thumb);

            var status = document.createElement('span');
            status.className = 'gallery-status ' + (img.status || '');
            status.textContent = (img.status === 'processing' ? '\u23F3' : img.status === 'done' ? '\u2713' : '');
            div.appendChild(status);

            var clickTimer = null;

            function clearLongPressTimer() {
                if (_galleryLongPressTimer) {
                    clearTimeout(_galleryLongPressTimer);
                    _galleryLongPressTimer = null;
                }
            }

            function startLongPress(e) {
                if ((e.type === 'mousedown' && e.button !== 0) || isProcessing) return;
                _galleryLongPressTriggered = false;
                clearLongPressTimer();
                _galleryLongPressTimer = setTimeout(function() {
                    _galleryLongPressTriggered = true;
                    clearLongPressTimer();
                    _selectedIndices = [index];
                    _lastClickedIndex = index;
                    renderGallery();
                    updateSelectionUI();
                    updateAllButtons();
                    deleteSelectedProjectImages([index]);
                }, 650);
            }

            function cancelLongPress() {
                clearLongPressTimer();
            }

            div.addEventListener('click', function(e) {
                if (_galleryLongPressTriggered) {
                    _galleryLongPressTriggered = false;
                    e.preventDefault();
                    return;
                }
                if (e.ctrlKey || e.metaKey || e.shiftKey) {
                    handleClick(index, e);
                    e.preventDefault();
                    return;
                }
                if (clickTimer) {
                    clearTimeout(clickTimer);
                    clickTimer = null;
                    handleDoubleClick(index);
                } else {
                    clickTimer = setTimeout(function() {
                        handleClick(index, e);
                        clickTimer = null;
                    }, 300);
                }
            });

            div.addEventListener('mousedown', startLongPress);
            div.addEventListener('touchstart', startLongPress, { passive: true });
            div.addEventListener('mouseup', cancelLongPress);
            div.addEventListener('mouseleave', cancelLongPress);
            div.addEventListener('touchend', cancelLongPress);
            div.addEventListener('touchcancel', cancelLongPress);
            div.setAttribute('draggable', 'true');
            div.addEventListener('dragstart', function(e) {
                if (isProcessing) {
                    e.preventDefault();
                    return;
                }
                clearLongPressTimer();
                _selectedIndices = [index];
                _lastClickedIndex = index;
                updateSelectionUI();
                updateAllButtons();
                _galleryDragImageId = img.id || '';
                div.classList.add('dragging');
                showGalleryDeleteDropzone();
                if (e.dataTransfer) {
                    e.dataTransfer.effectAllowed = 'move';
                    e.dataTransfer.setData('text/plain', img.id || '');
                    e.dataTransfer.setData('text/x-colorchase-gallery-id', img.id || '');
                }
            });
            div.addEventListener('dragend', function() {
                div.classList.remove('dragging');
                var deleteImageId = _galleryDeleteHover ? (img.id || _galleryDragImageId || '') : '';
                if (deleteImageId) {
                    _galleryPendingDeleteImageId = deleteImageId;
                    hideGalleryDeleteDropzone();
                    setTimeout(function() {
                        var pendingId = _galleryPendingDeleteImageId;
                        _galleryPendingDeleteImageId = '';
                        if (pendingId) {
                            deleteSelectedProjectImageById(pendingId);
                        }
                    }, 0);
                } else if (!_galleryPendingDeleteImageId) {
                    hideGalleryDeleteDropzone();
                }
            });

            list.appendChild(div);
        })(i);
    }

    updateSelectionUI();
    refreshGalleryWarnings();
}

function handleClick(index, e) {
    var ctrl = e.ctrlKey || e.metaKey;
    var shift = e.shiftKey;

    if (shift && _lastClickedIndex !== null && _lastClickedIndex >= 0 && _lastClickedIndex < targetImages.length) {
        var start = Math.min(_lastClickedIndex, index);
        var end = Math.max(_lastClickedIndex, index);
        for (var i = start; i <= end; i++) {
            if (!isSelected(i)) {
                _selectedIndices.push(i);
            }
        }
        _lastClickedIndex = index;
        renderGallery();
        updateSelectionUI();
        updateAllButtons();
    } else if (ctrl) {
        if (isSelected(index)) {
            _selectedIndices = _selectedIndices.filter(function(i) { return i !== index; });
        } else {
            _selectedIndices.push(index);
            _lastClickedIndex = index;
        }
        renderGallery();
        updateSelectionUI();
        updateAllButtons();
    } else {
        _lastClickedIndex = index;
        _selectedIndices = [index];
        switchTarget(index);
        updateSelectionUI();
        updateAllButtons();
    }
}

function handleDoubleClick(index) {
    switchTarget(index);
}

/* ---------- batch import ---------- */
async function importBatchFiles(files) {
    if (!files || files.length === 0) return;

    const formData = new FormData();
    for (const f of files) {
        formData.append('files', f);
    }
    if (window.currentProjectId) {
        formData.append('project_id', String(window.currentProjectId));
    }

    showToast(`正在导入 ${files.length} 张图片...`);

    try {
        const resp = await fetch(`${API_BASE}/api/upload_batch`, {
            method: 'POST',
            headers: (typeof getAuthHeaders === 'function' ? getAuthHeaders() : {}),
            body: formData
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            showToast('导入失败: ' + (err.detail || resp.status));
            return;
        }
        const data = await resp.json();

        for (const item of data.images) {
            let projectSavedPath = item.path || '';
            // 优先用 HTTP URL（asset_url/thumbnail），避免用本地路径(item.path)作为 <img src> 触发 file:// 错误
            let projectSourcePath = item.asset_url || item.thumbnail || item.path;
            let projectThumbnailUrl = item.thumbnail || item.asset_url || '';
            if (window.currentProjectId && !item.project_saved) {
                try {
            const projectAssetPath = await saveFileToProject(item.asset_url || '', 'source', item.name);
            if (projectAssetPath) {
                projectSavedPath = projectAssetPath;
                projectSourcePath = projectAssetPath;
                projectThumbnailUrl = projectAssetPath;
                    }
                } catch (e) {}
            }
            const newImg = {
                id: 'img_' + Date.now() + '_' + Math.random().toString(36).substr(2, 5),
                name: item.name,
                sourcePath: projectSourcePath,
                thumbnailUrl: projectThumbnailUrl,
                meta: item.meta || '',
                resultDataUrl: null,
                refDataUrl: null,
                refSavedPath: '',
                subjectMaskPath: '',
                subjectMaskUrl: '',
                subjectMaskMode: 'protect_subject',
                subjectMaskPoints: [],
                depthLayerPath: '',
                depthLayerUrl: '',
                semanticMatchMeta: null,
                sessionId: null,
                mergedSessionId: null,
                profileId: null,
                params: { intensity: 100, exposure: 100, contrast: 100, highlight: 100, shadow: 100, vibrance: 100 },
                status: 'pending',
                rating: 0,
                resultSavedPath: '',
                savedPath: projectSavedPath,
                // localSourcePath 用于 <img src> 显示，必须是浏览器可解码的 URL（JPG/PNG/HTTP）。
                // RAW 文件（.CR2/.NEF 等）浏览器原生不能渲染，必须用后端生成的 JPG 缩略图 URL。
                // sourcePath 字段保留原 asset_url（可能是 .CR2 URL）供追色接口解析回本地 RAW 文件。
                localSourcePath: projectThumbnailUrl || projectSourcePath,
                localReferencePath: '',
                localResultPath: '',
            };
            targetImages.push(newImg);
        }

        renderGallery();
        if (currentTargetIndex < 0 && targetImages.length > 0) {
            currentTargetIndex = 0;
            saveCurrentState();
            restoreCurrentState();
            const img = targetImages[0];
            $('#canvas-filename').textContent = img.name;
            $('#canvas-resolution').textContent = img.meta || '';
            $('#canvas-placeholder').hidden = true;
            $('#canvas-stack').hidden = false;
            const thumbSrc = img.thumbnailUrl || img.sourcePath;
            $('#canvas-original').src = thumbSrc;
            $('#canvas-result').src = thumbSrc;
            _origCanvasDataUrl = thumbSrc;
            setViewMode('single');
            renderGallery();
        }
        if (window.currentProjectId) {
            saveLocalProjectSnapshot();
        }
        showToast(`已导入 ${data.images.length} 张图片`);
        updateAllButtons();
    } catch (err) {
        showToast('网络错误: ' + err.message);
    }
}

/* ---------- right sidebar: tabs ---------- */
function switchTab(name) {
    activeTab = name;
    $$('.tab-btn-s').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    $$('.tab-panel').forEach(p => p.classList.toggle('active', p.dataset.panel === name));
    if (name === 'export') { updateExportPreview(); updateExportButton(); }
}

/* ---------- AI tab ---------- */
function updateAlgoInfo() {
    const algo = $('#ai-algorithm-select').value;
    const info = {
        reinhard: '经典 LAB 空间统计迁移，速度极快',
        histogram: '逐通道直方图 CDF 匹配，色彩分布精确',
        luminance_partition: '按高光/中间调/阴影分区独立迁移',
        neural_preset: 'CVPR 2023 论文方法，需先训练模型',
        modflows: 'AAAI 2025 SOTA，调制神经 ODE 流',
        modflows_b0: 'ModFlows 轻量版快速模式，适合预览和低配设备',
        regional_modflows: 'ModFlows + 语义分割，肤色保护',
        regional_luminance: '亮度分区迁移 + 语义分割后处理',
        ai_portrait: 'MediaPipe 面部语义 + 肤色/妆容保护',
        dncm_lut: 'DNCM 3x3矩阵 + 全色彩空间LUT采样',
    };
    $('#algo-info-s').textContent = info[algo] || '';

    const blendGroup = $('#ai-blend-group');
    blendGroup.style.display = (algo === 'luminance_partition' || algo === 'modflows' || algo === 'modflows_b0' ||
        algo === 'regional_modflows' || algo === 'regional_luminance') ? '' : 'none';
    const dncmModeGroup = $('#dncm-lut-mode-group');
    if (dncmModeGroup) dncmModeGroup.style.display = algo === 'dncm_lut' ? '' : 'none';
}

function fetchModelStatusCached(force) {
    if (force) {
        _modelStatusCache = null;
    }
    if (!force && _modelStatusCache) return Promise.resolve(_modelStatusCache);
    if (!force && _modelStatusPromise) return _modelStatusPromise;
    _modelStatusPromise = fetch(`${API_BASE}/api/model_status`, { method: 'GET', cache: 'no-store' })
        .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
        .then(function(result) {
            if (!result.ok) throw new Error((result.data && result.data.detail) || '模型状态读取失败');
            _modelStatusCache = result.data || {};
            return _modelStatusCache;
        })
        .finally(function() { _modelStatusPromise = null; });
    return _modelStatusPromise;
}

function getModelStatusMap(status) {
    var map = {};
    ((status && status.models) || []).forEach(function(model) {
        if (model && model.key) map[model.key] = model;
    });
    return map;
}

function applyModelOptionAvailability(selectEl, configs, modelMap) {
    if (!selectEl) return;
    var current = selectEl.value || 'auto';
    var firstEnabled = 'auto';
    Array.from(selectEl.options || []).forEach(function(option) {
        var cfg = configs[option.value];
        if (!cfg) return;
        var disabled = false;
        var title = '';
        if (cfg.type === 'always') {
            disabled = false;
        } else if (cfg.type === 'custom') {
            disabled = !!cfg.disabled;
            title = cfg.note || '';
        } else {
            var model = modelMap[cfg.modelKey];
            disabled = !model || model.enabled === false || model.ready === false || (cfg.requireReadyStatus && model.status !== 'ready');
            if (disabled && model && model.note) title = model.note;
        }
        option.disabled = !!disabled;
        option.title = title;
        if (!disabled && firstEnabled === 'auto') firstEnabled = option.value;
    });
    if (selectEl.options[selectEl.selectedIndex] && selectEl.options[selectEl.selectedIndex].disabled) {
        selectEl.value = firstEnabled;
        showToast('当前选择的模型暂不可用，已自动切换');
    } else if (!selectEl.value) {
        selectEl.value = firstEnabled;
    } else {
        selectEl.value = current;
    }
}

async function refreshCapabilityModelSelectors(force) {
    try {
        var status = await fetchModelStatusCached(!!force);
        var modelMap = getModelStatusMap(status);
        applyModelOptionAvailability($('#ai-mask-model'), {
            auto: { type: 'always' },
            birefnet: { type: 'model', modelKey: 'birefnet_subject_mask', requireReadyStatus: true },
            sam: { type: 'custom', disabled: true, note: 'SAM/SAM2 推理链路尚未接入，暂不可选' },
            fallback: { type: 'always' },
        }, modelMap);
        applyModelOptionAvailability($('#ai-depth-model'), {
            auto: { type: 'always' },
            depth_anything_v2: { type: 'model', modelKey: 'depth_anything_v2', requireReadyStatus: true },
            fallback: { type: 'always' },
        }, modelMap);
        applyModelOptionAvailability($('#ai-semantic-model'), {
            auto: { type: 'always' },
            dinov2: { type: 'model', modelKey: 'dinov2_semantic_match', requireReadyStatus: true },
            fallback: { type: 'always' },
        }, modelMap);
    } catch (err) {
        console.warn('[model-status] refresh selectors failed', err);
    }
}

async function ensureDncmLutReady() {
    const status = await fetchModelStatusCached(false);
    if (status && status.neural_preset_ready) return true;
    const missing = status && status.neuralpreset_missing_weights && status.neuralpreset_missing_weights.length
        ? status.neuralpreset_missing_weights.join('、')
        : 'norm_stage_best.pth、style_stage_best.pth';
    const dirs = status && status.neuralpreset_weight_dirs && status.neuralpreset_weight_dirs.length
        ? status.neuralpreset_weight_dirs.join('；')
        : 'models/neural_preset；weights/neuralpreset';
    showToast('DNCM / NeuralPreset LUT 权重不完整，缺少 ' + missing + '。请放入: ' + dirs, 7000);
    return false;
}

function isLocalMaskMode(mode) {
    return mode === 'protect_local' || mode === 'local_only';
}

function updateMaskUI() {
    var enabled = $('#ai-mask-enabled');
    var mode = $('#ai-mask-mode');
    var previewToggle = $('#ai-mask-preview-toggle');
    var strength = $('#ai-mask-strength');
    var status = $('#ai-mask-status');
    var maskImg = $('#canvas-mask-preview');
    if (mode) mode.value = _subjectMaskMode || 'protect_subject';
    if (strength && $('#ai-mask-strength-value')) $('#ai-mask-strength-value').textContent = strength.value;
    if (status) {
        if (_subjectMaskPath) {
            status.textContent = (_subjectMaskPoints.length ? '点选 mask 已生成' : '主体 mask 已生成') + (_subjectMaskUrl ? '，可预览/追色' : '');
        } else if (_subjectMaskPoints.length) {
            status.textContent = '已点选 ' + _subjectMaskPoints.length + ' 个点，点击生成 mask';
        } else {
            status.textContent = '未生成 mask';
        }
    }
    if (maskImg) {
        var shouldShow = !!(enabled && enabled.checked && previewToggle && previewToggle.checked && _subjectMaskUrl);
        maskImg.hidden = !shouldShow;
        if (shouldShow && maskImg.src !== _subjectMaskUrl) maskImg.src = _subjectMaskUrl;
    }
}

function clearSubjectMask(options) {
    options = options || {};
    _subjectMaskPath = '';
    _subjectMaskUrl = '';
    if (options.clearPoints) _subjectMaskPoints = [];
    var img = getCurrentImage();
    if (img) {
        img.subjectMaskPath = '';
        img.subjectMaskUrl = '';
        if (options.clearPoints) img.subjectMaskPoints = [];
    }
    updateMaskUI();
    saveSnapshot();
}

async function generateSubjectMask() {
    var img = getCurrentImage();
    if (!img || !img.sourcePath) { showToast('请先选择目标图片'); return; }
    await refreshCapabilityModelSelectors(true);
    var mode = ($('#ai-mask-mode') && $('#ai-mask-mode').value) || 'protect_subject';
    _subjectMaskMode = mode;
    var maskMode = isLocalMaskMode(mode) ? 'local' : 'subject';
    if (isLocalMaskMode(mode) && !_subjectMaskPoints.length) {
        showToast('请先在画布上点选区域');
        return;
    }
    var btn = $('#ai-mask-generate');
    if (btn) btn.disabled = true;
    if ($('#ai-mask-status')) $('#ai-mask-status').textContent = '正在生成 mask...';
    try {
        var fd = new FormData();
        fd.append('target_path', img.sourcePath);
        fd.append('mode', maskMode);
        fd.append('mask_model', ($('#ai-mask-model') && $('#ai-mask-model').value) || 'auto');
        fd.append('points_json', JSON.stringify(_subjectMaskPoints));
        var resp = await fetch(API_BASE + '/api/mask/subject', { method: 'POST', body: fd, headers: getAuthHeaders() });
        var data = await resp.json().catch(function() { return {}; });
        if (!resp.ok || !data.success) throw new Error(data.detail || 'mask 生成失败');
        _subjectMaskPath = data.mask_path || '';
        _subjectMaskUrl = data.mask_url || '';
        img.subjectMaskPath = _subjectMaskPath;
        img.subjectMaskUrl = _subjectMaskUrl;
        img.subjectMaskMode = _subjectMaskMode;
        img.subjectMaskPoints = _subjectMaskPoints.slice();
        if ($('#ai-mask-enabled')) $('#ai-mask-enabled').checked = true;
        if ($('#ai-mask-preview-toggle')) $('#ai-mask-preview-toggle').checked = true;
        updateMaskUI();
        saveSnapshot();
        showToast(data.cached ? '已使用缓存 mask' : 'mask 已生成');
    } catch (err) {
        showToast('mask 生成失败: ' + (err && err.message ? err.message : err));
        updateMaskUI();
    } finally {
        if (btn) btn.disabled = false;
    }
}

function handleMaskCanvasClick(e) {
    var enabled = $('#ai-mask-enabled');
    var mode = ($('#ai-mask-mode') && $('#ai-mask-mode').value) || 'protect_subject';
    if (!enabled || !enabled.checked || !isLocalMaskMode(mode) || currentViewMode !== 'single') return;
    var resultEl = $('#canvas-result');
    if (!resultEl || !resultEl.naturalWidth || !resultEl.naturalHeight) return;
    var rect = resultEl.getBoundingClientRect();
    if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) return;
    var x = (e.clientX - rect.left) / Math.max(rect.width, 1);
    var y = (e.clientY - rect.top) / Math.max(rect.height, 1);
    _subjectMaskPoints.push({ x: Math.max(0, Math.min(1, x)), y: Math.max(0, Math.min(1, y)), label: 'fg' });
    _subjectMaskPath = '';
    _subjectMaskUrl = '';
    var img = getCurrentImage();
    if (img) {
        img.subjectMaskPoints = _subjectMaskPoints.slice();
        img.subjectMaskPath = '';
        img.subjectMaskUrl = '';
    }
    updateMaskUI();
}

function updateDepthUI() {
    var enabled = $('#ai-depth-enabled');
    var previewToggle = $('#ai-depth-preview-toggle');
    var strength = $('#ai-depth-strength');
    var status = $('#ai-depth-status');
    var depthImg = $('#canvas-depth-preview');
    if (strength && $('#ai-depth-strength-value')) $('#ai-depth-strength-value').textContent = strength.value;
    if (status) {
        status.textContent = _depthLayerPath ? '深度图已生成，可预览/追色' : '未生成深度图';
    }
    if (depthImg) {
        var shouldShow = !!(enabled && enabled.checked && previewToggle && previewToggle.checked && _depthLayerUrl);
        depthImg.hidden = !shouldShow;
        if (shouldShow && depthImg.src !== _depthLayerUrl) depthImg.src = _depthLayerUrl;
    }
}

function clearDepthLayers() {
    _depthLayerPath = '';
    _depthLayerUrl = '';
    var img = getCurrentImage();
    if (img) {
        img.depthLayerPath = '';
        img.depthLayerUrl = '';
    }
    updateDepthUI();
    saveSnapshot();
}

async function generateDepthLayers() {
    var img = getCurrentImage();
    if (!img || !img.sourcePath) { showToast('请先选择目标图片'); return; }
    await refreshCapabilityModelSelectors(true);
    var btn = $('#ai-depth-generate');
    if (btn) btn.disabled = true;
    if ($('#ai-depth-status')) $('#ai-depth-status').textContent = '正在生成深度图...';
    try {
        var fd = new FormData();
        fd.append('target_path', img.sourcePath);
        fd.append('depth_model', ($('#ai-depth-model') && $('#ai-depth-model').value) || 'auto');
        var resp = await fetch(API_BASE + '/api/depth/layers', { method: 'POST', body: fd, headers: getAuthHeaders() });
        var data = await resp.json().catch(function() { return {}; });
        if (!resp.ok || !data.success) throw new Error(data.detail || '深度图生成失败');
        _depthLayerPath = data.depth_path || '';
        _depthLayerUrl = data.depth_url || '';
        img.depthLayerPath = _depthLayerPath;
        img.depthLayerUrl = _depthLayerUrl;
        if ($('#ai-depth-enabled')) $('#ai-depth-enabled').checked = true;
        if ($('#ai-depth-preview-toggle')) $('#ai-depth-preview-toggle').checked = true;
        updateDepthUI();
        saveSnapshot();
        showToast(data.cached ? '已使用缓存深度图' : '深度图已生成');
    } catch (err) {
        showToast('深度图生成失败: ' + (err && err.message ? err.message : err));
        updateDepthUI();
    } finally {
        if (btn) btn.disabled = false;
    }
}

function updateSemanticUI() {
    var strength = $('#ai-semantic-strength');
    var status = $('#ai-semantic-status');
    if (strength && $('#ai-semantic-strength-value')) $('#ai-semantic-strength-value').textContent = strength.value;
    if (!status) return;
    if (!_semanticMatchMeta || !_semanticMatchMeta.matches) {
        status.textContent = '未分析语义匹配';
        return;
    }
    var matches = (_semanticMatchMeta.matches || []).slice(0, 3).map(function(item) {
        return item.target + '→' + item.reference;
    });
    status.textContent = matches.length ? ('已匹配: ' + matches.join('，')) : '未找到稳定语义匹配';
}

async function analyzeSemanticMatch() {
    var img = getCurrentImage();
    var referenceUpload = getReferenceUploadFile();
    if (!img || !img.sourcePath) { showToast('请先选择目标图片'); return; }
    if (!referenceUpload) { showToast('请先上传参考图'); return; }
    await refreshCapabilityModelSelectors(true);
    var btn = $('#ai-semantic-analyze');
    if (btn) btn.disabled = true;
    if ($('#ai-semantic-status')) $('#ai-semantic-status').textContent = '正在分析语义匹配...';
    try {
        var fd = new FormData();
        fd.append('target_path', img.sourcePath);
        fd.append('semantic_model', ($('#ai-semantic-model') && $('#ai-semantic-model').value) || 'auto');
        if (img.refSavedPath || window._refSavedPath) {
            fd.append('reference_path', img.refSavedPath || window._refSavedPath);
        } else {
            fd.append('reference', referenceUpload, refFile && refFile.name ? refFile.name : 'reference.jpg');
        }
        var resp = await fetch(API_BASE + '/api/semantic/match', { method: 'POST', body: fd, headers: getAuthHeaders() });
        var data = await resp.json().catch(function() { return {}; });
        if (!resp.ok || !data.success) throw new Error(data.detail || '语义匹配分析失败');
        _semanticMatchMeta = data.meta || null;
        if (data.reference_path) {
            img.refSavedPath = data.reference_path;
            window._refSavedPath = data.reference_path;
        }
        img.semanticMatchMeta = _semanticMatchMeta;
        if ($('#ai-semantic-enabled')) $('#ai-semantic-enabled').checked = true;
        updateSemanticUI();
        saveSnapshot();
        showToast('语义匹配分析完成');
    } catch (err) {
        showToast('语义匹配分析失败: ' + (err && err.message ? err.message : err));
        updateSemanticUI();
    } finally {
        if (btn) btn.disabled = false;
    }
}

function updateProgress(elPrefix, progress, message) {
    $('#canvas-status').hidden = progress <= 0;
    $(`#${elPrefix}-progress-bar`).style.width = progress + '%';
    $(`#${elPrefix}-progress-text`).textContent = Math.round(progress) + '%';
    if (message) $(`#${elPrefix}-progress-message`).textContent = message;
}

function startProgressSSE(taskId, elPrefix, trace) {
    let retryCount = 0, isTerminated = false;
    function connect() {
        const evtSource = new EventSource(`${API_BASE}/api/progress/${taskId}`);
        evtSource.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                updateProgress(elPrefix, data.progress, data.message);
                const isFinalDone = data.stage === 'done' && Number(data.progress || 0) >= 100;
                if (isFinalDone || data.stage === 'error' || data.stage === 'cancelled') {
                    if (trace) trace('sse_' + data.stage, { progress: data.progress, message: data.message || '' });
                    isTerminated = true; evtSource.close();
                    if (isFinalDone) {
                        updateProgress(elPrefix, 100, '追色完成，正在准备结果...');
                    } else {
                        setTimeout(() => $('#canvas-status').hidden = true, 3000);
                    }
                }
            } catch(e) {}
        };
        evtSource.onerror = () => {
            evtSource.close();
            if (isTerminated) return;
            if (++retryCount < 3) setTimeout(connect, 1000 * retryCount);
        };
        return evtSource;
    }
    return connect();
}

async function doAITransfer() {
    const img = getCurrentImage();
    const referenceUpload = getReferenceUploadFile();
    const referencePath = img && (img.refSavedPath || window._refSavedPath) ? (img.refSavedPath || window._refSavedPath) : '';
    if (!img) { showToast('请先选择目标图片并上传参考图'); return; }
    if (!referenceUpload && !referencePath) { showToast('请先上传参考图'); return; }

    const algorithm = $('#ai-algorithm-select').value;
    await refreshCapabilityModelSelectors(true);
    if (algorithm === 'dncm_lut' && !(await ensureDncmLutReady())) return;
    if ($('#ai-depth-enabled') && $('#ai-depth-enabled').checked) {
        if (!_depthLayerPath) {
            await generateDepthLayers();
        }
        if (!_depthLayerPath) return;
    }
    if ($('#ai-mask-enabled') && $('#ai-mask-enabled').checked) {
        _subjectMaskMode = ($('#ai-mask-mode') && $('#ai-mask-mode').value) || 'protect_subject';
        if (!_subjectMaskPath) {
            await generateSubjectMask();
        }
        if (!_subjectMaskPath) return;
    }

    isProcessing = true; updateAllButtons();

    const taskId = Date.now().toString(36) + Math.random().toString(36).substr(2, 6);
    const perfTrace = createPerfTrace('ai-transfer', {
        task_id: taskId,
        algorithm: algorithm,
        image: img.name || '',
    });
    perfTrace('start');

    updateProgress('canvas', 2, '准备中...');
    await new Promise(r => setTimeout(r, 200));
    const sse = startProgressSSE(taskId, 'canvas', perfTrace);
    await new Promise(r => setTimeout(r, 300));

    try {
        const formData = new FormData();
        formData.append('target', new File([], img.name));
        formData.append('target_path', img.sourcePath);
        if (window.currentProjectId) {
            formData.append('project_id', String(window.currentProjectId));
        }
        if (referencePath) {
            formData.append('reference_path', referencePath);
        } else {
            formData.append('reference', referenceUpload, refFile && refFile.name ? refFile.name : 'reference.jpg');
        }
        formData.append('algorithm', algorithm);
        formData.append('blend_strength', $('#ai-blend-slider').value / 100);
        formData.append('enable_postprocess', $('#ai-postprocess').checked);
        formData.append('enable_metrics', $('#ai-metrics').checked);
        formData.append('task_id', taskId);
        formData.append('generate_lut_only', '1');
        if ($('#ai-semantic-enabled') && $('#ai-semantic-enabled').checked) {
            formData.append('enable_semantic_match', '1');
            formData.append('semantic_model', ($('#ai-semantic-model') && $('#ai-semantic-model').value) || 'auto');
            formData.append('semantic_strength', (($('#ai-semantic-strength') && $('#ai-semantic-strength').value) || 55) / 100);
        }
        if ($('#ai-depth-enabled') && $('#ai-depth-enabled').checked && _depthLayerPath) {
            formData.append('enable_depth_layers', '1');
            formData.append('depth_model', ($('#ai-depth-model') && $('#ai-depth-model').value) || 'auto');
            formData.append('depth_path', _depthLayerPath);
            formData.append('depth_strength', (($('#ai-depth-strength') && $('#ai-depth-strength').value) || 65) / 100);
        }
        if ($('#ai-mask-enabled') && $('#ai-mask-enabled').checked && _subjectMaskPath) {
            formData.append('mask_path', _subjectMaskPath);
            formData.append('mask_model', ($('#ai-mask-model') && $('#ai-mask-model').value) || 'auto');
            formData.append('mask_mode', _subjectMaskMode || 'protect_subject');
            formData.append('mask_strength', (($('#ai-mask-strength') && $('#ai-mask-strength').value) || 100) / 100);
        }
        if (algorithm === 'dncm_lut') {
            formData.append('lut_mode', ($('#dncm-lut-mode') && $('#dncm-lut-mode').value) || 'fast');
        }

        perfTrace('fetch_start');
        const resp = await fetch(`${API_BASE}/api/transfer`, { method: 'POST', body: formData, headers: getAuthHeaders() });
        perfTrace('fetch_response', { ok: resp.ok, status: resp.status });
        if (!resp.ok) {
            let errMsg = `请求失败 (${resp.status})`;
            try { const d = await resp.json(); errMsg = d.detail || errMsg; } catch {}
            showToast('AI追色失败: ' + errMsg);
            return;
        }
        const data = await resp.json();
        perfTrace('json_parsed', {
            success: !!data.success,
            session_id: data.session_id || '',
            has_result_url: !!(data.images && data.images.result_url),
        });

        if (data.success) {
            _lastSessionId = data.session_id;
            lutAI = data.session_id;
            lutProfile = null; _profileBuiltin = null; _profileFile = null;
            _profileSessionId = null;

            $('#profile-select').value = 'standard';
            $('#profile-status-text').textContent = '未加载配置文件';
            $('#apply-profile-btn').disabled = true;

            const resultSrc = data.images.result_url || data.images.result;
            const targetSrc = data.images.target_url || data.images.target;
            perfTrace('result_sources_ready', {
                result_kind: resultSrc && resultSrc.startsWith('data:') ? 'data' : 'url',
                target_kind: targetSrc && targetSrc.startsWith('data:') ? 'data' : 'url',
            });
            _resultCanvasDataUrl = resultSrc;
            _origCanvasDataUrl = targetSrc;

            img.sessionId = data.session_id;
            img.resultDataUrl = resultSrc;
            img.resultSavedPath = data.result_path || img.resultSavedPath || '';
            img.localResultPath = resultSrc || img.localResultPath || '';
            img.status = 'done';
            img.aiAlgo = data.algorithm || $('#ai-algorithm-select').value || '';
            var previousRefPath = img.refSavedPath || window._refSavedPath || '';
            var returnedRefUrl = normalizeProjectAssetUrl(data.reference_path || '', window.currentProjectId);
            img.refSavedPath = previousRefPath || returnedRefUrl || data.reference_path || '';
            img.localReferencePath = getImageReferenceSrc(img) || returnedRefUrl || '';
            window._refSavedPath = img.refSavedPath || window._refSavedPath || '';
            if (data.mask) {
                img.subjectMaskPath = data.mask.mask_path || _subjectMaskPath || '';
                img.subjectMaskMode = data.mask.mode || _subjectMaskMode || 'protect_subject';
                img.subjectMaskUrl = _subjectMaskUrl || img.subjectMaskUrl || '';
                img.subjectMaskPoints = _subjectMaskPoints.slice();
            }
            if (data.depth) {
                img.depthLayerPath = data.depth.depth_path || _depthLayerPath || '';
                img.depthLayerUrl = _depthLayerUrl || img.depthLayerUrl || '';
            }
            if (data.semantic && data.semantic.meta) {
                _semanticMatchMeta = data.semantic.meta;
                img.semanticMatchMeta = _semanticMatchMeta;
                updateSemanticUI();
            }
            if (data.reusable_preset && data.reusable_preset.id) {
                img.generatedStyleId = data.reusable_preset.id;
                loadStyleGallery();
            }

            perfTrace('before_save_snapshot');
            saveSnapshot();
            perfTrace('after_save_snapshot');

            $('#canvas-status').hidden = true;
            $('#canvas-original').src = targetSrc;
            var resultEl = $('#canvas-result');
            resultEl.addEventListener('load', function onResultLoad() {
                resultEl.removeEventListener('load', onResultLoad);
                perfTrace('canvas_result_loaded', {
                    width: resultEl.naturalWidth || 0,
                    height: resultEl.naturalHeight || 0,
                });
            });
            perfTrace('before_set_result_src');
            resultEl.src = resultSrc;
            perfTrace('after_set_result_src');
            deferIdle(function() {
                perfTrace('image_data_cache_start');
                Promise.all([
                    base64ToImageData(targetSrc),
                    base64ToImageData(resultSrc),
                ]).then(function(items) {
                    _originalImageData = items[0];
                    _stylizedImageData = items[1];
                    perfTrace('image_data_cache_done', {
                        width: _stylizedImageData ? _stylizedImageData.width : 0,
                        height: _stylizedImageData ? _stylizedImageData.height : 0,
                    });
                }).catch(function(e) {
                    perfTrace('image_data_cache_error', { message: e && e.message ? e.message : String(e) });
                    console.error('ImageData cache failed:', e);
                });
            });

            resetAdjustSliders();
            $('#adjust-sliders').hidden = false;

            if (data.metrics) {
                var m = data.metrics;
                var mEl = $('#metrics-result');
                mEl.hidden = false;
                var styleVal = m.style_similarity != null ? m.style_similarity : 0;
                var contentVal = m.content_similarity != null ? m.content_similarity : 0;
                var overallVal = m.overall_score != null ? m.overall_score : 0;
                $('#metrics-style-val').textContent = (styleVal * 100).toFixed(1) + '%';
                $('#metrics-content-val').textContent = (contentVal * 100).toFixed(1) + '%';
                $('#metrics-overall-val').textContent = (overallVal * 100).toFixed(1) + '%';
                setTimeout(function() {
                    $('#metrics-style-bar').style.width = (styleVal * 100) + '%';
                    $('#metrics-content-bar').style.width = (contentVal * 100) + '%';
                    $('#metrics-overall-bar').style.width = (overallVal * 100) + '%';
                }, 50);
            } else {
                $('#metrics-result').hidden = true;
                $('#metrics-style-bar').style.width = '0';
                $('#metrics-content-bar').style.width = '0';
                $('#metrics-overall-bar').style.width = '0';
            }

            saveCurrentState();
            perfTrace('after_save_current_state');
            renderGallery();
            perfTrace('after_render_gallery');
            updateAllButtons();
            perfTrace('after_update_buttons');

            deferIdle(function() {
                perfTrace('merge_idle_start');
                mergeAndUpdateCanvas().then(function() {
                    perfTrace('merge_idle_done');
                }).catch(function(err) {
                    perfTrace('merge_idle_error', { message: err && err.message ? err.message : String(err) });
                });
            });
            setViewMode('single');
            perfTrace('after_set_view_mode');
            showToast(data.reusable_preset && data.reusable_preset.name
                ? 'AI 追色完成，已保存 LUT 预设: ' + data.reusable_preset.name
                : 'AI 追色完成！');
        }
    } catch(err) {
        perfTrace('error', { message: err.message });
        showToast('网络错误: ' + err.message);
    } finally {
        isProcessing = false; updateAllButtons();
        if (sse) sse.close();
        setTimeout(() => { $('#canvas-status').hidden = true; }, 3500);
        perfTrace('finally_done');
    }
}

/* ---------- Profile tab ---------- */
function updateProfileStatus() {
    const val = $('#profile-select').value;
    if (val === 'standard') {
        _profileBuiltin = null;
        if (!_profileFile) {
            lutProfile = null;
            $('#profile-status-text').textContent = '未加载配置文件';
        }
    } else if (BUILTIN_PROFILES.includes(val)) {
        _profileBuiltin = val;
        _profileFile = null;
        $('#profile-status-text').textContent = '预设: ' + $('#profile-select option:checked').textContent;
        lutProfile = { type: 'builtin', name: val };
    } else if (val.startsWith('custom_') && !window._capturedStyleLutPath) {
        if (window._profileFile) {
            updateProfileApplyButton();
        }
    }
    updateProfileApplyButton();
}

function importProfileFile() {
    $('#profile-file-input').click();
}

function handleProfileFileInput(file) {
    if (!file) return;
    const ext = '.' + file.name.split('.').pop().toLowerCase();
    const allowed = ['.cube', '.3dl', '.csp', '.spi3d', '.spi1d', '.lut', '.pf3', '.xmp'];
    if (!allowed.includes(ext)) { showToast('不支持的格式: ' + ext); return; }

    _profileFile = file;
    _profileBuiltin = null;

    if (ext !== '.xmp') {
        resetLrUpgradeHint();
    }

    const optionValue = 'custom_' + Date.now();
    const select = $('#profile-select');
    const existing = select.querySelector('option[value^="custom_"]');
    if (existing) existing.remove();

    const opt = document.createElement('option');
    opt.value = optionValue;
    opt.textContent = '自定义: ' + file.name;
    select.appendChild(opt);
    select.value = optionValue;
    $('#profile-status-text').textContent = '自定义: ' + file.name;

    lutProfile = { type: 'custom', file: file };
    updateProfileApplyButton();
}

var _selectedStyleId = null;

async function loadStyleGallery() {
    var gallery = $('#style-gallery');
    var empty = $('#style-gallery-empty');
    if (!gallery || !empty) return;
    gallery.innerHTML = '';
    try {
        var resp = await fetch('/api/list_styles');
        var styles = await resp.json();
        if (!styles || styles.length === 0) {
            gallery.style.display = 'none';
            empty.style.display = '';
            return;
        }
        gallery.style.display = 'grid';
        empty.style.display = 'none';
        styles.forEach(function(s) {
            var card = document.createElement('div');
            card.style.cssText = 'position:relative;display:flex;flex-direction:column;align-items:center;cursor:pointer;border-radius:6px;padding:4px;background:var(--bg-card);border:2px solid transparent;transition:transform 0.35s cubic-bezier(0.34,1.56,0.64,1),box-shadow 0.35s ease,border-color 0.35s ease;';
            card.dataset.styleId = s.id;
            if (s.id === _selectedStyleId) {
                card.style.borderColor = '#f59e0b';
            }

            var img = document.createElement('img');
            img.src = '/static/assets/style-icon.png';
            img.style.cssText = 'width:48px;height:48px;object-fit:contain;border-radius:4px;pointer-events:none;background:#222;image-rendering:auto;-webkit-font-smoothing:antialiased;';
            card.appendChild(img);

            var label = document.createElement('div');
            label.style.cssText = 'font-size:10px;color:var(--text-secondary);margin-top:2px;max-width:60px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:center;pointer-events:none;';
            label.textContent = s.name || s.id;
            card.appendChild(label);

            var clickTimer = null;
            card.addEventListener('click', function() {
                if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; return; }
                clickTimer = setTimeout(function() {
                    clickTimer = null;
                    if (s.id === _selectedStyleId) {
                        _selectedStyleId = null;
                        gallery.querySelectorAll('div[data-style-id]').forEach(function(el) { el.style.borderColor = 'transparent'; });
                        unapplyCameraStyle();
                        return;
                    }
                    _selectedStyleId = s.id;
                    gallery.querySelectorAll('div[data-style-id]').forEach(function(el) { el.style.borderColor = 'transparent'; });
                    card.style.borderColor = '#f59e0b';
                    applyStyle(s.id, s.name);
                }, 250);
            });

            card.addEventListener('dblclick', function(e) {
                e.preventDefault();
                e.stopPropagation();
                if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
                startInlineRename(s, card, label);
            });

            card.addEventListener('contextmenu', function(e) {
                e.preventDefault();
                e.stopPropagation();
                showStyleContextMenu(e, s, card);
            });

            card.addEventListener('mouseenter', function() {
                card.style.transform = 'scale(1.04) translateY(-2px)';
                card.style.boxShadow = '0 6px 20px rgba(0,0,0,0.35)';
            });
            card.addEventListener('mouseleave', function() {
                card.style.transform = '';
                card.style.boxShadow = '';
            });

            gallery.appendChild(card);
        });
    } catch (e) {
        gallery.style.display = 'none';
        empty.style.display = '';
    }
}

async function loadVideoStyleGallery() {
    var gallery = document.getElementById('vstyle-gallery');
    var empty = document.getElementById('vstyle-gallery-empty');
    if (!gallery || !empty) return;
    gallery.innerHTML = '';
    try {
        var resp = await fetch('/api/list_styles');
        var styles = await resp.json();
        if (!styles || styles.length === 0) {
            gallery.style.display = 'none';
            empty.style.display = '';
            return;
        }
        gallery.style.display = 'grid';
        empty.style.display = 'none';
        styles.forEach(function(s) {
            var card = document.createElement('div');
            card.style.cssText = 'position:relative;display:flex;flex-direction:column;align-items:center;cursor:pointer;border-radius:6px;padding:4px;background:var(--bg-card,#141c29);border:2px solid transparent;transition:transform 0.35s cubic-bezier(0.34,1.56,0.64,1),box-shadow 0.35s ease,border-color 0.35s ease;';
            card.dataset.styleId = s.id;
            if (s.id === _selectedStyleId) card.style.borderColor = '#f59e0b';

            var img = document.createElement('img');
            img.src = '/static/assets/style-icon.png';
            img.style.cssText = 'width:48px;height:48px;object-fit:contain;border-radius:4px;pointer-events:none;background:#222;';
            card.appendChild(img);

            var label = document.createElement('div');
            label.style.cssText = 'font-size:10px;color:#9CA3AF;margin-top:2px;max-width:60px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:center;pointer-events:none;';
            label.textContent = s.name || s.id;
            card.appendChild(label);

            var clickTimer = null;
            card.addEventListener('click', function() {
                if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; return; }
                clickTimer = setTimeout(function() {
                    clickTimer = null;
                    if (s.id === _selectedStyleId) {
                        _selectedStyleId = null;
                        gallery.querySelectorAll('div[data-style-id]').forEach(function(el) { el.style.borderColor = 'transparent'; });
                        unapplyCameraStyle();
                        return;
                    }
                    _selectedStyleId = s.id;
                    gallery.querySelectorAll('div[data-style-id]').forEach(function(el) { el.style.borderColor = 'transparent'; });
                    card.style.borderColor = '#f59e0b';
                    applyStyle(s.id, s.name);
                }, 250);
            });
            card.addEventListener('dblclick', function(e) {
                e.preventDefault(); e.stopPropagation();
                if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
                startInlineRename(s, card, label);
            });
            card.addEventListener('contextmenu', function(e) {
                e.preventDefault(); e.stopPropagation();
                showStyleContextMenu(e, s, card);
            });

            card.addEventListener('mouseenter', function() {
                card.style.transform = 'scale(1.04) translateY(-2px)';
                card.style.boxShadow = '0 6px 20px rgba(0,0,0,0.35)';
            });
            card.addEventListener('mouseleave', function() {
                card.style.transform = '';
                card.style.boxShadow = '';
            });

            gallery.appendChild(card);
        });
    } catch (e) {
        gallery.style.display = 'none';
        empty.style.display = '';
    }
}

async function renameStyle(styleId, newName) {
    try {
        var fd = new FormData();
        fd.append('style_id', styleId);
        fd.append('new_name', newName);
        var resp = await fetch('/api/rename_style', { method: 'POST', body: fd });
        var data = await resp.json();
        if (!resp.ok) {
            showToast('重命名失败: ' + (data.detail || ''));
            return;
        }
        showToast('已重命名为: ' + newName);
        loadStyleGallery();
    } catch (err) {
        showToast('重命名失败: ' + (err.message || ''));
    }
}

function startInlineRename(styleObj, cardEl, labelEl) {
    if (!cardEl || !labelEl) return;
    var oldInput = cardEl.querySelector('.rename-input');
    if (oldInput) { oldInput.focus(); oldInput.select(); return; }

    labelEl.style.display = 'none';

    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'rename-input';
    input.value = styleObj.name || styleObj.id;
    input.style.cssText = 'width:56px;font-size:10px;color:#fff;background:#252540;border:1px solid #f59e0b;border-radius:3px;padding:1px 3px;text-align:center;outline:none;';

    cardEl.appendChild(input);
    input.focus();
    input.select();

    var done = false;
    function finish() {
        if (done) return;
        done = true;
        var val = input.value.trim();
        cardEl.removeChild(input);
        labelEl.style.display = '';
        if (val && val !== styleObj.name) {
            labelEl.textContent = val;
            renameStyle(styleObj.id, val);
        }
    }
    input.addEventListener('blur', finish);
    input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); finish(); }
        if (e.key === 'Escape') { input.value = styleObj.name; finish(); }
    });
}

function showStyleContextMenu(e, styleObj, cardEl) {
    var old = document.getElementById('style-ctx-menu');
    if (old) old.remove();

    var menu = document.createElement('div');
    menu.id = 'style-ctx-menu';
    menu.style.cssText = 'position:fixed;z-index:9999;background:#1e1e38;border:1px solid #383860;border-radius:6px;padding:4px 0;min-width:120px;box-shadow:0 4px 16px rgba(0,0,0,0.5);';

    var items = [
        { label: '✏️ 重命名', action: function() {
            var labelEl = cardEl ? cardEl.querySelector('div') : null;
            startInlineRename(styleObj, cardEl, labelEl);
        }},
        { label: '应用风格', action: function() {
            _selectedStyleId = styleObj.id;
            var gallery = $('#style-gallery');
            if (gallery) gallery.querySelectorAll('div[data-style-id]').forEach(function(el) { el.style.borderColor = 'transparent'; });
            if (cardEl) cardEl.style.borderColor = '#f59e0b';
            applyStyle(styleObj.id, styleObj.name);
        }},
    ];

    items.forEach(function(item) {
        var row = document.createElement('div');
        row.textContent = item.label;
        row.style.cssText = 'padding:6px 12px;font-size:12px;color:#ccc;cursor:pointer;white-space:nowrap;';
        row.addEventListener('mouseenter', function() { row.style.background = '#28284a'; row.style.color = '#fff'; });
        row.addEventListener('mouseleave', function() { row.style.background = ''; row.style.color = '#ccc'; });
        row.addEventListener('click', function() { menu.remove(); item.action(); });
        menu.appendChild(row);
    });

    menu.style.left = e.clientX + 'px';
    menu.style.top = e.clientY + 'px';
    document.body.appendChild(menu);

    var closeHandler = function() { menu.remove(); document.removeEventListener('click', closeHandler); };
    setTimeout(function() { document.addEventListener('click', closeHandler); }, 10);
}

async function applyStyle(styleId, styleName) {
    if (window.currentProjectType === 'video') {
        var resp = await fetch('/api/get_style/' + styleId);
        var styleData = await resp.json();
        if (styleData.npy_path) {
            window._capturedStyleLutPath = styleData.npy_path;
            window._capturedStyleName = styleName;
            window._profileBuiltin = null;
            window._profileFile = null;
            window._capturedStyleId = styleId;
            var statusEl = document.getElementById('video-profile-status');
            if (statusEl) statusEl.textContent = '相机风格: ' + styleName;
            var outsideEl = document.getElementById('video-profile-outside-status');
            if (outsideEl) outsideEl.textContent = '📷 相机风格: ' + styleName;
        }
        return;
    }
    var img = getCurrentImage();
    if (!img) { showToast('请先选择目标图片'); return; }

    isProcessing = true; updateAllButtons();
    try {
        var fd = new FormData();
        fd.append('target_path', img.sourcePath);
        fd.append('style_id', styleId);
        var sid = img.sessionId || img.mergedSessionId || _lastSessionId;
        if (sid) fd.append('session_id', sid);

        var resp = await fetch('/api/apply_style', { method: 'POST', body: fd });
        var data = await resp.json();
        if (!resp.ok) {
            showToast('风格应用失败: ' + (data.detail || '未知错误'));
            return;
        }

        img.profileId = styleId;
        img.sessionId = data.merged_session_id || img.sessionId;
        img.mergedSessionId = data.merged_session_id || img.mergedSessionId;
        _lastSessionId = data.merged_session_id || _lastSessionId;
        img.status = 'done';

        if (data.result_b64) {
            _resultCanvasDataUrl = data.result_b64;
            $('#canvas-result').src = data.result_b64;
            img.resultDataUrl = data.result_b64;
        }
        if (data.original_b64) {
            $('#canvas-original').src = data.original_b64;
            _origCanvasDataUrl = data.original_b64;
        }

        if (data.result_b64) {
            try {
                var sd = await base64ToImageData(data.result_b64);
                _stylizedImageData = sd;
            } catch(e) {}
        }

        $('#adjust-sliders').hidden = true;
        resetAdjustSliders();

        saveCurrentState();
        showToast('已应用风格: ' + (styleName || styleId));
    } catch (err) {
        showToast('风格应用失败: ' + (err.message || ''));
    } finally {
        isProcessing = false; updateAllButtons();
    }
}

function unapplyCameraStyle() {
    var img = getCurrentImage();
    if (!img) return;
    img.profileId = null;
    img.sessionId = null;
    img.mergedSessionId = null;
    _lastSessionId = null;
    _stylizedImageData = null;
    if (_origCanvasDataUrl) {
        _resultCanvasDataUrl = _origCanvasDataUrl;
        $('#canvas-result').src = _origCanvasDataUrl;
        img.resultDataUrl = _origCanvasDataUrl;
    }
    saveCurrentState();
    showToast('已取消相机风格');
}

async function applyProfile() {
    var img = getCurrentImage();
    if (!img) { showToast('请先选择目标图片'); return; }
    var val = $('#profile-select').value;

    if (val === 'standard' && !_profileFile) {
        _profileSessionId = null;
        lutProfile = null;
        img.profileId = null;
        _profileBuiltin = null;
        _profileFile = null;

        var customOpt = $('#profile-select').querySelector('option[value^="custom_"]');
        if (customOpt) customOpt.remove();
        $('#profile-status-text').textContent = '标准（无滤镜）';

        if (_lastSessionId) {
            await mergeAndUpdateCanvas();
        } else {
            var origSrc = _origCanvasDataUrl || img.thumbnailUrl || img.sourcePath;
            if (origSrc) {
                _resultCanvasDataUrl = origSrc;
                $('#canvas-result').src = origSrc;
                img.resultDataUrl = origSrc;
            }
        }

        $('#adjust-sliders').hidden = true;
        resetAdjustSliders();
        saveCurrentState();
        updateAllButtons();
        showToast('已恢复标准（无滤镜）');
        return;
    }

    isProcessing = true; updateAllButtons();

    const formData = new FormData();
    formData.append('target_path', img.sourcePath);
    formData.append('session_id', _lastSessionId || '');
    if (window.currentProjectId) {
        formData.append('project_id', String(window.currentProjectId));
    }

    if (_profileBuiltin) {
        formData.append('profile_builtin', _profileBuiltin);
    } else if (_profileFile) {
        formData.append('profile_file', _profileFile);
    }

    try {
        const resp = await fetch(`${API_BASE}/api/apply_profile`, {
            method: 'POST', body: formData,
            headers: getAuthHeaders(),
        });
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            showToast('应用配置失败: ' + (d.detail || resp.status));
            return;
        }
        const data = await resp.json();

        if (data.success) {
            _profileSessionId = data.lut_path || null;
            lutProfile = _profileSessionId;
            img.profileId = _profileBuiltin || _profileSessionId;

            _resultCanvasDataUrl = data.result_b64;
            $('#canvas-result').src = data.result_b64;
            img.resultDataUrl = data.result_b64;
            img.status = 'done';

            if (data.original_b64) {
                $('#canvas-original').src = data.original_b64;
                _origCanvasDataUrl = data.original_b64;
                try {
                    _originalImageData = await base64ToImageData(data.original_b64);
                } catch(e) {}
            }

            if (data.result_b64) {
                try {
                    const sd = await base64ToImageData(data.result_b64);
                    _stylizedImageData = sd;
                } catch(e) {}
            }

            $('#adjust-sliders').hidden = true;
            resetAdjustSliders();

            saveCurrentState();
            updateAllButtons();

            await mergeAndUpdateCanvas();
            showToast('配置已应用！');
            if (_profileFile && _profileFile.name && _profileFile.name.toLowerCase().endsWith('.xmp')) {
                showToast("✅ 快速预览已生成 (80%精度)。如需98%高保真还原，请使用下方的 Lightroom 预设高保真提取功能。", 5000);
                linkXmpToHiFi(_profileFile, _profileFile.name);
            } else {
                resetLrUpgradeHint();
            }
        }
    } catch(err) {
        showToast('网络错误: ' + err.message);
    } finally {
        isProcessing = false; updateAllButtons();
    }
}

/* ---------- XMP HiFi Link ---------- */
var _lrXmpFile = null;

async function linkXmpToHiFi(xmpFile, fileName) {
    var styleName = fileName.replace(/\.xmp$/i, '');
    var section = $('#lr-hifi-section');
    var hint = $('#lr-upgrade-hint');
    var nameEl = $('#lr-upgrade-name');
    var dlEl = $('#lr-upgrade-download');
    var manualArea = $('#lr-manual-area');

    if (!section || !hint || !nameEl || !dlEl) return;

    section.style.display = '';
    nameEl.textContent = styleName;
    hint.style.display = '';
    manualArea.style.display = 'none';

    _lrXmpFile = xmpFile;

    try {
        var fd = new FormData();
        fd.append('xmp_file', xmpFile);
        fd.append('style_name', styleName);
        var resp = await fetch(API_BASE + '/api/prepare_lr_preset', { method: 'POST', body: fd });
        if (!resp.ok) {
            showToast('DNG 包生成失败');
            return;
        }
        var blob = await resp.blob();
        var url = URL.createObjectURL(blob);
        dlEl.href = url;
        dlEl.download = styleName + '_dng_pack.zip';

        dlEl.onclick = function(e) {
            setTimeout(function() {
                showToast('请在 Lightroom 中打开 DNG 并导出 JPG，然后拖回本页面上传', 5000);
            }, 500);
        };
    } catch(err) {
        showToast('DNG 包生成出错: ' + err.message);
    }
}

function resetLrUpgradeHint() {
    var section = $('#lr-hifi-section');
    var hint = $('#lr-upgrade-hint');
    var manualArea = $('#lr-manual-area');
    if (section) section.style.display = 'none';
    if (hint) hint.style.display = 'none';
    if (manualArea) manualArea.style.display = 'none';
    _lrXmpFile = null;
}

/* ---------- LUT merge ---------- */
async function mergeAndUpdateCanvas() {
    const img = getCurrentImage();
    if (!img) return;

    const formData = new FormData();
    if (_lastSessionId) formData.append('ai_session_id', _lastSessionId);
    if (_profileBuiltin) formData.append('profile_builtin', _profileBuiltin);
    else if (_profileSessionId) formData.append('profile_session_id', _profileSessionId);
    if (img.sourcePath) formData.append('target_path', img.sourcePath);

    try {
        const resp = await fetch(`${API_BASE}/api/merge_luts`, { method: 'POST', body: formData });
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            console.error('LUT merge failed:', d.detail || resp.status);
            return;
        }
        const data = await resp.json();
        _mergedSessionId = data.merged_session_id;
        img.mergedSessionId = data.merged_session_id;

        if (data.result_b64) {
            _resultCanvasDataUrl = data.result_b64;
            $('#canvas-result').src = data.result_b64;
            img.resultDataUrl = data.result_b64;

            if (_originalImageData) {
                try {
                    const sd = await base64ToImageData(data.result_b64);
                    _stylizedImageData = sd;
                    requestWorkerAdjust();
                } catch(e) {}
            }
        }

        saveCurrentState();
        updateBatchApplyButton();
        $('#adjust-sliders').hidden = !(_lastSessionId || _profileSessionId);
        updateExportButton();
    } catch(err) {
        console.error('Merge error:', err.message);
    }
}

/* ---------- Batch Apply Style ---------- */
function openBatchModal() {
    const current = getCurrentImage();
    if (!current) return;

    if (!current.sessionId && !current.profileId) {
        showToast('当前图片没有任何调色效果'); return;
    }

    saveCurrentState();

    var targets = getSelectedImages().filter(function(img) {
        return img.id !== current.id;
    });
    if (targets.length === 0) {
        showToast('没有其他已选中的图片可应用'); return;
    }

    const hasAI = !!(current.sessionId || current.profileId);
    const hasAdjust = true;

    const modal = $('#batch-modal');
    const targetCount = $('#batch-target-count');
    const optAI = $('#batch-opt-ai');
    const optAdjust = $('#batch-opt-adjust');
    const labelAI = $('#batch-opt-ai-label');
    const confirmBtn = $('#batch-modal-confirm');
    const cancelBtn = $('#batch-modal-cancel');
    const closeBtn = $('#batch-modal-close');

    if (!modal || !targetCount || !optAI || !optAdjust || !labelAI || !confirmBtn || !cancelBtn || !closeBtn) {
        showToast('批量应用弹窗未加载完整');
        return;
    }

    targetCount.textContent = targets.length;

    optAI.checked = hasAI;
    optAdjust.checked = true;

    if (!hasAI) {
        optAI.disabled = true;
        labelAI.style.opacity = '0.45';
        labelAI.title = '当前图片未进行 AI 追色';
    } else {
        optAI.disabled = false;
        labelAI.style.opacity = '1';
        labelAI.title = '';
    }

    function onCheckChange() {
        confirmBtn.disabled = !optAI.checked && !optAdjust.checked;
    }

    optAI.onchange = onCheckChange;
    optAdjust.onchange = onCheckChange;
    onCheckChange();

    modal.hidden = false;

    const doConfirm = async () => {
        modal.hidden = true;

        const doAI = optAI.checked && hasAI;
        const doAdjust = optAdjust.checked;

        const total = targets.length;
        let done = 0;
        let failed = 0;

        const srcParams = current.params || getAdjustParams();
        var progressDiv = $('#batch-progress');
        var progressBar = $('#batch-progress-bar');
        var progressText = $('#batch-progress-text');
        progressDiv.hidden = false;
        progressBar.style.width = '0%';
        progressText.textContent = '批量应用 0/' + total + '...';

        isProcessing = true;
        updateAllButtons();

        for (var idx = 0; idx < targets.length; idx++) {
            var img = targets[idx];
            img.status = 'processing';
            renderGallery();

            try {
                if (doAI) {
                    var formData = new FormData();
                    formData.append('target_path', img.sourcePath);
                    if (current.sessionId) formData.append('ai_session_id', current.sessionId);
                    if (_profileBuiltin) formData.append('profile_builtin', _profileBuiltin);
                    else if (_profileSessionId) formData.append('profile_session_id', _profileSessionId);

                    var resp = await fetch(API_BASE + '/api/merge_luts', { method: 'POST', body: formData });
                    if (!resp.ok) {
                        console.error('Batch merge failed for', img.name);
                        img.status = 'pending';
                        failed++;
                    } else {
                        var data = await resp.json();
                        img.sessionId = current.sessionId;
                        img.profileId = current.profileId;
                        img.mergedSessionId = data.merged_session_id;
                        if (data.result_b64) {
                            img.resultDataUrl = data.result_b64;
                        }
                        img.status = 'done';
                        done++;
                    }
                }

                if (doAdjust) {
                    img.params = {};
                    for (var k in srcParams) { img.params[k] = srcParams[k]; }
                    if (!doAI && img.sessionId) img.status = 'done';
                    else if (!doAI) done++;
                }

                if (doAI && doAdjust) img.status = 'done';
            } catch(err) {
                console.error('Batch error:', img.name, err.message);
                img.status = 'pending';
                failed++;
            }

            var pct = Math.round(((idx + 1) / total) * 100);
            progressBar.style.width = pct + '%';
            progressText.textContent = '批量应用 ' + (idx + 1) + '/' + total + '...';
        }

        isProcessing = false;
        renderGallery();
        updateAllButtons();

        setTimeout(function() { progressDiv.hidden = true; }, 2000);

        if (failed > 0) {
            showToast('批量应用完成: ' + done + ' 成功, ' + failed + ' 失败');
        } else {
            showToast('已完成 ' + done + '/' + total + ' 张图片');
        }

        confirmBtn.removeEventListener('click', doConfirm);
    };

    const doCancel = () => {
        modal.hidden = true;
        confirmBtn.removeEventListener('click', doConfirm);
    };

    confirmBtn.addEventListener('click', doConfirm);
    cancelBtn.onclick = doCancel;
    closeBtn.onclick = doCancel;
}

async function batchApplyStyle() {
    openBatchModal();
}

/* ---------- Adjust tab (Worker) ---------- */
const ADJUST_PARAMS = [
    { slider: 'intensity-slider', num: 'intensity-num', api: 'intensity', def: 100 },
    { slider: 'exposure-slider',    num: 'exposure-num',    api: 'exposure',    def: 100 },
    { slider: 'contrast-slider',    num: 'contrast-num',    api: 'contrast',    def: 100 },
    { slider: 'highlight-slider',   num: 'highlight-num',   api: 'highlight',   def: 100 },
    { slider: 'shadow-slider',      num: 'shadow-num',      api: 'shadow',      def: 100 },
    { slider: 'vibrance-slider',    num: 'vibrance-num',    api: 'vibrance',    def: 100 },
];

function resetAdjustSliders() {
    ADJUST_PARAMS.forEach(p => {
        $(`#${p.slider}`).value = p.def;
        $(`#${p.num}`).value = p.def;
    });
}

function getAdjustValue(p) {
    const nv = parseFloat($(`#${p.num}`).value);
    if (!isNaN(nv) && nv >= 0 && nv <= 200) return nv;
    return parseInt($(`#${p.slider}`).value) || p.def;
}

function setAdjustValue(p, val) {
    val = Math.max(0, Math.min(200, parseFloat(val) || p.def));
    $(`#${p.num}`).value = val;
    $(`#${p.slider}`).value = Math.round(val);
}

function getAdjustParams() {
    const params = {};
    ADJUST_PARAMS.forEach(p => { params[p.api] = getAdjustValue(p); });
    return params;
}

function base64ToImageData(base64) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => {
            const canvas = document.createElement('canvas');
            canvas.width = img.naturalWidth;
            canvas.height = img.naturalHeight;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0);
            resolve(ctx.getImageData(0, 0, canvas.width, canvas.height));
        };
        img.onerror = reject;
        img.src = base64;
    });
}

let adjustWorker = null, workerBusy = false, pendingWorkerParams = null, canvasConvCtx = null;

function initAdjustWorker() {
    if (adjustWorker) return;
    adjustWorker = new Worker('/static/js/adjust_worker.js');
    adjustWorker.onerror = (e) => { console.error('Worker err:', e.message); workerBusy = false; };
    adjustWorker.onmessage = (e) => {
        workerBusy = false;
        if (e.data && e.data.error) { console.error('Worker:', e.data.error); }
        if (e.data && e.data.buffer) {
            try {
                const w = _stylizedImageData ? _stylizedImageData.width : 0;
                const h = _stylizedImageData ? _stylizedImageData.height : 0;
                if (w > 0 && h > 0) {
                    const nd = new ImageData(new Uint8ClampedArray(e.data.buffer), w, h);
                    canvasConvCtx.putImageData(nd, 0, 0);
                    _resultCanvasDataUrl = canvasConvCtx.canvas.toDataURL('image/jpeg', 0.92);
                    $('#canvas-result').src = _resultCanvasDataUrl;
                    const img = getCurrentImage();
                    if (img) {
                        img.params = getAdjustParams();
                    }
                }
            } catch(err) { console.error('Render failed:', err); }
        }
        if (pendingWorkerParams) {
            const pp = pendingWorkerParams; pendingWorkerParams = null;
            workerBusy = true;
            ensureCanvasContext();
            canvasConvCtx.canvas.width = pp.w;
            canvasConvCtx.canvas.height = pp.h;
            adjustWorker.postMessage({ originalData: pp.od, stylizedData: pp.sd, params: pp.params }, [pp.od.buffer, pp.sd.buffer]);
        }
    };
}

function ensureCanvasContext() {
    if (!canvasConvCtx) { const c = document.createElement('canvas'); canvasConvCtx = c.getContext('2d'); }
}

function requestWorkerAdjust() {
    if (!adjustWorker || !_originalImageData || !_stylizedImageData) return;
    const params = getAdjustParams();
    const od = Uint8ClampedArray.from(_originalImageData.data);
    const sd = Uint8ClampedArray.from(_stylizedImageData.data);
    const w = _originalImageData.width;
    const h = _originalImageData.height;

    if (workerBusy) { pendingWorkerParams = { od, sd, params, w, h }; return; }
    workerBusy = true;
    ensureCanvasContext();
    canvasConvCtx.canvas.width = w;
    canvasConvCtx.canvas.height = h;
    adjustWorker.postMessage({ originalData: od, stylizedData: sd, params }, [od.buffer, sd.buffer]);
}

function onAdjustSliderChange() {
    ADJUST_PARAMS.forEach(p => { $(`#${p.num}`).value = Math.round(parseInt($(`#${p.slider}`).value)); });
    requestWorkerAdjust();
}

function onAdjustNumChange() {
    ADJUST_PARAMS.forEach(p => {
        const val = parseFloat($(`#${p.num}`).value);
        if (!isNaN(val)) $(`#${p.slider}`).value = Math.round(Math.max(0, Math.min(200, val)));
    });
    requestWorkerAdjust();
}

/* ---------- Export Settings & localStorage ---------- */
const EXPORT_STORAGE_KEY = 'colorchase_export_settings';
const EXPORT_FOLDER_KEY = 'colorchase_export_folder';

let exportFolderHandle = null;
let exportFolderName = '';

function defaultExportSettings() {
    return {
        template: 'web',
        org: 'flat',
        naming: 'original', namingCustom: '{name}_{style}',
        seqDigits: 2,
        format: 'jpg', quality: 95,
        colorspace: 'srgb', bitdepth: '8',
        size: 'full', sizeCustom: 3000,
        after: 'open_folder',
    };
}

function getExportSettings() {
    try {
        const raw = localStorage.getItem(EXPORT_STORAGE_KEY);
        if (raw) return { ...defaultExportSettings(), ...JSON.parse(raw) };
    } catch {}
    return defaultExportSettings();
}

function saveExportSettings(s) {
    try { localStorage.setItem(EXPORT_STORAGE_KEY, JSON.stringify(s)); } catch {}
}

function loadExportFolder() {
    try {
        const raw = localStorage.getItem(EXPORT_FOLDER_KEY);
        if (raw) { const d = JSON.parse(raw); exportFolderName = d.name || ''; }
    } catch {}
}

loadExportFolder();

/* ---------- Export helpers ---------- */
function getStyleName(img) {
    if (img.profileId && STYLE_TAGS[img.profileId]) return STYLE_TAGS[img.profileId];
    if (img.profileId) return img.profileId;
    if (img.sessionId && img.aiAlgo && STYLE_TAGS[img.aiAlgo]) return STYLE_TAGS[img.aiAlgo];
    if (img.sessionId) return 'AI';
    return 'Original';
}

function dateStamp() {
    const n = new Date();
    return `${n.getFullYear()}${String(n.getMonth()+1).padStart(2,'0')}${String(n.getDate()).padStart(2,'0')}`;
}

function generateFileName(img, index, format) {
    const s = getExportSettings();
    const baseName = img.name.replace(/\.[^.]+$/, '');
    const ext = format === 'both' ? 'jpg' : format;
    const styleName = getStyleName(img);
    const ds = dateStamp();
    const seq = String(index + 1).padStart(s.seqDigits, '0');

    let pattern;
    switch (s.naming) {
        case 'original_style': pattern = '{name}_{style}'; break;
        case 'date_original': pattern = '{date}_{name}'; break;
        case 'date_original_style': pattern = '{date}_{name}_{style}'; break;
        case 'custom': pattern = s.namingCustom || '{name}'; break;
        default: pattern = '{name}';
    }

    let name = pattern
        .replace(/\{date\}/g, ds)
        .replace(/\{name\}/g, baseName)
        .replace(/\{原文件名\}/g, baseName)
        .replace(/\{style\}/g, styleName)
        .replace(/\{风格\}/g, styleName)
        .replace(/\{seq\}/g, seq)
        .replace(/\{序号\}/g, seq);

    name = name.replace(/[\/:*?"<>|]/g, '_').replace(/\s+/g, '_');

    if (!name.includes(seq) && s.naming === 'original') {
        name = name + '_' + seq;
    }

    return name + '.' + ext;
}

function getOrganizationPath(img) {
    const s = getExportSettings();
    const parts = [];
    if (s.org === 'date' || s.org === 'date_style') {
        parts.push(`ColorChase_${dateStamp()}`);
    }
    if (s.org === 'style' || s.org === 'date_style') {
        parts.push(getStyleName(img));
    }
    return parts;
}

function updateExportPreview() {
    const img = getCurrentImage();
    if (!img) return;

    const s = getExportSettings();
    let sampleName = generateFileName(img, 0, s.format === 'both' ? 'jpg' : s.format);
    if (sampleName.length > 30) {
        sampleName = sampleName.substring(0, 12) + '...' + sampleName.substring(sampleName.length - 15);
    }
    $('#export-naming-preview-value').textContent = sampleName;

    const orgPath = getOrganizationPath(img);
    let fullPath = exportFolderName || '未选择';
    if (orgPath.length) fullPath += ' / ' + orgPath.join(' / ');
    $('.preview-path').textContent = fullPath;

    const pixCount = estimatePixelCount();
    var estBytes = 0;
    var bit16 = s.bitdepth === '16';

    if (s.format === 'jpg') {
        estBytes = pixCount * 3 * (0.03 + (s.quality / 100) * 0.12);
    } else if (s.format === 'png') {
        estBytes = pixCount * (bit16 ? 5.5 : 2.5);
    } else if (s.format === 'both') {
        var jpgBytes = pixCount * 3 * (0.03 + (s.quality / 100) * 0.12);
        var pngBytes = pixCount * (bit16 ? 5.5 : 2.5);
        estBytes = jpgBytes + pngBytes;
    }

    var sizeText;
    if (estBytes < 1024) {
        sizeText = '~' + estBytes.toFixed(0) + ' B';
    } else if (estBytes < 1048576) {
        sizeText = '~' + (estBytes / 1024).toFixed(0) + ' KB';
    } else {
        sizeText = '~' + (estBytes / 1048576).toFixed(1) + ' MB';
    }
    $('#export-size-est').textContent = sizeText;

    $('#export-quality-val').textContent = s.quality;
    $('#export-naming-custom-row').hidden = s.naming !== 'custom';
    $('#export-quality-row').hidden = (s.format === 'png');
    $('#export-size-custom-row').hidden = s.size !== 'custom';
}

function estimatePixelCount() {
    const img = getCurrentImage();
    if (!img) return 8000000;
    const s = getExportSettings();
    const meta = img.meta || '';
    const m = meta.match(/(\d+)[x×](\d+)/i);
    var w = 4000, h = 3000;
    if (m) { w = parseInt(m[1]); h = parseInt(m[2]); }

    if (s.size === '2x') { w *= 2; h *= 2; }
    else if (s.size === 'half') { w = Math.floor(w / 2); h = Math.floor(h / 2); }
    else if (s.size === 'custom' && s.sizeCustom > 0) {
        var longEdge = Math.max(w, h);
        if (longEdge > s.sizeCustom) {
            var scale = s.sizeCustom / longEdge;
            w = Math.round(w * scale);
            h = Math.round(h * scale);
        }
    }

    return w * h;
}

/* ---------- Template system ---------- */
function applyExportTemplate(template) {
    const s = getExportSettings();
    switch (template) {
        case 'web':
            s.format = 'jpg'; s.quality = 95; s.colorspace = 'srgb';
            s.bitdepth = '8'; s.size = 'full';
            break;
        case 'print':
            s.format = 'png'; s.colorspace = 'adobergb';
            s.bitdepth = '16'; s.size = 'full';
            break;
        case 'social':
            s.format = 'jpg'; s.quality = 85; s.colorspace = 'srgb';
            s.bitdepth = '8'; s.size = 'custom'; s.sizeCustom = 2000;
            break;
    }
    s.template = template;
    applySettingsToUI(s);
    saveExportSettings(s);
    updateExportPreview();
}

function applySettingsToUI(s) {
    setVal('#export-template', s.template);
    setVal('#export-org', s.org);
    setVal('#export-naming', s.naming);
    setVal('#export-naming-custom', s.namingCustom);
    setVal('#export-seq-digits', String(s.seqDigits));
    setVal('#export-format', s.format);
    setVal('#export-quality', s.quality);
    setVal('#export-colorspace', s.colorspace);
    setVal('#export-bitdepth', s.bitdepth);
    setVal('#export-size', s.size);
    setVal('#export-size-custom', s.sizeCustom);
    setVal('#export-after', s.after);
    $('#export-quality-val').textContent = s.quality;
    $('#export-naming-custom-row').hidden = s.naming !== 'custom';
    $('#export-quality-row').hidden = s.format === 'png';
    $('#export-size-custom-row').hidden = s.size !== 'custom';
}

function setVal(sel, val) {
    const el = $(sel);
    if (!el) return;
    if (el.type === 'range') { el.value = val; return; }
    el.value = val;
}

/* ---------- Read settings from UI ---------- */
function readExportSettingsFromUI() {
    const s = getExportSettings();
    s.template = 'custom';
    s.org = $('#export-org').value;
    s.naming = $('#export-naming').value;
    s.namingCustom = $('#export-naming-custom').value || '{原文件名}';
    s.seqDigits = parseInt($('#export-seq-digits').value) || 2;
    s.format = $('#export-format').value;
    s.quality = parseInt($('#export-quality').value) || 95;
    s.colorspace = $('#export-colorspace').value;
    s.bitdepth = $('#export-bitdepth').value;
    s.size = $('#export-size').value;
    s.sizeCustom = parseInt($('#export-size-custom').value) || 3000;
    s.after = $('#export-after').value;
    s.template = $('#export-template').value === s.template ? s.template : 'custom';
    return s;
}

/* ---------- Download to Folder ---------- */
async function renderSingleImageBlob(img, format, sizeMode, sizeCustomVal) {
    const s = getExportSettings();
    const fmt = format || s.format === 'both' ? 'jpg' : s.format;

    const formData = new FormData();
    formData.append('target_path', img.sourcePath);
    formData.append('session_id', img.sessionId || '');
    formData.append('format', fmt === 'both' ? 'jpg' : fmt);
    formData.append('project_id', String(window.currentProjectId || 0));
    formData.append('asset_name', img.name || '');
    formData.append('rating', String(img.rating || 0));
    formData.append('algorithm', img.aiAlgo || $('#ai-algorithm-select').value || '');
    formData.append('reference_path', img.refSavedPath || window._refSavedPath || '');
    if (!(img.refSavedPath || window._refSavedPath)) {
        formData.append('reference_data_url', img.refDataUrl || _refDataUrl || '');
    }
    if (sizeMode) formData.append('size_mode', sizeMode);
    if (sizeMode === 'custom' && sizeCustomVal) formData.append('custom_long_edge', String(sizeCustomVal));
    if (img.mergedSessionId) formData.append('merged_session_id', img.mergedSessionId);
    if (img.params) {
        formData.append('intensity', img.params.intensity);
        formData.append('exposure', img.params.exposure);
        formData.append('contrast', img.params.contrast);
        formData.append('highlight', img.params.highlight);
        formData.append('shadow', img.params.shadow);
        formData.append('vibrance', img.params.vibrance);
    }
    if (s.format === 'both') formData.append('export_both', '1');

    const resp = await fetch(`${API_BASE}/api/render_single`, { method: 'POST', body: formData, headers: getAuthHeaders() });
    if (!resp.ok) {
        var errData = await resp.json().catch(function() { return {}; });
        var detail = errData.detail;
        if (Array.isArray(detail)) {
            detail = detail.map(function(d) { return d.msg || JSON.stringify(d); }).join('; ');
        }
        throw new Error(detail || '渲染失败(' + resp.status + ')');
    }
    return resp.blob();
}

async function downloadToFolder() {
    var checked = getSelectedImages().filter(function(img) { return hasImageResult(img); });
    if (checked.length === 0) { showToast('没有可下载的图片'); return; }
    if (!checkRatingsBeforeExport(checked)) return;

    const s = readExportSettingsFromUI();
    saveExportSettings(s);

    try {
        const dirHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
        exportFolderHandle = dirHandle;
        exportFolderName = dirHandle.name;
        try { localStorage.setItem(EXPORT_FOLDER_KEY, JSON.stringify({ name: dirHandle.name })); } catch {}
        $('#export-folder-path').textContent = dirHandle.name;
    } catch(e) {
        if (e.name === 'AbortError') return;
    }

    const progressDiv = $('#batch-progress');
    const progressBar = $('#batch-progress-bar');
    const progressText = $('#batch-progress-text');
    const currentFileEl = $('#export-current-file');
    const currentFileName = $('#export-current-file-name');
    progressDiv.hidden = false;
    currentFileEl.hidden = false;

    let completed = 0, total = checked.length * (s.format === 'both' ? 2 : 1);
    const sizeMode = s.size === 'custom' ? 'custom' : s.size;
    const lastFilePaths = [];
    let exportedBytes = 0;

    function updateProgress() {
        const pct = Math.round((completed / total) * 100);
        progressBar.style.width = pct + '%';
        progressText.textContent = `正在导出 ${completed}/${total}...`;
        $('#export-btn').textContent = `\u23F3 \u5BFC\u51FA\u4E2D ${completed}/${total}...`;
    }
    updateProgress();

    isProcessing = true; updateExportButton();

    for (let i = 0; i < checked.length; i++) {
        const img = checked[i];
        const orgParts = getOrganizationPath(img);
        const formats = s.format === 'both' ? ['jpg', 'png'] : [s.format];

        let subDir = exportFolderHandle;
        for (const part of orgParts) {
            subDir = await subDir.getDirectoryHandle(part, { create: true });
        }

        for (const fmt of formats) {
            const fileName = generateFileName(img, i, fmt);
            currentFileName.textContent = fileName;
            currentFileEl.hidden = false;

            let suffix = 0, finalName = fileName;
            const base = fileName.replace(/\.[^.]+$/, '');
            const ext = '.' + fmt;
            while (true) {
                try { await subDir.getFileHandle(finalName); suffix++; finalName = base + '_' + suffix + ext; }
                catch { break; }
            }

        const blob = await renderSingleImageBlob(img, fmt, sizeMode, s.sizeCustom);
        exportedBytes += blob.size || 0;
        const fh = await subDir.getFileHandle(finalName, { create: true });
        const wr = await fh.createWritable();
        await wr.write(blob);
        await wr.close();
        if (browserProjectRootHandle) {
            await writeBrowserProjectResult(finalName, blob);
        }

        if (i === checked.length - 1) lastFilePaths.push(finalName);
        completed++;
        updateProgress();
        }

        if (window.currentProjectId) {
            saveLocalProjectSnapshot();
        }
    }

    isProcessing = false;
    currentFileEl.hidden = true;
    updateExportButton();
    saveExportSettings(s);
    reportFrontendExportMetric({
        fileCount: total,
        totalBytes: exportedBytes,
        exportFormat: s.format,
        sizeMode: sizeMode,
        fileName: lastFilePaths.length ? lastFilePaths[lastFilePaths.length - 1] : '',
        sourceImageKey: checked.map(function(img) { return img.sourcePath || img.savedPath || img.id || ''; }).filter(Boolean).join('|')
    });

    progressText.textContent = `${checked.length} 张图片已保存`;

    const orgParts = getOrganizationPath(checked[checked.length - 1]);
    const folderDisplay = exportFolderName + (orgParts.length ? ' / ' + orgParts.join(' / ') : '');
    const toastDiv = document.createElement('div');
    toastDiv.style.cssText = 'text-align:center;line-height:1.5';
    toastDiv.innerHTML = `\u2705 \u5DF2\u4FDD\u5B58 ${checked.length} \u5F20\u56FE\u7247\u5230<br>`;
    const pathSpan = document.createElement('span');
    pathSpan.style.cssText = 'color:#60a5fa;cursor:pointer;text-decoration:underline;font-size:11px';
    pathSpan.textContent = folderDisplay;
    pathSpan.title = '\u70B9\u51FB\u590D\u5236\u8DEF\u5F84';
    pathSpan.addEventListener('click', async (e) => {
        e.stopPropagation();
        try {
            await navigator.clipboard.writeText(folderDisplay);
            pathSpan.textContent = '\u5DF2\u590D\u5236!';
            setTimeout(() => { pathSpan.textContent = folderDisplay; }, 1500);
        } catch {
            const ta = document.createElement('textarea');
            ta.value = folderDisplay; ta.style.position = 'fixed'; ta.style.opacity = '0';
            document.body.appendChild(ta); ta.select();
            document.execCommand('copy'); document.body.removeChild(ta);
            pathSpan.textContent = '\u5DF2\u590D\u5236!';
            setTimeout(() => { pathSpan.textContent = folderDisplay; }, 1500);
        }
    });
    toastDiv.appendChild(pathSpan);
    showToast(toastDiv, 5000);

    if (s.after === 'open_folder' && exportFolderHandle) {
        setTimeout(() => { progressDiv.hidden = true; currentFileEl.hidden = true; }, 3000);
    } else {
        setTimeout(() => { progressDiv.hidden = true; currentFileEl.hidden = true; }, 3000);
    }
}

function reportFrontendExportMetric(payload) {
    var token = localStorage.getItem('cc_token');
    if (!token) return Promise.resolve();
    var body = {
        file_count: Math.max(parseInt(payload.fileCount || 0, 10) || 0, 0),
        total_bytes: Math.max(parseInt(payload.totalBytes || 0, 10) || 0, 0),
        export_format: payload.exportFormat || '',
        size_mode: payload.sizeMode || '',
        project_id: window.currentProjectId || 0,
        file_name: payload.fileName || '',
        source_image_key: payload.sourceImageKey || ''
    };
    if (body.file_count <= 0 && body.total_bytes <= 0) return Promise.resolve();
    return fetch('/api/projects/record_export_metric', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, (typeof getAuthHeaders === 'function' ? getAuthHeaders() : {})),
        body: JSON.stringify(body)
    }).catch(function() { return null; });
}

/* ---------- ref upload ---------- */
function setupRefUpload() {
    const area = $('#ref-upload-area');
    const input = $('#ref-input');
    const preview = $('#ref-preview');
    const placeholder = $('#ref-placeholder');
    const clearBtn = $('#ref-clear');

    area.addEventListener('click', () => input.click());
    area.addEventListener('dragover', (e) => { e.preventDefault(); area.style.borderColor = 'var(--accent)'; });
    area.addEventListener('dragleave', () => area.style.borderColor = '');
    area.addEventListener('drop', (e) => {
        e.preventDefault(); area.style.borderColor = '';
        const f = e.dataTransfer.files[0];
        if (f) loadRefFile(f);
    });
    input.addEventListener('change', (e) => {
        if (e.target.files[0]) loadRefFile(e.target.files[0]);
    });
    clearBtn.addEventListener('click', () => {
        refFile = null; refDataUrl = null;
        _refDataUrl = null;
        window._refSavedPath = '';
        const currentImg = getCurrentImage();
        if (currentImg) {
            currentImg.refDataUrl = null;
            currentImg.refSavedPath = '';
        }
        $('#canvas-reference').src = '';
        preview.hidden = true; placeholder.hidden = false; clearBtn.hidden = true;
        input.value = '';
        updateAllButtons();
    });

    function loadRefFile(file) {
        refFile = file;
        const reader = new FileReader();
        reader.onload = (e) => {
            preview.src = e.target.result;
            refDataUrl = e.target.result;
            _refDataUrl = e.target.result;
            window._refSavedPath = '';
            const currentImg = getCurrentImage();
            if (currentImg) {
                currentImg.refDataUrl = e.target.result;
                currentImg.refSavedPath = '';
            }
            $('#canvas-reference').src = e.target.result;
            preview.hidden = false; placeholder.hidden = true; clearBtn.hidden = false;
            if (window.currentProjectId) {
                saveFileToProject(file, 'reference', file.name).then(function(savedPath) {
                    if (!savedPath) return;
                    window._refSavedPath = savedPath;
                    var activeImg = getCurrentImage();
                    if (activeImg) activeImg.refSavedPath = savedPath;
                    saveLocalProjectSnapshot();
                }).catch(function() {});
            }
            updateAllButtons();
        };
        reader.readAsDataURL(file);
    }
}

/* ---------- canvas drag-drop import ---------- */
function setupCanvasDragDrop() {
    var dragCounter = 0;
    var overlay = document.createElement('div');
    overlay.className = 'global-drop-overlay';
    overlay.innerHTML = "<div class='global-drop-icon'>📥</div><div class='global-drop-text'>松开导入图片</div><div class='global-drop-subtext'>支持 JPG / PNG / WEBP / RAW 等常见格式</div>";
    document.body.appendChild(overlay);

    document.addEventListener('dragenter', function(e) {
        e.preventDefault();
        if (e.dataTransfer && e.dataTransfer.types && e.dataTransfer.types.indexOf('Files') === -1) return;
        dragCounter++;
        if (dragCounter === 1) {
            overlay.classList.add('visible');
        }
    });

    document.addEventListener('dragleave', function(e) {
        e.preventDefault();
        dragCounter--;
        if (dragCounter <= 0) {
            dragCounter = 0;
            overlay.classList.remove('visible');
        }
    });

    document.addEventListener('dragover', function(e) {
        e.preventDefault();
        if (_galleryDragImageId) {
            var dropzone = _galleryDeleteDropzone;
            var overTrash = false;
            if (dropzone && dropzone.classList.contains('visible')) {
                var rect = dropzone.getBoundingClientRect();
                overTrash = e.clientX >= rect.left && e.clientX <= rect.right && e.clientY >= rect.top && e.clientY <= rect.bottom;
                dropzone.classList.toggle('drag-over', overTrash);
            }
            _galleryDeleteHover = overTrash;
            if (e.dataTransfer) e.dataTransfer.dropEffect = overTrash ? 'move' : 'none';
            return;
        }
        if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    });

    document.addEventListener('drop', function(e) {
        e.preventDefault();
        dragCounter = 0;
        overlay.classList.remove('visible');
        if (_galleryDragImageId || _galleryPendingDeleteImageId) return;
        if (!e.dataTransfer || !e.dataTransfer.files || e.dataTransfer.files.length === 0) return;
        var files = [];
        for (var i = 0; i < e.dataTransfer.files.length; i++) {
            var f = e.dataTransfer.files[i];
            var ext = f.name.split('.').pop().toLowerCase();
            if (['jpg','jpeg','png','bmp','tiff','tif','webp','dng','cr2','cr3','crw','nef','nrw','arw','srf','sr2','raf','rw2','raw','rwl','orf','pef','ptx','3fr','fff','iiq','cap','eip','mef','mos','mfw','x3f','dcr','kdc','k25','dcs','srw','erf','cs1','cs4','cs16','sti','bay','pxn','braw','r3d','ari','cine','lfp','rwz'].indexOf(ext) !== -1) {
                files.push(f);
            }
        }
        if (files.length > 0) {
            importBatchFiles(files);
        } else {
            showToast('不支持的文件格式');
        }
    });
}

/* ---------- sidebar toggle ---------- */
function setupSidebarToggle() {
    const btn = $('#sidebar-toggle');
    const sidebar = $('#left-sidebar');
    btn.addEventListener('click', () => {
        sidebar.classList.toggle('collapsed');
        btn.textContent = sidebar.classList.contains('collapsed') ? '▶' : '◀';
    });
}

/* ---------- init ---------- */
function init() {
    activeTab = 'ai';
    currentViewMode = 'single';

    $$('.tab-btn-s').forEach(btn => {
        btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    document.addEventListener('click', function(e) {
        var header = e.target && e.target.closest ? e.target.closest('.fold-header') : null;
        if (!header) return;
        var fold = header.closest('.fold-panel');
        if (!fold) return;
        if (e.target.closest && e.target.closest('button, input, select, textarea, label, a')) return;
        fold.classList.toggle('collapsed');
    });

    setupRefUpload();
    setupCanvasDragDrop();
    setupSidebarToggle();
    initAdjustWorker();
    setupPressCompare();
    setupDividerDrag();
    setupZoom();

    $$('.view-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            if (btn.disabled) return;
            const mode = btn.dataset.view;
            _dividerPos = 50;
            setViewMode(mode);
        });
    });

    $('#batch-import-btn').addEventListener('click', () => $('#batch-file-input').click());
    $('#batch-file-input').addEventListener('change', (e) => importBatchFiles(e.target.files));

    $('#toggle-select-btn').addEventListener('click', toggleSelectAll);

    $('#batch-star-rating').addEventListener('click', function(e) {
        if (e.target.classList.contains('star')) {
            var r = parseInt(e.target.dataset.star || 0);
            if (r > 0) setRating(currentTargetIndex, r);
        }
    });

    var ratingInterceptBtn = document.getElementById('rating-intercept-confirm-btn');
    if (ratingInterceptBtn) {
        ratingInterceptBtn.addEventListener('click', function() {
            var modal = document.getElementById('rating-intercept-modal');
            if (modal) modal.style.display = 'none';
        });
    }

    $('#gallery-list').addEventListener('click', function(e) {
        if (e.target === this || e.target.classList.contains('gallery-empty') || e.target.parentElement === this || (e.target.closest && !e.target.closest('.gallery-item'))) {
            deselectAll();
        }
    });

    $('#ai-transfer-btn').addEventListener('click', doAITransfer);
    $('#ai-algorithm-select').addEventListener('change', updateAlgoInfo);
    $('#ai-blend-slider').addEventListener('input', (e) => { $('#ai-blend-value').textContent = e.target.value; });
    if ($('#ai-mask-generate')) $('#ai-mask-generate').addEventListener('click', generateSubjectMask);
    if ($('#ai-mask-clear-points')) $('#ai-mask-clear-points').addEventListener('click', function() { clearSubjectMask({ clearPoints: true }); });
    if ($('#ai-mask-enabled')) $('#ai-mask-enabled').addEventListener('change', updateMaskUI);
    if ($('#ai-mask-preview-toggle')) $('#ai-mask-preview-toggle').addEventListener('change', updateMaskUI);
    if ($('#ai-mask-model')) $('#ai-mask-model').addEventListener('change', function() { clearSubjectMask({ clearPoints: false }); });
    if ($('#ai-mask-mode')) $('#ai-mask-mode').addEventListener('change', function(e) {
        _subjectMaskMode = e.target.value;
        clearSubjectMask({ clearPoints: false });
    });
    if ($('#ai-mask-strength')) $('#ai-mask-strength').addEventListener('input', function(e) {
        $('#ai-mask-strength-value').textContent = e.target.value;
    });
    if ($('#canvas-area')) $('#canvas-area').addEventListener('click', handleMaskCanvasClick);
    if ($('#ai-depth-generate')) $('#ai-depth-generate').addEventListener('click', generateDepthLayers);
    if ($('#ai-depth-clear')) $('#ai-depth-clear').addEventListener('click', clearDepthLayers);
    if ($('#ai-depth-enabled')) $('#ai-depth-enabled').addEventListener('change', updateDepthUI);
    if ($('#ai-depth-preview-toggle')) $('#ai-depth-preview-toggle').addEventListener('change', updateDepthUI);
    if ($('#ai-depth-model')) $('#ai-depth-model').addEventListener('change', clearDepthLayers);
    if ($('#ai-depth-strength')) $('#ai-depth-strength').addEventListener('input', function(e) {
        $('#ai-depth-strength-value').textContent = e.target.value;
    });
    if ($('#ai-semantic-analyze')) $('#ai-semantic-analyze').addEventListener('click', analyzeSemanticMatch);
    if ($('#ai-semantic-enabled')) $('#ai-semantic-enabled').addEventListener('change', updateSemanticUI);
    if ($('#ai-semantic-model')) $('#ai-semantic-model').addEventListener('change', function() {
        _semanticMatchMeta = null;
        var img = getCurrentImage();
        if (img) img.semanticMatchMeta = null;
        updateSemanticUI();
        saveSnapshot();
    });
    if ($('#ai-semantic-strength')) $('#ai-semantic-strength').addEventListener('input', function(e) {
        $('#ai-semantic-strength-value').textContent = e.target.value;
    });
    refreshCapabilityModelSelectors(true);
    window.addEventListener('focus', function() { refreshCapabilityModelSelectors(true); });
    setInterval(function() { refreshCapabilityModelSelectors(true); }, 30000);

    $('#profile-select').addEventListener('change', updateProfileStatus);
    $('#profile-import-btn').addEventListener('click', importProfileFile);
    $('#profile-file-input').addEventListener('change', (e) => handleProfileFileInput(e.target.files[0]));
    $('#apply-profile-btn').addEventListener('click', applyProfile);

    $('#lr-xmp-import-btn').addEventListener('click', function() { $('#lr-xmp-input').click(); });
    $('#lr-xmp-input').addEventListener('change', function(e) {
        var f = e.target.files[0];
        if (!f) return;
        _lrXmpFile = f;
        var section = $('#lr-hifi-section');
        if (section) section.style.display = '';
        var hint = $('#lr-upgrade-hint');
        if (hint) hint.style.display = 'none';
        var manualArea = $('#lr-manual-area');
        if (manualArea) manualArea.style.display = 'flex';
        var dlBtn = $('#lr-download-btn');
        if (dlBtn) dlBtn.disabled = false;
        var upBtn = $('#lr-upload-jpg-btn');
        if (upBtn) upBtn.disabled = true;
        showToast('已导入 XMP: ' + f.name);
    });
    $('#lr-download-btn').addEventListener('click', async function() {
        if (!_lrXmpFile) { showToast('请先导入 XMP 文件'); return; }
        var styleName = _lrXmpFile.name.replace(/\.xmp$/i, '');
        try {
            var fd = new FormData();
            fd.append('xmp_file', _lrXmpFile);
            fd.append('style_name', styleName);
            var resp = await fetch(API_BASE + '/api/prepare_lr_preset', { method: 'POST', body: fd });
            if (!resp.ok) { showToast('DNG 包生成失败'); return; }
            var blob = await resp.blob();
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            a.download = styleName + '_dng_pack.zip';
            a.click();
            URL.revokeObjectURL(url);
            var uploadBtn = $('#lr-upload-jpg-btn');
            if (uploadBtn) uploadBtn.disabled = false;
            showToast('DNG 包已下载，请在 Lightroom 中打开 DNG 并导出 JPG，然后上传');
        } catch(err) {
            showToast('DNG 包生成出错: ' + err.message);
        }
    });
    $('#lr-upload-jpg-btn').addEventListener('click', function() { $('#lr-jpg-input').click(); });
    $('#lr-jpg-input').addEventListener('change', function(e) {
        var f = e.target.files[0];
        if (!f) return;
        showToast('已收到 Lightroom 导出的 JPG，正在处理...');
    });

    var captureRawFile = null;
    var captureJpgFile = null;
    var vCaptureRawFile = null;
    var vCaptureJpgFile = null;
    $('#capture-raw-btn').addEventListener('click', function() { $('#capture-raw-input').click(); });
    $('#capture-jpg-btn').addEventListener('click', function() { $('#capture-jpg-input').click(); });
    $('#capture-raw-input').addEventListener('change', function(e) {
        captureRawFile = e.target.files[0] || null;
        if (captureRawFile) {
            $('#capture-status-text').textContent = 'RAW: ' + captureRawFile.name + (captureJpgFile ? ' | JPG: ' + captureJpgFile.name : '');
        }
        $('#btn-capture-style').disabled = !(captureRawFile && captureJpgFile);
    });
    $('#capture-jpg-input').addEventListener('change', function(e) {
        captureJpgFile = e.target.files[0] || null;
        if (captureJpgFile) {
            $('#capture-status-text').textContent = (captureRawFile ? 'RAW: ' + captureRawFile.name + ' | ' : '') + 'JPG: ' + captureJpgFile.name;
        }
        $('#btn-capture-style').disabled = !(captureRawFile && captureJpgFile);
    });
    $('#btn-capture-style').addEventListener('click', async function() {
        if (!captureRawFile || !captureJpgFile) return;
        var btn = $('#btn-capture-style');
        btn.disabled = true;
        btn.textContent = '⏳ 提取中...';
        $('#capture-status-text').textContent = '正在提取相机风格，请稍候...';
        try {
            var fd = new FormData();
            fd.append('raw_file', captureRawFile);
            fd.append('camera_jpg', captureJpgFile);
            var resp = await fetch('/api/capture_style', { method: 'POST', body: fd });
            var data = await resp.json();
            if (!resp.ok) {
                throw new Error(data.detail || '提取失败');
            }
            $('#capture-status-text').textContent = '✅ 风格提取成功: ' + (data.style.name || '');
            if (data.npy_path) {
                window._capturedStyleLutPath = data.npy_path;
                window._capturedStyleName = data.style.name || '';
                var statusText = $('#profile-status-text');
                statusText.textContent = '已捕获: ' + (data.style.name || '') + ' (' + (data.style.camera || '') + ')';
                $('#apply-profile-btn').disabled = false;
            }
            showToast('相机风格提取成功！');
            loadStyleGallery();
        } catch (err) {
            var msg = err.message || '未知错误';
            if (Array.isArray(msg)) msg = msg.map(function(x) { return x.msg || String(x); }).join('; ');
            $('#capture-status-text').textContent = '❌ ' + msg;
            showToast('风格提取失败: ' + msg);
        } finally {
            btn.disabled = !(captureRawFile && captureJpgFile);
            btn.textContent = '🔍 提取相机风格';
        }
    });
    $$('.batch-apply-btn').forEach(btn => {
        btn.addEventListener('click', batchApplyStyle);
    });

    ADJUST_PARAMS.forEach(function(p) {
        var slider = $('#' + p.slider);
        var num = $('#' + p.num);
        if (slider) {
            slider.addEventListener('input', onAdjustSliderChange);
            slider.addEventListener('dblclick', function() {
                setAdjustValue(p, p.def);
                requestWorkerAdjust();
            });
        }
        if (num) num.addEventListener('input', onAdjustNumChange);
    });

    document.querySelectorAll('.adjust-item label').forEach(label => {
        label.addEventListener('dblclick', () => {
            const idx = parseInt(label.dataset.param);
            if (idx >= 0 && idx < ADJUST_PARAMS.length) {
                setAdjustValue(ADJUST_PARAMS[idx], ADJUST_PARAMS[idx].def);
                requestWorkerAdjust();
            }
        });
    });

    $('#export-folder-btn').addEventListener('click', downloadToFolder);

    var _exportMode = 'quick';
    $('#export-mode-quick').addEventListener('click', function() {
        _exportMode = 'quick';
        $('#export-quick-panel').hidden = false;
        $('#export-custom-panel').hidden = true;
        $('#export-mode-quick').classList.add('is-selected');
        $('#export-mode-custom').classList.remove('is-selected');
    });
    $('#export-mode-custom').addEventListener('click', function() {
        _exportMode = 'custom';
        $('#export-quick-panel').hidden = true;
        $('#export-custom-panel').hidden = false;
        $('#export-mode-custom').classList.add('is-selected');
        $('#export-mode-quick').classList.remove('is-selected');
    });
    $('#export-mode-quick').classList.add('is-selected');
    $('#export-mode-custom').classList.remove('is-selected');

    $('#quick-size').addEventListener('change', function() {
        $('#quick-size-custom-row').hidden = this.value !== 'custom';
    });

    $('#quick-export-btn').addEventListener('click', function() {
        var img = getCurrentImage();
        if (!img || !_resultCanvasDataUrl) return;
        if (!checkRatingsBeforeExport([img])) return;
        var fmt = $('#quick-format').value;
        var sizeVal = $('#quick-size').value;
        var customPx = parseInt($('#quick-size-custom').value) || 3000;
        isProcessing = true; updateAllButtons();
        renderSingleImageBlob(img, fmt, sizeVal, customPx).then(function(blob) {
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url; a.download = generateFileName(img, 0, fmt);
            document.body.appendChild(a); a.click(); document.body.removeChild(a);
            URL.revokeObjectURL(url);
            reportFrontendExportMetric({
                fileCount: 1,
                totalBytes: blob.size || 0,
                exportFormat: fmt,
                sizeMode: sizeVal,
                fileName: a.download || '',
                sourceImageKey: img.sourcePath || img.savedPath || img.id || ''
            });
            showToast('下载完成');
        }).catch(function(err) { showToast('下载失败: ' + err.message); })
        .finally(function() { isProcessing = false; updateAllButtons(); });
    });

    $('#quick-export-folder-btn').addEventListener('click', downloadToFolder);

    $('#export-btn').addEventListener('click', function() {
        const img = getCurrentImage();
        if (!img || !_resultCanvasDataUrl) return;
        if (!checkRatingsBeforeExport([img])) return;
        const s = readExportSettingsFromUI();
        saveExportSettings(s);
        const fmt = s.format === 'both' ? 'jpg' : s.format;
        const fileName = generateFileName(img, 0, fmt);

        isProcessing = true; updateAllButtons();
        renderSingleImageBlob(img, fmt, s.size, s.sizeCustom).then(blob => {
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = fileName;
            document.body.appendChild(a); a.click(); document.body.removeChild(a);
            URL.revokeObjectURL(url);
            reportFrontendExportMetric({
                fileCount: 1,
                totalBytes: blob.size || 0,
                exportFormat: fmt,
                sizeMode: s.size,
                fileName: fileName,
                sourceImageKey: img.sourcePath || img.savedPath || img.id || ''
            });
            showToast('下载完成: ' + fileName);
        }).catch(err => showToast('下载失败: ' + err.message))
        .finally(() => { isProcessing = false; updateAllButtons(); });
    });

    const expSettings = getExportSettings();
    applySettingsToUI(expSettings);
    updateExportPreview();

    $('#export-template').addEventListener('change', function() {
        if (this.value !== 'custom') applyExportTemplate(this.value);
    });
    $('#export-pick-folder').addEventListener('click', async () => {
        try {
            const dh = await window.showDirectoryPicker({ mode: 'readwrite' });
            exportFolderHandle = dh; exportFolderName = dh.name;
            try { localStorage.setItem(EXPORT_FOLDER_KEY, JSON.stringify({ name: dh.name })); } catch {}
            $('#export-folder-path').textContent = dh.name;
            updateExportPreview();
            updateExportButton();
        } catch {}
    });

    function onExportSettingChange(e) {
        const s = readExportSettingsFromUI();
        if (e && e.target && e.target.id === 'export-bitdepth' && e.target.value === '16' && s.format !== 'png') {
            s.format = 'png';
            $('#export-format').value = 'png';
        }
        s.template = 'custom';
        $('#export-template').value = 'custom';
        saveExportSettings(s);
        updateExportPreview();
        updateExportButton();
    }

    ['export-org','export-naming','export-seq-digits','export-format','export-colorspace','export-bitdepth','export-size','export-after'].forEach(id => {
        const el = $('#' + id);
        if (el) el.addEventListener('change', onExportSettingChange);
    });

    $('#export-quality').addEventListener('input', function() {
        const s = readExportSettingsFromUI();
        s.template = 'custom'; s.quality = parseInt(this.value);
        $('#export-template').value = 'custom';
        saveExportSettings(s);
        updateExportPreview();
    });

    $('#export-naming-custom').addEventListener('input', function() {
        const s = readExportSettingsFromUI();
        s.template = 'custom'; s.namingCustom = this.value;
        $('#export-template').value = 'custom';
        saveExportSettings(s);
        updateExportPreview();
    });

    $('#export-size-custom').addEventListener('input', function() {
        const s = readExportSettingsFromUI();
        s.template = 'custom'; s.sizeCustom = parseInt(this.value) || 3000;
        $('#export-template').value = 'custom';
        saveExportSettings(s);
        updateExportPreview();
    });

    updateExportButton();

    updateAlgoInfo();
    updateAllButtons();

    loadStyleGallery();
}

document.addEventListener('DOMContentLoaded', init);

window.exitWorkspace = function() {
    localStorage.removeItem('cc_token');
    localStorage.removeItem('cc_user');
    localStorage.removeItem('cc_admin_welcome_shown');
    if (typeof rNavigate === 'function') {
        rNavigate('home');
    }
};

var videoFileUrl = null;
var currentVideoFile = null;
var videoDuration = 0;
var videoFps = 0;
var _originalVideoFps = 0;
var _originalVideoWidth = 0;
var _originalVideoHeight = 0;
var _resultVideoLoading = false;
var chaseTaskId = null;
var progressTimer = null;
var _frameThumbnails = [];
var _frameThumbCanvas = null;
var _frameThumbActiveIdx = -1;

window._profileBuiltin = null;
window._profileFile = null;
window._videoRefImageData = null;
window._videoRefSavedPath = '';

function switchWorkspaceMode(isVideo) {
    window.currentProjectType = isVideo ? 'video' : 'image';
    var videoContainer = document.getElementById('video-workspace-container');
    var mainLayout = document.querySelector('.main-layout');
    var progressBar = document.getElementById('video-global-progress');
    var rightSidebar = document.getElementById('right-sidebar');
    var workspaceTitle = document.querySelector('.sidebar-title');
    var imgSidebarActions = document.getElementById('sidebar-actions') || document.querySelector('.sidebar-actions');
    var galleryList = document.getElementById('gallery-list');
    var selectionCountBar = document.getElementById('selection-count-bar');

    if (isVideo) {
        if (mainLayout) mainLayout.style.display = 'none';
        if (videoContainer) videoContainer.style.display = '';
        if (progressBar) progressBar.style.display = 'none';
        if (rightSidebar) rightSidebar.style.display = 'none';
        if (imgSidebarActions) imgSidebarActions.style.display = 'none';
        if (galleryList) galleryList.style.display = 'none';
        if (workspaceTitle) workspaceTitle.textContent = '📹 视频素材';
        if (selectionCountBar) selectionCountBar.style.display = 'none';
        initVideoChaseUI();
    } else {
        if (videoContainer) videoContainer.style.display = 'none';
        if (progressBar) progressBar.style.display = 'none';
        if (mainLayout) mainLayout.style.display = '';
        if (rightSidebar) rightSidebar.style.display = '';
        if (imgSidebarActions) imgSidebarActions.style.display = '';
        if (galleryList) galleryList.style.display = '';
        if (workspaceTitle) workspaceTitle.textContent = '📂 目标图库';
        if (selectionCountBar) selectionCountBar.style.display = '';

        var player = document.getElementById('video-player');
        if (player) {
            player.pause();
            player.removeAttribute('src');
            player.load();
            player.style.display = 'none';
        }
        if (videoFileUrl) {
            URL.revokeObjectURL(videoFileUrl);
            videoFileUrl = null;
        }
        stopProgressTracking();

        var placeholder = document.getElementById('video-placeholder');
        if (placeholder) placeholder.style.display = '';
    }
}

function initVideoChaseUI() {
    if (window._videoUIInitiated) return;
    window._videoUIInitiated = true;

    if (!_frameThumbCanvas) {
        _frameThumbCanvas = document.createElement('canvas');
        _frameThumbCanvas.width = 320;
        _frameThumbCanvas.height = 180;
        _frameThumbCanvas.style.display = 'none';
        document.body.appendChild(_frameThumbCanvas);
    }

    var uploadBtn = document.getElementById('video-upload-btn');
    var fileInput = document.getElementById('video-file-input');
    var player = document.getElementById('video-player');
    var placeholder = document.getElementById('video-placeholder');
    var refDropzone = document.getElementById('ref-dropzone');
    var refFileInput = document.getElementById('ref-file-input');
    var startBtn = document.getElementById('start-video-chase');
    var cancelBtn = document.getElementById('btn-cancel-chase');
    var keyframeSlider = document.getElementById('keyframe-interval-slider');
    var keyframeValue = document.getElementById('keyframe-interval-value');
    var intensitySlider = document.getElementById('blend-intensity-slider');
    var intensityValue = document.getElementById('blend-intensity-value');
    var blendSlider = document.getElementById('frame-blend-slider');
    var blendValue = document.getElementById('frame-blend-value');
    var playBtn = document.getElementById('btn-play');
    var pauseBtn = document.getElementById('btn-pause');
    var prevBtn = document.getElementById('btn-prev');
    var nextBtn = document.getElementById('btn-next');
    var playPauseIcon = document.getElementById('btn-play-pause');
    var progressBar = document.getElementById('video-progress-bar');
    var progressFill = document.getElementById('video-progress-fill');
    var timeDisplay = document.getElementById('video-time-display');

    if (uploadBtn && fileInput) {
        uploadBtn.addEventListener('click', function() { fileInput.click(); });
        fileInput.addEventListener('change', handleVideoFileSelect);
    }

    if (refDropzone && refFileInput) {
        refDropzone.addEventListener('click', function() { refFileInput.click(); });
        refFileInput.addEventListener('change', function(e) {
            var file = e.target.files[0];
            if (file) {
                var reader = new FileReader();
                reader.onload = function(ev) {
                    window._videoRefImageData = ev.target.result;
                    window._videoRefSavedPath = '';
                    var placeholder = document.getElementById('ref-dropzone-placeholder');
                    var preview = document.getElementById('video-ref-preview');
                    if (placeholder) placeholder.style.display = 'none';
                    if (preview) { preview.src = ev.target.result; preview.hidden = false; }
                    if (window.currentProjectId) {
                saveFileToProject(file, 'reference', file.name).then(function(savedPath) {
                    if (!savedPath) return;
                    window._videoRefSavedPath = savedPath;
                    saveLocalProjectSnapshot();
                }).catch(function() {});
                    }
                };
                reader.readAsDataURL(file);
            }
        });
        refDropzone.addEventListener('dragover', function(e) { e.preventDefault(); });
        refDropzone.addEventListener('drop', function(e) {
            e.preventDefault();
            var file = e.dataTransfer.files[0];
            if (file && refFileInput) {
                var dt = new DataTransfer();
                dt.items.add(file);
                refFileInput.files = dt.files;
                refFileInput.dispatchEvent(new Event('change'));
            }
        });
    }

    if (keyframeSlider && keyframeValue) {
        keyframeSlider.addEventListener('input', function() {
            keyframeValue.textContent = this.value;
            if (videoDuration > 0) { generateKeyframeNodes(); generateFrameThumbnails(); }
        });
    }
    if (intensitySlider && intensityValue) {
        intensitySlider.addEventListener('input', function() { intensityValue.textContent = parseFloat(this.value).toFixed(2); });
    }
    if (blendSlider && blendValue) {
        blendSlider.addEventListener('input', function() { blendValue.textContent = this.value; });
    }

    if (playBtn && player) playBtn.addEventListener('click', function() { player.play(); });
    if (pauseBtn && player) pauseBtn.addEventListener('click', function() { player.pause(); });
    if (playPauseIcon && player) {
        playPauseIcon.addEventListener('click', function() {
            if (player.paused) { player.play(); } else { player.pause(); }
        });
    }
    if (prevBtn && player) prevBtn.addEventListener('click', function() { seekFrame(-1); });
    if (nextBtn && player) nextBtn.addEventListener('click', function() { seekFrame(1); });
    if (player) {
        player.addEventListener('timeupdate', updateVideoProgressUI);
        player.addEventListener('loadedmetadata', function() {
            videoDuration = player.duration;
            if (_resultVideoLoading) {
                _resultVideoLoading = false;
                player.style.display = 'block';
                if (placeholder) placeholder.style.display = 'none';
                return;
            }
            _originalVideoWidth = player.videoWidth;
            _originalVideoHeight = player.videoHeight;
            player.style.display = '';
            if (placeholder) placeholder.style.display = 'none';
        });
        player.addEventListener('play', function() {
            if (playPauseIcon) { playPauseIcon.classList.remove('fa-play'); playPauseIcon.classList.add('fa-pause'); }
        });
        player.addEventListener('pause', function() {
            if (playPauseIcon) { playPauseIcon.classList.remove('fa-pause'); playPauseIcon.classList.add('fa-play'); }
        });
    }

    if (progressBar && player) {
        progressBar.addEventListener('click', function(e) {
            var rect = progressBar.getBoundingClientRect();
            var pos = (e.clientX - rect.left) / rect.width;
            player.currentTime = pos * videoDuration;
        });
    }

    if (startBtn) {
        startBtn.addEventListener('click', startVideoChase);
    }

    if (cancelBtn) {
        cancelBtn.addEventListener('click', cancelVideoChase);
    }

    var profilePanelBtn = document.getElementById('btn-profile-panel');
    var exportPanelBtn = document.getElementById('btn-export-panel');
    var profilePopup = document.getElementById('video-profile-popup');
    var exportPopup = document.getElementById('video-export-popup');
    var closeProfileBtn = document.getElementById('close-video-profile');
    var closeExportBtn = document.getElementById('close-video-export');

    if (profilePanelBtn && profilePopup) {
        profilePanelBtn.addEventListener('click', function() { profilePopup.style.display = 'flex'; });
    }
    if (exportPanelBtn && exportPopup) {
        exportPanelBtn.addEventListener('click', function() { exportPopup.style.display = 'flex'; });
    }
    if (closeProfileBtn && profilePopup) {
        closeProfileBtn.addEventListener('click', function() { profilePopup.style.display = 'none'; });
        profilePopup.addEventListener('click', function(e) { if (e.target === profilePopup) profilePopup.style.display = 'none'; });
    }
    if (closeExportBtn && exportPopup) {
        closeExportBtn.addEventListener('click', function() { exportPopup.style.display = 'none'; });
        exportPopup.addEventListener('click', function(e) { if (e.target === exportPopup) exportPopup.style.display = 'none'; });
    }

    var applyProfileBtn = document.getElementById('btn-apply-video-profile');
    var videoProfileSelect = document.getElementById('video-profile-select');
    var videoProfileStatus = document.getElementById('video-profile-status');
    if (applyProfileBtn && videoProfileSelect) {
        applyProfileBtn.addEventListener('click', function() {
            var val = videoProfileSelect.value;
            if (val === 'standard') {
                window._profileBuiltin = null;
                window._profileFile = null;
                if (videoProfileStatus) videoProfileStatus.textContent = '标准（无滤镜）';
                var ous = document.getElementById('video-profile-outside-status');
                if (ous) ous.textContent = '预设管理 / 导入 LUT / 保存配置';
            } else {
                window._profileBuiltin = val;
                window._profileFile = null;
                if (videoProfileStatus) videoProfileStatus.textContent = '预设: ' + val;
                var ous2 = document.getElementById('video-profile-outside-status');
                if (ous2) ous2.textContent = '预设: ' + val;
            }
            document.getElementById('video-profile-popup').style.display = 'none';
            startVideoChase();
        });
    }

    var importLutBtn = document.getElementById('btn-import-video-lut');
    var lutInput = document.getElementById('video-lut-input');
    if (importLutBtn && lutInput) {
        importLutBtn.addEventListener('click', function() { lutInput.click(); });
        lutInput.addEventListener('change', function(e) {
            var file = e.target.files[0];
            if (file) {
                window._profileFile = file;
                window._profileBuiltin = null;
                if (videoProfileStatus) videoProfileStatus.textContent = '已加载: ' + file.name;
                if (videoProfileSelect) videoProfileSelect.value = 'standard';
                var ous3 = document.getElementById('video-profile-outside-status');
                if (ous3) ous3.textContent = '📁 已加载: ' + file.name;
            }
        });
    }

    var savePresetBtn = document.getElementById('btn-save-video-preset');
    var presetNameInput = document.getElementById('video-preset-name');
    if (savePresetBtn && presetNameInput) {
        savePresetBtn.addEventListener('click', function() {
            var name = presetNameInput.value.trim();
            if (!name) { alert('请输入预设名称'); return; }
            window._profileBuiltin = name;
            window._profileFile = null;
            document.getElementById('video-profile-popup').style.display = 'none';
        });
    }

    var vCapRawBtn = document.getElementById('vcapture-raw-btn');
    var vCapRawInput = document.getElementById('vcapture-raw-input');
    var vCapJpgBtn = document.getElementById('vcapture-jpg-btn');
    var vCapJpgInput = document.getElementById('vcapture-jpg-input');
    var vCapStyleBtn = document.getElementById('vbtn-capture-style');
    var vCapStatus = document.getElementById('vcapture-status-text');

    if (vCapRawBtn && vCapRawInput) vCapRawBtn.addEventListener('click', function() { vCapRawInput.click(); });
    if (vCapJpgBtn && vCapJpgInput) vCapJpgBtn.addEventListener('click', function() { vCapJpgInput.click(); });
    if (vCapRawInput) vCapRawInput.addEventListener('change', function(e) {
        vCaptureRawFile = e.target.files[0] || null;
        if (vCaptureRawFile && vCapStatus) vCapStatus.textContent = 'RAW: ' + vCaptureRawFile.name + (vCaptureJpgFile ? ' | JPG: ' + vCaptureJpgFile.name : '');
        if (vCapStyleBtn) vCapStyleBtn.disabled = !(vCaptureRawFile && vCaptureJpgFile);
    });
    if (vCapJpgInput) vCapJpgInput.addEventListener('change', function(e) {
        vCaptureJpgFile = e.target.files[0] || null;
        if (vCaptureJpgFile && vCapStatus) vCapStatus.textContent = (vCaptureRawFile ? 'RAW: ' + vCaptureRawFile.name + ' | ' : '') + 'JPG: ' + vCaptureJpgFile.name;
        if (vCapStyleBtn) vCapStyleBtn.disabled = !(vCaptureRawFile && vCaptureJpgFile);
    });
    if (vCapStyleBtn) vCapStyleBtn.addEventListener('click', async function() {
        if (!vCaptureRawFile || !vCaptureJpgFile) return;
        vCapStyleBtn.disabled = true;
        vCapStyleBtn.textContent = '提取中...';
        if (vCapStatus) vCapStatus.textContent = '正在提取相机风格，请稍候...';
        try {
            var fd = new FormData();
            fd.append('raw_file', vCaptureRawFile);
            fd.append('camera_jpg', vCaptureJpgFile);
            var resp = await fetch('/api/capture_style', { method: 'POST', body: fd });
            var data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || '提取失败');
            if (vCapStatus) vCapStatus.textContent = '风格提取成功: ' + (data.style.name || '');
            loadVideoStyleGallery();
        } catch (err) {
            var msg = err.message || '未知错误';
            if (Array.isArray(msg)) msg = msg.map(function(x) { return x.msg || String(x); }).join('; ');
            if (vCapStatus) vCapStatus.textContent = msg;
        } finally {
            if (vCapStyleBtn) { vCapStyleBtn.disabled = !(vCaptureRawFile && vCaptureJpgFile); vCapStyleBtn.textContent = '提取相机风格'; }
        }
    });

    var profilePopup = document.getElementById('video-profile-popup');
    if (profilePopup) {
        var observer = new MutationObserver(function(mutations) {
            mutations.forEach(function(m) {
                if (m.type === 'attributes' && m.attributeName === 'style') {
                    if (profilePopup.style.display === 'flex') loadVideoStyleGallery();
                }
            });
        });
        observer.observe(profilePopup, { attributes: true });
    }

    var bitrateSlider = document.getElementById('video-bitrate-slider');
    var bitrateValue = document.getElementById('video-bitrate-value');
    if (bitrateSlider && bitrateValue) {
        bitrateSlider.addEventListener('input', function() { bitrateValue.textContent = this.value; });
    }

    var exportVideoBtn = document.getElementById('btn-export-video');
    if (exportVideoBtn) {
        exportVideoBtn.addEventListener('click', async function() {
            var resultUrl = window._lastResultUrl;
            if (!resultUrl) {
                alert('请先完成追色处理后再导出');
                return;
            }
            var format = document.getElementById('video-export-format').value;
            var bitrate = document.getElementById('video-bitrate-slider').value;
            var resolution = document.getElementById('video-export-resolution').value;
            var fps = document.getElementById('video-export-fps').value;

            exportVideoBtn.disabled = true;
            exportVideoBtn.innerHTML = '<i class=\"fas fa-spinner fa-spin\"></i> 转码中...';

            var formData = new FormData();
            formData.append('source_url', resultUrl);
            formData.append('format', format);
            formData.append('bitrate', bitrate);
            formData.append('resolution', resolution);
            formData.append('fps', fps);
            if (window.currentProjectId) {
                formData.append('project_id', String(window.currentProjectId));
            }

            try {
                var resp = await fetch('/api/export_video', { method: 'POST', body: formData, headers: getAuthHeaders() });
                if (!resp.ok) {
                    var err = await resp.json();
                    throw new Error(err.detail || '导出失败');
                }
                var blob = await resp.blob();
                var url = URL.createObjectURL(blob);
                var a = document.createElement('a');
                a.href = url;
                a.download = 'ColorChase_export.mp4';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
            } catch (e) {
                alert('导出失败: ' + e.message);
            } finally {
                exportVideoBtn.disabled = false;
                exportVideoBtn.innerHTML = '<i class=\"fas fa-download\"></i> 导出视频';
            }
        });
    }
}

function handleVideoFileSelect(e) {
    var file = e.target.files[0];
    if (!file) return;
    currentVideoFile = file;

    if (videoFileUrl) URL.revokeObjectURL(videoFileUrl);
    videoFileUrl = URL.createObjectURL(file);

    var player = document.getElementById('video-player');
    if (player) {
        player.src = videoFileUrl;
        player.load();
    }

    document.getElementById('vinfo-name').textContent = file.name;
    document.getElementById('vinfo-size').textContent = (file.size / (1024 * 1024)).toFixed(2) + ' MB';

    if (window.currentProjectId) {
        saveFileToProject(file, 'video_source', file.name).then(function(savedPath) {
            if (savedPath) {
                window._videoSavedPath = savedPath;
                saveLocalProjectSnapshot();
            }
        });
    }

    var metaForm = new FormData();
    metaForm.append('video', file);
    fetch('/api/video_metadata', { method: 'POST', body: metaForm })
        .then(function(r) { return r.json(); })
        .then(function(meta) {
            if (meta.error) throw new Error(meta.error);
            videoFps = meta.fps;
            videoDuration = meta.duration;
            _originalVideoFps = meta.fps;
            _originalVideoWidth = meta.width;
            _originalVideoHeight = meta.height;
            window._videoFrameCount = meta.frame_count;
            document.getElementById('vinfo-fps').textContent = meta.fps.toFixed(2) + ' fps';
            document.getElementById('vinfo-resolution').textContent = meta.width + ' x ' + meta.height;
            document.getElementById('vinfo-codec').textContent = meta.codec.toUpperCase();
            document.getElementById('vinfo-duration').textContent = formatTime(meta.duration);
            generateKeyframeNodes();
            generateFrameThumbnails();
        })
        .catch(function(err) {
            videoFps = 25;
            videoDuration = player ? player.duration : 0;
            _originalVideoFps = 25;
            _originalVideoWidth = player ? player.videoWidth : 0;
            _originalVideoHeight = player ? player.videoHeight : 0;
            document.getElementById('vinfo-fps').textContent = '25 fps';
            document.getElementById('vinfo-resolution').textContent = (player ? player.videoWidth : 0) + ' x ' + (player ? player.videoHeight : 0);
            document.getElementById('vinfo-codec').textContent = 'H264';
            document.getElementById('vinfo-duration').textContent = formatTime(player ? player.duration : 0);
            generateKeyframeNodes();
            generateFrameThumbnails();
        });
}

function updateVideoInfoPanel() {
    var player = document.getElementById('video-player');
    if (!player) return;
    var dur = player.duration;
    var hrs = Math.floor(dur / 3600);
    var mins = Math.floor((dur % 3600) / 60);
    var secs = Math.floor(dur % 60);
    var frames = Math.floor((dur - Math.floor(dur)) * 24);
    document.getElementById('vinfo-duration').textContent =
        String(hrs).padStart(2, '0') + ':' + String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0') + ':' + String(frames).padStart(2, '0');
    document.getElementById('vinfo-resolution').textContent = player.videoWidth + ' x ' + player.videoHeight;
    document.getElementById('vinfo-fps').textContent = videoFps + ' fps';
    document.getElementById('vinfo-codec').textContent = 'H.264 / AAC';
}

var _kfDragNode = null;
var _kfDragStartX = 0;
var _kfDragStartLeft = 0;
var _kfHasMoved = false;
var _kfContainer = null;
var _kfInsetRatio = 0;

function generateKeyframeNodes() {
    var container = document.getElementById('keyframe-nodes-container');
    var timeline = document.getElementById('keyframe-timeline');
    if (!container || !timeline || videoDuration === 0) return;
    container.innerHTML = '';

    var oldOverlays = timeline.querySelectorAll('.keyframe-label, .keyframe-connector');
    oldOverlays.forEach(function(el) { el.remove(); });

    _kfContainer = container;
    _kfDragNode = null;

    var interval = parseInt(document.getElementById('keyframe-interval-slider').value, 10);
    var frameCount = Math.ceil(videoDuration * videoFps);
    var stepFrames = interval;
    var totalKeyframes = Math.ceil(frameCount / stepFrames) + 1;

    var insetPx = 50;
    var containerWidth = container.offsetWidth || 1;
    _kfInsetRatio = Math.min(insetPx / containerWidth, 0.45);

    for (var i = 0; i < totalKeyframes; i++) {
        var frame = i * stepFrames;
        if (frame > frameCount) frame = frameCount;
        var time = frame / videoFps;
        var rawPercent = (time / videoDuration) * 100;
        var percent = _kfInsetRatio * 100 + rawPercent * (100 - 2 * _kfInsetRatio * 100) / 100;
        var label = formatTime(time);

        var node = document.createElement('div');
        node.className = 'keyframe-node';
        node.style.left = percent + '%';
        node.setAttribute('data-frame', frame);
        node.setAttribute('data-index', i);

        var innerDot = document.createElement('div');
        innerDot.className = 'inner-dot';
        node.appendChild(innerDot);

        node.addEventListener('mousedown', function(e) {
            e.preventDefault();
            _kfDragNode = this;
            _kfDragStartX = e.clientX;
            _kfDragStartLeft = parseFloat(this.style.left);
            _kfHasMoved = false;

            var allNodes = document.querySelectorAll('.keyframe-node');
            allNodes.forEach(function(n) { n.classList.remove('active'); });
            this.classList.add('active');
        });

        node.addEventListener('touchstart', function(e) {
            e.preventDefault();
            _kfDragNode = this;
            _kfDragStartX = e.touches[0].clientX;
            _kfDragStartLeft = parseFloat(this.style.left);
            _kfHasMoved = false;

            var allNodes = document.querySelectorAll('.keyframe-node');
            allNodes.forEach(function(n) { n.classList.remove('active'); });
            this.classList.add('active');
        }, { passive: false });

        var connector = document.createElement('div');
        connector.className = 'keyframe-connector';
        connector.style.left = percent + '%';
        connector.setAttribute('data-index', i);

        var labelEl = document.createElement('span');
        labelEl.className = 'keyframe-label';
        labelEl.style.left = percent + '%';
        labelEl.textContent = label;
        labelEl.setAttribute('data-index', i);

        container.appendChild(node);
        timeline.appendChild(connector);
        timeline.appendChild(labelEl);
    }
}

function updateKfNodePosition(dragNode, newLeftPercent) {
    var container = document.getElementById('keyframe-nodes-container');
    var timeline = document.getElementById('keyframe-timeline');
    if (!container || !timeline) return;

    var minPct = _kfInsetRatio * 100;
    var maxPct = 100 - _kfInsetRatio * 100;
    newLeftPercent = Math.max(minPct, Math.min(maxPct, newLeftPercent));
    dragNode.style.left = newLeftPercent + '%';

    var clampTime = ((newLeftPercent - minPct) / (maxPct - minPct)) * videoDuration;
    var clampFrame = Math.round(clampTime * videoFps);
    dragNode.setAttribute('data-frame', clampFrame);

    var idx = dragNode.getAttribute('data-index');
    var connector = timeline.querySelector('.keyframe-connector[data-index="' + idx + '"]');
    var labelEl = timeline.querySelector('.keyframe-label[data-index="' + idx + '"]');
    if (connector) connector.style.left = newLeftPercent + '%';
    if (labelEl) {
        labelEl.style.left = newLeftPercent + '%';
        labelEl.textContent = formatTime(clampFrame / videoFps);
    }

    var thumbCard = document.querySelector('.frame-thumb-card[data-idx="' + idx + '"]');
    if (thumbCard && _frameThumbnails[idx]) {
        _frameThumbnails[idx].frameNumber = clampFrame;
        _frameThumbnails[idx].time = clampTime;
        var ftTime = thumbCard.querySelector('.ft-time');
        var ftFrameNum = thumbCard.querySelector('.ft-frame-num');
        if (ftTime) ftTime.textContent = formatTime(clampFrame / videoFps);
        if (ftFrameNum) ftFrameNum.textContent = '帧 #' + clampFrame;
        if (thumbCard.querySelector('.ft-thumb-img')) {
            captureVideoFrame(clampTime, function(dataUrl) {
                if (dataUrl && _frameThumbnails[idx]) {
                    _frameThumbnails[idx].originalThumb = dataUrl;
                    var cardEl = document.querySelector('.frame-thumb-card[data-idx="' + idx + '"]');
                    if (cardEl) {
                        var imgEl = cardEl.querySelector('.ft-thumb-img');
                        if (imgEl) imgEl.src = dataUrl;
                    }
                }
            });
        }
    }
}

document.addEventListener('mousemove', function(e) {
    if (!_kfDragNode || !_kfContainer) return;
    var dx = e.clientX - _kfDragStartX;
    if (Math.abs(dx) < 2) return;
    _kfHasMoved = true;

    var containerWidth = _kfContainer.offsetWidth || 1;
    var newLeft = _kfDragStartLeft + (dx / containerWidth) * 100;
    updateKfNodePosition(_kfDragNode, newLeft);
});

document.addEventListener('mouseup', function(e) {
    if (!_kfDragNode) return;
    if (!_kfHasMoved) {
        var frame = parseInt(_kfDragNode.getAttribute('data-frame'), 10);
        var player = document.getElementById('video-player');
        if (player) player.currentTime = frame / videoFps;
    }
    _kfDragNode = null;
});

document.addEventListener('touchmove', function(e) {
    if (!_kfDragNode || !_kfContainer) return;
    var dx = e.touches[0].clientX - _kfDragStartX;
    if (Math.abs(dx) < 2) return;
    _kfHasMoved = true;

    var containerWidth = _kfContainer.offsetWidth || 1;
    var newLeft = _kfDragStartLeft + (dx / containerWidth) * 100;
    updateKfNodePosition(_kfDragNode, newLeft);
}, { passive: false });

document.addEventListener('touchend', function(e) {
    if (!_kfDragNode) return;
    if (!_kfHasMoved) {
        var frame = parseInt(_kfDragNode.getAttribute('data-frame'), 10);
        var player = document.getElementById('video-player');
        if (player) player.currentTime = frame / videoFps;
    }
    _kfDragNode = null;
});

function captureVideoFrame(timeSeconds, callback) {
    var player = document.getElementById('video-player');
    if (!player || !_frameThumbCanvas) { callback(null); return; }
    if (isNaN(timeSeconds) || !isFinite(timeSeconds)) { callback(null); return; }
    if (isNaN(videoFps) || videoFps <= 0) videoFps = 25;
    var savedTime = player.currentTime;
    var savedPaused = player.paused;
    player.currentTime = timeSeconds;
    function onSeeked() {
        player.removeEventListener('seeked', onSeeked);
        var ctx = _frameThumbCanvas.getContext('2d');
        ctx.drawImage(player, 0, 0, _frameThumbCanvas.width, _frameThumbCanvas.height);
        var dataUrl = _frameThumbCanvas.toDataURL('image/jpeg', 0.7);
        player.currentTime = savedTime;
        if (savedPaused && !player.paused) player.pause();
        callback(dataUrl);
    }
    player.addEventListener('seeked', onSeeked);
}

var _frameThumbsGenerating = false;

function generateFrameThumbnails() {
    var container = document.getElementById('frame-thumbnails-list');
    if (!container || videoDuration === 0) return;
    if (_frameThumbsGenerating) return;
    _frameThumbsGenerating = true;

    var interval = parseInt(document.getElementById('keyframe-interval-slider').value, 10);
    var frameCount = Math.ceil(videoDuration * videoFps);
    var stepFrames = interval;
    var totalFrames = Math.ceil(frameCount / stepFrames) + 1;
    var maxThumbs = 80;
    if (totalFrames > maxThumbs) totalFrames = maxThumbs;

    container.innerHTML = '<div class="text-center text-gray-500 text-xs py-4">正在提取帧缩略图...</div>';
    _frameThumbnails = [];
    _frameThumbActiveIdx = -1;

    var pending = totalFrames;
    var items = [];

    for (var i = 0; i < totalFrames; i++) {
        (function(idx) {
            var frame = idx * stepFrames;
            if (frame > frameCount) frame = frameCount;
            var time = frame / videoFps;
            var label = formatTime(time);
            var item = {
                index: idx,
                frameNumber: frame,
                time: time,
                timeLabel: label,
                isKeyframe: true,
                blendIntensity: parseFloat(document.getElementById('blend-intensity-slider').value),
                originalThumb: '',
                resultThumb: '',
                status: 'pending',
                progress: 0
            };
            items[idx] = item;
            captureVideoFrame(time, function(dataUrl) {
                item.originalThumb = dataUrl || '';
                pending--;
                if (pending === 0) renderAllThumbCards(items);
            });
        })(i);
    }
}

function renderAllThumbCards(items) {
    _frameThumbsGenerating = false;
    _frameThumbnails = items;
    var container = document.getElementById('frame-thumbnails-list');
    if (!container) return;
    container.innerHTML = '';
    for (var i = 0; i < items.length; i++) {
        var card = createThumbCard(items[i], i);
        container.appendChild(card);
    }
}

function createThumbCard(item, idx) {
    var card = document.createElement('div');
    card.className = 'frame-thumb-card is-keyframe';
    card.setAttribute('data-idx', idx);

    var thumbWrap = document.createElement('div');
    thumbWrap.className = 'ft-thumb-wrap';

    var img = document.createElement('img');
    img.className = 'ft-thumb-img';
    img.src = item.resultThumb || item.originalThumb;
    thumbWrap.appendChild(img);

    var badge = document.createElement('span');
    badge.className = 'ft-status-badge';
    if (item.status === 'done') badge.textContent = '✅';
    else if (item.status === 'processing') badge.textContent = '🔄';
    else badge.textContent = '⭐';
    thumbWrap.appendChild(badge);

    card.appendChild(thumbWrap);

    var info = document.createElement('div');
    info.className = 'ft-info';

    var timeEl = document.createElement('span');
    timeEl.className = 'ft-time';
    timeEl.textContent = item.timeLabel;
    info.appendChild(timeEl);

    var frameNumEl = document.createElement('span');
    frameNumEl.className = 'ft-frame-num';
    frameNumEl.textContent = '帧 #' + item.frameNumber;
    info.appendChild(frameNumEl);

    var progBar = document.createElement('div');
    progBar.className = 'ft-progress-bar';
    var progFill = document.createElement('div');
    progFill.className = 'ft-progress-fill';
    progFill.style.width = item.progress + '%';
    if (item.status === 'done') progFill.classList.add('done');
    if (item.status === 'processing') progFill.classList.add('processing');
    progBar.appendChild(progFill);
    info.appendChild(progBar);

    card.appendChild(info);

    card.addEventListener('click', function() {
        var allCards = document.querySelectorAll('.frame-thumb-card');
        allCards.forEach(function(c) { c.classList.remove('active'); });
        card.classList.add('active');
        _frameThumbActiveIdx = idx;
        var player = document.getElementById('video-player');
        if (player) player.currentTime = item.time;
    });

    return card;
}

function updateThumbCard(idx, updates) {
    if (idx < 0 || idx >= _frameThumbnails.length) return;
    var item = _frameThumbnails[idx];
    if (updates.status !== undefined) item.status = updates.status;
    if (updates.progress !== undefined) item.progress = updates.progress;
    if (updates.resultThumb !== undefined) item.resultThumb = updates.resultThumb;

    var container = document.getElementById('frame-thumbnails-list');
    if (!container) return;
    var card = container.querySelector('.frame-thumb-card[data-idx="' + idx + '"]');
    if (!card) return;

    var img = card.querySelector('.ft-thumb-img');
    var badge = card.querySelector('.ft-status-badge');
    var progFill = card.querySelector('.ft-progress-fill');
    if (img && item.resultThumb) img.src = item.resultThumb;
    if (badge) {
        if (item.status === 'done') badge.textContent = '✅';
        else if (item.status === 'processing') badge.textContent = '🔄';
        else badge.textContent = '⭐';
    }
    if (progFill) {
        progFill.style.width = item.progress + '%';
        progFill.className = 'ft-progress-fill';
        if (item.status === 'done') progFill.classList.add('done');
        if (item.status === 'processing') progFill.classList.add('processing');
    }
    if (item.status === 'done' && !card.classList.contains('is-keyframe')) {
        card.classList.add('is-keyframe');
    }
}

function blendFramesCanvas(img1, img2, ratio) {
    var canvas = _frameThumbCanvas;
    if (!canvas) return '';
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    var tmpImg1 = new Image();
    var tmpImg2 = new Image();
    return new Promise(function(resolve) {
        var loaded = 0;
        function check() {
            loaded++;
            if (loaded < 2) return;
            ctx.globalAlpha = 1 - ratio;
            ctx.drawImage(tmpImg1, 0, 0, canvas.width, canvas.height);
            ctx.globalAlpha = ratio;
            ctx.drawImage(tmpImg2, 0, 0, canvas.width, canvas.height);
            ctx.globalAlpha = 1;
            resolve(canvas.toDataURL('image/jpeg', 0.7));
        }
        tmpImg1.onload = check;
        tmpImg2.onload = check;
        tmpImg1.src = img1;
        tmpImg2.src = img2;
    });
}

function formatTime(seconds) {
    var hrs = Math.floor(seconds / 3600);
    var mins = Math.floor((seconds % 3600) / 60);
    var secs = Math.floor(seconds % 60);
    var frames = Math.floor((seconds - Math.floor(seconds)) * videoFps);
    return String(hrs).padStart(2, '0') + ':' + String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0') + ':' + String(frames).padStart(2, '0');
}

function updateVideoProgressUI() {
    var player = document.getElementById('video-player');
    var fill = document.getElementById('video-progress-fill');
    var thumb = document.getElementById('video-progress-thumb');
    var display = document.getElementById('video-time-display');
    if (!player || !fill || !thumb || !display) return;
    var percent = (player.currentTime / player.duration) * 100;
    fill.style.width = percent + '%';
    thumb.style.left = percent + '%';
    display.textContent = formatTime(player.currentTime) + ' / ' + formatTime(player.duration);

    var nearestIdx = -1;
    var minDist = Infinity;
    for (var i = 0; i < _frameThumbnails.length; i++) {
        var dist = Math.abs(_frameThumbnails[i].time - player.currentTime);
        if (dist < minDist) { minDist = dist; nearestIdx = i; }
    }
    if (nearestIdx !== _frameThumbActiveIdx) {
        _frameThumbActiveIdx = nearestIdx;
        var allCards = document.querySelectorAll('.frame-thumb-card');
        allCards.forEach(function(c) { c.classList.remove('active'); });
        if (nearestIdx >= 0) {
            var activeCard = document.querySelector('.frame-thumb-card[data-idx="' + nearestIdx + '"]');
            if (activeCard) {
                activeCard.classList.add('active');
                activeCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }
    }
}

function seekFrame(offset) {
    var player = document.getElementById('video-player');
    if (!player) return;
    var frameTime = 1 / videoFps;
    player.currentTime = Math.max(0, Math.min(player.duration, player.currentTime + offset * frameTime));
}

function startVideoChase() {
    _resultVideoLoading = false;
    if (!currentVideoFile && !window._videoSavedPath) {
        alert('请先上传视频');
        return;
    }
    var refImage = window._videoRefImageData;
    var profileBuiltin = window._profileBuiltin;
    var profileFile = window._profileFile;
    var modeSelect = document.getElementById('chase-mode-select');
    var mode = modeSelect ? modeSelect.value : 'neural';
    var interval = parseInt(document.getElementById('keyframe-interval-slider').value, 10) || 30;
    if (isNaN(interval)) interval = 30;
    var intensity = document.getElementById('blend-intensity-slider').value;
    var blendFrames = document.getElementById('frame-blend-slider').value;

    var formData = new FormData();
    if (window._videoSavedPath) {
        formData.append('video_path', window._videoSavedPath);
    } else {
        formData.append('video', currentVideoFile);
    }
    formData.append('key_frame_interval', interval);
    formData.append('blend_strength', intensity);
    formData.append('transition_frames', blendFrames);
    formData.append('algorithm', mode);
    formData.append('enable_scene_detect', document.getElementById('enable-scene-detect').checked);
    if (window.currentProjectId) {
        formData.append('project_id', String(window.currentProjectId));
    }
    var customKeyframes = [];
    var kfNodes = document.querySelectorAll('.keyframe-node');
    var totalFrameCount = Math.ceil(videoDuration * videoFps);
    kfNodes.forEach(function(node) {
        var frame = parseInt(node.getAttribute('data-frame'), 10);
        if (!isNaN(frame) && frame > 0 && frame < totalFrameCount) {
            customKeyframes.push(frame);
        }
    });
    if (customKeyframes.length > 0) {
        formData.append('custom_keyframes', customKeyframes.join(','));
    }
    if (window._capturedStyleId) {
        formData.append('captured_style_id', window._capturedStyleId);
    }
    if (window._videoRefSavedPath) {
        formData.append('reference_path', window._videoRefSavedPath);
    } else if (refImage && refImage.startsWith('data:')) {
        var blob = dataURLtoBlob(refImage);
        formData.append('reference', blob, 'reference.jpg');
    }
    if (profileBuiltin) {
        formData.append('profile_builtin', profileBuiltin);
    }
    if (profileFile) {
        formData.append('profile_file', profileFile);
    }

    var progressContainer = document.getElementById('video-global-progress');
    if (progressContainer) progressContainer.style.display = '';
    updateProgressBar(0, 100);
    document.getElementById('progress-status-text').textContent = '准备处理...';
    document.getElementById('progress-time-info').textContent = '已处理: 00:00:00 / 00:00:00';
    document.getElementById('progress-remaining').textContent = '剩余时间: 计算中...';

    fetch('/api/video_transfer', {
        method: 'POST',
        body: formData,
        headers: getAuthHeaders()
    }).then(function(response) {
        if (!response.ok) throw new Error('请求失败');
        return response.json();
    }).then(function(data) {
        if (data.task_id) {
            chaseTaskId = data.task_id;
            startProgressTracking();
        } else {
            alert('任务提交失败，未返回 task_id');
            resetProgressUI();
        }
    }).catch(function(err) {
        alert('错误: ' + err.message);
        resetProgressUI();
    });
}

function startProgressTracking() {
    stopProgressTracking();
    var totalThumbs = _frameThumbnails.length;
    for (var t = 0; t < totalThumbs; t++) {
        updateThumbCard(t, { status: 'processing', progress: 0 });
    }
    var errorCount = 0;
    var MAX_ERRORS = 3;
    progressTimer = setInterval(function() {
        fetch('/api/task/' + chaseTaskId + '/progress')
        .then(function(r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        })
        .then(function(data) {
            errorCount = 0;
            if (data.video_fps && _originalVideoFps === 0) {
                _originalVideoFps = data.video_fps;
                _originalVideoWidth = data.video_width || _originalVideoWidth;
                _originalVideoHeight = data.video_height || _originalVideoHeight;
                videoFps = _originalVideoFps;
                document.getElementById('vinfo-fps').textContent = _originalVideoFps + ' fps';
                document.getElementById('vinfo-resolution').textContent = _originalVideoWidth + ' x ' + _originalVideoHeight;
            }
            updateProgressBar(data.current, data.total);
            var elapsed = data.elapsed || 0;
            var totalPct = data.total > 0 ? (data.current / data.total) : 0;
            var totalEst = totalPct > 0 ? Math.round(elapsed / totalPct) : 0;
            var remaining = Math.max(0, totalEst - elapsed);
            var remainingStr = remaining > 0 ? '约 ' + formatDuration(remaining) : '计算中...';
            document.getElementById('progress-time-info').textContent = '已处理: ' + formatDuration(elapsed) + ' / ' + formatDuration(totalEst);
            document.getElementById('progress-remaining').textContent = '剩余时间: ' + remainingStr;
            document.getElementById('progress-status-text').textContent = formatStatusText(data.message);

            var progressPct = data.total > 0 ? Math.round((data.current / data.total) * 100) : 0;
            var liveTotalThumbs = _frameThumbnails.length;
            var framesPerThumb = Math.ceil(data.total / Math.max(liveTotalThumbs, 1));
            for (var t = 0; t < liveTotalThumbs; t++) {
                var thumbFrameStart = t * framesPerThumb;
                if (data.current >= thumbFrameStart) {
                    var localPct = data.total > 0 ? Math.min(100, Math.round(((data.current - thumbFrameStart) / framesPerThumb) * 100)) : 0;
                    var thumbStatus = localPct >= 100 ? 'done' : 'processing';
                    updateThumbCard(t, { status: thumbStatus, progress: localPct });
                }
            }

            if (data.status === 'done' || data.status === 'completed' || data.status === 'error') {
                stopProgressTracking();
                var finalStatus = (data.status === 'done' || data.status === 'completed') ? 'done' : 'pending';
                var totalThumbsDone = _frameThumbnails.length;
                for (var t2 = 0; t2 < totalThumbsDone; t2++) {
                    updateThumbCard(t2, { status: finalStatus, progress: finalStatus === 'done' ? 100 : 0 });
                }
                if (data.status === 'done' || data.status === 'completed') {
                    document.getElementById('progress-status-text').textContent = '逐帧追色完成';
                    document.getElementById('progress-remaining').textContent = '';
                    var cancelBtn = document.getElementById('btn-cancel-chase');
                    if (cancelBtn) cancelBtn.style.display = 'none';
                    setTimeout(function() {
                        var bar = document.getElementById('video-global-progress');
                        if (bar) bar.style.display = 'none';
                    }, 3000);
                    if (data.result_url) {
                        window._lastResultUrl = data.result_url;
                        if (window.currentProjectId) {
                            saveFileToProject(data.result_url, 'result', 'video_result.mp4').then(function(savedPath) {
                                if (savedPath) {
                                    window._lastResultUrl = savedPath;
                                    saveLocalProjectSnapshot();
                                } else {
                                    saveSnapshot();
                                }
                            }).catch(function() {
                                saveSnapshot();
                            });
                        } else {
                            saveSnapshot();
                        }
                        var player = document.getElementById('video-player');
                        if (player) {
                            _resultVideoLoading = true;
                            player.pause();
                            player.src = '';
                            player.load();
                            setTimeout(function() {
                                player.src = window._lastResultUrl || data.result_url;
                                player.load();
                                player.addEventListener('canplay', function() {
                                    refreshResultThumbnails();
                                    player.play().catch(function() {});
                                }, { once: true });
                            }, 50);
                        }
                    } else {
                        setTimeout(function() { refreshResultThumbnails(); }, 500);
                    }
                } else {
                    document.getElementById('progress-status-text').textContent = '处理出错: ' + (data.message || '未知错误');
                }
            }
        })
        .catch(function() {
            errorCount++;
            if (errorCount >= MAX_ERRORS) {
                stopProgressTracking();
                document.getElementById('progress-status-text').textContent = '连接断开，处理可能仍在进行';
            }
        });
    }, 1000);
}

function refreshResultThumbnails() {
    if (_frameThumbnails.length === 0) return;
    var player = document.getElementById('video-player');
    if (!player) return;
    var thumbs = _frameThumbnails.slice();
    var idx = 0;
    var total = thumbs.length;
    function processNext() {
        if (idx >= total) return;
        captureVideoFrame(thumbs[idx].time, function(dataUrl) {
            if (dataUrl) updateThumbCard(idx, { resultThumb: dataUrl, status: 'done', progress: 100 });
            idx++;
            setTimeout(processNext, 150);
        });
    }
    processNext();
}

function stopProgressTracking() {
    if (progressTimer) {
        clearInterval(progressTimer);
        progressTimer = null;
    }
}

function cancelVideoChase() {
    if (chaseTaskId) {
        fetch('/api/task/' + chaseTaskId + '/cancel', { method: 'POST', headers: getAuthHeaders() });
    }
    stopProgressTracking();
    resetProgressUI();
}

function resetProgressUI() {
    var container = document.getElementById('video-global-progress');
    if (container) container.style.display = 'none';
    stopProgressTracking();
    updateProgressBar(0, 0);
}

function formatDuration(sec) {
    if (!sec || sec <= 0) return '00:00:00';
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = Math.floor(sec % 60);
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
}

function formatStatusText(msg) {
    if (!msg) return '追色处理中...';
    var match;
    match = msg.match(/分析(首帧|间隔|场景切换|关键帧) (\d+)/);
    if (match) {
        if (match[1] === '首帧') return '首帧 初始化追色...';
        if (match[1] === '间隔') return '间隔关键帧#' + match[2] + ' 追色中...';
        if (match[1] === '场景切换') return '场景切换帧#' + match[2] + ' 追色中...';
        return '关键帧#' + match[2] + ' 追色中...';
    }
    match = msg.match(/(?:渲染帧|过渡渲染帧|LUT 极速渲染帧) (\d+)/);
    if (match) return '帧#' + match[1] + ' 渲染中...';
    if (msg.indexOf('末帧融合') !== -1) return '末帧融合追色';
    if (msg.indexOf('末帧') !== -1) return '末帧跳过追色';
    if (msg.indexOf('合成视频') !== -1) return '正在合成视频...';
    if (msg.indexOf('平均像素差异') !== -1) return msg;
    if (msg.indexOf('完成') !== -1) return '逐帧追色完成';
    return '追色处理中...';
}

function updateProgressBar(current, total) {
    var container = document.getElementById('progress-bar-segments');
    var percentEl = document.getElementById('progress-percent');
    if (!container) return;
    var totalSegments = 60;
    var filled = total === 0 ? 0 : Math.round((current / total) * totalSegments);
    container.innerHTML = '';
    for (var i = 0; i < totalSegments; i++) {
        var seg = document.createElement('div');
        seg.className = 'h-full w-[4px] rounded-sm flex-shrink-0 ' + (i < filled ? 'bg-indigo-500' : 'bg-gray-700');
        container.appendChild(seg);
    }
    if (percentEl) {
        percentEl.textContent = total === 0 ? '0%' : Math.round((current / total) * 100) + '%';
    }
}

function dataURLtoBlob(dataurl) {
    var arr = dataurl.split(',');
    var mime = arr[0].match(/:(.*?);/)[1];
    var bstr = atob(arr[1]);
    var n = bstr.length;
    var u8arr = new Uint8Array(n);
    while (n--) { u8arr[n] = bstr.charCodeAt(n); }
    return new Blob([u8arr], { type: mime });
}

/* ── 项目地址设置 ─────────────────────── */
var BROWSER_PROJECT_ROOT_KEY = 'colorchase_browser_project_root';
var browserProjectRootHandle = null;

function safeLocalProjectName(name, fallback) {
    return String(name || fallback || 'file.bin').replace(/[\\/:*?"<>|]+/g, '_').replace(/\s+/g, '_');
}

function updateBrowserProjectRootUI() {
    var input = document.getElementById('sc-browser-project-root');
    var status = document.getElementById('browser-project-root-status');
    var savedName = '';
    try {
        var raw = localStorage.getItem(BROWSER_PROJECT_ROOT_KEY);
        if (raw) savedName = JSON.parse(raw).name || '';
    } catch(e) {}
    if (input) input.value = browserProjectRootHandle ? browserProjectRootHandle.name : savedName;
    if (status) {
        if (!window.showDirectoryPicker) {
            status.textContent = '当前浏览器不支持直接写本地文件夹，将使用上方项目文件路径保存。';
        } else if (browserProjectRootHandle) {
            status.textContent = '已授权本地项目目录：' + browserProjectRootHandle.name;
        } else {
            status.textContent = '不选择时使用上方项目文件路径保存；支持的浏览器可额外同步到本地文件夹。';
        }
    }
}

async function chooseBrowserProjectRoot() {
    if (!window.showDirectoryPicker) {
        showToast('当前浏览器不支持直接选择本地项目文件夹，已使用后端默认路径保存。');
        updateBrowserProjectRootUI();
        return;
    }
    try {
        browserProjectRootHandle = await window.showDirectoryPicker({ mode: 'readwrite' });
        try { localStorage.setItem(BROWSER_PROJECT_ROOT_KEY, JSON.stringify({ name: browserProjectRootHandle.name })); } catch(e) {}
        updateBrowserProjectRootUI();
        showToast('本地项目目录已授权：' + browserProjectRootHandle.name);
    } catch(e) {
        if (e && e.name !== 'AbortError') showToast('本地目录授权失败');
    }
}

async function getBrowserProjectDirectory() {
    if (!browserProjectRootHandle || !window.currentProjectId) return null;
    try {
        if (browserProjectRootHandle.queryPermission) {
            var permission = await browserProjectRootHandle.queryPermission({ mode: 'readwrite' });
            if (permission !== 'granted' && browserProjectRootHandle.requestPermission) {
                permission = await browserProjectRootHandle.requestPermission({ mode: 'readwrite' });
            }
            if (permission !== 'granted') return null;
        }
        return browserProjectRootHandle.getDirectoryHandle(String(window.currentProjectId), { create: true });
    } catch(e) {
        return null;
    }
}

async function writeBrowserProjectFile(bucket, fileName, content) {
    var projectDir = await getBrowserProjectDirectory();
    if (!projectDir || !content) return false;
    try {
        var targetDir = bucket ? await projectDir.getDirectoryHandle(bucket, { create: true }) : projectDir;
        var handle = await targetDir.getFileHandle(safeLocalProjectName(fileName), { create: true });
        var writer = await handle.createWritable();
        await writer.write(content);
        await writer.close();
        return true;
    } catch(e) {
        return false;
    }
}

async function writeBrowserProjectResult(fileName, content) {
    return writeBrowserProjectFile('result', fileName, content);
}

function writeBrowserProjectSnapshot(snap) {
    getBrowserProjectDirectory().then(async function(projectDir) {
        if (!projectDir) return;
        try {
            var handle = await projectDir.getFileHandle('snapshot.json', { create: true });
            var writer = await handle.createWritable();
            await writer.write(new Blob([JSON.stringify(snap, null, 2)], { type: 'application/json' }));
            await writer.close();
        } catch(e) {}
    });
}

var STORAGE_FIELDS = {
    'sc-project-assets': 'project_assets',
    'sc-image-uploads': 'image_uploads',
    'sc-image-luts': 'image_luts',
    'sc-image-debug': 'image_debug',
    'sc-video-uploads': 'video_uploads',
    'sc-video-results': 'video_results',
    'sc-video-frames': 'video_frames',
};

function getStorageRequestHeaders() {
    var headers = {'Content-Type': 'application/json'};
    var token = localStorage.getItem('cc_token');
    if (token) headers['Authorization'] = 'Bearer ' + token;
    return headers;
}

function showStorageSettings() {
    var modal = document.getElementById('storage-settings-modal');
    if (!modal) return;
    modal.style.display = 'flex';
    fetch('/api/user_config', {
        headers: (function() {
            var token = localStorage.getItem('cc_token');
            return token ? {'Authorization': 'Bearer ' + token} : {};
        })(),
    })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var cur = data.current || {};
            Object.keys(STORAGE_FIELDS).forEach(function(fid) {
                var el = document.getElementById(fid);
                if (el) el.value = cur[STORAGE_FIELDS[fid]] || '';
            });
            updateBrowserProjectRootUI();
            if (data.disk_free_gb) {
                document.getElementById('storage-disk-space').textContent = data.disk_free_gb + ' GB';
            }
        })
        .catch(function() {});
}

function hideStorageSettings() {
    var modal = document.getElementById('storage-settings-modal');
    if (modal) modal.style.display = 'none';
}

function saveStorageSettings() {
    var data = {};
    Object.keys(STORAGE_FIELDS).forEach(function(fid) {
        var el = document.getElementById(fid);
        if (el && el.value.trim()) {
            data[STORAGE_FIELDS[fid]] = el.value.trim();
        }
    });
    var projectAssetsEl = document.getElementById('sc-project-assets');
    if (projectAssetsEl && projectAssetsEl.value.trim()) {
        data.project_assets = projectAssetsEl.value.trim();
    }
    fetch('/api/user_config', {
        method: 'POST',
        headers: getStorageRequestHeaders(),
        body: JSON.stringify(data),
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
        alert(d.message || '已保存');
        hideStorageSettings();
    })
    .catch(function() { alert('保存失败'); });
}

function resetStorageSettings() {
    if (!confirm('恢复默认路径？')) return;
    fetch('/api/user_config', {
        method: 'POST',
        headers: getStorageRequestHeaders(),
        body: JSON.stringify({}),
    })
    .then(function(r) { return r.json(); })
    .then(function() {
        fetch('/api/user_config', {
            headers: (function() {
                var token = localStorage.getItem('cc_token');
                return token ? {'Authorization': 'Bearer ' + token} : {};
            })(),
        })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var cur = data.config || {};
                var projectAssetsEl = document.getElementById('sc-project-assets');
                if (projectAssetsEl) projectAssetsEl.value = cur.project_assets || '';
                Object.keys(STORAGE_FIELDS).forEach(function(fid) {
                    var el = document.getElementById(fid);
                    if (el) el.value = cur[STORAGE_FIELDS[fid]] || '';
                });
            });
    });
}

document.addEventListener('DOMContentLoaded', function() {
    var saveBtn = document.getElementById('storage-save-btn');
    var cancelBtn = document.getElementById('storage-cancel-btn');
    var resetBtn = document.getElementById('storage-reset-btn');
    var projectRootBtn = document.getElementById('browser-project-root-btn');
    if (saveBtn) saveBtn.addEventListener('click', saveStorageSettings);
    if (cancelBtn) cancelBtn.addEventListener('click', hideStorageSettings);
    if (resetBtn) resetBtn.addEventListener('click', resetStorageSettings);
    if (projectRootBtn) projectRootBtn.addEventListener('click', chooseBrowserProjectRoot);
    document.querySelectorAll('.storage-pick-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var targetId = btn.getAttribute('data-target');
            fetch('/api/pick_folder', { method: 'POST' })
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (d.path) document.getElementById(targetId).value = d.path;
                });
        });
    });
    updateBrowserProjectRootUI();
});

/* ── 项目备份 ─────────────────────── */
function buildSnapshotData() {
    return {
        projectType: window.currentProjectType,
        targetImages: (targetImages || []).map(function(img) {
            return {
                name: img.name,
                sourcePath: img.sourcePath,
                thumbnailUrl: img.thumbnailUrl,
                meta: img.meta || '',
                status: img.status || 'pending',
                sessionId: img.sessionId,
                mergedSessionId: img.mergedSessionId,
                resultDataUrl: img.resultDataUrl && !String(img.resultDataUrl).startsWith('data:') ? img.resultDataUrl : '',
                refSavedPath: img.refSavedPath || '',
                subjectMaskPath: img.subjectMaskPath || '',
                subjectMaskUrl: img.subjectMaskUrl || '',
                subjectMaskMode: img.subjectMaskMode || 'protect_subject',
                subjectMaskPoints: Array.isArray(img.subjectMaskPoints) ? img.subjectMaskPoints : [],
                depthLayerPath: img.depthLayerPath || '',
                depthLayerUrl: img.depthLayerUrl || '',
                semanticMatchMeta: img.semanticMatchMeta || null,
                profileId: img.profileId,
                aiAlgo: img.aiAlgo || '',
                params: img.params,
                rating: img.rating,
                resultSavedPath: img.resultSavedPath || '',
                savedPath: img.savedPath || '',
                localSourcePath: img.localSourcePath || '',
                localReferencePath: img.localReferencePath || '',
                localResultPath: img.localResultPath || '',
            };
        }),
        currentTargetIndex: currentTargetIndex,
        refSavedPath: window._refSavedPath || '',
        algorithm: $('#ai-algorithm-select') ? $('#ai-algorithm-select').value : '',
        profileBuiltin: _profileBuiltin,
        lutAI: lutAI, lutProfile: lutProfile,
        videoFileSavedPath: window._videoSavedPath || '',
        videoRefSavedPath: window._videoRefSavedPath || '',
        adjustSliders: (function() {
            var s = {};
            ADJUST_PARAMS.forEach(function(p) {
                var el = $('#adjust-' + p.id);
                if (el) s[p.id] = el.value;
            });
            return s;
        })(),
        videoResultUrl: window._lastResultUrl || '',
        videoMeta: {
            fps: _originalVideoFps || 0,
            width: _originalVideoWidth || 0,
            height: _originalVideoHeight || 0,
            duration: videoDuration || 0,
            frameCount: window._videoFrameCount || 0,
        },
        localProjectRootName: browserProjectRootHandle ? browserProjectRootHandle.name : '',
    };
}

function saveSnapshot(pid) {
    var snap = buildSnapshotData();
    if (!pid) pid = window.currentProjectId;
    if (!pid) return;
    var token = localStorage.getItem('cc_token');
    var headers = {'Content-Type': 'application/json'};
    if (token) headers['Authorization'] = 'Bearer ' + token;
    fetch('/api/projects/' + pid + '/snapshot', {
        method: 'PUT',
        headers: headers,
        body: JSON.stringify({ snapshot: JSON.stringify(snap) }),
    }).catch(function() {});
    if (browserProjectRootHandle) {
        writeBrowserProjectSnapshot(snap);
    }
}

function saveLocalProjectSnapshot() {
    if (window.currentProjectId) saveSnapshot(window.currentProjectId);
}

async function saveFileToProject(file, bucket, fileName) {
    var pid = window.currentProjectId;
    if (!pid || !file) return '';
    var fd = new FormData();
    var safeBucket = bucket || 'source';
    var safeName = fileName || '';
    var localContent = file;
    if (typeof file === 'string') {
        try {
            var sourceResp = await fetch(file);
            if (!sourceResp.ok) return '';
            var blob = await sourceResp.blob();
            var sourceName = file.split('/').pop().split('?')[0] || 'asset.bin';
            safeName = safeName || sourceName;
            localContent = blob;
            fd.append('file', new File([blob], sourceName, { type: blob.type || 'application/octet-stream' }));
        } catch(e) {
            return '';
        }
    } else {
        safeName = safeName || file.name || 'asset.bin';
        localContent = file;
        fd.append('file', file);
    }
    try {
        var token = localStorage.getItem('cc_token');
        var headers = {};
        if (token) headers['Authorization'] = 'Bearer ' + token;
        fd.append('bucket', safeBucket);
        var r = await fetch('/api/projects/' + pid + '/upload', { method: 'POST', body: fd, headers: headers });
        var d = await r.json();
        if (browserProjectRootHandle) {
            await writeBrowserProjectFile(safeBucket, safeName, localContent);
        }
        return d.asset_url || d.path || '';
    } catch(e) { return ''; }
}

function loadSnapshot(pid) {
    if (!pid) return;
    var token = localStorage.getItem('cc_token');
    var headers = {};
    if (token) headers['Authorization'] = 'Bearer ' + token;
    fetch('/api/projects/', { headers: headers })
        .then(function(r) { return r.json(); })
        .then(function(projects) {
            var p = projects.find(function(x) { return x.id === pid; });
            if (!p || !p.snapshot) {
                clearWorkspaceState();
                return;
            }
            try {
                var snap = JSON.parse(p.snapshot);
                loadSnapshotData(snap, pid);
            } catch(e) {}
        });
}

function clearWorkspaceState() {
    targetImages = [];
    window.targetImages = targetImages;
    currentTargetIndex = -1;
    _lastSessionId = null;
    _mergedSessionId = null;
    _profileSessionId = null;
    _profileBuiltin = null;
    _profileFile = null;
    lutAI = null;
    lutProfile = null;
    _refDataUrl = null;
    _subjectMaskPath = '';
    _subjectMaskUrl = '';
    _subjectMaskPoints = [];
    _subjectMaskMode = 'protect_subject';
    _depthLayerPath = '';
    _depthLayerUrl = '';
    _semanticMatchMeta = null;
    window._refSavedPath = '';
    window._videoSavedPath = '';
    window._videoRefSavedPath = '';
    window._lastResultUrl = '';
    window._refSavedPath = '';
    currentVideoFile = null;
    videoDuration = 0;
    videoFps = 25;
    refFile = null;
    if (videoFileUrl) { URL.revokeObjectURL(videoFileUrl); videoFileUrl = null; }
    // 清空帧缩略图状态，避免跨项目残留
    _frameThumbnails = [];
    _frameThumbActiveIdx = -1;
    _frameThumbsGenerating = false;
    var _frameList = document.getElementById('frame-thumbnails-list');
    if (_frameList) _frameList.innerHTML = '';
    // 清空调整基准数据，避免叠加
    _originalImageData = null;
    _stylizedImageData = null;
    resetAdjustSliders();
    renderGallery();
    var canvasPlaceholder = document.getElementById('canvas-placeholder');
    if (canvasPlaceholder) canvasPlaceholder.hidden = false;
    var canvasStack = document.getElementById('canvas-stack');
    if (canvasStack) canvasStack.hidden = true;
    var maskPreview = document.getElementById('canvas-mask-preview');
    if (maskPreview) { maskPreview.src = ''; maskPreview.hidden = true; }
    var depthPreview = document.getElementById('canvas-depth-preview');
    if (depthPreview) { depthPreview.src = ''; depthPreview.hidden = true; }
    setViewMode('single');
    updateMaskUI();
    updateDepthUI();
    updateSemanticUI();
    updateAllButtons();
}

function loadSnapshotData(snap, pid) {
    if (snap.algorithm) {
        var sel = $('#ai-algorithm-select');
        if (sel) sel.value = snap.algorithm;
        updateAlgoInfo();
    }
    if (snap.profileBuiltin) {
        _profileBuiltin = snap.profileBuiltin;
    }
    if (snap.lutAI) lutAI = snap.lutAI;
    if (snap.lutProfile) lutProfile = snap.lutProfile;
    var refSrc = snap.refDataUrl || normalizeProjectAssetUrl(snap.refSavedPath || '', pid) || '';
    if (refSrc) {
        _refDataUrl = snap.refDataUrl || null;
        _refSavedPath = snap.refSavedPath || '';
        window._refSavedPath = snap.refSavedPath || '';
        var refPreview = $('#ref-preview');
        if (refPreview) {
            refPreview.src = refSrc;
            refPreview.style.display = 'block';
            var refPlaceholder = $('#ref-placeholder');
            if (refPlaceholder) refPlaceholder.style.display = 'none';
            var refClear = $('#ref-clear');
            if (refClear) refClear.style.display = 'inline-block';
            refFile = new File([], 'reference.jpg');
        }
    } else if (snap.refSavedPath) {
        window._refSavedPath = snap.refSavedPath;
    }
    if (snap.adjustSliders) {
        Object.keys(snap.adjustSliders).forEach(function(k) {
            var el = $('#adjust-' + k);
            if (el) { el.value = snap.adjustSliders[k]; el.dispatchEvent(new Event('input')); }
        });
    }

    if (snap.projectType === 'video') {
        window._videoSavedPath = snap.videoFileSavedPath || '';
        window._videoRefSavedPath = snap.videoRefSavedPath || '';
        window._lastResultUrl = snap.videoResultUrl || snap.videoResultSavedPath || '';
        if (snap.videoMeta) {
            _originalVideoFps = snap.videoMeta.fps || 25;
            _originalVideoWidth = snap.videoMeta.width || 1920;
            _originalVideoHeight = snap.videoMeta.height || 1080;
            videoDuration = snap.videoMeta.duration || 0;
            videoFps = snap.videoMeta.fps || 25;
            window._videoFrameCount = snap.videoMeta.frameCount || 0;
        }
        if (window._videoSavedPath && typeof initVideoChaseUI === 'function') {
            initVideoChaseUI();
            var player = document.getElementById('video-player');
            var placeholder = document.getElementById('video-placeholder');
            if (player && window._videoSavedPath) {
                player.src = window._videoSavedPath;
                player.style.display = '';
                if (placeholder) placeholder.style.display = 'none';
                player.addEventListener('loadedmetadata', function() {
                    videoDuration = Math.round(player.duration);
                    document.getElementById('vinfo-duration').textContent = formatTime(videoDuration);
                    document.getElementById('vinfo-resolution').textContent = snap.videoMeta.width + ' x ' + snap.videoMeta.height;
                    generateKeyframeNodes();
                    generateFrameThumbnails();
                    if (window._lastResultUrl) {
                        var resultPlayer = document.getElementById('video-result-player');
                        if (resultPlayer) resultPlayer.src = window._lastResultUrl;
                    }
                });
            }
            document.getElementById('vinfo-name').textContent = window._videoSavedPath.split('/').pop() || '视频文件';
            document.getElementById('vinfo-resolution').textContent = (snap.videoMeta ? snap.videoMeta.width + ' x ' + snap.videoMeta.height : '');
            document.getElementById('vinfo-fps').textContent = (snap.videoMeta ? snap.videoMeta.fps.toFixed(2) + ' fps' : '');
        }
        return;
    }

    if (snap.targetImages && snap.targetImages.length > 0) {
        var defParams = { intensity: 100, exposure: 100, contrast: 100, highlight: 100, shadow: 100, vibrance: 100 };
        // 快照里可能存的是本地绝对路径(D:\...\storage\projects\assets\{pid}\source\xxx)，
        // 直接赋给 <img src> 会触发 file:// 拒绝加载；这里用 normalizeProjectAssetUrl 转成 /api/project_assets/ HTTP URL
        var _norm = function(v) { return normalizeProjectAssetUrl(v, pid); };
        // 浏览器原生不能解码 RAW 格式（.CR2/.NEF/.ARW 等），显示用 URL 必须过滤掉这些扩展名，
        // 改用后端生成的 JPG 缩略图 URL（thumbs/{uid}_thumb.jpg）。
        // 注意：sourcePath 字段不过滤（保留 .CR2 URL 供追色接口解析回本地原 RAW 文件）。
        var RAW_EXTS = ['.cr2', '.cr3', '.crw', '.nef', '.nrw', '.arw', '.srf', '.sr2', '.raf', '.rw2', '.raw', '.rwl', '.orf', '.pef', '.ptx', '.3fr', '.fff', '.iiq', '.cap', '.eip', '.mef', '.mos', '.mfw', '.x3f', '.dcr', '.kdc', '.k25', '.dcs', '.srw', '.erf', '.cs1', '.cs4', '.cs16', '.sti', '.bay', '.pxn', '.braw', '.r3d', '.ari', '.cine', '.lfp', '.rwz', '.dng'];
        var _isDisplayable = function(u) {
            var s = String(u || '').toLowerCase().split('?')[0];
            if (!s) return false;
            if (/^(data:|blob:)/.test(s)) return true;
            for (var i = 0; i < RAW_EXTS.length; i++) {
                if (s.endsWith(RAW_EXTS[i])) return false;
            }
            return true;
        };
        // 把 RAW source URL 转成后端生成的 thumbs JPG URL。
        // 规则：/api/project_assets/{pid}/source/{uid}.{ext}  →  /api/project_assets/{pid}/thumbs/{uid}_thumb.jpg
        // 后端 upload_batch 在生成原图同时会用 rawpy 解码 RAW 并写入 thumbs/{uid}_thumb.jpg（main.py 行 802-810）
        var _toThumbUrl = function(normalizedUrl) {
            if (!normalizedUrl) return '';
            var m = normalizedUrl.match(/^\/api\/project_assets\/(\d+)\/source\/([^\/]+)$/);
            if (!m) return '';
            var pid2 = m[1];
            var name = m[2];
            var dotIdx = name.lastIndexOf('.');
            var stem = dotIdx > 0 ? name.slice(0, dotIdx) : name;
            return '/api/project_assets/' + pid2 + '/thumbs/' + stem + '_thumb.jpg';
        };
        var _dispUrl = function(v) {
            var u = _norm(v);
            if (!u) return '';
            if (_isDisplayable(u)) return u;
            // RAW 扩展名被过滤，尝试转成后端生成的 thumbs JPG URL
            return _toThumbUrl(u);
        };
        targetImages = snap.targetImages.map(function(img, idx) {
            // sourcePath 用于追色 target_path，后端能解析 .CR2 URL 回本地 RAW 文件，所以保留原值不过滤
            var _src = _norm(img.localSourcePath) || _norm(img.savedPath) || _norm(img.sourcePath) || '';
            // 显示用 _thumb 必须是浏览器可解码的 URL（JPG/PNG/HTTP/data:），过滤 RAW 扩展名
            var _thumb = _dispUrl(img.thumbnailUrl) || _dispUrl(img.localSourcePath) || _dispUrl(img.savedPath) || _dispUrl(img.sourcePath) || _src;
            return {
                id: 'img_' + Date.now() + '_' + idx,
                name: img.name || '',
                sourcePath: _src,
                thumbnailUrl: _thumb,
                meta: img.meta || '',
                resultDataUrl: img.resultDataUrl || null,
                refDataUrl: img.refDataUrl || null,
                refSavedPath: img.refSavedPath || '',
                subjectMaskPath: img.subjectMaskPath || '',
                subjectMaskUrl: img.subjectMaskUrl || '',
                subjectMaskMode: img.subjectMaskMode || 'protect_subject',
                subjectMaskPoints: Array.isArray(img.subjectMaskPoints) ? img.subjectMaskPoints : [],
                depthLayerPath: img.depthLayerPath || '',
                depthLayerUrl: img.depthLayerUrl || '',
                semanticMatchMeta: img.semanticMatchMeta || null,
                sessionId: img.sessionId || null,
                mergedSessionId: img.mergedSessionId || null,
                profileId: img.profileId || null,
                aiAlgo: img.aiAlgo || '',
                params: img.params || Object.assign({}, defParams),
                status: img.status || 'pending',
                rating: img.rating || 0,
                resultSavedPath: img.resultSavedPath || '',
                savedPath: img.savedPath || '',
                localSourcePath: img.localSourcePath || '',
                localReferencePath: img.localReferencePath || '',
                localResultPath: img.localResultPath || '',
            };
        });
        window.targetImages = targetImages;
        renderGallery();

        var idx = snap.currentTargetIndex;
        if (idx >= 0 && idx < targetImages.length) {
            currentTargetIndex = idx;
        } else {
            currentTargetIndex = 0;
        }

        if (targetImages.length > 0) {
            var img = targetImages[currentTargetIndex];
            $('#canvas-filename').textContent = img.name || '';
            $('#canvas-resolution').textContent = img.meta || '';
            $('#canvas-placeholder').hidden = true;
            $('#canvas-stack').hidden = false;
            // 显示用 thumbSrc 必须过滤 RAW 扩展名（浏览器不能直接解码 .CR2 等）
            var thumbSrc = _dispUrl(img.thumbnailUrl) || _dispUrl(img.localSourcePath) || _dispUrl(img.sourcePath) || '';
            $('#canvas-original').src = thumbSrc;
            $('#canvas-result').src = normalizeProjectAssetUrl(img.localResultPath, pid) || img.resultDataUrl || normalizeProjectAssetUrl(img.resultSavedPath || '', pid) || thumbSrc;
            _origCanvasDataUrl = thumbSrc;
            _resultCanvasDataUrl = normalizeProjectAssetUrl(img.localResultPath, pid) || img.resultDataUrl || normalizeProjectAssetUrl(img.resultSavedPath || '', pid) || thumbSrc;
            setViewMode('single');
            restoreCurrentState();
            if (img.localResultPath || img.resultDataUrl || img.resultSavedPath) {
                loadSwitchImageData();
            }
        }

        updateProfileStatus();
        updateAllButtons();
        updateExportPreview();
    }
}

window._pendingExitAction = null;
function confirmExitProject(action, pid) {
    window._pendingExitAction = action;
    var doAction = function(save) {
        if (save) saveSnapshot(pid || window.currentProjectId);
        if (window._pendingExitAction) window._pendingExitAction();
        window._pendingExitAction = null;
    };
    var div = document.createElement('div');
    div.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);z-index:99999;display:flex;align-items:center;justify-content:center;';
    div.innerHTML = '<div style=\"background:#2b2b3d;border-radius:8px;padding:24px;text-align:center;max-width:360px;\">' +
        '<div style=\"color:#e8e8f0;font-size:15px;margin-bottom:8px;\">是否备份当前项目？</div>' +
        '<div style=\"color:#888;font-size:12px;margin-bottom:20px;\">备份后刷新或切换回来可恢复工作状态</div>' +
        '<div style=\"display:flex;gap:10px;justify-content:center;\">' +
        '<button id=\"backup-cancel-btn\" style=\"background:transparent;border:1px solid #555;color:#aaa;padding:8px 24px;border-radius:6px;cursor:pointer;\">取消</button>' +
        '<button id=\"backup-save-btn\" style=\"background:#7c3aed;border:none;color:#fff;padding:8px 24px;border-radius:6px;cursor:pointer;\">备份</button>' +
        '</div></div>';
    document.body.appendChild(div);
    div.querySelector('#backup-cancel-btn').onclick = function() { document.body.removeChild(div); doAction(false); };
    div.querySelector('#backup-save-btn').onclick = function() { document.body.removeChild(div); doAction(true); };
}

window.addEventListener('beforeunload', function(e) {
    var pid = window.currentProjectId;
    if (pid && pid > 0) {
        var snap = buildSnapshotData();
        var token = localStorage.getItem('cc_token');
        var headers = {'Content-Type': 'application/json'};
        if (token) headers['Authorization'] = 'Bearer ' + token;
        fetch('/api/projects/' + pid + '/snapshot', {
            method: 'PUT',
            headers: headers,
            body: JSON.stringify({ snapshot: JSON.stringify(snap) }),
            keepalive: true,
        }).catch(function() {});
    }
});
