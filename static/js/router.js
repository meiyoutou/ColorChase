var $r = function(sel) { return document.querySelector(sel); };
var currentView = 'home';
var authToken = localStorage.getItem('cc_token');
var currentUser = JSON.parse(localStorage.getItem('cc_user') || 'null');
var currentTheme = localStorage.getItem('cc_theme') || 'dark';
var adminDashboardCache = null;
var userSpaceDashboardCache = null;
var adminDashboardRefreshTimer = null;
var adminModelManagerCache = null;
var homeProjectsRequestSeq = 0;
var settingsProfileRequest = null;
var portalMessagesCache = null;
var portalNoticeSelection = {};
var portalMessagesLoaded = false;
var portalNoticeAutoShownForVersion = null;

function isAdminUser() {
    return !!(currentUser && currentUser.role === 'admin');
}

function isAdminSpaceView() {
    return isAdminUser() && currentView === 'space';
}

function updateAdminSurfaceState() {
    var homeView = document.getElementById('view-home');
    if (!homeView) return;
    homeView.classList.toggle('admin-space-active', isAdminSpaceView());
}

function updateAdminVisibility() {
    var adminNavs = document.querySelectorAll('.admin-only-nav');
    adminNavs.forEach(function(el) {
        el.style.display = isAdminUser() ? '' : 'none';
    });
    var banner = $r('#admin-welcome-banner');
    if (banner) banner.style.display = isAdminUser() && (currentView === 'home' || currentView === 'space') ? '' : 'none';
    updatePortalMessageMode();
    updateAdminSurfaceState();
}

function maybeShowAdminLoginToast() {
    if (!isAdminUser()) return;
    var flagKey = 'cc_admin_welcome_shown';
    var userKey = currentUser && currentUser.id ? String(currentUser.id) : 'admin';
    if (localStorage.getItem(flagKey) === userKey) return;
    localStorage.setItem(flagKey, userKey);
    if (typeof showToast === 'function') {
        showToast('已识别为管理员账号，管理员功能已开启。');
    }
}

function getAuthHeaders() {
    var token = localStorage.getItem('cc_token');
    return token ? { 'Authorization': 'Bearer ' + token } : {};
}

function getSettingsFallbackDisplayName(user) {
    user = user || currentUser || {};
    if (userSpaceDashboardCache && userSpaceDashboardCache.profile && userSpaceDashboardCache.profile.display_name) {
        return userSpaceDashboardCache.profile.display_name;
    }
    if (user && user.display_name) return user.display_name;
    if (user && user.email) return String(user.email).split('@')[0];
    if (user && user.phone) return user.phone;
    return '当前用户';
}

function applySettingsProfileData(profile) {
    var user = currentUser || JSON.parse(localStorage.getItem('cc_user') || '{}');
    var usernameEl = $r('#settings-username');
    var nameValEl = $r('#settings-name-val');
    var roleValEl = $r('#settings-role-val');
    var nicknameInput = $r('#settings-nickname-input');
    var ratedCountEl = $r('#settings-rated-count');
    var totalCountEl = $r('#settings-total-count');
    var displayName = (profile && profile.display_name) || getSettingsFallbackDisplayName(user);
    if (usernameEl) usernameEl.textContent = displayName;
    if (nicknameInput && document.activeElement !== nicknameInput) nicknameInput.value = displayName;
    if (nameValEl) nameValEl.textContent = (profile && profile.account_id) || user.email || user.phone || '未知';
    if (roleValEl) roleValEl.textContent = (profile && profile.account_type) || (user.role === 'admin' ? '管理员账号' : '普通用户');
    var ratingSummary = profile && profile.rating_summary ? profile.rating_summary : null;
    if (ratedCountEl && ratingSummary) ratedCountEl.textContent = Number(ratingSummary.rated_count || 0);
    if (totalCountEl && ratingSummary) totalCountEl.textContent = Number(ratingSummary.total_count || 0);
    if (profile && profile.display_name && currentUser) {
        currentUser.display_name = profile.display_name;
        localStorage.setItem('cc_user', JSON.stringify(currentUser));
    }
}

function loadSettingsProfile(force) {
    if (!localStorage.getItem('cc_token')) return Promise.resolve(null);
    if (!force && userSpaceDashboardCache && userSpaceDashboardCache.profile) {
        applySettingsProfileData(userSpaceDashboardCache.profile);
        return Promise.resolve(userSpaceDashboardCache.profile);
    }
    if (!force && settingsProfileRequest) return settingsProfileRequest;
    settingsProfileRequest = fetch('/api/projects/space_dashboard_v2', {
        headers: getAuthHeaders()
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) throw new Error((result.data && result.data.detail) || '加载用户资料失败');
        if (result.data && result.data.profile) applySettingsProfileData(result.data.profile);
        return result.data && result.data.profile ? result.data.profile : null;
    })
    .catch(function(err) {
        console.warn('loadSettingsProfile failed:', err);
        return null;
    })
    .finally(function() {
        settingsProfileRequest = null;
    });
    return settingsProfileRequest;
}

function renderSpacePanelForUser() {
    if (isAdminUser()) {
        renderAdminSpaceDashboard();
        return;
    }
    renderUserSpaceDashboard();
}

function getPortalSeenKey(kind) {
    var userId = currentUser && currentUser.id ? String(currentUser.id) : 'guest';
    return 'cc_portal_seen_' + kind + '_' + userId;
}

function getPortalSeenVersion(kind) {
    var raw = localStorage.getItem(getPortalSeenKey(kind));
    var value = parseInt(raw, 10);
    return Number.isFinite(value) ? value : 0;
}

function setPortalSeenVersion(kind, version) {
    localStorage.setItem(getPortalSeenKey(kind), String(version || 0));
}

function setBadgeVisible(el, visible) {
    if (!el) return;
    el.style.display = visible ? '' : 'none';
}

function formatMetricCardValue(item) {
    if (!item) return '0';
    if (item.display_value !== undefined && item.display_value !== null && item.display_value !== '') {
        return String(item.display_value);
    }
    return String(item.value == null ? 0 : item.value) + (item.unit || '');
}

function renderPortalMessages() {
    if (!portalMessagesCache) return;
    var noticeView = $r('#notice-modal-view');
    var contactView = $r('#contact-modal-view');
    var notice = portalMessagesCache.notice || {};
    var contact = portalMessagesCache.contact || {};
    if (noticeView) {
        var noticeItems = Array.isArray(notice.items) && notice.items.length ? notice.items : [{
            title: notice.title || '更新日志',
            body: notice.body || '',
            created_at: notice.updated_at || ''
        }];
        noticeView.innerHTML = '<div class="portal-message-list">' + noticeItems.map(function(item) {
            return '<div class="portal-message-item">' +
                '<h4>' + escapeHtml(item.title || '新消息') + '</h4>' +
                '<p>' + escapeHtml(item.body || '') + '</p>' +
                (item.created_at ? '<div class="portal-meta-line">' + escapeHtml(formatPortalMessageTime(item.created_at)) + '</div>' : '') +
            '</div>';
        }).join('') + '</div>';
    }
    if (contactView) {
        contactView.innerHTML = '<div class="contact-info-line">' +
            '<span class="contact-info-label">沟通QQ群</span>' +
            '<strong>' + escapeHtml(contact.qq || '955749464') + '</strong>' +
        '</div>' +
        '<div class="portal-meta-line">' + escapeHtml(contact.notes || '') + '</div>';
    }
}

function normalizeUserSpaceTaskCenter(items) {
    return (Array.isArray(items) ? items : []).map(function(item) {
        var normalized = item || {};
        var status = String(normalized.status || 'info').toLowerCase();
        return {
            task_id: normalized.task_id || '',
            created_at: normalized.created_at || '--',
            task_type: normalized.task_type || '未知任务',
            status: status,
            status_label: normalized.status_label || (status === 'ok' ? '成功' : (status === 'fail' ? '失败' : (status === 'cancel' ? '已取消' : '处理中'))),
            summary: normalized.summary || '暂无摘要',
            detail: normalized.detail || '',
            failure_reason: normalized.failure_reason || '',
            model: normalized.model || '',
            primary_action: normalized.primary_action || { type: '', label: '处理' },
        };
    });
}

function getSelectedPortalNoticeIds() {
    return Object.keys(portalNoticeSelection).filter(function(id) {
        return !!portalNoticeSelection[id];
    });
}

function renderPortalAdminNoticeHistory() {
    if (!isAdminUser()) return;
    var historyEl = $r('#notice-admin-history-list');
    var selectAllEl = $r('#notice-admin-select-all');
    var deleteBtn = $r('#notice-admin-delete-selected');
    var notice = portalMessagesCache && portalMessagesCache.notice ? portalMessagesCache.notice : {};
    var items = Array.isArray(notice.items) ? notice.items : [];

    if (!historyEl) return;

    if (!items.length) {
        historyEl.innerHTML = '<div class="admin-empty-state user-space-inline-empty">暂无历史更新日志</div>';
        if (deleteBtn) deleteBtn.disabled = true;
        if (selectAllEl) {
            selectAllEl.checked = false;
            selectAllEl.indeterminate = false;
        }
        return;
    }

    historyEl.innerHTML = items.map(function(item, index) {
        var itemId = String(item.id || ('legacy-' + index));
        var checked = !!portalNoticeSelection[itemId];
        return '<div class="portal-history-item">' +
            '<div class="portal-history-check"><input class="notice-history-checkbox" type="checkbox" data-notice-id="' + escapeAttr(itemId) + '"' + (checked ? ' checked' : '') + '></div>' +
            '<div class="portal-history-copy">' +
                '<h4>' + escapeHtml(item.title || ('更新日志 ' + (index + 1))) + '</h4>' +
                '<p>' + escapeHtml(item.body || '') + '</p>' +
                '<div class="portal-meta-line">' + escapeHtml(formatPortalMessageTime(item.created_at)) + '</div>' +
            '</div>' +
            '<button class="portal-history-delete" type="button" data-notice-delete-id="' + escapeAttr(itemId) + '">删除</button>' +
        '</div>';
    }).join('');

    var selectedIds = getSelectedPortalNoticeIds().filter(function(id) {
        return items.some(function(item) { return String(item.id || '') === id; });
    });
    if (deleteBtn) {
        deleteBtn.disabled = selectedIds.length === 0;
    }
    if (selectAllEl) {
        selectAllEl.checked = items.length > 0 && selectedIds.length === items.length;
        selectAllEl.indeterminate = selectedIds.length > 0 && selectedIds.length < items.length;
    }

    historyEl.querySelectorAll('.notice-history-checkbox').forEach(function(checkbox) {
        checkbox.addEventListener('click', function(event) {
            event.stopPropagation();
        });
        checkbox.addEventListener('change', function() {
            var id = this.getAttribute('data-notice-id') || '';
            portalNoticeSelection[id] = !!this.checked;
            renderPortalAdminNoticeHistory();
        });
    });

    historyEl.querySelectorAll('[data-notice-delete-id]').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var id = this.getAttribute('data-notice-delete-id') || '';
            if (id) {
                deletePortalNoticeItems([id]);
            }
        });
    });
}

function deletePortalNoticeItems(ids) {
    var validIds = (ids || []).filter(function(id) { return !!id; });
    if (!validIds.length) return;
    var confirmText = validIds.length > 1 ? ('确定删除选中的 ' + validIds.length + ' 条更新日志吗？') : '确定删除这条更新日志吗？';
    if (typeof window !== 'undefined' && !window.confirm(confirmText)) return;
    fetch('/api/admin/portal_messages/delete', {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json' }, getAuthHeaders()),
        body: JSON.stringify({ ids: validIds })
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) {
            throw new Error((result.data && result.data.detail) || '删除更新日志失败');
        }
        validIds.forEach(function(id) {
            delete portalNoticeSelection[id];
        });
        portalMessagesCache = result.data;
        portalMessagesLoaded = true;
        renderPortalMessages();
        renderPortalAdminNoticeHistory();
        updatePortalMessageMode();
        portalNoticeSelection = {};
        if (typeof updateAdminVisibility === 'function') updateAdminVisibility();
        if (typeof showToast === 'function') showToast('更新日志已删除');
    })
    .catch(function(err) {
        if (typeof showToast === 'function') showToast(err.message || '删除更新日志失败');
    });
}

function formatPortalMessageTime(value) {
    if (!value) return '--';
    var parsed = new Date(value);
    if (isNaN(parsed.getTime())) return String(value);
    var yyyy = parsed.getFullYear();
    var mm = String(parsed.getMonth() + 1).padStart(2, '0');
    var dd = String(parsed.getDate()).padStart(2, '0');
    var hh = String(parsed.getHours()).padStart(2, '0');
    var mi = String(parsed.getMinutes()).padStart(2, '0');
    return yyyy + '-' + mm + '-' + dd + ' ' + hh + ':' + mi;
}

function ensurePortalAdminEditor(kind) {
    if (!isAdminUser()) return;
    if (kind === 'notice') {
        var noticeView = $r('#notice-modal-view');
        var noticeAdmin = $r('#notice-modal-admin');
        var notice = portalMessagesCache && portalMessagesCache.notice ? portalMessagesCache.notice : {};
        var titleEl = $r('#notice-admin-title');
        var bodyEl = $r('#notice-admin-body');
        var saveBtn = $r('#notice-admin-save');
        if (noticeView) {
            noticeView.style.display = 'none';
            noticeView.innerHTML = '';
        }
        if (noticeAdmin) {
            noticeAdmin.style.display = 'grid';
            noticeAdmin.style.pointerEvents = 'auto';
            noticeAdmin.style.position = 'relative';
            noticeAdmin.style.zIndex = '5';
        }
        if (titleEl) {
            titleEl.disabled = false;
            titleEl.readOnly = false;
            titleEl.style.pointerEvents = 'auto';
            titleEl.value = notice.title || titleEl.value || '';
        }
        if (bodyEl) {
            bodyEl.disabled = false;
            bodyEl.readOnly = false;
            bodyEl.style.pointerEvents = 'auto';
            bodyEl.value = notice.body || bodyEl.value || '';
        }
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.style.pointerEvents = 'auto';
        }
        renderPortalAdminNoticeHistory();
        return;
    }

    var contactView = $r('#contact-modal-view');
    var contactAdmin = $r('#contact-modal-admin');
    var contact = portalMessagesCache && portalMessagesCache.contact ? portalMessagesCache.contact : {};
    var qqEl = $r('#contact-admin-qq');
    var notesEl = $r('#contact-admin-notes');
    var contactSaveBtn = $r('#contact-admin-save');
    if (contactView) {
        contactView.style.display = 'none';
        contactView.innerHTML = '';
    }
    if (contactAdmin) {
        contactAdmin.style.display = 'grid';
        contactAdmin.style.pointerEvents = 'auto';
        contactAdmin.style.position = 'relative';
        contactAdmin.style.zIndex = '5';
    }
    if (qqEl) {
        qqEl.disabled = false;
        qqEl.readOnly = false;
        qqEl.style.pointerEvents = 'auto';
        qqEl.value = contact.qq || qqEl.value || '';
    }
    if (notesEl) {
        notesEl.disabled = false;
        notesEl.readOnly = false;
        notesEl.style.pointerEvents = 'auto';
        notesEl.value = contact.notes || notesEl.value || '';
    }
    if (contactSaveBtn) {
        contactSaveBtn.disabled = false;
        contactSaveBtn.style.pointerEvents = 'auto';
    }
}

function updatePortalMessageMode() {
    var noticeAdmin = $r('#notice-modal-admin');
    var contactAdmin = $r('#contact-modal-admin');
    var noticeView = $r('#notice-modal-view');
    var contactView = $r('#contact-modal-view');
    var noticeBadge = $r('#nav-notice-badge');
    var contactBadge = $r('#nav-contact-badge');
    var workspaceNoticeBadge = $r('#workspace-notice-badge');
    var workspaceContactBadge = $r('#workspace-contact-badge');
    var adminMode = isAdminUser();

    if (noticeAdmin) noticeAdmin.style.display = adminMode ? 'grid' : 'none';
    if (contactAdmin) contactAdmin.style.display = adminMode ? 'grid' : 'none';
    if (noticeView) noticeView.style.display = adminMode ? 'none' : '';
    if (contactView) contactView.style.display = adminMode ? 'none' : '';
    if (adminMode) {
        ensurePortalAdminEditor('notice');
        ensurePortalAdminEditor('contact');
        renderPortalAdminNoticeHistory();
    }

    if (!portalMessagesCache) {
        setBadgeVisible(noticeBadge, false);
        setBadgeVisible(contactBadge, false);
        setBadgeVisible(workspaceNoticeBadge, false);
        setBadgeVisible(workspaceContactBadge, false);
        return;
    }

    var noticeVersion = Number((portalMessagesCache.notice || {}).version || 0);
    var contactVersion = Number((portalMessagesCache.contact || {}).version || 0);

    setBadgeVisible(noticeBadge, !adminMode && noticeVersion > getPortalSeenVersion('notice'));
    setBadgeVisible(contactBadge, !adminMode && contactVersion > getPortalSeenVersion('contact'));
    setBadgeVisible(workspaceNoticeBadge, !adminMode && noticeVersion > getPortalSeenVersion('notice'));
    setBadgeVisible(workspaceContactBadge, !adminMode && contactVersion > getPortalSeenVersion('contact'));
}

function markPortalRead(kind) {
    if (!portalMessagesCache || isAdminUser()) return;
    var version = Number((portalMessagesCache[kind] || {}).version || 0);
    if (version) {
        setPortalSeenVersion(kind, version);
    }
    updatePortalMessageMode();
}

function maybeAutoShowPortalNotice() {
    if (isAdminUser() || !portalMessagesCache || currentView === 'login') return;
    var noticeVersion = Number((portalMessagesCache.notice || {}).version || 0);
    if (!noticeVersion || portalNoticeAutoShownForVersion === noticeVersion) return;
    if (noticeVersion > getPortalSeenVersion('notice')) {
        portalNoticeAutoShownForVersion = noticeVersion;
        showNoticeModal(true);
    }
}

function getPortalMessages() {
    return fetch('/api/portal_messages', {
        method: 'GET',
        headers: getAuthHeaders()
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) {
            throw new Error((result.data && result.data.detail) || '加载通知失败');
        }
        portalMessagesCache = result.data;
        portalMessagesLoaded = true;
        renderPortalMessages();
        updatePortalMessageMode();
        maybeAutoShowPortalNotice();
        return portalMessagesCache;
    })
    .catch(function(err) {
        if (typeof console !== 'undefined' && console.warn) {
            console.warn('portal messages load failed:', err);
        }
        return null;
    });
}

function clearAdminDashboardRefresh() {
    if (adminDashboardRefreshTimer) {
        clearInterval(adminDashboardRefreshTimer);
        adminDashboardRefreshTimer = null;
    }
}

function ensureUserSpaceRefresh() {
    clearAdminDashboardRefresh();
    if (isAdminSpaceView() || !userSpaceDashboardCache || currentView !== 'space') return;
    var meta = userSpaceDashboardCache.meta || {};
    var refreshSeconds = Math.max(15, Number(meta.auto_refresh_seconds || 60));
    adminDashboardRefreshTimer = setInterval(function() {
        if (currentView !== 'space' || isAdminUser()) {
            clearAdminDashboardRefresh();
            return;
        }
        fetchUserSpaceDashboard(true);
    }, refreshSeconds * 1000);
}

function ensureAdminDashboardRefresh() {
    clearAdminDashboardRefresh();
    if (!isAdminSpaceView() || !adminDashboardCache) return;
    var meta = adminDashboardCache.meta || {};
    var refreshSeconds = Math.max(15, Number(meta.auto_refresh_seconds || 60));
    adminDashboardRefreshTimer = setInterval(function() {
        if (!isAdminSpaceView()) {
            clearAdminDashboardRefresh();
            return;
        }
        fetchAdminDashboard(true);
    }, refreshSeconds * 1000);
}

function fetchAdminDashboard(force) {
    if (!isAdminUser()) return Promise.resolve(null);
    if (adminDashboardCache && !force) {
        renderAdminSpaceDashboard();
        ensureAdminDashboardRefresh();
        return Promise.resolve(adminDashboardCache);
    }
    return fetch('/api/admin/dashboard', {
        method: 'GET',
        headers: getAuthHeaders()
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) {
            throw new Error((result.data && result.data.detail) || '加载管理员面板失败');
        }
        adminDashboardCache = result.data;
        renderAdminSpaceDashboard();
        ensureAdminDashboardRefresh();
        return adminDashboardCache;
    })
    .catch(function(err) {
        var container = $r('#space-panel-content');
        if (container) {
            container.innerHTML = '<div class="admin-empty-state">管理员概览加载失败：' + escapeHtml(err.message || '未知错误') + '</div>';
        }
        return null;
    });
}

function getTrainingCorpusDataTypes(modelData) {
    modelData = modelData || {};
    return (modelData.training_corpus_data_types && modelData.training_corpus_data_types.length)
        ? modelData.training_corpus_data_types
        : [
            { label: '原图 target', count: modelData.training_corpus_target_count || 0, unit: '个', ready: (modelData.training_corpus_target_count || 0) > 0 },
            { label: '参考图 reference', count: modelData.training_corpus_reference_count || 0, unit: '个', ready: (modelData.training_corpus_reference_count || 0) > 0 },
            { label: '导出结果 result', count: modelData.training_corpus_result_count || 0, unit: '个', ready: (modelData.training_corpus_result_count || 0) > 0 },
            { label: '满意度评分 rating', count: modelData.training_corpus_rating_count || 0, unit: '条', ready: (modelData.training_corpus_rating_count || 0) > 0 },
            { label: '导出参数 meta', count: modelData.training_corpus_meta_count || 0, unit: '个', ready: (modelData.training_corpus_meta_count || 0) > 0 },
        ];
}

function getTrainingCorpusStatRows(modelData) {
    modelData = modelData || {};
    return [
        ['样本组数', String(modelData.training_corpus_sample_count || 0) + ' 组'],
        ['用户目录', String(modelData.training_corpus_user_count || 0) + ' 个'],
        ['文件总数', String(modelData.training_corpus_file_count || 0) + ' 个'],
        ['占用空间', Number(modelData.training_corpus_size_mb || 0).toFixed(1) + ' MB'],
        ['原图 target', String(modelData.training_corpus_target_count || 0) + ' 个'],
        ['参考图 reference', String(modelData.training_corpus_reference_count || 0) + ' 个'],
        ['导出结果 result', String(modelData.training_corpus_result_count || 0) + ' 个'],
        ['元数据 meta', String(modelData.training_corpus_meta_count || 0) + ' 个'],
    ];
}

function buildTrainingCorpusDataTypesPanel(modelData) {
    var html = '<div class="admin-surface-card admin-training-corpus-types-section">';
    html += '<div class="admin-card-head"><div><div class="admin-card-title">训练数据接入内容</div><div class="admin-card-subtitle">当前训练样本副本包含的文件类型</div></div></div>';
    html += '<div class="admin-category-list admin-training-corpus-type-list">';
    getTrainingCorpusDataTypes(modelData).forEach(function(item) {
        var normalized = (item && typeof item === 'object')
            ? item
            : { label: String(item || '--'), count: null, unit: '', ready: true };
        var hasCount = normalized.count !== null && normalized.count !== undefined;
        var isReady = normalized.ready !== false && (!hasCount || Number(normalized.count || 0) > 0);
        var countText = normalized.count === null || normalized.count === undefined
            ? (isReady ? '已接入' : '未检测到')
            : (isReady ? String(normalized.count) + ' ' + (normalized.unit || '') : '未检测到');
        html += '<div class="admin-category-row">';
        html += '<span class="admin-ring-dot" style="background:' + (isReady ? '#18a999' : '#c6ceda') + ';"></span>';
        html += '<span class="admin-category-label">' + escapeHtml(String(normalized.label || '--')) + '</span>';
        html += '<span class="admin-category-value' + (isReady ? '' : ' muted') + '">' + escapeHtml(countText.trim()) + '</span>';
        html += '</div>';
    });
    html += '</div></div>';
    return html;
}

function buildTrainingCorpusStatsStrip(modelData) {
    var html = '<div class="admin-surface-card admin-training-corpus-stats-section">';
    html += '<div class="admin-card-head"><div><div class="admin-card-title">训练样本副本数据量</div><div class="admin-card-subtitle">按邮箱隔离保存的训练语料统计</div></div></div>';
    html += '<div class="admin-corpus-stat-strip">';
    getTrainingCorpusStatRows(modelData).forEach(function(row) {
        html += '<div class="admin-corpus-stat"><span>' + escapeHtml(row[0]) + '</span><strong>' + escapeHtml(row[1]) + '</strong></div>';
    });
    html += '</div></div>';
    return html;
}

function buildTrainingCorpusSummaryRow(modelData) {
    var html = '<div class="admin-training-corpus-summary-row">';
    html += buildTrainingCorpusDataTypesPanel(modelData);
    html += buildTrainingCorpusStatsStrip(modelData);
    html += '</div>';
    return html;
}

function fetchUserSpaceDashboard(force) {
    if (isAdminUser()) return Promise.resolve(null);
    if (userSpaceDashboardCache && !force) {
        renderUserSpaceDashboard();
        ensureUserSpaceRefresh();
        return Promise.resolve(userSpaceDashboardCache);
    }
    return fetch('/api/projects/space_dashboard_v2', {
        method: 'GET',
        headers: getAuthHeaders()
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) {
            throw new Error((result.data && result.data.detail) || '加载个人空间失败');
        }
        userSpaceDashboardCache = result.data;
        renderUserSpaceDashboard();
        ensureUserSpaceRefresh();
        return userSpaceDashboardCache;
    })
    .catch(function(err) {
        var container = $r('#space-panel-content');
        if (container) {
            container.innerHTML = '<div class="admin-empty-state">个人空间加载失败：' + escapeHtml(err.message || '未知错误') + '</div>';
        }
        return null;
    });
}

function renderAdminSpaceDashboard() {
    var container = $r('#space-panel-content');
    if (!container) return;
    if (!currentUser) {
        container.innerHTML = '<div class="admin-empty-state">请先登录后查看个人空间。</div>';
        return;
    }
    if (!isAdminUser()) {
        renderUserSpaceDashboard();
        return;
    }
    if (!adminDashboardCache) {
        container.innerHTML = '<div class="admin-empty-state">正在加载管理员概览...</div>';
        return;
    }

    var overview = adminDashboardCache.overview || {};
    var users = overview.users || {};
    var projects = overview.projects || {};
    var modelData = overview.model_data || {};
    var taskStats = overview.task_stats || {};
    var cards = adminDashboardCache.cards || [];
    var rings = adminDashboardCache.rings || [];
    var bars = adminDashboardCache.bars || [];
    var progress = adminDashboardCache.progress || [];
    var categories = adminDashboardCache.categories || [];
    var generatedAt = adminDashboardCache.generated_at || overview.generated_at || '';
    var meta = adminDashboardCache.meta || {};

    function formatDelta(delta, unit) {
        if (delta === null || delta === undefined || delta === '') return meta.compare_empty_label || '暂无上周';
        var num = Number(delta);
        var sign = num > 0 ? '+' : '';
        return sign + delta + (unit || '');
    }

    function formatCompareDelta(delta, unit) {
        var text = formatDelta(delta, unit);
        var emptyLabel = meta.compare_empty_label || '暂无上周';
        if (text === emptyLabel) return emptyLabel;
        return (meta.compare_label || '较上周') + ' ' + text;
    }

    function formatCompactDelta(delta, unit) {
        if (delta === null || delta === undefined || delta === '') return '--';
        var num = Number(delta);
        var sign = num > 0 ? '+' : (num < 0 ? '-' : '');
        var value = Math.abs(num).toFixed(1).replace('.0', '');
        return sign + value + (unit || '');
    }

    function formatPercent(value) {
        return (Number(value) || 0).toFixed(1).replace('.0', '') + '%';
    }

    function formatDateLabel(value) {
        if (!value) return '--';
        var raw = String(value).trim();
        var parsed = new Date(raw);
        if (!isNaN(parsed.getTime())) {
            var yyyy = parsed.getFullYear();
            var month = String(parsed.getMonth() + 1).padStart(2, '0');
            var day = String(parsed.getDate()).padStart(2, '0');
            var hh = String(parsed.getHours()).padStart(2, '0');
            var mm = String(parsed.getMinutes()).padStart(2, '0');
            var ss = String(parsed.getSeconds()).padStart(2, '0');
            return yyyy + '-' + month + '-' + day + ' ' + hh + ':' + mm + ':' + ss;
        }
        var text = raw.replace('T', ' ').replace('Z', '');
        var parts = text.split('.');
        if (parts.length >= 1) return parts[0];
        return text.split('.')[0];
    }

    function polarToCartesian(centerX, centerY, radius, angleInDegrees) {
        var angleInRadians = (angleInDegrees - 90) * Math.PI / 180.0;
        return {
            x: centerX + radius * Math.cos(angleInRadians),
            y: centerY + radius * Math.sin(angleInRadians)
        };
    }

    function describeArc(radius, startAngle, endAngle, clockwise) {
        var centerX = 205;
        var centerY = 205;
        var start = polarToCartesian(centerX, centerY, radius, startAngle);
        var end = polarToCartesian(centerX, centerY, radius, endAngle);
        var delta = clockwise
            ? ((endAngle - startAngle) % 360 + 360) % 360
            : ((startAngle - endAngle) % 360 + 360) % 360;
        var largeArcFlag = delta <= 180 ? '0' : '1';
        var sweepFlag = clockwise ? '1' : '0';
        return [
            'M', start.x, start.y,
            'A', radius, radius, 0, largeArcFlag, sweepFlag, end.x, end.y
        ].join(' ');
    }

    function buildSparkline(points, color) {
        var width = 320;
        var height = 72;
        var safe = (points && points.length ? points : [0, 0, 0, 0, 0]).map(function(item) { return Number(item || 0); });
        var max = Math.max.apply(null, safe.concat([1]));
        var min = Math.min.apply(null, safe);
        var span = Math.max(max - min, 1);
        var step = safe.length > 1 ? width / (safe.length - 1) : width;
        var d = '';
        for (var i = 0; i < safe.length; i++) {
            var x = step * i;
            var y = height - ((safe[i] - min) / span) * (height - 10) - 5;
            d += (i === 0 ? 'M' : ' L') + x.toFixed(2) + ' ' + y.toFixed(2);
        }
        return '<svg viewBox="0 0 ' + width + ' ' + height + '" preserveAspectRatio="none">' +
            '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"></path>' +
            '</svg>';
    }

    function buildTrendBars() {
        var maxTask = 1;
        var maxExport = 1;
        bars.forEach(function(item) {
            maxTask = Math.max(maxTask, Number(item.tasks || 0));
            maxExport = Math.max(maxExport, Number(item.exports || 0));
        });
        var html = '<div class="admin-trend-bars">';
        bars.forEach(function(item) {
            var taskHeight = Math.max(34, Math.round((Number(item.tasks || 0) / maxTask) * 210));
            var exportHeight = Math.max(20, Math.round((Number(item.exports || 0) / maxExport) * 96));
            html += '<div class="admin-trend-day">';
            html += '<div class="admin-trend-bar-group">';
            html += '<div class="admin-trend-bar tasks" style="height:' + taskHeight + 'px;"></div>';
            html += '<div class="admin-trend-bar exports" style="height:' + exportHeight + 'px;"></div>';
            html += '</div>';
            html += '<div class="admin-trend-label">' + escapeHtml(item.label || '--') + '</div>';
            html += '</div>';
        });
        html += '</div>';
        return html;
    }

function buildRingSvg() {
        var trackStartAngle = 0;
        var trackEndAngle = 90;
        var activeSpan = 270;
        var radii = [150, 121, 92];
        var strokeWidths = [18, 18, 18];
        var html = '<svg class="admin-ring-svg" viewBox="0 0 410 410">';
        rings.slice(0, 3).forEach(function(item, index) {
            var radius = radii[index] || 96;
            var strokeWidth = strokeWidths[index] || 18;
            var percent = Math.max(0, Math.min(100, Number(item.percent || 0)));
            var activeEndAngle = 360 - activeSpan * (percent / 100);
            html += '<path d="' + describeArc(radius, trackStartAngle, trackEndAngle, false) + '" fill="none" stroke="rgba(226,232,244,0.92)" stroke-width="' + strokeWidth + '" stroke-linecap="round"></path>';
            html += '<path d="' + describeArc(radius, 0, activeEndAngle, false) + '" fill="none" stroke="' + (item.color || '#4f63ff') + '" stroke-width="' + strokeWidth + '" stroke-linecap="round"></path>';
        });
    html += '</svg>';
    return html;
}

function buildAdminTaskLogsPreview() {
    var html = '';
    html += '<div class="admin-log-center-shell">';
    html += '<div class="admin-log-center-head">';
    html += '<div><div class="admin-card-title">任务日志中心</div><div class="admin-card-subtitle">真实运行日志，可按用户名、taskId、任务类型和失败原因检索</div></div>';
    html += '<div class="admin-log-toolbar">';
    html += '<input id="admin-log-search" class="admin-log-search" type="text" placeholder="搜索用户 / taskId / 失败关键词" />';
    html += '<select id="admin-log-status" class="admin-log-filter"><option value="all">全部状态</option><option value="fail">仅失败</option><option value="ok">仅成功</option><option value="cancel">仅取消</option></select>';
    html += '<select id="admin-log-type" class="admin-log-filter"><option value="all">全部类型</option><option value="模型训练">模型训练</option><option value="图片追色">图片追色</option><option value="视频追色">视频追色</option><option value="导出">导出</option><option value="任务控制">任务控制</option></select>';
    html += '<button id="admin-log-backfill" class="admin-chip" type="button">回填历史日志</button>';
    html += '<button id="admin-log-refresh" class="admin-chip" type="button">刷新</button>';
    html += '</div></div>';
    html += '<div id="admin-log-alerts" class="admin-log-alerts"></div>';
    html += '<div class="admin-log-table-shell">';
    html += '<div class="admin-log-table-head">';
    html += '<span>时间</span><span>昵称</span><span>邮箱</span><span>任务类型</span><span>模型</span><span>状态</span><span>失败原因 / 摘要</span>';
    html += '</div>';
    html += '<div id="admin-log-table-body" class="admin-log-table-body">';
    html += '<div class="admin-log-table-row is-empty"><span>--</span><span>--</span><span>--</span><span>--</span><span><em class="admin-log-status">加载中</em></span><span>正在读取真实任务日志...</span></div>';
    html += '</div>';
    html += '</div>';
    html += '<div id="admin-log-detail" class="admin-log-detail">';
    html += '<strong>选中日志详情</strong>';
    html += '<span>点击上方某条真实日志后，这里会显示 task_id、失败详情、资源快照和额外上下文。</span>';
    html += '<div id="admin-log-detail-grid" class="admin-log-detail-grid"></div>';
    html += '</div>';
    html += '</div>';
    return html;
}

function formatAdminLogTime(value) {
    if (!value) return '--';
    var raw = String(value).replace('T', ' ').replace('Z', '');
    return raw.length > 16 ? raw.slice(5, 16) : raw;
}

function renderAdminTaskLogDetail(entry) {
    var detail = $r('#admin-log-detail');
    var grid = $r('#admin-log-detail-grid');
    if (!detail || !grid) return;
    if (!entry) {
        detail.querySelector('span').textContent = '点击上方某条真实日志后，这里会显示 task_id、昵称、邮箱、耗时、资源快照和扩展参数。';
        grid.innerHTML = '';
        return;
    }

    function formatResourceValue(value) {
        return value === null || value === undefined || value === '' ? '--' : String(value);
    }

    function formatAdminMetaValue(key, value) {
        if (value === null || value === undefined || value === '') return '--';
        if (typeof value === 'boolean') return value ? '开启' : '关闭';
        if (key === 'training_size_mb' || key === 'export_size_mb') return String(value) + ' MB';
        if (key === 'epochs' || key === 'batch_size' || key === 'training_file_count' || key === 'project_id') return String(value);
        if (key === 'lr') return String(value);
        if (key === 'source') {
            if (value === 'frontend_export') return '前端导出';
            if (value === 'apply_profile') return '配置应用';
            if (value === 'apply_style') return '风格应用';
        }
        if (key === 'export_size_bytes') {
            var num = Number(value);
            if (!isNaN(num)) {
                if (num >= 1024 * 1024) return (num / 1024 / 1024).toFixed(2) + ' MB';
                if (num >= 1024) return (num / 1024).toFixed(1) + ' KB';
                return Math.round(num) + ' B';
            }
        }
        return String(value);
    }

    var timing = entry.timing || {};
    var display = entry.display || {};
    var user = entry.user || {};
    var durationValue = timing.duration_ms;
    if (durationValue === null || durationValue === undefined) durationValue = entry.duration_ms;
    var durationText = durationValue === null || durationValue === undefined
        ? '未记录'
        : (Math.round(Number(durationValue) / 100) / 10) + 's';
    var resource = entry.resource || {};
    var meta = entry.meta_raw || entry.meta || {};
    var metaDisplay = Array.isArray(entry.meta_display) ? entry.meta_display : [];
    var userLabel = user.display_name || entry.user_label || '--';
    var userEmail = user.email || entry.email || '--';
    var prettyMeta = {
        enable_metrics: '质量评估',
        enable_postprocess: '智能后处理',
        enable_scene_detect: '场景检测',
        stage: '训练阶段',
        epochs: 'Epoch',
        batch_size: 'Batch Size',
        lr: '学习率',
        training_file_count: '训练图数',
        training_size_mb: '训练数据量',
        export_format: '导出格式',
        size_mode: '尺寸模式',
        export_size_bytes: '导出体积(字节)',
        export_file_count: '导出文件数',
        project_id: '项目编号',
        file_name: '文件名',
        source_image_key: '源图标识',
        source: '来源',
        user_email: '用户邮箱',
        user_account: '用户账号',
        bitrate: '码率',
        resolution: '分辨率',
        fps: '帧率',
        export_path: '导出路径'
    };
    var metaItems = metaDisplay.length
        ? metaDisplay.map(function(item) {
            return '<div class="admin-log-detail-kv"><strong>' + escapeHtml(item.label || '--') + '</strong><span>' + escapeHtml(item.value || '--') + '</span></div>';
        }).join('')
        : Object.keys(meta).map(function(key) {
            var value = meta[key];
            return '<div class="admin-log-detail-kv"><strong>' + escapeHtml(prettyMeta[key] || key) + '</strong><span>' + escapeHtml(formatAdminMetaValue(key, value)) + '</span></div>';
        }).join('');
    var summaryText = display.summary || entry.summary || '';
    var detailText = display.detail || entry.detail || '';
    if (detailText === 'image_export') detailText = '图片导出';
    else if (detailText === 'video_export') detailText = '视频导出';
    else if (!detailText && summaryText) detailText = summaryText;
    detail.querySelector('span').textContent =
        'task_id: ' + (entry.task_id || '--') +
        ' | 用户: ' + userLabel +
        ' | 邮箱: ' + userEmail +
        ' | 结果: ' + (entry.status || '--') +
        ' | 摘要: ' + (summaryText || '--');
    grid.innerHTML =
        '<div class="admin-log-detail-card"><strong>昵称</strong><span>' + escapeHtml(userLabel) + '</span></div>' +
        '<div class="admin-log-detail-card"><strong>邮箱</strong><span>' + escapeHtml(userEmail) + '</span></div>' +
        '<div class="admin-log-detail-card"><strong>耗时</strong><span>' + escapeHtml(durationText) + '</span></div>' +
        '<div class="admin-log-detail-card"><strong>资源快照</strong><span>' +
            (entry.resource_live_fallback ? '当前资源 ' : '') +
            '磁盘 ' + escapeHtml(formatResourceValue(resource.disk_used_percent)) + '% / ' +
            '内存 ' + escapeHtml(formatResourceValue(resource.memory_used_percent)) + '% / ' +
            'CPU ' + escapeHtml(formatResourceValue(resource.cpu_used_percent)) + '%</span></div>' +
        '<div class="admin-log-detail-card admin-log-detail-meta"><strong>扩展参数</strong><div>' + (metaItems || '<span>--</span>') + '</div></div>' +
        '<div class="admin-log-detail-card"><strong>原始详情</strong><span>' + escapeHtml(detailText || '--') + '</span></div>';
}

function renderAdminTaskLogsSection(data) {
    var alertsEl = $r('#admin-log-alerts');
    var bodyEl = $r('#admin-log-table-body');
    if (!alertsEl || !bodyEl) return;

    var alerts = data && data.alerts ? data.alerts : [];
    var logs = data && data.logs ? data.logs : [];
    var resourceSummary = data && data.resource_summary ? data.resource_summary : null;

    var priorityTypes = ['disk', 'cpu'];
    var pinnedAlerts = [];
    var otherAlerts = [];
    alerts.forEach(function(alert) {
        if (priorityTypes.indexOf(String(alert.type || '').toLowerCase()) >= 0) pinnedAlerts.push(alert);
        else otherAlerts.push(alert);
    });
    pinnedAlerts.sort(function(a, b) {
        return priorityTypes.indexOf(String(a.type || '').toLowerCase()) - priorityTypes.indexOf(String(b.type || '').toLowerCase());
    });

    function renderAlertItem(alert) {
        return '<div class="admin-log-alert ' + escapeHtml(alert.level || 'info') + '"><strong>' +
            escapeHtml(alert.title || '--') + '</strong><span>' + escapeHtml(alert.message || '--') + '</span></div>';
    }

    var alertsHtml = alerts.length ? (
        pinnedAlerts.map(renderAlertItem).join('') +
        otherAlerts.map(renderAlertItem).join('')
    ) : '<div class="admin-log-alert info"><strong>当前无系统告警</strong><span>磁盘、内存、CPU 与活跃任务状态都在安全区间内。</span></div>';

    if (resourceSummary) {
        var diskValue = resourceSummary.disk_used_percent === null || resourceSummary.disk_used_percent === undefined ? '--' : resourceSummary.disk_used_percent;
        var memoryValue = resourceSummary.memory_used_percent === null || resourceSummary.memory_used_percent === undefined ? '--' : resourceSummary.memory_used_percent;
        var cpuValue = resourceSummary.cpu_used_percent === null || resourceSummary.cpu_used_percent === undefined ? '--' : resourceSummary.cpu_used_percent;
        alertsHtml += '<div class="admin-log-alert admin-log-alert-resource info"><strong>资源快照</strong><span>当前资源 磁盘 ' +
            escapeHtml(String(diskValue)) + '% / 内存 ' + escapeHtml(String(memoryValue)) + '% / CPU ' + escapeHtml(String(cpuValue)) + '%</span></div>';
    }
    alertsEl.innerHTML = alertsHtml;

    if (!logs.length) {
        bodyEl.innerHTML = '<div class="admin-log-table-row is-empty"><span>--</span><span>--</span><span>--</span><span>--</span><span><em class="admin-log-status">空</em></span><span>当前筛选条件下没有匹配日志</span></div>';
        renderAdminTaskLogDetail(null);
        return;
    }

    bodyEl.innerHTML = logs.map(function(entry, index) {
        var user = entry.user || {};
        var display = entry.display || {};
        var statusText = entry.status === 'ok' ? '成功' : (entry.status === 'cancel' ? '取消' : (entry.status === 'fail' ? '失败' : '记录'));
        return '<button type="button" class="admin-log-table-row admin-log-row-btn" data-log-index="' + index + '">' +
            '<span>' + escapeHtml(formatAdminLogTime(entry.created_at)) + '</span>' +
            '<span>' + escapeHtml(user.display_name || entry.user_label || '--') + '</span>' +
            '<span>' + escapeHtml(user.email || entry.email || '--') + '</span>' +
            '<span>' + escapeHtml(entry.task_type || '--') + '</span>' +
            '<span>' + escapeHtml(entry.model || '--') + '</span>' +
            '<span><em class="admin-log-status ' + escapeHtml(entry.status || 'info') + '">' + statusText + '</em></span>' +
            '<span>' + escapeHtml(display.summary || entry.summary || '--') + '</span>' +
            '</button>';
    }).join('');

    bodyEl.querySelectorAll('.admin-log-row-btn').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var idx = Number(btn.getAttribute('data-log-index'));
            renderAdminTaskLogDetail(logs[idx] || null);
        });
    });

    renderAdminTaskLogDetail(logs[0]);
}

function fetchAdminTaskLogs() {
    var query = (($r('#admin-log-search') || {}).value || '').trim();
    var status = (($r('#admin-log-status') || {}).value || 'all').trim();
    var taskType = (($r('#admin-log-type') || {}).value || 'all').trim();
    var url = '/api/admin/task_logs?query=' + encodeURIComponent(query) +
        '&status=' + encodeURIComponent(status) +
        '&task_type=' + encodeURIComponent(taskType) +
        '&limit=50';
    return fetch(url, {
        method: 'GET',
        headers: getAuthHeaders()
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) {
            throw new Error((result.data && result.data.detail) || '任务日志加载失败');
        }
        renderAdminTaskLogsSection(result.data);
        return result.data;
    })
    .catch(function(err) {
        var bodyEl = $r('#admin-log-table-body');
        if (bodyEl) {
            bodyEl.innerHTML = '<div class="admin-log-table-row is-empty"><span>--</span><span>--</span><span>--</span><span>--</span><span>--</span><span><em class="admin-log-status fail">失败</em></span><span>' + escapeHtml(err.message || '任务日志加载失败') + '</span></div>';
        }
        renderAdminTaskLogDetail(null);
        return null;
    });
}

function runAdminTaskLogsBackfill() {
    var btn = $r('#admin-log-backfill');
    if (btn) {
        btn.disabled = true;
        btn.dataset.loading = '1';
        btn.textContent = '回填中...';
    }
    return fetch('/api/admin/task_logs/backfill', {
        method: 'POST',
        headers: getAuthHeaders()
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) {
            throw new Error((result.data && result.data.detail) || '历史日志回填失败');
        }
        if (typeof showToast === 'function') {
            showToast((result.data && result.data.message) || '历史日志回填完成');
        }
        return fetchAdminTaskLogs();
    })
    .catch(function(err) {
        if (typeof showToast === 'function') {
            showToast(err.message || '历史日志回填失败');
        }
        return null;
    })
    .finally(function() {
        if (btn) {
            btn.disabled = false;
            delete btn.dataset.loading;
            btn.textContent = '回填历史日志';
        }
    });
}

    var cardClasses = ['is-lilac', 'is-cyan', 'is-peach', 'is-mint'];
    var sparkColors = ['#b8a8ff', '#88ddf3', '#ffb39a', '#a8aef9'];
    var healthScore = Number(meta.health_score);
    if (!isFinite(healthScore)) {
        var ringPercents = rings.map(function(item) { return Number(item.percent || 0); });
        var ringTotalPercent = 0;
        ringPercents.forEach(function(value) { ringTotalPercent += value; });
        healthScore = ringPercents.length ? (ringTotalPercent / ringPercents.length) : 0;
    }

    var logLines = [
        '近 7 日活跃用户 ' + (users.active_7d || 0) + ' / ' + (users.total || 0) + '，活跃占比 ' + formatPercent(users.total ? users.active_7d / users.total * 100 : 0) + '。',
        '任务完成率 ' + formatPercent(taskStats.task_completion_rate || 0) + '，任务成功率 ' + formatPercent(taskStats.task_success_rate || 0) + '，任务失败率 ' + formatPercent(taskStats.task_failure_rate || 0) + '。',
        '模型就绪 ' + (modelData.ready_models || 0) + ' / ' + (modelData.total_models || 0) + '，累计模型调用 ' + (taskStats.model_calls_total || 0) + ' 次。'
    ];

    var html = '<div class="admin-center-shell">';
    html += '<div class="admin-center-topbar">';
    html += '<div class="admin-center-brand"><img class="admin-center-brand-logo" src="/static/assets/logo.png" alt="ColorChase"><span class="admin-center-brand-mark">Admin Center</span></div>';
    html += '<div class="admin-center-tabs">';
    html += '<button class="admin-center-tab active" type="button">Dashboard</button>';
    html += '<button class="admin-center-tab" type="button">Models</button>';
    html += '<button class="admin-center-tab" type="button">Training</button>';
    html += '<button class="admin-center-tab" type="button">Exports</button>';
    html += '</div>';
    html += '<div class="admin-center-tools"><span>最近更新: ' + escapeHtml(formatDateLabel(generatedAt)) + ' | ' + escapeHtml(meta.weekly_refresh_rule || 'Asia/Shanghai 每周一 08:00') + ' | 自动刷新 ' + escapeHtml(String(meta.auto_refresh_seconds || 60)) + 's</span><button class="admin-center-refresh" id="admin-refresh-btn" type="button">刷新</button></div>';
    html += '</div>';
    html += '<div class="admin-dashboard-shell-wrap">';
    html += '<div class="admin-dashboard-top">';
    html += '<div class="admin-surface-card">';
    html += '<div class="admin-card-head"><div><div class="admin-card-title">Today Task</div><div class="admin-card-subtitle">管理员核心指标与新增变化</div></div><span class="admin-chip">Admin</span></div>';
    html += '<div class="admin-task-grid">';
    cards.forEach(function(item, index) {
        html += '<div class="admin-task-card ' + cardClasses[index % cardClasses.length] + '">';
        html += '<div class="admin-task-index">' + String(index + 1).padStart(2, '0') + '</div>';
        html += '<div class="admin-task-title">' + escapeHtml(item.label || '--') + '</div>';
        html += '<div class="admin-task-value-row"><div class="admin-task-value">' + escapeHtml(item.display_value || String(item.value || 0) + (item.unit || '')) + '</div><div class="admin-task-delta">' + escapeHtml(formatCompactDelta(item.delta, item.unit || '')) + '</div></div>';
        html += '<div class="admin-task-sparkline">' + buildSparkline(item.sparkline || [], sparkColors[index % sparkColors.length]) + '</div>';
        html += '</div>';
    });
    html += '</div></div>';
    html += '<div class="admin-surface-card compact admin-health-card">';
    html += '<div class="admin-card-head"><div><div class="admin-card-title">系统运行健康程度</div><div class="admin-card-subtitle">任务完成率、模型就绪率、任务成功率</div></div><span class="admin-chip">Today</span></div>';
    html += '<div class="admin-ring-wrap">';
    html += '<div class="admin-ring-canvas">' + buildRingSvg();
    html += '<div class="admin-ring-center-copy"><div class="admin-ring-total">' + escapeHtml(formatPercent(healthScore)) + '</div><div class="admin-ring-caption">' + escapeHtml(meta.health_score_label || '系统健康度') + '</div><div class="admin-ring-live">Live</div></div></div>';
    html += '<div class="admin-ring-stats">';
    rings.forEach(function(item) {
        html += '<div class="admin-ring-stat">';
        html += '<span class="admin-ring-dot" style="background:' + (item.color || '#4f63ff') + ';"></span>';
        html += '<span class="admin-ring-label">' + escapeHtml(item.label || '--') + '</span>';
        html += '<span class="admin-ring-number">' + escapeHtml(String(item.value || 0)) + '</span>';
        html += '<span class="admin-ring-badge">' + formatPercent(item.percent || 0) + '</span>';
        html += '</div>';
    });
    html += '</div></div></div>';
    html += '</div>';
    html += '<div class="admin-dashboard-shell">';
    html += '<div class="admin-surface-card admin-lower-trend">';
    html += '<div class="admin-card-head"><div><div class="admin-card-title">近 7 日任务趋势</div><div class="admin-card-subtitle">任务与导出变化</div></div><span class="admin-chip">This Week</span></div>';
    html += '<div class="admin-trend-layout">';
    html += '<div class="admin-trend-legend"><span class="admin-legend-item"><span class="admin-legend-dot" style="background:#4f63ff;"></span>任务</span><span class="admin-legend-item"><span class="admin-legend-dot" style="background:#47c5e7;"></span>导出</span></div>';
    html += buildTrendBars();
    html += '</div>';
    html += '</div>';
    html += '<div class="admin-dashboard-side">';
    html += '<div class="admin-dashboard-side-top">';
    html += '<div class="admin-surface-card compact admin-lower-metrics">';
    html += '<div class="admin-card-head"><div><div class="admin-card-title">管理员辅助指标</div><div class="admin-card-subtitle">真实数据指标占比</div></div><span class="admin-chip">Live</span></div>';
    html += '<div class="admin-mini-columns">';
    progress.forEach(function(item) {
        var value = Math.max(0, Math.min(100, Number(item.value || 0)));
        html += '<div class="admin-mini-column">';
        html += '<div class="admin-mini-rail"><div class="admin-mini-fill" style="height:' + value + '%; background:' + (item.color || '#4f63ff') + ';"></div></div>';
        html += '<div class="admin-mini-label">' + escapeHtml(item.label || '--') + '</div>';
        html += '<div class="admin-mini-value">' + formatPercent(value) + '</div>';
        html += '</div>';
    });
    html += '</div></div>';
    html += '<div class="admin-surface-card compact admin-lower-category">';
    html += '<div class="admin-card-head"><div><div class="admin-card-title">资源与资产分类</div><div class="admin-card-subtitle">当前项目资源总览</div></div></div>';
    html += '<div class="admin-category-list">';
    categories.forEach(function(item) {
        html += '<div class="admin-category-row">';
        html += '<span class="admin-ring-dot" style="background:' + (item.color || '#4f63ff') + ';"></span>';
        html += '<span class="admin-category-label">' + escapeHtml(item.label || '--') + '</span>';
        html += '<span class="admin-category-value">' + escapeHtml(String(item.value || '--')) + '</span>';
        html += '</div>';
    });
    html += '</div></div>';
    html += '</div>';
    html += '<div class="admin-surface-card admin-lower-log">';
    html += '<div class="admin-card-head"><div><div class="admin-card-title">系统日志</div><div class="admin-card-subtitle">管理员视角关键摘要</div></div></div>';
    html += '<div class="admin-log-list">';
    logLines.forEach(function(line) {
        html += '<div class="admin-log-item">' + escapeHtml(line) + '</div>';
    });
    html += '</div></div>';
    html += '</div></div>';
    html += '</div></div>';
    html += buildTrainingCorpusSummaryRow(modelData);
    html += '<div class="admin-surface-card admin-task-log-card">';
    html += buildAdminTaskLogsPreview();
    html += '</div>';

    container.innerHTML = html;
    var trainingCount = $r('#training-data-count');
    if (trainingCount) {
        trainingCount.textContent = (modelData.training_file_count || 0) + ' 张 / ' + (modelData.training_size_mb || 0).toFixed(1) + ' MB';
    }
    var refreshBtn = $r('#admin-refresh-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', function() {
            fetchAdminDashboard(true);
        });
    }

    var logRefreshBtn = $r('#admin-log-refresh');
    if (logRefreshBtn && !logRefreshBtn.dataset.bound) {
        logRefreshBtn.dataset.bound = '1';
        logRefreshBtn.addEventListener('click', function() {
            fetchAdminTaskLogs();
        });
    }
    var logBackfillBtn = $r('#admin-log-backfill');
    if (logBackfillBtn && !logBackfillBtn.dataset.bound) {
        logBackfillBtn.dataset.bound = '1';
        logBackfillBtn.addEventListener('click', function() {
            runAdminTaskLogsBackfill();
        });
    }
    ['#admin-log-search', '#admin-log-status', '#admin-log-type'].forEach(function(sel) {
        var el = $r(sel);
        if (el && !el.dataset.bound) {
            el.dataset.bound = '1';
            el.addEventListener('input', function() {
                fetchAdminTaskLogs();
            });
            el.addEventListener('change', function() {
                fetchAdminTaskLogs();
            });
        }
    });
    fetchAdminTaskLogs();
}

function formatUserSpacePercent(value) {
    return (Number(value) || 0).toFixed(1).replace('.0', '') + '%';
}

function formatUserSpaceDate(value) {
    if (!value) return '--';
    var raw = String(value).trim();
    var parsed = new Date(raw);
    if (!isNaN(parsed.getTime())) {
        var yyyy = parsed.getFullYear();
        var month = String(parsed.getMonth() + 1).padStart(2, '0');
        var day = String(parsed.getDate()).padStart(2, '0');
        var hh = String(parsed.getHours()).padStart(2, '0');
        var mm = String(parsed.getMinutes()).padStart(2, '0');
        var ss = String(parsed.getSeconds()).padStart(2, '0');
        return yyyy + '-' + month + '-' + day + ' ' + hh + ':' + mm + ':' + ss;
    }
    return raw.replace('T', ' ').replace('Z', '').split('.')[0];
}

function buildUserSpaceSparkline(points, color) {
    var values = (points && points.length ? points : [0, 0, 0, 0, 0]).map(function(item) {
        return Number(item || 0);
    });
    var width = 180;
    var height = 64;
    var max = Math.max.apply(null, values.concat([1]));
    var min = Math.min.apply(null, values);
    var span = Math.max(max - min, 1);
    var step = values.length > 1 ? width / (values.length - 1) : width;
    var d = '';
    for (var i = 0; i < values.length; i++) {
        var x = step * i;
        var y = height - 1 - ((values[i] - min) / span) * (height - 28);
        d += (i === 0 ? 'M' : ' L') + x.toFixed(2) + ' ' + y.toFixed(2);
    }
    return '<svg viewBox="0 0 ' + width + ' ' + height + '" preserveAspectRatio="none">' +
        '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"></path>' +
        '</svg>';
}

function buildUserSpaceTrendBars(items) {
    var list = items || [];
    var maxTask = 1;
    var maxExport = 1;
    list.forEach(function(item) {
        maxTask = Math.max(maxTask, Number(item.tasks || 0));
        maxExport = Math.max(maxExport, Number(item.exports || 0));
    });
    var html = '<div class="user-space-trend-chart-shell"><div class="user-space-trend-bars">';
    list.forEach(function(item) {
        var taskValue = Number(item.tasks || 0);
        var exportValue = Number(item.exports || 0);
        var taskHeight = taskValue > 0 ? Math.max(20, Math.round((taskValue / maxTask) * 176)) : 0;
        var exportHeight = exportValue > 0 ? Math.max(12, Math.round((exportValue / maxExport) * 132)) : 0;
        html += '<div class="user-space-trend-day">';
        html += '<div class="user-space-trend-bar-stack">';
        html += '<div class="user-space-trend-bar user-space-trend-task" style="height:' + taskHeight + 'px;"></div>';
        html += '<div class="user-space-trend-bar user-space-trend-export" style="height:' + exportHeight + 'px;"></div>';
        html += '</div>';
        html += '<div class="user-space-trend-day-label">' + escapeHtml(item.label || '--') + '</div>';
        html += '</div>';
    });
    html += '</div></div>';
    return html;
}

function fetchUserVisibleModelStatus() {
    return fetch('/api/model_status', { method: 'GET', cache: 'no-store' })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) throw new Error((result.data && result.data.detail) || '读取模型状态失败');
        renderUserSpaceModelStatus(result.data || {});
        return result.data || {};
    })
    .catch(function() {
        renderUserSpaceModelStatus(null);
        return null;
    });
}

function renderUserSpaceModelStatus(data) {
    var summaryEl = $r('#user-space-model-status-summary');
    var listEl = $r('#user-space-model-status-list');
    var deviceEl = $r('#user-space-model-status-device');
    if (!summaryEl || !listEl) return;
    if (!data || !Array.isArray(data.models)) {
        summaryEl.textContent = '暂时无法读取模型状态';
        listEl.innerHTML = '<div class="user-space-model-empty">模型状态读取失败</div>';
        if (deviceEl) deviceEl.textContent = 'unknown';
        return;
    }

    var summary = data.summary || {};
    var management = data.management || {};
    var totalModels = Number(summary.total || data.models.length || 0);
    var readyModels = Number(summary.ready || 0);
    var readyRate = summary.ready_rate;
    if (readyRate === undefined || readyRate === null || readyRate === '') {
        readyRate = totalModels > 0 ? (readyModels / totalModels * 100) : 0;
    }
    summaryEl.textContent = '模型数 ' + totalModels + ' 个 · 就绪率 ' + formatUserSpacePercent(readyRate) +
        ' · 可用 ' + readyModels + ' 个' +
        (management.default_model ? ' · 默认 ' + management.default_model : '');
    if (deviceEl) deviceEl.textContent = (data.device || 'unknown') + (data.device_label ? ' · ' + data.device_label : '');

    listEl.innerHTML = data.models.map(function(model) {
        var flags = [];
        if (model.is_default) flags.push('默认');
        if (model.enabled === false) flags.push('已禁用');
        if (model.benchmark && model.benchmark.elapsed_ms !== undefined) flags.push('benchmark ' + Number(model.benchmark.elapsed_ms || 0).toFixed(1) + ' ms');
        var note = getModelWeightSummary(model, model.note || '');
        if (flags.length) note += ' · ' + flags.join(' · ');
        return '<div class="user-space-model-status-item ' + getTrainingModelStatusClass(model) + '">' +
            '<div class="user-space-model-status-main">' +
                '<em>' + escapeHtml(getTrainingModelInitial(model)) + '</em>' +
                '<div><strong>' + escapeHtml(model.name || model.key || '未知模型') + '</strong>' +
                '<span>' + escapeHtml(note) + '</span></div>' +
            '</div>' +
            '<b>' + escapeHtml(getTrainingModelStatusLabel(model)) + '</b>' +
        '</div>';
    }).join('');
}

function openUserSpaceProject(projectId, projectType) {
    if (!projectId) return;
    window.currentProjectId = Number(projectId);
    window._pendingProjectType = projectType === 'video' ? 'video' : 'image';
    rNavigate('workspace');
}

function renderUserSpaceShell(dashboardData) {
    var container = $r('#space-panel-content');
    if (!container) return;

    var profile = dashboardData.profile || {};
    var cards = dashboardData.cards || [];
    var bars = dashboardData.bars || [];
    var taskDashboard = dashboardData.task_dashboard || {};
    var resources = dashboardData.resources || [];
    var taskCenter = normalizeUserSpaceTaskCenter((dashboardData.task_center || {}).items || dashboardData.task_center || dashboardData.recent_logs || []);
    var preferences = dashboardData.preferences || {};
    var history = dashboardData.history || {};
    var account = dashboardData.account || {};
    var logs = dashboardData.logs || [];
    var meta = dashboardData.meta || {};
    var generatedAt = dashboardData.generated_at || '';
    var recentProjects = history.recent_projects || [];
    var recentExports = history.recent_exports || [];
    var recentModelRecords = history.recent_model_records || [];
    var modelShare = taskDashboard.model_share || preferences.model_share || [];
    var sparkColors = ['#7b6df6', '#2cbdd3', '#ff8f6b', '#4f67f7', '#6bce91'];
    var avatarHtml = profile.avatar_url
        ? '<img class="user-space-avatar-image" src="' + escapeAttr(profile.avatar_url) + '" alt="头像">'
        : escapeHtml(profile.avatar_text || 'U');

    var html = '<div class="user-space-shell">';
    html += '<div class="admin-center-topbar user-space-topbar">';
    html += '<div class="admin-center-brand"><img class="admin-center-brand-logo" src="/static/assets/logo.png" alt="ColorChase"><span class="admin-center-brand-mark">User Space</span></div>';
    html += '<div class="user-space-topbar-copy"><div class="user-space-topbar-title">个人空间</div><div class="user-space-topbar-subtitle">只展示你自己的任务、资产、偏好和历史记录</div></div>';
    html += '<div class="admin-center-tools"><span>最近更新: ' + escapeHtml(formatUserSpaceDate(generatedAt)) + ' | ' + escapeHtml(meta.weekly_refresh_rule || '个人空间自动更新') + ' | 自动刷新 ' + escapeHtml(String(meta.auto_refresh_seconds || 60)) + 's</span><button class="admin-center-refresh" id="user-space-refresh" type="button">刷新</button></div>';
    html += '</div>';
    html += '<div class="user-space-overview-grid">';
    html += '<section class="admin-surface-card user-space-profile-card">';
    html += '<div class="user-space-profile-head"><div class="user-space-avatar-wrap"><div class="user-space-avatar">' + avatarHtml + '</div><label class="user-space-avatar-upload" for="user-space-avatar-input">更换头像</label><input id="user-space-avatar-input" type="file" accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp" hidden></div><div class="user-space-profile-copy"><div class="user-space-profile-name-row"><div class="user-space-profile-name">' + escapeHtml(profile.display_name || '当前用户') + '</div><button id="user-space-edit-profile-btn" class="user-space-inline-edit-btn" type="button">编辑资料</button></div><div class="user-space-profile-role">' + escapeHtml(profile.account_type || '普通用户') + '</div><div class="user-space-profile-id">' + escapeHtml(profile.account_id || '--') + '</div><div id="user-space-profile-editor" class="user-space-profile-editor" style="display:none;"><input id="user-space-nickname-input" class="user-space-profile-input" type="text" maxlength="24" placeholder="输入昵称" value="' + escapeAttr(profile.display_name || '') + '"><div class="user-space-profile-actions"><button id="user-space-profile-save" class="user-space-action-btn" type="button">保存</button><button id="user-space-profile-cancel" class="user-space-action-btn is-ghost" type="button">取消</button></div></div></div><div class="user-space-profile-health"><div class="user-space-profile-health-value">' + escapeHtml(formatUserSpacePercent(meta.health_score || 0)) + '</div><div class="user-space-profile-health-label">' + escapeHtml(meta.health_score_label || '个人健康度') + '</div></div></div>';
    html += '<div class="user-space-profile-meta"><div class="user-space-profile-meta-item"><span>注册时间</span><strong>' + escapeHtml(profile.created_at || '--') + '</strong></div><div class="user-space-profile-meta-item"><span>最近登录</span><strong>' + escapeHtml(profile.last_login_at || '--') + '</strong></div></div>';
    html += '<div class="user-space-profile-summary">' + escapeHtml(meta.health_score_caption || '个人使用状态预估') + '</div>';
    html += '</section><section class="user-space-core-grid">';
    cards.forEach(function(item, index) {
        html += '<article class="admin-task-card user-space-core-card"><div class="user-space-core-label">' + escapeHtml(item.label || '--') + '</div><div class="user-space-core-value">' + escapeHtml(item.display_value || String(item.value == null ? 0 : item.value) + (item.unit || '')) + '</div><div class="user-space-core-sparkline">' + buildUserSpaceSparkline(item.sparkline || [], sparkColors[index % sparkColors.length]) + '</div></article>';
    });
    html += '</section></div>';
    html += '<div class="user-space-main-grid"><section class="admin-surface-card user-space-trend-card"><div class="admin-card-head"><div><div class="admin-card-title">用户视角小仪表盘</div><div class="admin-card-subtitle">近 7 日任务趋势、近 7 日导出趋势</div></div><span class="admin-chip">7 Days</span></div><div class="user-space-trend-legend"><span><i class="user-space-legend-dot is-task"></i>任务</span><span><i class="user-space-legend-dot is-export"></i>导出</span></div>' + buildUserSpaceTrendBars(bars) + '</section>';
    html += '<div class="user-space-side-grid"><section class="admin-surface-card user-space-insight-card"><div class="admin-card-head"><div><div class="admin-card-title">任务结果与模型占比</div><div class="admin-card-subtitle">普通用户最常看的任务结果和常用模型</div></div></div><div class="user-space-kpi-grid"><div class="user-space-kpi"><span>操作任务</span><strong>' + escapeHtml(String(taskDashboard.operated_task_count || 0) + ' 个') + '</strong></div><div class="user-space-kpi"><span>任务导出率</span><strong>' + escapeHtml(formatUserSpacePercent(taskDashboard.task_export_rate || 0)) + '</strong></div><div class="user-space-kpi"><span>任务失败率</span><strong>' + escapeHtml(formatUserSpacePercent(taskDashboard.task_failure_rate || 0)) + '</strong></div></div><div class="user-space-share-list">';
    if (modelShare.length) {
        modelShare.forEach(function(item) {
            html += '<div class="user-space-share-row"><div class="user-space-share-copy"><span class="user-space-share-name">' + escapeHtml(item.label || '--') + '</span><span class="user-space-share-value">' + escapeHtml(formatUserSpacePercent(item.percent || 0)) + '</span></div><div class="user-space-share-track"><div class="user-space-share-fill" style="width:' + Math.max(0, Math.min(100, Number(item.percent || 0))) + '%; background:' + escapeHtml(item.color || '#5166f5') + ';"></div></div></div>';
        });
    } else {
        html += '<div class="admin-empty-state user-space-inline-empty">暂无模型/算法使用记录</div>';
    }
    html += '</div></section><section class="admin-surface-card user-space-resource-card"><div class="admin-card-head"><div><div class="admin-card-title">我的资源与资产</div><div class="admin-card-subtitle">原图、参考图、导出占用和项目资产概览</div></div></div><div class="user-space-resource-grid">';
    resources.forEach(function(item) {
        html += '<div class="user-space-resource-item"><span class="user-space-resource-label">' + escapeHtml(item.label || '--') + '</span><strong>' + escapeHtml(item.value || '--') + '</strong><em>' + escapeHtml(item.size || '') + '</em></div>';
    });
    html += '</div></section></div></div>';
    html += '<div class="user-space-bottom-grid"><section class="admin-surface-card user-space-task-card"><div class="admin-card-head"><div><div class="admin-card-title">我的任务中心</div><div class="admin-card-subtitle">最近任务状态、失败原因摘要与快捷操作</div></div><span class="admin-chip">Tasks</span></div>';
    if (taskCenter.length) {
        html += '<div class="user-space-task-list">';
        taskCenter.forEach(function(item, index) {
            html += '<div class="user-space-task-item"><div class="user-space-task-main"><div class="user-space-task-meta"><span class="user-space-task-type">' + escapeHtml(item.task_type || '--') + '</span><span class="admin-log-status ' + escapeHtml(item.status || 'info') + '">' + escapeHtml(item.status_label || '--') + '</span><span class="user-space-task-time">' + escapeHtml(item.created_at || '--') + '</span></div><div class="user-space-task-summary">' + escapeHtml(item.summary || '--') + '</div>' + (item.failure_reason ? '<div class="user-space-task-failure">失败原因：' + escapeHtml(item.failure_reason) + '</div>' : '') + '</div><div class="user-space-task-actions"><button class="user-space-action-btn" type="button" data-user-action="' + escapeAttr((item.primary_action || {}).type || '') + '">' + escapeHtml((item.primary_action || {}).label || '处理') + '</button><button class="user-space-action-btn is-ghost" type="button" data-user-toggle-log="' + index + '">查看日志</button></div></div><div class="user-space-task-detail" id="user-task-detail-' + index + '" style="display:none;"><div><strong>任务摘要</strong><span>' + escapeHtml(item.summary || '--') + '</span></div><div><strong>模型 / 算法</strong><span>' + escapeHtml(item.model || '暂无') + '</span></div><div><strong>日志详情</strong><span>' + escapeHtml(item.detail || item.failure_reason || '暂无详细日志') + '</span></div></div>';
        });
        html += '</div>';
    } else {
        html += '<div class="admin-empty-state">当前还没有任务记录，完成一次追色、训练或导出后，这里会自动出现。</div>';
    }
    html += '</section><section class="admin-surface-card user-space-model-card"><div class="admin-card-head"><div><div class="admin-card-title">我的模型与偏好</div><div class="admin-card-subtitle">常用模型、最近模型、导出默认项和预设偏好</div></div></div><div class="user-space-preference-grid"><div class="user-space-preference-item"><span>常用模型</span><strong>' + escapeHtml(preferences.common_model || '--') + '</strong></div><div class="user-space-preference-item"><span>最近使用模型</span><strong>' + escapeHtml(preferences.recent_model || '--') + '</strong></div><div class="user-space-preference-item"><span>自定义参数预设</span><strong>' + escapeHtml(String(preferences.preset_count || 0) + ' 个') + '</strong></div><div class="user-space-preference-item"><span>默认导出格式</span><strong>' + escapeHtml(preferences.default_export_format || '--') + '</strong></div><div class="user-space-preference-item"><span>默认尺寸 / 画质</span><strong>' + escapeHtml(preferences.default_size_quality || '--') + '</strong></div><div class="user-space-preference-item"><span>常用参考风格</span><strong>' + escapeHtml(preferences.reference_style || '--') + '</strong></div></div><div class="user-space-model-status-panel"><div class="user-space-model-status-head"><div><strong>当前模型状态</strong><span id="user-space-model-status-summary">正在读取模型状态...</span></div><em id="user-space-model-status-device">unknown</em></div><div class="user-space-model-status-list" id="user-space-model-status-list"><div class="user-space-model-empty">正在准备模型状态...</div></div></div></section></div>';
    html += '<div class="user-space-bottom-grid"><section class="admin-surface-card user-space-history-card"><div class="admin-card-head"><div><div class="admin-card-title">我的项目与历史记录</div><div class="admin-card-subtitle">最近打开项目、最近导出记录、最近训练/调用记录</div></div></div><div class="user-space-history-columns"><div class="user-space-history-group"><h4>最近项目</h4>';
    if (recentProjects.length) {
        recentProjects.forEach(function(item) {
            html += '<button class="user-space-history-entry user-space-project-entry" type="button" data-project-id="' + Number(item.id || 0) + '" data-project-type="' + escapeAttr(item.type || 'image') + '"><span>' + escapeHtml(item.name || '--') + '</span><em>' + escapeHtml(item.created_at || '--') + '</em></button>';
        });
    } else {
        html += '<div class="user-space-history-empty">暂无项目记录</div>';
    }
    html += '</div><div class="user-space-history-group"><h4>最近导出</h4>';
    if (recentExports.length) {
        recentExports.forEach(function(item) {
            html += '<div class="user-space-history-entry is-static"><span>' + escapeHtml(item.summary || '--') + '</span><em>' + escapeHtml(item.created_at || '--') + '</em></div>';
        });
    } else {
        html += '<div class="user-space-history-empty">暂无导出记录</div>';
    }
    html += '</div><div class="user-space-history-group"><h4>最近训练 / 调用</h4>';
    if (recentModelRecords.length) {
        recentModelRecords.forEach(function(item) {
            html += '<div class="user-space-history-entry is-static"><span>' + escapeHtml((item.model || '--') + ' · ' + (item.summary || '--')) + '</span><em>' + escapeHtml(item.created_at || '--') + '</em></div>';
        });
    } else {
        html += '<div class="user-space-history-empty">暂无训练或调用记录</div>';
    }
    html += '</div></div></section><section class="admin-surface-card user-space-account-card"><div class="admin-card-head"><div><div class="admin-card-title">账户与安全</div><div class="admin-card-subtitle">账户设置入口、存储路径与通知偏好</div></div></div><div class="user-space-account-list">';
    (account.settings_entries || []).forEach(function(item) {
        html += '<button class="user-space-account-entry" type="button" data-user-action="' + escapeAttr(item.type || '') + '"><strong>' + escapeHtml(item.label || '--') + '</strong><span>' + escapeHtml(item.description || '--') + '</span></button>';
    });
    html += '</div><div class="user-space-account-footnote">最近登录：' + escapeHtml(account.last_login_at || '--') + ' · 设备：' + escapeHtml(account.login_device || '--') + '</div></section></div>';
    if (logs.length) {
        html += '<section class="admin-surface-card user-space-summary-card"><div class="admin-card-head"><div><div class="admin-card-title">个人摘要</div><div class="admin-card-subtitle">快速了解当前账号近期状态</div></div></div><div class="admin-log-list">';
        logs.forEach(function(line) {
            html += '<div class="admin-log-item">' + escapeHtml(line) + '</div>';
        });
        html += '</div></section>';
    }
    html += '</div>';
    container.innerHTML = html;
    fetchUserVisibleModelStatus();

    var refreshBtn = $r('#user-space-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', function() { fetchUserSpaceDashboard(true); });
    var editProfileBtn = $r('#user-space-edit-profile-btn');
    var editor = $r('#user-space-profile-editor');
    var nicknameInput = $r('#user-space-nickname-input');
    var saveProfileBtn = $r('#user-space-profile-save');
    var cancelProfileBtn = $r('#user-space-profile-cancel');
    var avatarInput = $r('#user-space-avatar-input');
    if (editProfileBtn && editor) {
        editProfileBtn.addEventListener('click', function() {
            editor.style.display = editor.style.display === 'none' ? '' : 'none';
            if (editor.style.display !== 'none' && nicknameInput) nicknameInput.focus();
        });
    }
    if (cancelProfileBtn && editor && nicknameInput) {
        cancelProfileBtn.addEventListener('click', function() {
            nicknameInput.value = profile.display_name || '';
            editor.style.display = 'none';
        });
    }
    if (saveProfileBtn && nicknameInput) {
        saveProfileBtn.addEventListener('click', function() {
            fetch('/api/projects/space_profile', {
                method: 'POST',
                headers: Object.assign({ 'Content-Type': 'application/json' }, getAuthHeaders()),
                body: JSON.stringify({ nickname: nicknameInput.value || '' })
            })
            .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
            .then(function(result) {
                if (!result.ok) throw new Error((result.data && result.data.detail) || '昵称保存失败');
                if (typeof showToast === 'function') showToast('昵称已保存');
                fetchUserSpaceDashboard(true);
            })
            .catch(function(err) {
                if (typeof showToast === 'function') showToast(err.message || '昵称保存失败');
            });
        });
    }
    if (avatarInput) {
        avatarInput.addEventListener('change', function() {
            var file = avatarInput.files && avatarInput.files[0];
            if (!file) return;
            if (file.size > 2 * 1024 * 1024) {
                if (typeof showToast === 'function') showToast('头像大小不能超过 2MB');
                avatarInput.value = '';
                return;
            }
            var formData = new FormData();
            formData.append('file', file);
            fetch('/api/projects/space_profile/avatar', {
                method: 'POST',
                headers: getAuthHeaders(),
                body: formData
            })
            .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
            .then(function(result) {
                if (!result.ok) throw new Error((result.data && result.data.detail) || '头像上传失败');
                if (typeof showToast === 'function') showToast('头像已更新');
                fetchUserSpaceDashboard(true);
            })
            .catch(function(err) {
                if (typeof showToast === 'function') showToast(err.message || '头像上传失败');
            })
            .finally(function() {
                avatarInput.value = '';
            });
        });
    }
    container.querySelectorAll('[data-user-toggle-log]').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var idx = btn.getAttribute('data-user-toggle-log');
            var detail = $r('#user-task-detail-' + idx);
            if (detail) detail.style.display = detail.style.display === 'none' ? '' : 'none';
        });
    });
    container.querySelectorAll('[data-user-action]').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var action = btn.getAttribute('data-user-action');
            if (action === 'open_projects') showProjectsListModal();
            else if (action === 'open_train') rNavigate('train');
            else if (action === 'open_home') {
                rNavigate('home');
                if (typeof showToast === 'function') showToast('已返回主页，请在工作区重新导出');
            } else if (action === 'security') showChangePasswordModal();
            else if (action === 'storage') showStorageSettingsModal();
            else if (action === 'notifications' && typeof showToast === 'function') showToast('通知偏好入口已预留，后续可继续接入');
        });
    });
    container.querySelectorAll('.user-space-project-entry').forEach(function(btn) {
        btn.addEventListener('click', function() {
            openUserSpaceProject(btn.getAttribute('data-project-id'), btn.getAttribute('data-project-type'));
        });
    });
}

function renderUserSpaceDashboard() {
    var container = $r('#space-panel-content');
    if (!container) return;
    if (!currentUser) {
        container.innerHTML = '<div class="admin-empty-state">请先登录后查看个人空间。</div>';
        return;
    }
    if (!userSpaceDashboardCache) {
        container.innerHTML = '<div class="admin-empty-state">正在加载个人空间...</div>';
        return;
    }
    renderUserSpaceShell(userSpaceDashboardCache);
}

function renderDashboardShell(dashboardData, options) {
    options = options || {};
    var isAdmin = !!options.isAdmin;
    var showTaskLogs = !!options.showTaskLogs;
    var container = $r('#space-panel-content');
    if (!container) return;

    var overview = dashboardData.overview || {};
    var users = overview.users || overview.user || {};
    var projects = overview.projects || {};
    var modelData = overview.model_data || {};
    var taskStats = overview.task_stats || {};
    var cards = dashboardData.cards || [];
    var rings = dashboardData.rings || [];
    var bars = dashboardData.bars || [];
    var progress = dashboardData.progress || [];
    var categories = dashboardData.categories || [];
    var generatedAt = dashboardData.generated_at || overview.generated_at || '';
    var meta = dashboardData.meta || {};
    var logLines = dashboardData.logs || [
        '暂无个人摘要数据。',
        '完成几次任务后，这里会自动显示你的使用趋势。',
        '个人空间会围绕你的项目、任务和导出持续更新。'
    ];

    function formatDelta(delta, unit) {
        if (delta === null || delta === undefined || delta === '') return meta.compare_empty_label || '暂无上周';
        var num = Number(delta);
        var sign = num > 0 ? '+' : '';
        return sign + delta + (unit || '');
    }

    function formatCompactDelta(delta, unit) {
        if (delta === null || delta === undefined || delta === '') return '--';
        var num = Number(delta);
        var sign = num > 0 ? '+' : (num < 0 ? '-' : '');
        var value = Math.abs(num).toFixed(1).replace('.0', '');
        return sign + value + (unit || '');
    }

    function formatPercent(value) {
        return (Number(value) || 0).toFixed(1).replace('.0', '') + '%';
    }

    function formatDateLabel(value) {
        if (!value) return '--';
        var raw = String(value).trim();
        var parsed = new Date(raw);
        if (!isNaN(parsed.getTime())) {
            var yyyy = parsed.getFullYear();
            var month = String(parsed.getMonth() + 1).padStart(2, '0');
            var day = String(parsed.getDate()).padStart(2, '0');
            var hh = String(parsed.getHours()).padStart(2, '0');
            var mm = String(parsed.getMinutes()).padStart(2, '0');
            var ss = String(parsed.getSeconds()).padStart(2, '0');
            return yyyy + '-' + month + '-' + day + ' ' + hh + ':' + mm + ':' + ss;
        }
        var text = raw.replace('T', ' ').replace('Z', '');
        var parts = text.split('.');
        if (parts.length >= 1) return parts[0];
        return text.split('.')[0];
    }

    function polarToCartesian(centerX, centerY, radius, angleInDegrees) {
        var angleInRadians = (angleInDegrees - 90) * Math.PI / 180.0;
        return {
            x: centerX + radius * Math.cos(angleInRadians),
            y: centerY + radius * Math.sin(angleInRadians)
        };
    }

    function describeArc(radius, startAngle, endAngle, clockwise) {
        var centerX = 205;
        var centerY = 205;
        var start = polarToCartesian(centerX, centerY, radius, startAngle);
        var end = polarToCartesian(centerX, centerY, radius, endAngle);
        var delta = clockwise
            ? ((endAngle - startAngle) % 360 + 360) % 360
            : ((startAngle - endAngle) % 360 + 360) % 360;
        var largeArcFlag = delta <= 180 ? '0' : '1';
        var sweepFlag = clockwise ? '1' : '0';
        return [
            'M', start.x, start.y,
            'A', radius, radius, 0, largeArcFlag, sweepFlag, end.x, end.y
        ].join(' ');
    }

    function buildSparkline(points, color) {
        var width = 320;
        var height = 72;
        var safe = (points && points.length ? points : [0, 0, 0, 0, 0]).map(function(item) { return Number(item || 0); });
        var max = Math.max.apply(null, safe.concat([1]));
        var min = Math.min.apply(null, safe);
        var span = Math.max(max - min, 1);
        var step = safe.length > 1 ? width / (safe.length - 1) : width;
        var d = '';
        for (var i = 0; i < safe.length; i++) {
            var x = step * i;
            var y = height - ((safe[i] - min) / span) * (height - 10) - 5;
            d += (i === 0 ? 'M' : ' L') + x.toFixed(2) + ' ' + y.toFixed(2);
        }
        return '<svg viewBox="0 0 ' + width + ' ' + height + '" preserveAspectRatio="none">' +
            '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"></path>' +
            '</svg>';
    }

    function buildTrendBars() {
        var maxTask = 1;
        var maxExport = 1;
        bars.forEach(function(item) {
            maxTask = Math.max(maxTask, Number(item.tasks || 0));
            maxExport = Math.max(maxExport, Number(item.exports || 0));
        });
        var html = '<div class="admin-trend-bars">';
        bars.forEach(function(item) {
            var taskHeight = Math.max(34, Math.round((Number(item.tasks || 0) / maxTask) * 210));
            var exportHeight = Math.max(20, Math.round((Number(item.exports || 0) / maxExport) * 96));
            html += '<div class="admin-trend-day">';
            html += '<div class="admin-trend-bar-group">';
            html += '<div class="admin-trend-bar tasks" style="height:' + taskHeight + 'px;"></div>';
            html += '<div class="admin-trend-bar exports" style="height:' + exportHeight + 'px;"></div>';
            html += '</div>';
            html += '<div class="admin-trend-label">' + escapeHtml(item.label || '--') + '</div>';
            html += '</div>';
        });
        html += '</div>';
        return html;
    }

    function buildRingSvg() {
        var trackStartAngle = 0;
        var trackEndAngle = 90;
        var activeSpan = 270;
        var radii = [150, 121, 92];
        var strokeWidths = [18, 18, 18];
        var html = '<svg class="admin-ring-svg" viewBox="0 0 410 410">';
        rings.slice(0, 3).forEach(function(item, index) {
            var radius = radii[index] || 96;
            var strokeWidth = strokeWidths[index] || 18;
            var percent = Math.max(0, Math.min(100, Number(item.percent || 0)));
            var activeEndAngle = 360 - activeSpan * (percent / 100);
            html += '<path d="' + describeArc(radius, trackStartAngle, trackEndAngle, false) + '" fill="none" stroke="rgba(226,232,244,0.92)" stroke-width="' + strokeWidth + '" stroke-linecap="round"></path>';
            html += '<path d="' + describeArc(radius, 0, activeEndAngle, false) + '" fill="none" stroke="' + (item.color || '#4f63ff') + '" stroke-width="' + strokeWidth + '" stroke-linecap="round"></path>';
        });
        html += '</svg>';
        return html;
    }

    var cardClasses = ['is-lilac', 'is-cyan', 'is-peach', 'is-mint'];
    var sparkColors = ['#b8a8ff', '#88ddf3', '#ffb39a', '#a8aef9'];
    var healthScore = Number(meta.health_score);
    if (!isFinite(healthScore)) {
        var ringPercents = rings.map(function(item) { return Number(item.percent || 0); });
        var ringTotalPercent = 0;
        ringPercents.forEach(function(value) { ringTotalPercent += value; });
        healthScore = ringPercents.length ? (ringTotalPercent / ringPercents.length) : 0;
    }

    var html = '<div class="admin-center-shell">';
    html += '<div class="admin-center-topbar">';
    html += '<div class="admin-center-brand"><img class="admin-center-brand-logo" src="/static/assets/logo.png" alt="ColorChase"><span class="admin-center-brand-mark">' + escapeHtml(meta.brand_mark || (isAdmin ? 'Admin Center' : 'User Space')) + '</span></div>';
    html += '<div class="admin-center-tabs">';
    html += '<button class="admin-center-tab active" type="button">' + escapeHtml(meta.top_title || (isAdmin ? 'Dashboard' : 'My Space')) + '</button>';
    html += '<button class="admin-center-tab" type="button">Projects</button>';
    html += '<button class="admin-center-tab" type="button">Tasks</button>';
    html += '<button class="admin-center-tab" type="button">Assets</button>';
    html += '</div>';
    html += '<div class="admin-center-tools"><span>最近更新: ' + escapeHtml(formatDateLabel(generatedAt)) + ' | ' + escapeHtml(meta.weekly_refresh_rule || '自动更新') + ' | 自动刷新 ' + escapeHtml(String(meta.auto_refresh_seconds || 60)) + 's</span><button class="admin-center-refresh" id="admin-refresh-btn" type="button">刷新</button></div>';
    html += '</div>';
    html += '<div class="admin-dashboard-shell-wrap">';
    html += '<div class="admin-dashboard-top">';
    html += '<div class="admin-surface-card">';
    html += '<div class="admin-card-head"><div><div class="admin-card-title">Today Task</div><div class="admin-card-subtitle">' + escapeHtml(meta.hero_subtitle || '个人核心指标') + '</div></div><span class="admin-chip">' + escapeHtml(isAdmin ? 'Admin' : 'User') + '</span></div>';
    html += '<div class="admin-task-grid">';
    cards.forEach(function(item, index) {
        html += '<div class="admin-task-card ' + cardClasses[index % cardClasses.length] + '">';
        html += '<div class="admin-task-index">' + String(index + 1).padStart(2, '0') + '</div>';
        html += '<div class="admin-task-title">' + escapeHtml(item.label || '--') + '</div>';
        html += '<div class="admin-task-value-row"><div class="admin-task-value">' + escapeHtml(item.display_value || String(item.value || 0) + (item.unit || '')) + '</div><div class="admin-task-delta">' + escapeHtml(formatCompactDelta(item.delta, item.unit || '')) + '</div></div>';
        html += '<div class="admin-task-sparkline">' + buildSparkline(item.sparkline || [], sparkColors[index % sparkColors.length]) + '</div>';
        html += '</div>';
    });
    html += '</div></div>';
    html += '<div class="admin-surface-card compact admin-health-card">';
    html += '<div class="admin-card-head"><div><div class="admin-card-title">' + escapeHtml(meta.ring_title || '运行状态') + '</div><div class="admin-card-subtitle">' + escapeHtml(meta.ring_subtitle || '') + '</div></div><span class="admin-chip">Today</span></div>';
    html += '<div class="admin-ring-wrap">';
    html += '<div class="admin-ring-canvas">' + buildRingSvg();
    html += '<div class="admin-ring-center-copy"><div class="admin-ring-total">' + escapeHtml(formatPercent(healthScore)) + '</div><div class="admin-ring-caption">' + escapeHtml(meta.health_score_label || '状态评分') + '</div><div class="admin-ring-live">Live</div></div></div>';
    html += '<div class="admin-ring-stats">';
    rings.forEach(function(item) {
        html += '<div class="admin-ring-stat">';
        html += '<span class="admin-ring-dot" style="background:' + (item.color || '#4f63ff') + ';"></span>';
        html += '<span class="admin-ring-label">' + escapeHtml(item.label || '--') + '</span>';
        html += '<span class="admin-ring-number">' + escapeHtml(String(item.value || 0)) + '</span>';
        html += '<span class="admin-ring-badge">' + formatPercent(item.percent || 0) + '</span>';
        html += '</div>';
    });
    html += '</div></div></div>';
    html += '</div>';
    html += '<div class="admin-dashboard-shell">';
    html += '<div class="admin-surface-card admin-lower-trend">';
    html += '<div class="admin-card-head"><div><div class="admin-card-title">近 7 日任务趋势</div><div class="admin-card-subtitle">任务与导出变化</div></div><span class="admin-chip">This Week</span></div>';
    html += '<div class="admin-trend-layout">';
    html += '<div class="admin-trend-legend"><span class="admin-legend-item"><span class="admin-legend-dot" style="background:#4f63ff;"></span>任务</span><span class="admin-legend-item"><span class="admin-legend-dot" style="background:#47c5e7;"></span>导出</span></div>';
    html += buildTrendBars();
    html += '</div>';
    html += '</div>';
    html += '<div class="admin-dashboard-side">';
    html += '<div class="admin-dashboard-side-top">';
    html += '<div class="admin-surface-card compact admin-lower-metrics">';
    html += '<div class="admin-card-head"><div><div class="admin-card-title">' + escapeHtml(meta.progress_title || '辅助指标') + '</div><div class="admin-card-subtitle">' + escapeHtml(meta.progress_subtitle || '') + '</div></div><span class="admin-chip">Live</span></div>';
    html += '<div class="admin-mini-columns">';
    progress.forEach(function(item) {
        var value = Math.max(0, Math.min(100, Number(item.value || 0)));
        html += '<div class="admin-mini-column">';
        html += '<div class="admin-mini-rail"><div class="admin-mini-fill" style="height:' + value + '%; background:' + (item.color || '#4f63ff') + ';"></div></div>';
        html += '<div class="admin-mini-label">' + escapeHtml(item.label || '--') + '</div>';
        html += '<div class="admin-mini-value">' + formatPercent(value) + '</div>';
        html += '</div>';
    });
    html += '</div></div>';
    html += '<div class="admin-surface-card compact admin-lower-category">';
    html += '<div class="admin-card-head"><div><div class="admin-card-title">' + escapeHtml(meta.category_title || '资源与资产分类') + '</div><div class="admin-card-subtitle">' + escapeHtml(meta.category_subtitle || '') + '</div></div></div>';
    html += '<div class="admin-category-list">';
    categories.forEach(function(item) {
        html += '<div class="admin-category-row">';
        html += '<span class="admin-ring-dot" style="background:' + (item.color || '#4f63ff') + ';"></span>';
        html += '<span class="admin-category-label">' + escapeHtml(item.label || '--') + '</span>';
        html += '<span class="admin-category-value">' + escapeHtml(String(item.value || '--')) + '</span>';
        html += '</div>';
    });
    html += '</div></div>';
    html += '</div>';
    html += '<div class="admin-surface-card admin-lower-log">';
    html += '<div class="admin-card-head"><div><div class="admin-card-title">' + escapeHtml(meta.log_title || '最近动态') + '</div><div class="admin-card-subtitle">' + escapeHtml(meta.log_subtitle || '') + '</div></div></div>';
    html += '<div class="admin-log-list">';
    logLines.forEach(function(line) {
        html += '<div class="admin-log-item">' + escapeHtml(line) + '</div>';
    });
    html += '</div></div>';
    html += '</div></div>';
    html += '</div></div>';
    if (isAdmin) {
        html += buildTrainingCorpusSummaryRow(modelData);
    }
    if (showTaskLogs) {
        html += '<div class="admin-surface-card admin-task-log-card">';
        html += buildAdminTaskLogsPreview();
        html += '</div>';
    }

    container.innerHTML = html;
    var refreshBtn = $r('#admin-refresh-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', function() {
            if (isAdmin) fetchAdminDashboard(true);
            else fetchUserSpaceDashboard(true);
        });
    }

    if (showTaskLogs) {
        var logRefreshBtn = $r('#admin-log-refresh');
        if (logRefreshBtn && !logRefreshBtn.dataset.bound) {
            logRefreshBtn.dataset.bound = '1';
            logRefreshBtn.addEventListener('click', function() {
                fetchAdminTaskLogs();
            });
        }
        var logBackfillBtn = $r('#admin-log-backfill');
        if (logBackfillBtn && !logBackfillBtn.dataset.bound) {
            logBackfillBtn.dataset.bound = '1';
            logBackfillBtn.addEventListener('click', function() {
                runAdminTaskLogsBackfill();
            });
        }
        ['#admin-log-search', '#admin-log-status', '#admin-log-type'].forEach(function(sel) {
            var el = $r(sel);
            if (el && !el.dataset.bound) {
                el.dataset.bound = '1';
                el.addEventListener('input', function() {
                    fetchAdminTaskLogs();
                });
                el.addEventListener('change', function() {
                    fetchAdminTaskLogs();
                });
            }
        });
        fetchAdminTaskLogs();
    }
}

function updateThemeSwitches() {
    var label = currentTheme === 'light' ? '深色模式' : '浅色模式';
    var homeBtn = document.getElementById('home-theme-toggle');
    var settingsBtn = document.getElementById('settings-theme-toggle');
    if (homeBtn) homeBtn.textContent = label;
    if (settingsBtn) settingsBtn.textContent = label;
}

var trainingTaskId = null;

function setTextIfExists(selector, value) {
    var el = $r(selector);
    if (el) el.textContent = value;
}

function setTrainingTargetVisual(value) {
    var items = document.querySelectorAll('.training-target-item');
    items.forEach(function(item) {
        var input = item.querySelector('input[name="training-target"]');
        item.classList.toggle('is-selected', !!(input && input.value === value));
    });

    var hint = $r('#training-target-hint');
    if (!hint) return;
    if (value === 'neuralpreset') {
        hint.textContent = 'NeuralPreset 已接入真实训练链路，当前版本只有它可以直接启动训练。';
    } else if (value === 'modflows_b0') {
        hint.textContent = 'ModFlows B0 已接入后端目标分流，但训练实现尚未开放；当前选择它会收到明确的未实现提示。';
    } else {
        hint.textContent = 'ModFlows B6 已接入后端目标分流，但训练实现尚未开放；当前选择它会收到明确的未实现提示。';
    }
}

function syncTrainingSummaries() {
    var target = (document.querySelector('input[name="training-target"]:checked') || {}).value || 'neuralpreset';
    var targetMap = {
        neuralpreset: 'NeuralPreset（可训练）',
        modflows_b0: 'ModFlows B0（未开放）',
        modflows_b6: 'ModFlows B6（未开放）'
    };
    var stageEl = $r('#training-stage');
    var stage = stageEl ? stageEl.value : 'both';
    var stageMap = {
        both: '完整训练',
        norm: '归一化阶段',
        style: '风格阶段'
    };
    var stageDescMap = {
        both: '归一化 + 风格阶段同时执行',
        norm: '仅执行归一化阶段训练',
        style: '仅执行风格阶段训练'
    };
    var validationEnabled = !!($r('#training-val-enabled') && $r('#training-val-enabled').checked);

    setTextIfExists('#training-summary-target', targetMap[target] || 'NeuralPreset');
    setTextIfExists('#training-summary-stage', stageMap[stage] || '完整训练');
    setTextIfExists('#training-stage-desc', stageDescMap[stage] || '归一化 + 风格阶段同时执行');
    setTextIfExists('#training-summary-dir', ($r('#training-image-dir') || {}).value || 'temp_train_data');
    setTextIfExists('#training-summary-validation', validationEnabled ? '启用' : '关闭');
    setTextIfExists('#training-summary-mode', '同步执行');
    setTextIfExists('#training-mode-pill', '同步训练');
    setTextIfExists('#training-summary-epochs', (($r('#training-epoch') || {}).value || '100'));
    setTextIfExists('#training-summary-batch', (($r('#training-batch') || {}).value || '4'));
    setTextIfExists('#training-summary-lr', (($r('#training-lr') || {}).value || '0.0001'));
    setTextIfExists('#training-summary-size', (($r('#training-size') || {}).value || '512'));

    setTrainingTargetVisual(target);
}

function refreshTrainingModelStatus() {
    return fetch('/api/model_status', { method: 'GET', cache: 'no-store' })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) throw new Error((result.data && result.data.detail) || '读取模型状态失败');
        var data = result.data || {};
        setTextIfExists('#training-model-norm', data.norm_stage_trained ? '已就绪' : '未就绪');
        setTextIfExists('#training-model-style', data.style_stage_trained ? '已就绪' : '未就绪');
        setTextIfExists('#training-model-neural', data.neural_preset_ready ? '在线' : '未就绪');
        setTextIfExists('#training-model-modflows', data.modflows_ready ? '可用' : '预留');
        renderTrainingModelStatus(data);
        return data;
    })
    .catch(function(err) {
        if (typeof showToast === 'function') showToast(err.message || '模型状态刷新失败');
        return null;
    });
}

function getModelManagerStatusLabel(model) {
    if (!model) return '未知';
    if (!model.ready) return '缺失';
    if (model.status === 'fallback') return '降级可用';
    if (model.status === 'partial') return '部分可用';
    if (model.status === 'ready_reserved') return '已安装';
    return '可用';
}

function getModelManagerBenchmarkText(model) {
    var bench = model && model.benchmark;
    if (!bench) return '未跑 benchmark';
    var text = Number(bench.elapsed_ms || 0).toFixed(1) + ' ms';
    if (bench.finished_at) text += ' · ' + String(bench.finished_at).replace('T', ' ').replace('Z', '');
    return text;
}

function getModelWeightStats(model) {
    var files = Array.isArray(model && model.files) ? model.files : [];
    var installed = files.filter(function(item) { return item && item.exists; });
    var installedSize = installed.reduce(function(sum, item) {
        return sum + Number((item && item.size_mb) || 0);
    }, 0);
    return {
        total: files.length,
        installed: installed.length,
        installedSize: installedSize,
        missingRequired: Array.isArray(model && model.missing_files) ? model.missing_files.length : 0
    };
}

function getModelWeightSummary(model, fallbackText) {
    var stats = getModelWeightStats(model);
    var detected = stats.installed
        ? ('已检测 ' + stats.installed + ' 个权重')
        : '';
    if (detected && stats.installedSize > 0) detected += ' · ' + stats.installedSize.toFixed(1) + ' MB';
    if (stats.missingRequired) {
        return '缺少 ' + stats.missingRequired + ' 个必需权重' + (detected ? ' · ' + detected : '');
    }
    if (detected) {
        return detected + (fallbackText ? ' · ' + fallbackText : '');
    }
    return fallbackText || '状态已读取';
}

function renderModelManager(data) {
    var list = $r('#model-manager-list');
    if (!list || !data) return;
    var models = Array.isArray(data.models) ? data.models : [];
    var summary = data.summary || {};
    var management = data.management || {};
    setTextIfExists(
        '#model-manager-summary',
        '已安装/部分可用 ' + Number(summary.installed_or_partial || 0) + '/' + Number(summary.total || models.length) +
        ' · 可直接 ready ' + Number(summary.ready || 0) + ' · 缺失 ' + Number(summary.missing || 0)
    );
    setTextIfExists('#model-manager-device', (data.device || 'unknown') + (data.device_label ? ' · ' + data.device_label : ''));

    var select = $r('#model-manager-default-select');
    if (select) {
        var currentValue = select.value;
        var defaultModels = models.filter(function(model) { return model.default_selectable; });
        select.innerHTML = defaultModels.map(function(model) {
            var disabled = model.ready ? '' : ' disabled';
            var selected = model.key === management.default_model ? ' selected' : '';
            return '<option value="' + escapeAttr(model.key || '') + '"' + disabled + selected + '>' +
                escapeHtml(model.name || model.key || '未知模型') +
                '</option>';
        }).join('');
        if (!select.value && currentValue) select.value = currentValue;
    }

    if (!models.length) {
        list.innerHTML = '<div class="model-manager-empty">暂无模型状态</div>';
        return;
    }

    list.innerHTML = models.map(function(model) {
        var missing = Array.isArray(model.missing_files) ? model.missing_files.length : 0;
        var badgeClass = model.ready ? 'ready' : 'missing';
        var statusLabel = getModelManagerStatusLabel(model);
        var enabledText = model.enabled ? '禁用' : '启用';
        var defaultBadge = model.is_default ? '<span class="model-manager-badge default">默认</span>' : '';
        var defaultDisabled = (!model.ready || !model.default_selectable) ? ' disabled' : '';
        var lastError = model.last_error && model.last_error.message ? ('最近错误：' + model.last_error.message) : '';
        var installHint = model.install_hint || '';
        var metaParts = [
            '类型：' + (model.kind || 'unknown'),
            '用途：' + (Array.isArray(model.used_by) ? model.used_by.join(', ') : '-'),
            '权重：' + getModelWeightSummary(model, ''),
            'benchmark：' + getModelManagerBenchmarkText(model)
        ];
        if (missing) metaParts.push(installHint);
        if (lastError) metaParts.push(lastError);
        return '<div class="model-manager-row ' + (model.ready ? '' : 'is-missing ') + (model.enabled ? '' : 'is-disabled') + '">' +
            '<div class="model-manager-main">' +
                '<div class="model-manager-title">' +
                    '<strong>' + escapeHtml(model.name || model.key || '未知模型') + '</strong>' +
                    '<span class="model-manager-badge ' + badgeClass + '">' + escapeHtml(statusLabel) + '</span>' +
                    '<span class="model-manager-badge">' + (model.enabled ? '已启用' : '已禁用') + '</span>' +
                    defaultBadge +
                '</div>' +
                '<div class="model-manager-meta">' + escapeHtml(metaParts.join(' · ')) + '</div>' +
            '</div>' +
            '<div class="model-manager-actions">' +
                '<button type="button" data-model-action="toggle" data-model-key="' + escapeAttr(model.key || '') + '">' + enabledText + '</button>' +
                '<button type="button" data-model-action="default" data-model-key="' + escapeAttr(model.key || '') + '"' + defaultDisabled + '>设为默认</button>' +
                '<button type="button" data-model-action="benchmark" data-model-key="' + escapeAttr(model.key || '') + '"' + (!model.ready ? ' disabled' : '') + '>Benchmark</button>' +
            '</div>' +
        '</div>';
    }).join('');
}

function fetchModelManager(force) {
    if (adminModelManagerCache && !force) {
        renderModelManager(adminModelManagerCache);
        return Promise.resolve(adminModelManagerCache);
    }
    return fetch('/api/admin/models', {
        method: 'GET',
        cache: 'no-store',
        headers: getAuthHeaders()
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) throw new Error((result.data && result.data.detail) || '读取模型管理失败');
        adminModelManagerCache = result.data || {};
        renderModelManager(adminModelManagerCache);
        return adminModelManagerCache;
    })
    .catch(function(err) {
        if (typeof showToast === 'function') showToast(err.message || '读取模型管理失败');
        return null;
    });
}

function applyModelManagerPayload(data, message) {
    if (data && Array.isArray(data.models)) {
        adminModelManagerCache = data;
        renderModelManager(data);
    }
    refreshTrainingModelStatus();
    if (message && typeof showToast === 'function') showToast(message);
}

function postModelManagerAction(modelKey, action) {
    if (!modelKey) return Promise.resolve(null);
    var model = null;
    if (adminModelManagerCache && Array.isArray(adminModelManagerCache.models)) {
        model = adminModelManagerCache.models.find(function(item) { return item.key === modelKey; });
    }
    var url = '';
    var options = { method: 'POST', headers: Object.assign({ 'Content-Type': 'application/json' }, getAuthHeaders()) };
    if (action === 'toggle') {
        url = '/api/admin/models/' + encodeURIComponent(modelKey) + '/toggle';
        options.body = JSON.stringify({ enabled: !(model && model.enabled) });
    } else if (action === 'default') {
        url = '/api/admin/models/default';
        options.body = JSON.stringify({ default_model: modelKey });
    } else if (action === 'benchmark') {
        url = '/api/admin/models/' + encodeURIComponent(modelKey) + '/benchmark';
        options.headers = getAuthHeaders();
    } else {
        return Promise.resolve(null);
    }
    return fetch(url, options)
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) throw new Error((result.data && result.data.detail) || '模型管理操作失败');
        var data = result.data || {};
        applyModelManagerPayload(data, data.message || (data.success === false ? '操作失败' : '模型管理已更新'));
        return data;
    })
    .catch(function(err) {
        if (typeof showToast === 'function') showToast(err.message || '模型管理操作失败');
        return null;
    });
}

function bindModelManagerControls() {
    var refreshBtn = $r('#model-manager-refresh');
    if (refreshBtn && !refreshBtn.dataset.bound) {
        refreshBtn.dataset.bound = '1';
        refreshBtn.addEventListener('click', function() {
            fetchModelManager(true);
        });
    }

    var defaultSelect = $r('#model-manager-default-select');
    if (defaultSelect && !defaultSelect.dataset.bound) {
        defaultSelect.dataset.bound = '1';
        defaultSelect.addEventListener('change', function() {
            if (!defaultSelect.value) return;
            postModelManagerAction(defaultSelect.value, 'default');
        });
    }

    var list = $r('#model-manager-list');
    if (list && !list.dataset.bound) {
        list.dataset.bound = '1';
        list.addEventListener('click', function(event) {
            var btn = event.target && event.target.closest ? event.target.closest('button[data-model-action]') : null;
            if (!btn) return;
            var action = btn.getAttribute('data-model-action');
            var modelKey = btn.getAttribute('data-model-key');
            if (!action || !modelKey) return;
            btn.disabled = true;
            postModelManagerAction(modelKey, action).finally(function() {
                btn.disabled = false;
            });
        });
    }
}

function getTrainingModelStatusLabel(model) {
    if (!model) return '未知';
    if (model.enabled === false) return '已禁用';
    if (model.ready && model.status === 'ready_reserved') return '已安装';
    if (model.ready) return '可用';
    if (model.status === 'partial') return '部分可用';
    return '缺失';
}

function getTrainingModelStatusClass(model) {
    if (!model) return '';
    if (model.enabled === false) return 'training-model-missing';
    if (model.ready && model.status !== 'ready_reserved') return 'training-model-ready';
    if (model.ready || model.status === 'partial' || model.status === 'ready_reserved') return 'training-model-pending';
    return 'training-model-missing';
}

function getTrainingModelInitial(model) {
    var key = String((model && model.key) || '').toUpperCase();
    if (key.indexOf('MODFLOWS_B6') >= 0) return 'B6';
    if (key.indexOf('MODFLOWS_B0') >= 0) return 'B0';
    if (key.indexOf('SEGFACE') >= 0) return 'SF';
    if (key.indexOf('BIREFNET') >= 0) return 'BR';
    if (key.indexOf('DNCM') >= 0) return 'DL';
    if (key.indexOf('AI_PORTRAIT') >= 0) return 'AP';
    if (key.indexOf('TRAINING') >= 0) return 'TR';
    return 'M';
}

function renderTrainingModelStatus(data) {
    var list = $r('#training-model-list');
    if (!list || !data || !Array.isArray(data.models)) return;

    var summary = data.summary || {};
    var summaryText = '模型 ' + Number(summary.ready || 0) + ' 个可用，' +
        Number(summary.installed_or_partial || summary.ready || 0) + '/' + Number(summary.total || data.models.length) +
        ' 已安装或部分可用 · 设备 ' + escapeHtml(data.device || 'unknown');
    if (!summary.dncm_ready) summaryText += ' · DNCM LUT 未补齐';
    setTextIfExists('#training-model-status-summary', summaryText);

    list.innerHTML = data.models.map(function(model) {
        var note = getModelWeightSummary(model, model.note || '');
        return '<div class="training-model-item ' + getTrainingModelStatusClass(model) + '">' +
            '<div class="training-model-copy">' +
                '<em>' + escapeHtml(getTrainingModelInitial(model)) + '</em>' +
                '<div>' +
                    '<span>' + escapeHtml(model.name || model.key || '未知模型') + '</span>' +
                    '<small>' + escapeHtml(note || '状态已读取') + '</small>' +
                '</div>' +
            '</div>' +
            '<strong>' + escapeHtml(getTrainingModelStatusLabel(model)) + '</strong>' +
        '</div>';
    }).join('');
}

function updateTrainingDataCountFromDashboard() {
    var overview = adminDashboardCache && adminDashboardCache.overview ? adminDashboardCache.overview : {};
    var modelData = overview.model_data || {};
    var count = Number(modelData.training_file_count || 0);
    var size = Number(modelData.training_size_mb || 0);
    var text = count + ' 张 / ' + size.toFixed(1) + ' MB';
    setTextIfExists('#training-data-count', text);
    setTextIfExists('#training-summary-data', text);
}

function appendTrainingLog(message) {
    var el = $r('#training-log-box');
    if (!el) return;
    var stamp = new Date();
    var hh = String(stamp.getHours()).padStart(2, '0');
    var mm = String(stamp.getMinutes()).padStart(2, '0');
    var ss = String(stamp.getSeconds()).padStart(2, '0');
    var next = '[' + hh + ':' + mm + ':' + ss + '] ' + message;
    var current = (el.textContent || '').trim();
    el.textContent = current ? current + '\n' + next : next;
    el.scrollTop = el.scrollHeight;
}

function toggleTrainingButtons(running) {
    var startBtn = $r('#training-start-btn');
    var pauseBtn = $r('#training-pause-btn');
    var resumeBtn = $r('#training-resume-btn');
    var cancelBtn = $r('#training-cancel-btn');
    if (startBtn) startBtn.disabled = !!running;
    if (pauseBtn) pauseBtn.disabled = !running;
    if (resumeBtn) resumeBtn.disabled = !trainingTaskId;
    if (cancelBtn) cancelBtn.disabled = !trainingTaskId;
}

function startTrainingTask() {
    var form = new FormData();
    form.append('stage', (($r('#training-stage') || {}).value || 'both'));
    form.append('image_dir', (($r('#training-image-dir') || {}).value || 'temp_train_data'));
    form.append('epochs', (($r('#training-epoch') || {}).value || '100'));
    form.append('batch_size', (($r('#training-batch') || {}).value || '4'));
    form.append('lr', (($r('#training-lr') || {}).value || '0.0001'));

    setTextIfExists('#training-status-message', '训练进行中');
    toggleTrainingButtons(true);
    appendTrainingLog('开始训练请求已发出');

    return fetch('/api/train', {
        method: 'POST',
        body: form
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) throw new Error((result.data && result.data.detail) || '训练启动失败');
        trainingTaskId = (result.data && result.data.task_id) || trainingTaskId;
        setTextIfExists('#training-status-message', '训练已完成');
        setTextIfExists('#training-status-progress', '100');
        appendTrainingLog('训练完成');
        toggleTrainingButtons(false);
        refreshTrainingModelStatus();
        return result.data;
    })
    .catch(function(err) {
        setTextIfExists('#training-status-message', '启动失败');
        appendTrainingLog('错误：' + (err.message || '未知错误'));
        toggleTrainingButtons(false);
        if (typeof showToast === 'function') showToast(err.message || '训练启动失败');
        return null;
    });
}

function postTrainingTaskAction(action) {
    if (!trainingTaskId) {
        if (typeof showToast === 'function') showToast('当前没有可控的训练任务 ID');
        return Promise.resolve(null);
    }
    return fetch('/api/task/' + encodeURIComponent(trainingTaskId) + '/' + action, { method: 'POST' })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) throw new Error((result.data && result.data.detail) || '任务控制失败');
        appendTrainingLog(result.data && result.data.message ? result.data.message : ('任务' + action + '成功'));
        if (action === 'cancel') {
            trainingTaskId = null;
            toggleTrainingButtons(false);
            setTextIfExists('#training-status-message', '任务已取消');
        }
        return result.data;
    })
    .catch(function(err) {
        if (typeof showToast === 'function') showToast(err.message || '任务控制失败');
        return null;
    });
}

function initTrainingWorkbench() {
    syncTrainingSummaries();
    updateTrainingDataCountFromDashboard();
    refreshTrainingModelStatus();
    bindModelManagerControls();
    fetchModelManager(false);

    var targetInputs = document.querySelectorAll('input[name="training-target"]');
    targetInputs.forEach(function(input) {
        input.addEventListener('change', syncTrainingSummaries);
    });

    ['#training-stage', '#training-image-dir', '#training-epoch', '#training-batch', '#training-lr', '#training-size', '#training-val-enabled'].forEach(function(sel) {
        var el = $r(sel);
        if (!el) return;
        el.addEventListener('input', syncTrainingSummaries);
        el.addEventListener('change', syncTrainingSummaries);
    });

    var refreshBtn = $r('#training-data-refresh');
    if (refreshBtn && !refreshBtn.dataset.bound) {
        refreshBtn.dataset.bound = '1';
        refreshBtn.addEventListener('click', function() {
            fetchAdminDashboard(true).then(function() {
                updateTrainingDataCountFromDashboard();
                appendTrainingLog('训练数据统计已刷新');
            });
        });
    }

    var modelRefreshBtn = $r('#admin-model-refresh');
    if (modelRefreshBtn && !modelRefreshBtn.dataset.bound) {
        modelRefreshBtn.dataset.bound = '1';
        modelRefreshBtn.addEventListener('click', function() {
            refreshTrainingModelStatus();
            appendTrainingLog('模型状态已刷新');
        });
    }

    var clearBtn = $r('#training-log-clear');
    if (clearBtn && !clearBtn.dataset.bound) {
        clearBtn.dataset.bound = '1';
        clearBtn.addEventListener('click', function() {
            var log = $r('#training-log-box');
            if (log) log.textContent = '等待启动';
        });
    }

    var uploadBtn = $r('#training-upload-btn');
    var uploadInput = $r('#training-upload-input');
    if (uploadBtn && uploadInput && !uploadBtn.dataset.bound) {
        uploadBtn.dataset.bound = '1';
        uploadBtn.addEventListener('click', function() { uploadInput.click(); });
    }

    var startBtn = $r('#training-start-btn');
    if (startBtn && !startBtn.dataset.bound) {
        startBtn.dataset.bound = '1';
        startBtn.addEventListener('click', startTrainingTask);
    }

    var pauseBtn = $r('#training-pause-btn');
    if (pauseBtn && !pauseBtn.dataset.bound) {
        pauseBtn.dataset.bound = '1';
        pauseBtn.addEventListener('click', function() { postTrainingTaskAction('pause'); });
    }

    var resumeBtn = $r('#training-resume-btn');
    if (resumeBtn && !resumeBtn.dataset.bound) {
        resumeBtn.dataset.bound = '1';
        resumeBtn.addEventListener('click', function() { postTrainingTaskAction('resume'); });
    }

    var cancelBtn = $r('#training-cancel-btn');
    if (cancelBtn && !cancelBtn.dataset.bound) {
        cancelBtn.dataset.bound = '1';
        cancelBtn.addEventListener('click', function() { postTrainingTaskAction('cancel'); });
    }

    var enterBtn = $r('#admin-training-enter');
    if (enterBtn && !enterBtn.dataset.bound) {
        enterBtn.dataset.bound = '1';
        enterBtn.addEventListener('click', function() {
            var target = $r('.training-workbench');
            if (target && typeof target.scrollIntoView === 'function') {
                target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        });
    }

    var closeBtn = $r('#training-workbench-close');
    if (closeBtn && !closeBtn.dataset.bound) {
        closeBtn.dataset.bound = '1';
        closeBtn.addEventListener('click', function() {
            rNavigate('home');
        });
    }

    toggleTrainingButtons(false);
}

var trainingProgressTimer = null;

function updateTrainingDataCountDirect(fileCount, sizeMb) {
    var text = Number(fileCount || 0) + ' 张 / ' + Number(sizeMb || 0).toFixed(1) + ' MB';
    setTextIfExists('#training-data-count', text);
    setTextIfExists('#training-summary-data', text);
}

function stopTrainingProgressPolling() {
    if (trainingProgressTimer) {
        clearInterval(trainingProgressTimer);
        trainingProgressTimer = null;
    }
}

function startTrainingProgressPolling() {
    if (!trainingTaskId) return;
    stopTrainingProgressPolling();
    trainingProgressTimer = setInterval(function() {
        fetch('/api/task/' + encodeURIComponent(trainingTaskId) + '/progress', { method: 'GET' })
        .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
        .then(function(result) {
            if (!result.ok) throw new Error((result.data && result.data.detail) || '读取训练进度失败');
            var data = result.data || {};
            setTextIfExists('#training-status-progress', String(Number(data.current || 0)));
            setTextIfExists('#training-status-message', data.message || '训练进行中');
            setTextIfExists('#training-status-loss', data.loss !== undefined ? String(data.loss) : '--');
            setTextIfExists('#training-status-time', data.elapsed ? (data.elapsed + 's') : '--');
            setTextIfExists('#training-status-epoch', data.epoch || '--');
            setTextIfExists('#training-status-eta', data.eta || '--');

            if (data.status === 'done') {
                appendTrainingLog(data.message || '训练完成');
                toggleTrainingButtons(false);
                stopTrainingProgressPolling();
                refreshTrainingModelStatus();
            } else if (data.status === 'error' || data.status === 'cancelled') {
                appendTrainingLog(data.message || '训练已结束');
                if (data.status === 'cancelled') trainingTaskId = null;
                toggleTrainingButtons(false);
                stopTrainingProgressPolling();
            } else {
                toggleTrainingButtons(true);
            }
        })
        .catch(function() {
            stopTrainingProgressPolling();
        });
    }, 1500);
}

function startTrainingTask() {
    var form = new FormData();
    form.append('stage', (($r('#training-stage') || {}).value || 'both'));
    form.append('image_dir', (($r('#training-image-dir') || {}).value || 'temp_train_data'));
    form.append('epochs', (($r('#training-epoch') || {}).value || '100'));
    form.append('batch_size', (($r('#training-batch') || {}).value || '4'));
    form.append('lr', (($r('#training-lr') || {}).value || '0.0001'));

    setTextIfExists('#training-status-message', '训练进行中');
    setTextIfExists('#training-status-progress', '0');
    setTextIfExists('#training-status-loss', '--');
    setTextIfExists('#training-status-time', '--');
    setTextIfExists('#training-status-epoch', '--');
    setTextIfExists('#training-status-eta', '训练中...');
    toggleTrainingButtons(true);
    appendTrainingLog('开始训练请求已发出');

    return fetch('/api/train', {
        method: 'POST',
        body: form
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) throw new Error((result.data && result.data.detail) || '训练启动失败');
        trainingTaskId = (result.data && result.data.task_id) || trainingTaskId;
        if (result.data) updateTrainingDataCountDirect(result.data.training_file_count, result.data.training_size_mb);
        appendTrainingLog('训练任务已创建：' + trainingTaskId);
        startTrainingProgressPolling();
        return result.data;
    })
    .catch(function(err) {
        setTextIfExists('#training-status-message', '启动失败');
        appendTrainingLog('错误：' + (err.message || '未知错误'));
        toggleTrainingButtons(false);
        if (typeof showToast === 'function') showToast(err.message || '训练启动失败');
        return null;
    });
}

function postTrainingTaskAction(action) {
    if (!trainingTaskId) {
        if (typeof showToast === 'function') showToast('当前没有可控的训练任务 ID');
        return Promise.resolve(null);
    }
    return fetch('/api/task/' + encodeURIComponent(trainingTaskId) + '/' + action, { method: 'POST' })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) throw new Error((result.data && result.data.detail) || '任务控制失败');
        appendTrainingLog(result.data && result.data.message ? result.data.message : ('任务' + action + '成功'));
        if (action === 'cancel') {
            trainingTaskId = null;
            toggleTrainingButtons(false);
            setTextIfExists('#training-status-message', '任务已取消');
            stopTrainingProgressPolling();
        }
        if (action === 'resume') {
            startTrainingProgressPolling();
        }
        return result.data;
    })
    .catch(function(err) {
        if (typeof showToast === 'function') showToast(err.message || '任务控制失败');
        return null;
    });
}

function initTrainingWorkbench() {
    syncTrainingSummaries();
    updateTrainingDataCountFromDashboard();
    refreshTrainingModelStatus();
    bindModelManagerControls();
    fetchModelManager(false);

    var targetInputs = document.querySelectorAll('input[name="training-target"]');
    targetInputs.forEach(function(input) {
        input.addEventListener('change', syncTrainingSummaries);
    });

    ['#training-stage', '#training-image-dir', '#training-epoch', '#training-batch', '#training-lr', '#training-size', '#training-val-enabled'].forEach(function(sel) {
        var el = $r(sel);
        if (!el) return;
        el.addEventListener('input', syncTrainingSummaries);
        el.addEventListener('change', syncTrainingSummaries);
    });

    var refreshBtn = $r('#training-data-refresh');
    if (refreshBtn && !refreshBtn.dataset.bound) {
        refreshBtn.dataset.bound = '1';
        refreshBtn.addEventListener('click', function() {
            fetchAdminDashboard(true).then(function() {
                updateTrainingDataCountFromDashboard();
                appendTrainingLog('训练数据统计已刷新');
            });
        });
    }

    var modelRefreshBtn = $r('#admin-model-refresh');
    if (modelRefreshBtn && !modelRefreshBtn.dataset.bound) {
        modelRefreshBtn.dataset.bound = '1';
        modelRefreshBtn.addEventListener('click', function() {
            refreshTrainingModelStatus();
            appendTrainingLog('模型状态已刷新');
        });
    }

    var clearBtn = $r('#training-log-clear');
    if (clearBtn && !clearBtn.dataset.bound) {
        clearBtn.dataset.bound = '1';
        clearBtn.addEventListener('click', function() {
            var log = $r('#training-log-box');
            if (log) log.textContent = '等待启动';
        });
    }

    var uploadBtn = $r('#training-upload-btn');
    var uploadInput = $r('#training-upload-input');
    if (uploadBtn && uploadInput && !uploadBtn.dataset.bound) {
        uploadBtn.dataset.bound = '1';
        uploadBtn.addEventListener('click', function() { uploadInput.click(); });
        uploadInput.addEventListener('change', function() {
            if (!uploadInput.files || !uploadInput.files.length) return;
            var form = new FormData();
            Array.prototype.forEach.call(uploadInput.files, function(file) {
                form.append('files', file);
            });
            form.append('image_dir', (($r('#training-image-dir') || {}).value || 'temp_train_data'));
            fetch('/api/train/upload', {
                method: 'POST',
                body: form
            })
            .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
            .then(function(result) {
                if (!result.ok) throw new Error((result.data && result.data.detail) || '训练图片上传失败');
                updateTrainingDataCountDirect(result.data.training_file_count, result.data.training_size_mb);
                appendTrainingLog('已上传训练图片 ' + (result.data.saved_count || 0) + ' 张');
                uploadInput.value = '';
            })
            .catch(function(err) {
                if (typeof showToast === 'function') showToast(err.message || '训练图片上传失败');
                uploadInput.value = '';
            });
        });
    }

    var startBtn = $r('#training-start-btn');
    if (startBtn && !startBtn.dataset.bound) {
        startBtn.dataset.bound = '1';
        startBtn.addEventListener('click', startTrainingTask);
    }

    var pauseBtn = $r('#training-pause-btn');
    if (pauseBtn && !pauseBtn.dataset.bound) {
        pauseBtn.dataset.bound = '1';
        pauseBtn.addEventListener('click', function() { postTrainingTaskAction('pause'); });
    }

    var resumeBtn = $r('#training-resume-btn');
    if (resumeBtn && !resumeBtn.dataset.bound) {
        resumeBtn.dataset.bound = '1';
        resumeBtn.addEventListener('click', function() { postTrainingTaskAction('resume'); });
    }

    var cancelBtn = $r('#training-cancel-btn');
    if (cancelBtn && !cancelBtn.dataset.bound) {
        cancelBtn.dataset.bound = '1';
        cancelBtn.addEventListener('click', function() { postTrainingTaskAction('cancel'); });
    }

    var enterBtn = $r('#admin-training-enter');
    if (enterBtn && !enterBtn.dataset.bound) {
        enterBtn.dataset.bound = '1';
        enterBtn.addEventListener('click', function() {
            var target = $r('.training-workbench');
            if (target && typeof target.scrollIntoView === 'function') {
                target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        });
    }

    var closeBtn = $r('#training-workbench-close');
    if (closeBtn && !closeBtn.dataset.bound) {
        closeBtn.dataset.bound = '1';
        closeBtn.addEventListener('click', function() {
            stopTrainingProgressPolling();
            rNavigate('home');
        });
    }

    if (trainingTaskId) {
        startTrainingProgressPolling();
    } else {
        stopTrainingProgressPolling();
        toggleTrainingButtons(false);
    }
}

function applyTheme(theme) {
    currentTheme = theme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', currentTheme);
    localStorage.setItem('cc_theme', currentTheme);
    updateThemeSwitches();
}

function initTheme() {
    applyTheme(localStorage.getItem('cc_theme') || 'dark');
}

function toggleTheme() {
    applyTheme(currentTheme === 'light' ? 'dark' : 'light');
}

function escapeHtml(str) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}

function escapeAttr(str) {
    return escapeHtml(str).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function rNavigate(viewName) {
    var containers = document.querySelectorAll('.view-container');
    containers.forEach(function(c) { c.style.display = 'none'; });
    var target = document.getElementById('view-' + viewName);
    if (target) target.style.display = '';
    if (currentView === 'workspace' && viewName !== 'workspace' && window.currentProjectId && typeof saveSnapshot === 'function') {
        saveSnapshot(window.currentProjectId);
    }
    currentView = viewName;
    updateTopBarAuth();
    updateAdminVisibility();
    if (viewName === 'home') {
        clearAdminDashboardRefresh();
        switchWorkspaceMode(false);
        var funcCards = $r('#home-func-cards');
        if (funcCards) funcCards.style.display = '';
        var sep = $r('.home-section-sep');
        if (sep) sep.style.display = '';
        var subtitle = $r('.home-section-subtitle');
        if (subtitle) subtitle.style.display = '';
        var searchInput = $r('.home-search-input');
        if (searchInput) searchInput.style.display = '';
        var titleEl = $r('#home-content-title');
        if (titleEl) titleEl.textContent = '我的主页';
        var homeNavItems = document.querySelectorAll('.home-nav-item');
        homeNavItems.forEach(function(el) { el.classList.remove('active'); });
        var homeNav = document.querySelector('.home-nav-item[data-nav="home"]');
        if (homeNav) homeNav.classList.add('active');
        loadHomeProjectsGrid();
        maybeShowAdminLoginToast();
    }
    if (viewName === 'space') {
        setHomeSurfaceVisibility({
            showCards: false,
            showSubtitle: false,
            showSearch: false,
            showFilter: false,
            showTrashToolbar: false,
            showGrid: false,
            showSpace: true,
            showTrain: false,
            showBanner: true
        });
        renderAdminSpaceDashboard();
        if (isAdminUser()) fetchAdminDashboard(false);
        else fetchUserSpaceDashboard(false);
    }
    if (viewName === 'train') {
        clearAdminDashboardRefresh();
        setHomeSurfaceVisibility({
            showCards: false,
            showSubtitle: false,
            showSearch: false,
            showFilter: false,
            showTrashToolbar: false,
            showGrid: false,
            showSpace: false,
            showTrain: true,
            showBanner: false
        });
        if (isAdminUser()) {
            fetchAdminDashboard(false).then(function() {
                initTrainingWorkbench();
            });
        } else {
            initTrainingWorkbench();
        }
    }
    if (viewName === 'workspace') {
        clearAdminDashboardRefresh();
        var isVideo = window._pendingProjectType === 'video';
        switchWorkspaceMode(isVideo);
        if (window.currentProjectId && typeof loadSnapshot === 'function') {
            setTimeout(function() { loadSnapshot(window.currentProjectId); }, 100);
        }
    }
}

function updateTopBarAuth() {
    var token = localStorage.getItem('cc_token');
    var loginBtn = $r('#nav-login-btn');
    var settingsBtn = $r('#nav-settings-btn');
    var noticeBtn = $r('#nav-notice-btn');
    var contactBtn = $r('#nav-contact-btn');
    if (loginBtn && settingsBtn) {
        if (token) {
            loginBtn.style.display = 'none';
            settingsBtn.style.display = '';
            if (noticeBtn) noticeBtn.style.display = '';
            if (contactBtn) contactBtn.style.display = '';
        } else {
            loginBtn.style.display = '';
            settingsBtn.style.display = 'none';
            if (noticeBtn) noticeBtn.style.display = 'none';
            if (contactBtn) contactBtn.style.display = 'none';
        }
    }
}

function isLoggedIn() {
    return !!localStorage.getItem('cc_token');
}

function loginUser(token, user) {
    localStorage.setItem('cc_token', token);
    localStorage.setItem('cc_user', JSON.stringify(user));
    authToken = token;
    currentUser = user;
    adminDashboardCache = null;
    adminModelManagerCache = null;
    updateTopBarAuth();
    updateAdminVisibility();
    maybeShowAdminLoginToast();
    getPortalMessages();
}

function syncCurrentUser() {
    if (!authToken) return Promise.resolve(null);
    return fetch('/api/auth/me', {
        method: 'GET',
        headers: getAuthHeaders()
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) {
            logoutUser();
            return null;
        }
        currentUser = result.data;
        localStorage.setItem('cc_user', JSON.stringify(currentUser));
        updateTopBarAuth();
        updateAdminVisibility();
        renderSpacePanelForUser();
        getPortalMessages();
        if (isAdminUser()) {
            maybeShowAdminLoginToast();
            return fetchAdminDashboard(true);
        }
        return currentUser;
    })
    .catch(function() {
        logoutUser();
        return null;
    });
}

function logoutUser() {
    try {
        fetch('/api/auth/logout', {
            method: 'POST',
            headers: getAuthHeaders()
        }).catch(function() {});
    } catch (e) {}
    localStorage.removeItem('cc_token');
    localStorage.removeItem('cc_user');
    localStorage.removeItem('cc_admin_welcome_shown');
    authToken = null;
    currentUser = null;
    adminDashboardCache = null;
    adminModelManagerCache = null;
    portalMessagesCache = null;
    portalMessagesLoaded = false;
    portalNoticeAutoShownForVersion = null;
    clearAdminDashboardRefresh();
    updateTopBarAuth();
    updateAdminVisibility();
}

function showProjectModal(projectType) {
    var modal = $r('#create-project-modal');
    var typeInput = $r('#project-type-input');
    var nameInput = $r('#project-name-input');
    if (!modal || !typeInput) return;
    typeInput.value = projectType;
    if (nameInput) nameInput.value = '未命名项目';

    modal.setAttribute('data-project-type', projectType === 'video' ? 'video' : 'image');

    modal.style.display = 'flex';
}

function hideProjectModal() {
    var modal = $r('#create-project-modal');
    if (modal) modal.style.display = 'none';
}

function createProjectAndEnter() {
    var nameInput = $r('#project-name-input');
    var typeInput = $r('#project-type-input');
    var name = (nameInput && nameInput.value || '').trim() || '未命名项目';
    var type = typeInput ? typeInput.value : 'image';

    var token = localStorage.getItem('cc_token');
    if (!token) { rNavigate('login'); return; }

    fetch('/api/projects/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + token,
        },
        body: JSON.stringify({ name: name, type: type })
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) {
            alert(result.data.detail || '创建项目失败');
            return;
        }
        hideProjectModal();
        window.currentProjectId = result.data.id;
        window._pendingProjectType = type;
        rNavigate('workspace');
    })
    .catch(function(err) {
        alert('网络错误：' + err.message);
    });
}

function goToHome() {
    rNavigate('home');
}

function showSettingsModal() {
    if (!$r('#settings-username')) {
        console.error("Settings DOM missing");
        return;
    }
    var modal = $r('#settings-modal');
    if (!modal) return;
    var user = JSON.parse(localStorage.getItem('cc_user') || '{}');
    var usernameEl = $r('#settings-username');
    var nameValEl = $r('#settings-name-val');
    var roleValEl = $r('#settings-role-val');
    var ratedCountEl = $r('#settings-rated-count');
    var totalCountEl = $r('#settings-total-count');

    var nickname = localStorage.getItem('cc_nickname') || '';
    var displayName = nickname || user.email || user.phone || '未知';
    if (usernameEl) usernameEl.textContent = displayName;
    if (nameValEl) nameValEl.textContent = user.email || user.phone || '未知';
    if (roleValEl) roleValEl.textContent = user.role === 'admin' ? '管理员' : '普通用户';

    var imgs = window.targetImages || [];
    var total = imgs.length;
    var rated = 0;
    for (var i = 0; i < imgs.length; i++) {
        if (imgs[i].rating && imgs[i].rating > 0) rated++;
    }
    if (ratedCountEl) ratedCountEl.textContent = rated;
    if (totalCountEl) totalCountEl.textContent = total;

    var editArea = $r('#settings-name-edit-area');
    if (editArea) editArea.style.display = 'none';

    var deleteAccountBtn = $r('#settings-delete-account-btn');
    if (deleteAccountBtn) {
        if (user.role === 'admin') {
            deleteAccountBtn.textContent = '管理员账户不可注销';
            deleteAccountBtn.style.opacity = '0.55';
            deleteAccountBtn.style.pointerEvents = 'none';
        } else {
            deleteAccountBtn.textContent = '注销账户';
            deleteAccountBtn.style.opacity = '';
            deleteAccountBtn.style.pointerEvents = '';
        }
    }

    modal.style.display = 'block';
    if (isAdminUser()) fetchAdminDashboard(false);
}

function hideSettingsModal() {
    var modal = $r('#settings-modal');
    if (modal) modal.style.display = 'none';
}

function showNoticeModal(fromAuto) {
    var modal = $r('#notice-modal');
    if (modal) modal.style.display = 'flex';
    if (isAdminUser()) {
        ensurePortalAdminEditor('notice');
        updatePortalMessageMode();
        var titleEl = $r('#notice-admin-title');
        if (titleEl) titleEl.focus();
        return;
    } else if (!fromAuto) {
        markPortalRead('notice');
    }
}

function hideNoticeModal() {
    var modal = $r('#notice-modal');
    if (modal) modal.style.display = 'none';
}

function showContactModal() {
    var modal = $r('#contact-modal');
    if (modal) modal.style.display = 'flex';
    if (isAdminUser()) {
        ensurePortalAdminEditor('contact');
        updatePortalMessageMode();
        var qqEl = $r('#contact-admin-qq');
        if (qqEl) qqEl.focus();
        return;
    } else {
        markPortalRead('contact');
    }
}

function hideContactModal() {
    var modal = $r('#contact-modal');
    if (modal) modal.style.display = 'none';
}

function showDeleteAccountModal() {
    var modal = $r('#delete-account-modal');
    var passwordEl = $r('#delete-account-password');
    var codeEl = $r('#delete-account-code');
    var confirmEl = $r('#delete-account-confirm');
    var errorEl = $r('#delete-account-error');
    var emailTipEl = $r('#delete-account-email-tip');
    if (!modal) return;
    var user = JSON.parse(localStorage.getItem('cc_user') || '{}');
    if (passwordEl) passwordEl.value = '';
    if (codeEl) codeEl.value = '';
    if (confirmEl) confirmEl.value = '';
    if (emailTipEl) {
        emailTipEl.textContent = user && user.email
            ? ('验证码将发送到当前绑定邮箱：' + user.email)
            : '当前账户未绑定邮箱，暂不支持注销。';
    }
    if (errorEl) {
        errorEl.textContent = '';
        errorEl.style.display = 'none';
    }
    modal.style.display = 'flex';
}

function hideDeleteAccountModal() {
    var modal = $r('#delete-account-modal');
    if (modal) modal.style.display = 'none';
}

function showChangePasswordModal() {
    var modal = $r('#change-password-modal');
    var oldEl = $r('#change-password-old');
    var codeEl = $r('#change-password-code');
    var newEl = $r('#change-password-new');
    var confirmEl = $r('#change-password-confirm');
    var errorEl = $r('#change-password-error');
    var emailTipEl = $r('#change-password-email-tip');
    if (!modal) return;
    var user = JSON.parse(localStorage.getItem('cc_user') || '{}');
    if (oldEl) oldEl.value = '';
    if (codeEl) codeEl.value = '';
    if (newEl) newEl.value = '';
    if (confirmEl) confirmEl.value = '';
    if (emailTipEl) {
        emailTipEl.textContent = user && user.email
            ? ('验证码将发送到当前绑定邮箱：' + user.email)
            : '当前账户未绑定邮箱，暂不支持邮箱验证码改密。';
    }
    if (errorEl) {
        errorEl.textContent = '';
        errorEl.style.display = 'none';
        errorEl.style.color = '#ff6b81';
    }
    modal.style.display = 'flex';
}

function hideChangePasswordModal() {
    var modal = $r('#change-password-modal');
    if (modal) modal.style.display = 'none';
}

function startCountdown(btn, seconds, doneText) {
    if (!btn) return;
    var remain = seconds;
    btn.disabled = true;
    function tick() {
        if (remain <= 0) {
            btn.disabled = false;
            btn.textContent = doneText;
            return;
        }
        btn.textContent = remain + 's';
        remain -= 1;
        setTimeout(tick, 1000);
    }
    tick();
}

function sendDeleteAccountCode() {
    var passwordEl = $r('#delete-account-password');
    var errorEl = $r('#delete-account-error');
    var sendBtn = $r('#delete-account-send-code-btn');
    var password = (passwordEl && passwordEl.value || '').trim();
    var user = JSON.parse(localStorage.getItem('cc_user') || '{}');

    if (!user || !user.email) {
        if (errorEl) {
            errorEl.textContent = '当前账户未绑定邮箱，暂不支持注销';
            errorEl.style.display = '';
        }
        return;
    }
    if (!password) {
        if (errorEl) {
            errorEl.textContent = '请先输入当前密码，再发送验证码';
            errorEl.style.display = '';
        }
        return;
    }

    fetch('/api/auth/send_delete_code', {
        method: 'POST',
        headers: Object.assign({'Content-Type': 'application/json'}, getAuthHeaders()),
        body: JSON.stringify({ password: password })
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) {
            throw new Error((result.data && result.data.detail) || '注销验证码发送失败');
        }
        if (errorEl) {
            errorEl.textContent = result.data.message || '注销验证码已发送';
            errorEl.style.display = '';
            errorEl.style.color = '#8fd3a8';
        }
        startCountdown(sendBtn, 60, '发送验证码');
    })
    .catch(function(err) {
        if (errorEl) {
            errorEl.textContent = err.message || '注销验证码发送失败';
            errorEl.style.display = '';
            errorEl.style.color = '#ff6b81';
        }
    });
}

function deleteCurrentAccount() {
    var passwordEl = $r('#delete-account-password');
    var codeEl = $r('#delete-account-code');
    var confirmEl = $r('#delete-account-confirm');
    var errorEl = $r('#delete-account-error');
    var confirmBtn = $r('#delete-account-confirm-btn');
    var password = (passwordEl && passwordEl.value || '').trim();
    var code = (codeEl && codeEl.value || '').trim();
    var confirmText = (confirmEl && confirmEl.value || '').trim();

    if (errorEl) {
        errorEl.style.color = '#ff6b81';
    }
    if (!password) {
        if (errorEl) {
            errorEl.textContent = '请输入当前密码';
            errorEl.style.display = '';
        }
        return;
    }
    if (!code) {
        if (errorEl) {
            errorEl.textContent = '请输入邮箱验证码';
            errorEl.style.display = '';
        }
        return;
    }
    if (confirmText !== '注销账户') {
        if (errorEl) {
            errorEl.textContent = '请输入“注销账户”以确认';
            errorEl.style.display = '';
        }
        return;
    }

    if (confirmBtn) {
        confirmBtn.disabled = true;
        confirmBtn.textContent = '注销中...';
    }

    fetch('/api/auth/delete_account', {
        method: 'DELETE',
        headers: Object.assign({'Content-Type': 'application/json'}, getAuthHeaders()),
        body: JSON.stringify({
            password: password,
            confirm_text: confirmText,
            code: code
        })
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) {
            throw new Error((result.data && result.data.detail) || '注销账户失败');
        }
        hideDeleteAccountModal();
        hideSettingsModal();
        logoutUser();
        if (typeof showToast === 'function') {
            showToast(result.data.message || '账户已注销');
        }
        rNavigate('home');
    })
    .catch(function(err) {
        if (errorEl) {
            errorEl.textContent = err.message || '注销账户失败';
            errorEl.style.display = '';
        }
    })
    .finally(function() {
        if (confirmBtn) {
            confirmBtn.disabled = false;
            confirmBtn.textContent = '确认注销';
        }
    });
}

function showStorageSettingsModal() {
    var modal = $r('#storage-settings-modal');
    if (!modal) return;
    var pathInput = $r('#storage-path-input');
    if (pathInput) pathInput.value = localStorage.getItem('cc_storage_path') || '';
    refreshDiskSpace();
    modal.style.display = 'flex';
}

function hideStorageSettingsModal() {
    var modal = $r('#storage-settings-modal');
    if (modal) modal.style.display = 'none';
}

function refreshDiskSpace() {
    var el = $r('#storage-disk-space');
    if (!el) return;
    try {
        navigator.storage.estimate().then(function(estimate) {
            if (estimate.quota && estimate.usage) {
                var freeGB = ((estimate.quota - estimate.usage) / (1024 * 1024 * 1024)).toFixed(2);
                el.textContent = freeGB + ' GB';
            } else {
                el.textContent = '--';
            }
        }).catch(function() { el.textContent = '--'; });
    } catch(e) { el.textContent = '--'; }
}

function showProjectsListModal() {
    var modal = $r('#projects-list-modal');
    var container = $r('#projects-list-container');
    var token = localStorage.getItem('cc_token');

    if (!token) { rNavigate('login'); return; }

    if (container) container.innerHTML = '<p style="text-align:center;">加载中...</p>';
    if (modal) modal.style.display = 'flex';

    fetch('/api/projects/', {
        method: 'GET',
        headers: { 'Authorization': 'Bearer ' + token },
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!container) return;
        if (!result.ok) {
            container.innerHTML = '<p style="text-align:center;color:#e94560;">加载失败</p>';
            return;
        }
        var projects = result.data;
        if (!projects || projects.length === 0) {
            container.innerHTML = '<p style="text-align:center;">暂无工程记录</p>';
            return;
        }
        var html = '';
        projects.forEach(function(p) {
            var isVideo = p.type === 'video';
            var dotColor = isVideo ? '#6ba4e0' : '#e87088';
            var typeLabel = isVideo ? '视频' : '图片';
            var created = p.created_at ? p.created_at.slice(0, 19).replace('T', ' ') : '';
            var safeName = escapeHtml(p.name || '');
            html += '<div style="background:#151525; padding:12px; margin-bottom:8px; border-radius:6px; display:flex; justify-content:space-between; align-items:center;">';
            html += '<div style="display:flex; align-items:center; gap:8px;">';
            html += '<span style="display:inline-block; width:10px; height:10px; border-radius:50%; background:' + dotColor + '; flex-shrink:0;"></span>';
            html += '<div><div style="color:#e8e8f0; font-weight:600;">' + safeName + '</div>';
            html += '<div style="font-size:12px; color:#888;">' + typeLabel + ' | ' + created + '</div></div></div>';
            html += '<button class="auth-btn" style="width:auto; padding:6px 16px; font-size:12px;" data-project-id="' + p.id + '" data-project-name="' + safeName + '" data-project-type="' + (p.type || 'image') + '">进入</button>';
            html += '</div>';
        });
        container.innerHTML = html;

        container.querySelectorAll('button[data-project-id]').forEach(function(btn) {
            btn.addEventListener('click', function() {
                var pid = parseInt(btn.getAttribute('data-project-id'));
                var ptype = btn.getAttribute('data-project-type') || 'image';
                var doEnter = function() {
                    window.currentProjectId = pid;
                    window._pendingProjectType = ptype;
                    if (modal) modal.style.display = 'none';
                    rNavigate('workspace');
                };
                if (window.currentProjectId && window.currentProjectId !== pid) {
                    if (typeof confirmExitProject === 'function') confirmExitProject(doEnter, window.currentProjectId);
                    else doEnter();
                } else {
                    doEnter();
                }
            });
        });
    })
    .catch(function(err) {
        if (container) container.innerHTML = '<p style="text-align:center;color:#e94560;">网络错误</p>';
    });
}

function hideProjectsListModal() {
    var modal = $r('#projects-list-modal');
    if (modal) modal.style.display = 'none';
}

function loadHomeProjects() {
    loadHomeProjectsGrid();
}

function loadHomeProjectsLocal() {
    loadHomeProjectsGrid();
}

function loadHomeProjectsGrid() {
    var grid = $r('#home-project-grid');
    var countEl = $r('#home-project-count');
    if (!grid) return;
    grid.innerHTML = '<div class="home-project-card-new" id="home-new-project-card"><div class="home-project-card-new-icon">+</div><div class="home-project-card-new-text">新建项目</div></div>';

    var token = localStorage.getItem('cc_token');
    if (!token) {
        if (countEl) countEl.textContent = '0';
        return;
    }

    fetch('/api/projects/', {
        method: 'GET',
        headers: { 'Authorization': 'Bearer ' + token },
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok || !result.data) {
            if (countEl) countEl.textContent = '0';
            return;
        }
        var projects = result.data;
        if (countEl) countEl.textContent = projects.length;
        var html = '';
        projects.forEach(function(p) {
            var typeClass = p.type === 'video' ? 'project-type-video' : 'project-type-image';
            var created = p.created_at ? p.created_at.slice(0, 10) : '';
            var safeName = escapeHtml(p.name || '');
            var safeNameAttr = escapeAttr(p.name || '');
            html += '<div class="home-project-card ' + typeClass + '" data-project-id="' + p.id + '" data-project-name="' + safeNameAttr + '">';
            html += '<div class="home-project-card-thumb"></div>';
            html += '<div class="home-project-card-info">';
            html += '<div class="home-project-card-name">' + safeName + '</div>';
            html += '<div class="home-project-card-date">' + created + '</div>';
            html += '</div></div>';
        });
        grid.insertAdjacentHTML('beforeend', html);

        var newCard = $r('#home-new-project-card');
        if (newCard) {
            newCard.addEventListener('click', function() {
                showTypeSelectModal();
            });
        }

        grid.querySelectorAll('.home-project-card').forEach(function(card) {
            card.addEventListener('click', function() {
                var pid = parseInt(card.getAttribute('data-project-id'));
                if (!pid) return;
                window.currentProjectId = pid;
                window._pendingProjectType = card.classList.contains('project-type-video') ? 'video' : 'image';
                rNavigate('workspace');
            });

            card.addEventListener('contextmenu', function(e) {
                e.preventDefault();
                showContextMenu(e, card);
            });
        });
    })
    .catch(function() {
        if (countEl) countEl.textContent = '0';
    });
}

function setHomeSurfaceVisibility(options) {
    var funcCards = $r('#home-func-cards');
    var sep = $r('.home-section-sep');
    var subtitle = $r('.home-section-subtitle');
    var searchInput = $r('.home-search-input');
    var filterBar = $r('.home-filter-bar');
    var trashToolbar = $r('#trash-toolbar');
    var grid = $r('#home-project-grid');
    var spacePanel = $r('#space-panel');
    var trainPanel = $r('#train-panel');
    var banner = $r('#admin-welcome-banner');

    if (funcCards) funcCards.style.display = options.showCards ? '' : 'none';
    if (sep) sep.style.display = options.showCards ? '' : 'none';
    if (subtitle) subtitle.style.display = options.showSubtitle ? '' : 'none';
    if (searchInput) searchInput.style.display = options.showSearch ? '' : 'none';
    if (filterBar) filterBar.style.display = options.showFilter ? '' : 'none';
    if (trashToolbar) trashToolbar.style.display = options.showTrashToolbar ? '' : 'none';
    if (grid) grid.style.display = options.showGrid ? '' : 'none';
    if (spacePanel) spacePanel.style.display = options.showSpace ? '' : 'none';
    if (trainPanel) trainPanel.style.display = options.showTrain ? '' : 'none';
    if (banner) banner.style.display = options.showBanner && isAdminUser() ? '' : 'none';
}

function showTypeSelectModal() {
    var modal = $r('#type-select-modal');
    if (modal) modal.style.display = 'flex';
}

function hideTypeSelectModal() {
    var modal = $r('#type-select-modal');
    if (modal) modal.style.display = 'none';
}

function selectProjectType(type) {
    hideTypeSelectModal();
    showProjectModal(type);
}

var contextMenuTargetId = null;

function showContextMenu(e, card) {
    var menu = $r('#project-context-menu');
    if (!menu) return;
    contextMenuTargetId = parseInt(card.getAttribute('data-project-id'));
    menu.style.display = '';
    menu.style.left = e.clientX + 'px';
    menu.style.top = e.clientY + 'px';
}

function hideContextMenu() {
    var menu = $r('#project-context-menu');
    if (menu) menu.style.display = 'none';
    contextMenuTargetId = null;
}

function startInlineRename(projectId) {
    var card = document.querySelector('.home-project-card[data-project-id="' + projectId + '"]');
    if (!card) return;
    var nameEl = card.querySelector('.home-project-card-name');
    if (!nameEl) return;
    var oldName = nameEl.textContent;
    nameEl.style.display = 'none';
    var input = document.createElement('input');
    input.type = 'text';
    input.value = oldName;
    input.style.cssText = 'width:100%;background:#2a2a4e;border:1px solid #5a5af0;color:#e8e8f0;padding:4px 6px;border-radius:4px;font-size:13px;outline:none;box-sizing:border-box;';
    nameEl.parentNode.insertBefore(input, nameEl);
    input.focus();
    input.select();
    var commit = function() {
        var newName = input.value.trim();
        input.remove();
        nameEl.style.display = '';
        if (!newName || newName === oldName) return;
        var token = localStorage.getItem('cc_token');
        fetch('/api/projects/' + projectId, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
            body: JSON.stringify({ name: newName })
        })
        .then(function() { loadHomeProjectsGrid(); })
        .catch(function() { loadHomeProjectsGrid(); });
    };
    input.addEventListener('blur', commit);
    input.addEventListener('keydown', function(e) { if (e.key === 'Enter') commit(); });
}

function confirmMoveToTrash() {
    if (!contextMenuTargetId) return;
    if (!confirm('确定将此项目移入回收站吗？30天内可从回收站恢复。')) return;
    var token = localStorage.getItem('cc_token');
    fetch('/api/projects/' + contextMenuTargetId, {
        method: 'DELETE',
        headers: { 'Authorization': 'Bearer ' + token },
    })
    .then(function(resp) {
        if (resp.ok) {
            hideContextMenu();
            loadHomeProjectsGrid();
        } else {
            alert('操作失败');
        }
    })
    .catch(function() { alert('操作失败'); });
}

function confirmPermanentDeleteFromMenu() {
    if (!contextMenuTargetId) return;
    if (!confirm('确定永久删除此项目吗？此操作不可撤销！')) return;
    var token = localStorage.getItem('cc_token');
    fetch('/api/projects/' + contextMenuTargetId + '/permanent', {
        method: 'DELETE',
        headers: { 'Authorization': 'Bearer ' + token },
    })
    .then(function(resp) {
        if (resp.ok) {
            hideContextMenu();
            loadHomeProjectsGrid();
        } else {
            alert('删除失败');
        }
    })
    .catch(function() { alert('删除失败'); });
}

function loadTrashProjects() {
    var grid = document.getElementById('home-project-grid');
    if (!grid) return;
    grid.innerHTML = '<p style="color:#888;text-align:center;padding:40px;">加载中...</p>';
    var token = localStorage.getItem('cc_token');
    if (!token) {
        grid.innerHTML = '<p style="color:#888;text-align:center;padding:40px;">请先登录</p>';
        return;
    }
    fetch('/api/projects/trash/', {
        method: 'GET',
        headers: { 'Authorization': 'Bearer ' + token }
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok || !result.data || !result.data.length) {
            grid.innerHTML = '<p style="color:#888;text-align:center;padding:40px;">回收站为空</p>';
            return;
        }
        var projects = result.data;
        var html = '';
        projects.forEach(function(p) {
            var typeClass = p.type === 'video' ? 'project-type-video' : 'project-type-image';
            var safeName = escapeHtml(p.name || '');
            var safeNameAttr = escapeAttr(p.name || '');
            html += '<div class="home-project-card home-project-card-trashed ' + typeClass + '" data-project-id="' + p.id + '" data-project-name="' + safeNameAttr + '">';
            html += '<div class="home-project-card-thumb"></div>';
            html += '<div class="home-project-card-info">';
            html += '<div class="home-project-card-name">' + safeName + '</div>';
            html += '<div class="home-project-card-date">' + (p.created_at ? p.created_at.slice(0, 10) : '') + '</div>';
            if (p.deleted_at) {
                var delDate = new Date(p.deleted_at);
                var expireDate = new Date(delDate.getTime() + 30 * 24 * 60 * 60 * 1000);
                var now = new Date();
                var remainDays = Math.ceil((expireDate - now) / (24 * 60 * 60 * 1000));
                var remainClass = remainDays <= 7 ? ' warning' : '';
                html += '<div class="home-project-card-remaining' + remainClass + '">剩余 ' + remainDays + ' 天</div>';
            }
            html += '</div></div>';
        });
        grid.innerHTML = html;

        grid.querySelectorAll('.home-project-card').forEach(function(card) {
            card.addEventListener('click', function() {
                alert('回收站中的项目无法直接打开，请先恢复。');
            });

            card.addEventListener('contextmenu', function(e) {
                e.preventDefault();
                showTrashContextMenu(e, card);
            });
        });
    })
    .catch(function() {
        grid.innerHTML = '<p style="color:#888;text-align:center;padding:40px;">加载失败</p>';
    });
}

function restoreProject(projectId) {
    var token = localStorage.getItem('cc_token');
    if (!token) return;
    fetch('/api/projects/' + projectId + '/restore', {
        method: 'PUT',
        headers: { 'Authorization': 'Bearer ' + token }
    })
    .then(function(resp) {
        if (resp.ok) {
            hideTrashContextMenu();
            loadTrashProjects();
        }
    })
    .catch(function() {});
}

function permanentlyDeleteProject(projectId) {
    if (!confirm('确定永久删除吗？此操作不可撤销。')) return;
    var token = localStorage.getItem('cc_token');
    if (!token) return;
    fetch('/api/projects/' + projectId + '/permanent', {
        method: 'DELETE',
        headers: { 'Authorization': 'Bearer ' + token }
    })
    .then(function(resp) {
        if (resp.ok) {
            hideTrashContextMenu();
            loadTrashProjects();
        }
    })
    .catch(function() {});
}

function emptyTrash() {
    if (!confirm('确定清空回收站吗？所有项目将被永久删除且不可恢复。')) return;
    var token = localStorage.getItem('cc_token');
    if (!token) return;
    fetch('/api/projects/trash/empty', {
        method: 'DELETE',
        headers: { 'Authorization': 'Bearer ' + token }
    })
    .then(function(resp) {
        if (resp.ok) loadTrashProjects();
    })
    .catch(function() {});
}

function showTrashContextMenu(e, card) {
    var menu = $r('#trash-context-menu');
    if (!menu) return;
    menu.setAttribute('data-target-id', card.getAttribute('data-project-id'));
    hideContextMenu();
    menu.style.display = '';
    menu.style.left = e.clientX + 'px';
    menu.style.top = e.clientY + 'px';
}

function hideTrashContextMenu() {
    var menu = $r('#trash-context-menu');
    if (menu) { menu.style.display = 'none'; menu.removeAttribute('data-target-id'); }
}

function getTrashMenuTargetId() {
    var menu = $r('#trash-context-menu');
    if (!menu) return null;
    var id = menu.getAttribute('data-target-id');
    return id ? parseInt(id) : null;
}

function initRouter() {
    var homeNavItems = document.querySelectorAll('.home-nav-item');
    homeNavItems.forEach(function(item) {
        item.addEventListener('click', function() {
            var nav = item.getAttribute('data-nav');
            if (nav === 'train' && !isAdminUser()) return;

            homeNavItems.forEach(function(el) { el.classList.remove('active'); });
            item.classList.add('active');

            var titleEl = $r('#home-content-title');
            if (titleEl) {
                var titles = { home: '我的主页', local: '本地项目', space: '个人空间', train: '模型训练', trash: '回收站' };
                titleEl.textContent = titles[nav] || nav;
            }

            if (nav === 'home') {
                setHomeSurfaceVisibility({
                    showCards: true,
                    showSubtitle: true,
                    showSearch: true,
                    showFilter: true,
                    showTrashToolbar: false,
                    showGrid: true,
                    showSpace: false,
                    showTrain: false,
                    showBanner: true
                });
                loadHomeProjects();
            } else if (nav === 'local') {
                setHomeSurfaceVisibility({
                    showCards: false,
                    showSubtitle: false,
                    showSearch: true,
                    showFilter: true,
                    showTrashToolbar: false,
                    showGrid: true,
                    showSpace: false,
                    showTrain: false,
                    showBanner: false
                });
                loadHomeProjectsLocal();
            } else if (nav === 'space') {
                setHomeSurfaceVisibility({
                    showCards: false,
                    showSubtitle: false,
                    showSearch: false,
                    showFilter: false,
                    showTrashToolbar: false,
                    showGrid: false,
                    showSpace: true,
                    showTrain: false,
                    showBanner: true
                });
                renderAdminSpaceDashboard();
                if (isAdminUser()) fetchAdminDashboard(false);
                else fetchUserSpaceDashboard(false);
            } else if (nav === 'train') {
                setHomeSurfaceVisibility({
                    showCards: false,
                    showSubtitle: false,
                    showSearch: false,
                    showFilter: false,
                    showTrashToolbar: false,
                    showGrid: false,
                    showSpace: false,
                    showTrain: true,
                    showBanner: false
                });
                if (isAdminUser()) {
                    fetchAdminDashboard(false).then(function() {
                        initTrainingWorkbench();
                    });
                } else {
                    initTrainingWorkbench();
                }
            } else if (nav === 'trash') {
                setHomeSurfaceVisibility({
                    showCards: false,
                    showSubtitle: false,
                    showSearch: false,
                    showFilter: false,
                    showTrashToolbar: true,
                    showGrid: true,
                    showSpace: false,
                    showTrain: false,
                    showBanner: false
                });
                loadTrashProjects();
            } else {
                setHomeSurfaceVisibility({
                    showCards: false,
                    showSubtitle: false,
                    showSearch: false,
                    showFilter: false,
                    showTrashToolbar: false,
                    showGrid: true,
                    showSpace: false,
                    showTrain: false,
                    showBanner: false
                });
                var grid = $r('#home-project-grid');
                if (grid) grid.innerHTML = '<p style="color:#888;text-align:center;padding:40px;">暂无内容</p>';
            }
        });
    });

    var filterTags = document.querySelectorAll('.home-filter-tag');
    filterTags.forEach(function(tag) {
        tag.addEventListener('click', function() {
            filterTags.forEach(function(el) { el.classList.remove('active'); });
            tag.classList.add('active');
        });
    });

    var searchInput = $r('.home-search-input');
    if (searchInput) {
        var clearHomeSearchInput = function() {
            searchInput.value = '';
            searchInput.setAttribute('value', '');
        };
        clearHomeSearchInput();
        window.setTimeout(clearHomeSearchInput, 0);
        window.setTimeout(clearHomeSearchInput, 120);
        searchInput.addEventListener('input', function() {
            var query = (searchInput.value || '').toLowerCase();
            var cards = document.querySelectorAll('.home-project-card');
            cards.forEach(function(card) {
                var nameEl = card.querySelector('.home-project-card-name');
                var name = nameEl ? nameEl.textContent.toLowerCase() : '';
                card.style.display = name.indexOf(query) !== -1 ? '' : 'none';
            });
            var newCard = $r('#home-new-project-card');
            if (newCard) newCard.style.display = query ? 'none' : '';
        });
    }

    var funcCards = document.querySelectorAll('.card');
    funcCards.forEach(function(card) {
        card.addEventListener('click', function() {
            if (!isLoggedIn()) { rNavigate('login'); return; }
            var type = card.getAttribute('data-type');
            showProjectModal(type);
        });
    });

    var selectTypeImage = $r('#select-type-image');
    if (selectTypeImage) {
        selectTypeImage.addEventListener('click', function() { selectProjectType('image'); });
    }
    var selectTypeVideo = $r('#select-type-video');
    if (selectTypeVideo) {
        selectTypeVideo.addEventListener('click', function() { selectProjectType('video'); });
    }
    var typeSelectCancelBtn = $r('#type-select-cancel-btn');
    if (typeSelectCancelBtn) {
        typeSelectCancelBtn.addEventListener('click', hideTypeSelectModal);
    }
    var typeSelectModal = $r('#type-select-modal');
    if (typeSelectModal) {
        typeSelectModal.addEventListener('click', function(e) {
            if (e.target === typeSelectModal) hideTypeSelectModal();
        });
    }

    var contextMenu = $r('#project-context-menu');
    if (contextMenu) {
        contextMenu.querySelectorAll('.context-menu-item').forEach(function(item) {
            item.addEventListener('click', function() {
                var action = item.getAttribute('data-action');
                if (action === 'rename') {
                    hideContextMenu();
                    startInlineRename(contextMenuTargetId);
                } else if (action === 'trash') {
                    confirmMoveToTrash();
                } else if (action === 'delete') {
                    confirmPermanentDeleteFromMenu();
                }
            });
        });
    }
    document.addEventListener('click', function(e) {
        if (contextMenu && !contextMenu.contains(e.target)) hideContextMenu();
        var tcm = $r('#trash-context-menu');
        if (tcm && !tcm.contains(e.target)) hideTrashContextMenu();
    });

    var trashContextMenu = $r('#trash-context-menu');
    if (trashContextMenu) {
        trashContextMenu.querySelectorAll('.context-menu-item').forEach(function(item) {
            item.addEventListener('click', function() {
                var action = item.getAttribute('data-action');
                if (action === 'restore') {
                    var tid = getTrashMenuTargetId();
                    hideTrashContextMenu();
                    if (tid) restoreProject(tid);
                } else if (action === 'permanent-delete') {
                    var pid = getTrashMenuTargetId();
                    if (pid) permanentlyDeleteProject(pid);
                }
            });
        });
    }

    var emptyTrashBtn = $r('#empty-trash-btn');
    if (emptyTrashBtn) emptyTrashBtn.addEventListener('click', emptyTrash);

    var navLoginBtn = $r('#nav-login-btn');
    if (navLoginBtn) {
        navLoginBtn.addEventListener('click', function() { rNavigate('login'); });
    }

    var loginToRegister = $r('#login-to-register');
    if (loginToRegister) {
        loginToRegister.addEventListener('click', function() { rNavigate('register'); });
    }
    var registerToLogin = $r('#register-to-login');
    if (registerToLogin) {
        registerToLogin.addEventListener('click', function() { rNavigate('login'); });
    }

    var navSettingsBtn = $r('#nav-settings-btn');
    if (navSettingsBtn) {
        navSettingsBtn.addEventListener('click', function() {
            showSettingsModal();
        });
    }

    var navNoticeBtn = $r('#nav-notice-btn');
    if (navNoticeBtn) {
        navNoticeBtn.addEventListener('click', function() {
            showNoticeModal();
        });
    }

    var navContactBtn = $r('#nav-contact-btn');
    if (navContactBtn) {
        navContactBtn.addEventListener('click', function() {
            showContactModal();
        });
    }

    var workspaceSettingsBtn = $r('#workspace-settings-btn');
    if (workspaceSettingsBtn) {
        workspaceSettingsBtn.addEventListener('click', function() {
            showSettingsModal();
        });
    }

    var workspaceNoticeBtn = $r('#workspace-notice-btn');
    if (workspaceNoticeBtn) {
        workspaceNoticeBtn.addEventListener('click', function() {
            showNoticeModal();
        });
    }

    var workspaceContactBtn = $r('#workspace-contact-btn');
    if (workspaceContactBtn) {
        workspaceContactBtn.addEventListener('click', function() {
            showContactModal();
        });
    }

    var noticeAdminSave = $r('#notice-admin-save');
    if (noticeAdminSave) {
        noticeAdminSave.addEventListener('click', function() {
            var titleEl = $r('#notice-admin-title');
            var bodyEl = $r('#notice-admin-body');
            if (titleEl) {
                titleEl.disabled = false;
                titleEl.readOnly = false;
            }
            if (bodyEl) {
                bodyEl.disabled = false;
                bodyEl.readOnly = false;
            }
            fetch('/api/admin/portal_messages', {
                method: 'POST',
                headers: Object.assign({ 'Content-Type': 'application/json' }, getAuthHeaders()),
                body: JSON.stringify({
                    notice_title: titleEl ? titleEl.value : '',
                    notice_body: bodyEl ? bodyEl.value : ''
                })
            })
            .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
            .then(function(result) {
                if (!result.ok) {
                    throw new Error((result.data && result.data.detail) || '发布通知失败');
                }
                portalNoticeSelection = {};
                portalMessagesCache = result.data;
                renderPortalMessages();
                renderPortalAdminNoticeHistory();
                updatePortalMessageMode();
                if (typeof showToast === 'function') showToast('通知已发布');
            })
            .catch(function(err) {
                if (typeof showToast === 'function') showToast(err.message || '发布通知失败');
            });
        });
    }

    var noticeAdminSelectAll = $r('#notice-admin-select-all');
    if (noticeAdminSelectAll) {
        noticeAdminSelectAll.addEventListener('change', function() {
            var notice = portalMessagesCache && portalMessagesCache.notice ? portalMessagesCache.notice : {};
            var items = Array.isArray(notice.items) ? notice.items : [];
            if (this.checked) {
                items.forEach(function(item) {
                    var id = String(item.id || '');
                    if (id) portalNoticeSelection[id] = true;
                });
            } else {
                portalNoticeSelection = {};
            }
            renderPortalAdminNoticeHistory();
        });
    }

    var noticeAdminDeleteSelected = $r('#notice-admin-delete-selected');
    if (noticeAdminDeleteSelected) {
        noticeAdminDeleteSelected.addEventListener('click', function() {
            deletePortalNoticeItems(getSelectedPortalNoticeIds());
        });
    }

    var contactAdminSave = $r('#contact-admin-save');
    if (contactAdminSave) {
        contactAdminSave.addEventListener('click', function() {
            var qqEl = $r('#contact-admin-qq');
            var notesEl = $r('#contact-admin-notes');
            if (qqEl) {
                qqEl.disabled = false;
                qqEl.readOnly = false;
            }
            if (notesEl) {
                notesEl.disabled = false;
                notesEl.readOnly = false;
            }
            fetch('/api/admin/portal_messages', {
                method: 'POST',
                headers: Object.assign({ 'Content-Type': 'application/json' }, getAuthHeaders()),
                body: JSON.stringify({
                    contact_qq: qqEl ? qqEl.value : '',
                    contact_notes: notesEl ? notesEl.value : ''
                })
            })
            .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
            .then(function(result) {
                if (!result.ok) {
                    throw new Error((result.data && result.data.detail) || '更新联系方式失败');
                }
                portalMessagesCache = result.data;
                renderPortalMessages();
                updatePortalMessageMode();
                if (typeof showToast === 'function') showToast('联系方式已更新');
            })
            .catch(function(err) {
                if (typeof showToast === 'function') showToast(err.message || '更新联系方式失败');
            });
        });
    }

    var workspaceHomeBtn = $r('#workspace-home-btn');
    if (workspaceHomeBtn) {
        workspaceHomeBtn.addEventListener('click', function() {
            window.goToHome();
        });
    }

    var settingsModal = $r('#settings-modal');
    if (settingsModal) {
        settingsModal.addEventListener('click', function(e) {
            if (e.target === settingsModal) hideSettingsModal();
        });
        var menuItems = settingsModal.querySelectorAll('.settings-menu-item[data-action]');
        menuItems.forEach(function(item) {
            item.addEventListener('click', function() {
                var action = this.getAttribute('data-action');
                if (action === 'storage') { showStorageSettingsModal(); }
                else if (action === 'export') {
                hideSettingsModal();
                rNavigate('home');
            }
            });
        });
    }

    var noticeModal = $r('#notice-modal');
    if (noticeModal) {
        noticeModal.addEventListener('click', function(e) {
            if (e.target === noticeModal) hideNoticeModal();
        });
    }
    var noticeModalClose = $r('#notice-modal-close');
    if (noticeModalClose) {
        noticeModalClose.addEventListener('click', hideNoticeModal);
    }

    var contactModal = $r('#contact-modal');
    if (contactModal) {
        contactModal.addEventListener('click', function(e) {
            if (e.target === contactModal) hideContactModal();
        });
    }
    var contactModalClose = $r('#contact-modal-close');
    if (contactModalClose) {
        contactModalClose.addEventListener('click', hideContactModal);
    }

    var logoutBtn = $r('#settings-logout-btn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', function() {
            hideSettingsModal();
            if (window.exitWorkspace) {
                window.exitWorkspace();
            } else {
                logoutUser();
                rNavigate('home');
            }
        });
    }

    var deleteAccountBtn = $r('#settings-delete-account-btn');
    if (deleteAccountBtn) {
        deleteAccountBtn.addEventListener('click', function() {
            var user = JSON.parse(localStorage.getItem('cc_user') || '{}');
            if (user && user.role === 'admin') {
                hideSettingsModal();
                if (typeof showToast === 'function') {
                    showToast('管理员账号禁止注销');
                }
                return;
            }
            hideSettingsModal();
            showDeleteAccountModal();
        });
    }

    var deleteAccountModal = $r('#delete-account-modal');
    if (deleteAccountModal) {
        deleteAccountModal.addEventListener('click', function(e) {
            if (e.target === deleteAccountModal) hideDeleteAccountModal();
        });
    }

    var deleteCancelBtn = $r('#delete-account-cancel-btn');
    if (deleteCancelBtn) {
        deleteCancelBtn.addEventListener('click', function() {
            hideDeleteAccountModal();
        });
    }

    var deleteSendCodeBtn = $r('#delete-account-send-code-btn');
    if (deleteSendCodeBtn) {
        deleteSendCodeBtn.addEventListener('click', function() {
            sendDeleteAccountCode();
        });
    }

    var deleteConfirmBtn = $r('#delete-account-confirm-btn');
    if (deleteConfirmBtn) {
        deleteConfirmBtn.addEventListener('click', function() {
            deleteCurrentAccount();
        });
    }

    var changePasswordModal = $r('#change-password-modal');
    if (changePasswordModal) {
        changePasswordModal.addEventListener('click', function(e) {
            if (e.target === changePasswordModal) hideChangePasswordModal();
        });
    }

    var changePasswordCancelBtn = $r('#change-password-cancel-btn');
    if (changePasswordCancelBtn) {
        changePasswordCancelBtn.addEventListener('click', function() {
            hideChangePasswordModal();
        });
    }

    var changePasswordSendCodeBtn = $r('#change-password-send-code-btn');
    if (changePasswordSendCodeBtn) {
        changePasswordSendCodeBtn.addEventListener('click', function() {
            sendChangePasswordCode();
        });
    }

    var changePasswordConfirmBtn = $r('#change-password-confirm-btn');
    if (changePasswordConfirmBtn) {
        changePasswordConfirmBtn.addEventListener('click', function() {
            submitChangePassword();
        });
    }

    var homeThemeToggle = $r('#home-theme-toggle');
    if (homeThemeToggle) {
        homeThemeToggle.addEventListener('click', function() {
            toggleTheme();
        });
    }

    var settingsThemeToggle = $r('#settings-theme-toggle');
    if (settingsThemeToggle) {
        settingsThemeToggle.addEventListener('click', function() {
            toggleTheme();
        });
    }

    var editNameBtn = $r('#settings-edit-name-btn');
    if (editNameBtn) {
        editNameBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            var editArea = $r('#settings-name-edit-area');
            var nicknameInput = $r('#settings-nickname-input');
            if (editArea) editArea.style.display = '';
            if (nicknameInput) {
                nicknameInput.value = localStorage.getItem('cc_nickname') || '';
                nicknameInput.focus();
            }
        });
    }

    var nicknameInput = $r('#settings-nickname-input');
    if (nicknameInput) {
        function saveNickname() {
            var val = (nicknameInput.value || '').trim();
            if (val) {
                localStorage.setItem('cc_nickname', val);
                var usernameEl = $r('#settings-username');
                if (usernameEl) usernameEl.textContent = val;
            }
            var editArea = $r('#settings-name-edit-area');
            if (editArea) editArea.style.display = 'none';
        }
        nicknameInput.addEventListener('blur', function() { saveNickname(); });
        nicknameInput.addEventListener('keydown', function(e) { if (e.key === 'Enter') saveNickname(); });
    }

    var storagePickBtn = $r('#storage-pick-folder-btn');
    if (storagePickBtn) {
        storagePickBtn.addEventListener('click', async function() {
            try {
                var handle = await window.showDirectoryPicker({ mode: 'readwrite' });
                var pathInput = $r('#storage-path-input');
                if (pathInput) pathInput.value = handle.name;
                window._pendingStorageHandle = handle;
            } catch(e) {}
        });
    }

    var storageSaveBtn = $r('#storage-save-btn');
    if (storageSaveBtn) {
        storageSaveBtn.addEventListener('click', function() {
            var pathInput = $r('#storage-path-input');
            if (pathInput && pathInput.value) {
                localStorage.setItem('cc_storage_path', pathInput.value);
            }
            hideStorageSettingsModal();
        });
    }

    var storageCancelBtn = $r('#storage-cancel-btn');
    if (storageCancelBtn) {
        storageCancelBtn.addEventListener('click', function() {
            hideStorageSettingsModal();
        });
    }

    var authLinks = document.querySelectorAll('.auth-link-action');
    authLinks.forEach(function(link) {
        link.addEventListener('click', function() {
            var nav = link.getAttribute('data-nav');
            if (nav) rNavigate(nav);
        });
    });

    var loginSubmitBtn = $r('#login-submit-btn');
    if (loginSubmitBtn) {
        loginSubmitBtn.addEventListener('click', function() { submitLogin(); });
    }

    var registerSubmitBtn = $r('#register-submit-btn');
    if (registerSubmitBtn) {
        registerSubmitBtn.addEventListener('click', function() { submitRegister(); });
    }

    var createConfirmBtn = $r('#create-project-confirm-btn');
    if (createConfirmBtn) {
        createConfirmBtn.addEventListener('click', function(e) { e.preventDefault(); createProjectAndEnter(); });
    }

    var createCancelBtn = $r('#create-project-cancel-btn');
    if (createCancelBtn) {
        createCancelBtn.addEventListener('click', function() { hideProjectModal(); });
    }

    var projectsListCloseBtn = $r('#projects-list-close-btn');
    if (projectsListCloseBtn) {
        projectsListCloseBtn.addEventListener('click', function() { hideProjectsListModal(); });
    }

    document.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            if (currentView === 'login') submitLogin();
            if (currentView === 'register') submitRegister();
        }
        if (e.key === 'Escape') {
            hideProjectModal();
            hideSettingsModal();
            hideStorageSettingsModal();
            hideProjectsListModal();
        }
    });

    var projectModal = $r('#create-project-modal');
    if (projectModal) {
        projectModal.addEventListener('click', function(e) {
            if (e.target === projectModal) hideProjectModal();
        });
    }

    var projListModal = $r('#projects-list-modal');
    if (projListModal) {
        projListModal.addEventListener('click', function(e) {
            if (e.target === projListModal) hideProjectsListModal();
        });
    }

    loadHomeProjectsGrid = function() {
        var grid = $r('#home-project-grid');
        var countEl = $r('#home-project-count');
        if (!grid) return;

        var requestSeq = ++homeProjectsRequestSeq;
        var newCardHtml = '<div class="home-project-card-new" id="home-new-project-card"><div class="home-project-card-new-icon">+</div><div class="home-project-card-new-text">新建项目</div></div>';
        var lastStableHtml = grid.innerHTML;
        if (!lastStableHtml) {
            grid.innerHTML = newCardHtml;
            lastStableHtml = newCardHtml;
        }

        var token = localStorage.getItem('cc_token');
        if (!token) {
            grid.innerHTML = newCardHtml;
            if (countEl) countEl.textContent = '0';
            return;
        }

        fetch('/api/projects/', {
            method: 'GET',
            headers: { 'Authorization': 'Bearer ' + token },
        })
        .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
        .then(function(result) {
            if (requestSeq !== homeProjectsRequestSeq) return;
            if (!result.ok || !result.data) {
                return;
            }
            var projects = result.data;
            if (countEl) countEl.textContent = projects.length;
            var html = '';
            projects.forEach(function(p) {
                var typeClass = p.type === 'video' ? 'project-type-video' : 'project-type-image';
                var created = p.created_at ? p.created_at.slice(0, 10) : '';
                var safeName = escapeHtml(p.name || '');
                var safeNameAttr = escapeAttr(p.name || '');
                html += '<div class="home-project-card ' + typeClass + '" data-project-id="' + p.id + '" data-project-name="' + safeNameAttr + '">';
                html += '<div class="home-project-card-thumb"></div>';
                html += '<div class="home-project-card-info">';
                html += '<div class="home-project-card-name">' + safeName + '</div>';
                html += '<div class="home-project-card-date">' + created + '</div>';
                html += '</div></div>';
            });
            grid.innerHTML = newCardHtml + html;

            var newCard = $r('#home-new-project-card');
            if (newCard) {
                newCard.addEventListener('click', function() {
                    showTypeSelectModal();
                });
            }

            grid.querySelectorAll('.home-project-card').forEach(function(card) {
                card.addEventListener('click', function() {
                    var pid = parseInt(card.getAttribute('data-project-id'));
                    if (!pid) return;
                    window.currentProjectId = pid;
                    window._pendingProjectType = card.classList.contains('project-type-video') ? 'video' : 'image';
                    rNavigate('workspace');
                });

                card.addEventListener('contextmenu', function(e) {
                    e.preventDefault();
                    showContextMenu(e, card);
                });
            });
        })
        .catch(function() {
            if (requestSeq !== homeProjectsRequestSeq) return;
            grid.innerHTML = lastStableHtml || newCardHtml;
        });
    };

    rNavigate('home');
    updateTopBarAuth();
}

function submitLogin() {
    var accountEl = $r('#login-account');
    var passwordEl = $r('#login-password');
    var errorEl = $r('#login-error');

    var account = (accountEl && accountEl.value || '').trim();
    var password = passwordEl ? passwordEl.value : '';

    if (!account) {
        if (errorEl) { errorEl.textContent = '请输入账号'; errorEl.style.display = ''; }
        return;
    }
    if (!password) {
        if (errorEl) { errorEl.textContent = '请输入密码'; errorEl.style.display = ''; }
        return;
    }
    if (errorEl) errorEl.style.display = 'none';

    fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ account: account, password: password })
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) {
            if (errorEl) { errorEl.textContent = result.data.detail || '登录失败'; errorEl.style.display = ''; }
            return;
        }
        loginUser(result.data.access_token, {
            id: result.data.id || 0,
            phone: result.data.phone || '',
            email: result.data.email || account,
            role: result.data.role
        });
        rNavigate('home');
    })
    .catch(function(err) {
        if (errorEl) { errorEl.textContent = '网络错误，请重试'; errorEl.style.display = ''; }
    });
}

function startCountdown(btn, seconds, doneText) {
    var orig = doneText || btn.textContent;
    btn.disabled = true;
    function tick() {
        if (seconds <= 0) { btn.textContent = orig; btn.disabled = false; return; }
        btn.textContent = seconds + 's';
        seconds--;
        setTimeout(tick, 1000);
    }
    tick();
}

function sendChangePasswordCode() {
    var oldEl = $r('#change-password-old');
    var errorEl = $r('#change-password-error');
    var sendBtn = $r('#change-password-send-code-btn');
    var oldPassword = (oldEl && oldEl.value || '').trim();
    var user = JSON.parse(localStorage.getItem('cc_user') || '{}');

    if (!user || !user.email) {
        if (errorEl) {
            errorEl.textContent = '当前账户未绑定邮箱，暂不支持邮箱验证码改密';
            errorEl.style.display = '';
        }
        return;
    }
    if (!oldPassword) {
        if (errorEl) {
            errorEl.textContent = '请先输入当前密码，再发送验证码';
            errorEl.style.display = '';
        }
        return;
    }

    fetch('/api/auth/send_change_password_code', {
        method: 'POST',
        headers: Object.assign({'Content-Type': 'application/json'}, getAuthHeaders()),
        body: JSON.stringify({ old_password: oldPassword })
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) {
            throw new Error((result.data && result.data.detail) || '修改密码验证码发送失败');
        }
        if (errorEl) {
            errorEl.textContent = result.data.message || '修改密码验证码已发送';
            errorEl.style.display = '';
            errorEl.style.color = '#8fd3a8';
        }
        startCountdown(sendBtn, 60, '发送验证码');
    })
    .catch(function(err) {
        if (errorEl) {
            errorEl.textContent = err.message || '修改密码验证码发送失败';
            errorEl.style.display = '';
            errorEl.style.color = '#ff6b81';
        }
    });
}

function submitChangePassword() {
    var oldEl = $r('#change-password-old');
    var codeEl = $r('#change-password-code');
    var newEl = $r('#change-password-new');
    var confirmEl = $r('#change-password-confirm');
    var errorEl = $r('#change-password-error');
    var oldPassword = (oldEl && oldEl.value || '').trim();
    var code = (codeEl && codeEl.value || '').trim();
    var newPassword = newEl ? newEl.value : '';
    var confirmPassword = confirmEl ? confirmEl.value : '';

    if (!oldPassword) {
        if (errorEl) { errorEl.textContent = '请输入当前密码'; errorEl.style.display = ''; errorEl.style.color = '#ff6b81'; }
        return;
    }
    if (!code) {
        if (errorEl) { errorEl.textContent = '请输入邮箱验证码'; errorEl.style.display = ''; errorEl.style.color = '#ff6b81'; }
        return;
    }
    if (!newPassword) {
        if (errorEl) { errorEl.textContent = '请输入新密码'; errorEl.style.display = ''; errorEl.style.color = '#ff6b81'; }
        return;
    }
    if (newPassword !== confirmPassword) {
        if (errorEl) { errorEl.textContent = '两次输入的新密码不一致'; errorEl.style.display = ''; errorEl.style.color = '#ff6b81'; }
        return;
    }

    fetch('/api/auth/change_password', {
        method: 'POST',
        headers: Object.assign({'Content-Type': 'application/json'}, getAuthHeaders()),
        body: JSON.stringify({
            old_password: oldPassword,
            code: code,
            new_password: newPassword,
            confirm_password: confirmPassword
        })
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) {
            throw new Error((result.data && result.data.detail) || '修改密码失败');
        }
        hideChangePasswordModal();
        if (typeof showToast === 'function') showToast(result.data.message || '密码已修改');
    })
    .catch(function(err) {
        if (errorEl) {
            errorEl.textContent = err.message || '修改密码失败';
            errorEl.style.display = '';
            errorEl.style.color = '#ff6b81';
        }
    });
}

var _sendCodeBtn = $r('#send-code-btn');
if (_sendCodeBtn) {
    _sendCodeBtn.addEventListener('click', function() {
        var accountEl = $r('#register-account');
        var tipEl = $r('#send-code-tip');
        var email = (accountEl && accountEl.value || '').trim();
        if (!email || email.indexOf('@') < 0) {
            if (tipEl) { tipEl.textContent = '请先输入有效的邮箱地址'; tipEl.style.display = ''; }
            return;
        }
        fetch('/api/auth/send_code', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({email: email}),
        })
        .then(function(r) { return r.json().then(function(d) { return {ok:r.ok, data:d}; }); })
        .then(function(r) {
            if (!r.ok) {
                if (tipEl) { tipEl.textContent = r.data.detail || '发送失败'; tipEl.style.display = ''; }
                return;
            }
            if (tipEl) { tipEl.textContent = '验证码已发送，请查收邮件'; tipEl.style.display = ''; }
            startCountdown(_sendCodeBtn, 60);
        })
        .catch(function() {
            if (tipEl) { tipEl.textContent = '网络错误'; tipEl.style.display = ''; }
        });
    });
}

function submitRegister() {
    var accountEl = $r('#register-account');
    var codeEl = $r('#register-code');
    var passwordEl = $r('#register-password');
    var password2El = $r('#register-password2');
    var errorEl = $r('#register-error');

    var email = (accountEl && accountEl.value || '').trim();
    var code = (codeEl && codeEl.value || '').trim();
    var password = passwordEl ? passwordEl.value : '';
    var password2 = password2El ? password2El.value : '';

    if (!email || email.indexOf('@') < 0) {
        if (errorEl) { errorEl.textContent = '请输入有效的邮箱地址'; errorEl.style.display = ''; }
        return;
    }
    if (!code) {
        if (errorEl) { errorEl.textContent = '请输入验证码'; errorEl.style.display = ''; }
        return;
    }
    if (!password) {
        if (errorEl) { errorEl.textContent = '请输入密码'; errorEl.style.display = ''; }
        return;
    }
    if (password !== password2) {
        if (errorEl) { errorEl.textContent = '两次密码不一致'; errorEl.style.display = ''; }
        return;
    }
    if (errorEl) errorEl.style.display = 'none';

    fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email, password: password, code: code })
    })
    .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
    .then(function(result) {
        if (!result.ok) {
            if (errorEl) { errorEl.textContent = result.data.detail || '注册失败'; errorEl.style.display = ''; }
            return;
        }
        alert('注册成功，请登录');
        rNavigate('login');
    })
    .catch(function(err) {
        if (errorEl) { errorEl.textContent = '网络错误，请重试'; errorEl.style.display = ''; }
    });
}

document.addEventListener('DOMContentLoaded', function() {
    initTheme();
    updateAdminVisibility();
    initRouter();
    renderSpacePanelForUser();
    syncCurrentUser();
    getPortalMessages();
});

(function() {
    function getTrainingImageDirValue() {
        var input = $r('#training-image-dir');
        return ((input && input.value) || 'temp_train_data').trim() || 'temp_train_data';
    }

    function syncTrainingDataStatsToView(fileCount, sizeMb, imageDir) {
        if (typeof updateTrainingDataCountDirect === 'function') {
            updateTrainingDataCountDirect(fileCount, sizeMb, imageDir);
            return;
        }
        var countEl = $r('#training-data-count');
        if (countEl) {
            countEl.textContent = String(fileCount || 0) + ' 张 / ' + Number(sizeMb || 0).toFixed(1) + ' MB';
        }
        var dirEl = $r('#training-image-dir');
        if (dirEl && imageDir) {
            dirEl.value = imageDir;
        }
    }

    function fetchTrainingDataStats() {
        var imageDir = getTrainingImageDirValue();
        return fetch('/api/train/data_stats?image_dir=' + encodeURIComponent(imageDir), {
            method: 'GET',
            headers: getAuthHeaders()
        })
        .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
        .then(function(result) {
            if (!result.ok) {
                throw new Error((result.data && result.data.detail) || '训练数据统计加载失败');
            }
            syncTrainingDataStatsToView(
                result.data.training_file_count || 0,
                result.data.training_size_mb || 0,
                result.data.image_dir || imageDir
            );
            return result.data;
        })
        .catch(function(err) {
            if (typeof showToast === 'function') {
                showToast(err.message || '训练数据统计加载失败');
            }
            throw err;
        });
    }

    var originalUpdateTrainingDataCountFromDashboard =
        typeof updateTrainingDataCountFromDashboard === 'function'
            ? updateTrainingDataCountFromDashboard
            : null;

    updateTrainingDataCountFromDashboard = function() {
        return fetchTrainingDataStats().catch(function() {
            if (originalUpdateTrainingDataCountFromDashboard) {
                return originalUpdateTrainingDataCountFromDashboard();
            }
            return null;
        });
    };

    function rebindTrainingDataControls() {
        var refreshBtn = $r('#training-data-refresh');
        if (refreshBtn && !refreshBtn.dataset.statsBound) {
            var refreshClone = refreshBtn.cloneNode(true);
            refreshBtn.parentNode.replaceChild(refreshClone, refreshBtn);
            refreshClone.dataset.statsBound = '1';
            refreshClone.addEventListener('click', function() {
                fetchTrainingDataStats();
            });
        }

        var dirInput = $r('#training-image-dir');
        if (dirInput && !dirInput.dataset.statsBound) {
            dirInput.dataset.statsBound = '1';
            dirInput.addEventListener('change', function() {
                fetchTrainingDataStats();
            });
            dirInput.addEventListener('blur', function() {
                fetchTrainingDataStats();
            });
        }
    }

    var originalInitTrainingWorkbench =
        typeof initTrainingWorkbench === 'function'
            ? initTrainingWorkbench
            : null;

    initTrainingWorkbench = function() {
        var result = originalInitTrainingWorkbench
            ? originalInitTrainingWorkbench.apply(this, arguments)
            : undefined;
        setTimeout(function() {
            rebindTrainingDataControls();
            fetchTrainingDataStats().catch(function() { return null; });
        }, 0);
        return result;
    };

    if (!window.__ccTrainTargetPatched) {
        var nativeFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
            var requestUrl = typeof input === 'string' ? input : ((input && input.url) || '');
            if (
                requestUrl.indexOf('/api/train') !== -1 &&
                requestUrl.indexOf('/api/train/upload') === -1 &&
                requestUrl.indexOf('/api/train/data_stats') === -1 &&
                init &&
                init.body instanceof FormData &&
                !init.body.get('target')
            ) {
                var checkedTarget = document.querySelector('input[name="training-target"]:checked');
                init.body.append('target', (checkedTarget && checkedTarget.value) || 'neuralpreset');
            }
            return nativeFetch(input, init);
        };
        window.__ccTrainTargetPatched = true;
    }

    function saveSettingsNicknameFromServer() {
        var nicknameInput = $r('#settings-nickname-input');
        var editArea = $r('#settings-name-edit-area');
        if (!nicknameInput || nicknameInput.dataset.saving === '1') return;
        var val = (nicknameInput.value || '').trim();
        if (!val) {
            if (typeof showToast === 'function') showToast('昵称不能为空');
            nicknameInput.focus();
            return;
        }
        nicknameInput.dataset.saving = '1';
        nicknameInput.disabled = true;
        fetch('/api/projects/space_profile', {
            method: 'POST',
            headers: Object.assign({ 'Content-Type': 'application/json' }, getAuthHeaders()),
            body: JSON.stringify({ nickname: val })
        })
        .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
        .then(function(result) {
            if (!result.ok) throw new Error((result.data && result.data.detail) || '昵称保存失败');
            localStorage.removeItem('cc_nickname');
            applySettingsProfileData({
                display_name: result.data && result.data.nickname ? result.data.nickname : val
            });
            if (userSpaceDashboardCache && userSpaceDashboardCache.profile) {
                userSpaceDashboardCache.profile.display_name = result.data && result.data.nickname ? result.data.nickname : val;
            }
            if (currentView === 'space' && typeof fetchUserSpaceDashboard === 'function' && !isAdminUser()) {
                fetchUserSpaceDashboard(true).catch(function() { return null; });
            }
            if (typeof showToast === 'function') showToast('昵称已保存');
            if (editArea) editArea.style.display = 'none';
        })
        .catch(function(err) {
            if (typeof showToast === 'function') showToast(err.message || '昵称保存失败');
            nicknameInput.focus();
        })
        .finally(function() {
            nicknameInput.disabled = false;
            delete nicknameInput.dataset.saving;
        });
    }

    function rebindSettingsNicknameEditor() {
        var editNameBtn = $r('#settings-edit-name-btn');
        if (editNameBtn && !editNameBtn.dataset.rebound) {
            var editClone = editNameBtn.cloneNode(true);
            editNameBtn.parentNode.replaceChild(editClone, editNameBtn);
            editClone.dataset.rebound = '1';
            editClone.addEventListener('click', function(e) {
                e.stopPropagation();
                var editArea = $r('#settings-name-edit-area');
                var nicknameInput = $r('#settings-nickname-input');
                if (editArea) editArea.style.display = '';
                if (nicknameInput) {
                    nicknameInput.value = ($r('#settings-username') && $r('#settings-username').textContent) || getSettingsFallbackDisplayName(currentUser || {});
                    nicknameInput.focus();
                    nicknameInput.select();
                }
            });
        }

        var nicknameInput = $r('#settings-nickname-input');
        if (nicknameInput && !nicknameInput.dataset.rebound) {
            var inputClone = nicknameInput.cloneNode(true);
            nicknameInput.parentNode.replaceChild(inputClone, nicknameInput);
            inputClone.dataset.rebound = '1';
            inputClone.addEventListener('blur', function() {
                saveSettingsNicknameFromServer();
            });
            inputClone.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    saveSettingsNicknameFromServer();
                }
                if (e.key === 'Escape') {
                    var editArea = $r('#settings-name-edit-area');
                    if (editArea) editArea.style.display = 'none';
                }
            });
        }
    }

    window.showSettingsModal = function() {
        if (!$r('#settings-username')) {
            console.error('Settings DOM missing');
            return;
        }
        var modal = $r('#settings-modal');
        if (!modal) return;
        var user = currentUser || JSON.parse(localStorage.getItem('cc_user') || '{}');
        var editArea = $r('#settings-name-edit-area');
        if (editArea) editArea.style.display = 'none';
        var deleteAccountBtn = $r('#settings-delete-account-btn');
        if (deleteAccountBtn) {
            if (user.role === 'admin') {
                deleteAccountBtn.textContent = '管理员账户不可注销';
                deleteAccountBtn.style.opacity = '0.55';
                deleteAccountBtn.style.pointerEvents = 'none';
            } else {
                deleteAccountBtn.textContent = '注销账户';
                deleteAccountBtn.style.opacity = '';
                deleteAccountBtn.style.pointerEvents = '';
            }
        }
        localStorage.removeItem('cc_nickname');
        applySettingsProfileData({
            account_id: user.email || user.phone || '未知',
            account_type: user.role === 'admin' ? '管理员账号' : '普通用户',
            display_name: getSettingsFallbackDisplayName(user),
            rating_summary: userSpaceDashboardCache && userSpaceDashboardCache.profile ? userSpaceDashboardCache.profile.rating_summary : null
        });
        modal.style.display = 'block';
        rebindSettingsNicknameEditor();
        loadSettingsProfile(true);
        if (isAdminUser()) fetchAdminDashboard(false);
    };

    loadHomeProjectsGrid = function() {
        var grid = $r('#home-project-grid');
        var countEl = $r('#home-project-count');
        if (!grid) return;

        var requestSeq = ++homeProjectsRequestSeq;
        var newCardHtml = '<div class="home-project-card-new" id="home-new-project-card"><div class="home-project-card-new-icon">+</div><div class="home-project-card-new-text">鏂板缓椤圭洰</div></div>';
        var lastStableHtml = grid.innerHTML;
        if (!lastStableHtml) {
            grid.innerHTML = newCardHtml;
            lastStableHtml = newCardHtml;
        }

        var token = localStorage.getItem('cc_token');
        if (!token) {
            grid.innerHTML = newCardHtml;
            if (countEl) countEl.textContent = '0';
            return;
        }

        fetch('/api/projects/', {
            method: 'GET',
            headers: { 'Authorization': 'Bearer ' + token },
        })
        .then(function(resp) { return resp.json().then(function(data) { return { ok: resp.ok, data: data }; }); })
        .then(function(result) {
            if (requestSeq !== homeProjectsRequestSeq) return;
            if (!result.ok || !result.data) {
                return;
            }
            var projects = result.data;
            if (countEl) countEl.textContent = projects.length;
            var html = '';
            projects.forEach(function(p) {
                var typeClass = p.type === 'video' ? 'project-type-video' : 'project-type-image';
                var created = p.created_at ? p.created_at.slice(0, 10) : '';
                var safeName = escapeHtml(p.name || '');
                var safeNameAttr = escapeAttr(p.name || '');
                html += '<div class="home-project-card ' + typeClass + '" data-project-id="' + p.id + '" data-project-name="' + safeNameAttr + '">';
                html += '<div class="home-project-card-thumb"></div>';
                html += '<div class="home-project-card-info">';
                html += '<div class="home-project-card-name">' + safeName + '</div>';
                html += '<div class="home-project-card-date">' + created + '</div>';
                html += '</div></div>';
            });
            grid.innerHTML = newCardHtml + html;

            var newCard = $r('#home-new-project-card');
            if (newCard) {
                newCard.addEventListener('click', function() {
                    showTypeSelectModal();
                });
            }

            grid.querySelectorAll('.home-project-card').forEach(function(card) {
                card.addEventListener('click', function() {
                    var pid = parseInt(card.getAttribute('data-project-id'));
                    if (!pid) return;
                    window.currentProjectId = pid;
                    window._pendingProjectType = card.classList.contains('project-type-video') ? 'video' : 'image';
                    rNavigate('workspace');
                });

                card.addEventListener('contextmenu', function(e) {
                    e.preventDefault();
                    showContextMenu(e, card);
                });
            });
        })
        .catch(function() {
            if (requestSeq !== homeProjectsRequestSeq) return;
            grid.innerHTML = lastStableHtml || newCardHtml;
        });
    };

    rebindSettingsNicknameEditor();
    window.fetchTrainingDataStats = fetchTrainingDataStats;
})();
