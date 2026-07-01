(function() {
    'use strict';

    var PREVIEW_USER = {
        id: 1,
        email: 'demo@colorchase.local',
        phone: '',
        role: 'admin',
        display_name: 'ColorChase Demo',
        nickname: 'ColorChase Demo'
    };

    var nowIso = function() { return new Date().toISOString(); };

    var svgImage = function(label, a, b) {
        var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="860" viewBox="0 0 1280 860">' +
            '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="' + a + '"/><stop offset="1" stop-color="' + b + '"/></linearGradient>' +
            '<filter id="s"><feDropShadow dx="0" dy="24" stdDeviation="32" flood-color="#020617" flood-opacity=".32"/></filter></defs>' +
            '<rect width="1280" height="860" fill="url(#g)"/>' +
            '<circle cx="1030" cy="120" r="190" fill="#ffffff" opacity=".16"/>' +
            '<circle cx="190" cy="740" r="230" fill="#ffffff" opacity=".10"/>' +
            '<rect x="190" y="150" width="900" height="560" rx="42" fill="#111827" opacity=".42" filter="url(#s)"/>' +
            '<text x="640" y="390" text-anchor="middle" font-family="Inter,Segoe UI,Arial" font-size="76" font-weight="800" fill="#fff">ColorChase</text>' +
            '<text x="640" y="468" text-anchor="middle" font-family="Inter,Segoe UI,Arial" font-size="42" fill="#e5e7eb">' + label + '</text>' +
            '</svg>';
        return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
    };

    var SAMPLE_SOURCE = svgImage('Reference Image', '#0f766e', '#7c3aed');
    var SAMPLE_RESULT = svgImage('AI Color Result', '#7c2d12', '#0f172a');
    var SAMPLE_VIDEO = svgImage('Video Frame Preview', '#1d4ed8', '#111827');

    var projects = [
        { id: 101, name: '商业人像追色 Demo', type: 'image', created_at: '2026-07-01T10:18:00' },
        { id: 102, name: '短片调色 Demo', type: 'video', created_at: '2026-06-30T18:42:00' },
        { id: 103, name: '婚礼胶片风格', type: 'image', created_at: '2026-06-29T15:06:00' }
    ];

    var styles = [
        { id: 'warm_cinema', name: 'Warm Cinema', npy_path: 'mock://styles/warm_cinema.npy' },
        { id: 'clean_portrait', name: 'Clean Portrait', npy_path: 'mock://styles/clean_portrait.npy' },
        { id: 'night_teal', name: 'Night Teal', npy_path: 'mock://styles/night_teal.npy' }
    ];

    var dashboard = function(isAdmin) {
        var meta = {
            brand_mark: isAdmin ? 'Admin Center' : 'User Space',
            top_title: isAdmin ? 'Dashboard' : 'My Space',
            hero_subtitle: isAdmin ? '全站任务、模型和资源概览' : '个人项目、资产和任务概览',
            ring_title: '运行状态',
            ring_subtitle: '任务、资源、模型健康度',
            progress_title: '辅助指标',
            progress_subtitle: '自动刷新展示当前趋势',
            category_title: '资源与资产分类',
            category_subtitle: isAdmin ? '整个项目的存储使用' : '整个项目的存储使用',
            log_title: '最近动态',
            log_subtitle: '静态预览 mock 数据',
            health_score: 86,
            health_score_label: '健康度',
            compare_label: '较上周',
            compare_empty_label: '暂无上周',
            weekly_refresh_rule: '静态预览',
            auto_refresh_seconds: 60
        };
        return {
            generated_at: nowIso(),
            meta: meta,
            overview: {
                users: { total: 18, active_today: 7 },
                projects: { total: 42, image: 31, video: 11 },
                task_stats: { total: 126, success: 112, failed: 5 },
                model_data: {
                    total_size_mb: 18342.7,
                    model_count: 7,
                    ready_count: 6,
                    training_corpus_sample_count: 86,
                    training_corpus_user_count: 5,
                    training_corpus_file_count: 258,
                    training_corpus_size_mb: 924.5,
                    training_corpus_target_count: 86,
                    training_corpus_reference_count: 86,
                    training_corpus_result_count: 86,
                    training_corpus_meta_count: 86,
                    training_corpus_rating_count: 64,
                    training_corpus_path: 'storage/training/corpus/'
                }
            },
            cards: [
                { label: 'Today Task', value: 18, display_value: '18', delta: 4, unit: '', sparkline: [4, 7, 5, 9, 12, 10, 18] },
                { label: 'AI Jobs', value: 11, display_value: '11', delta: 2, unit: '', sparkline: [3, 3, 5, 6, 8, 9, 11] },
                { label: 'Exports', value: 29, display_value: '29', delta: 6, unit: '', sparkline: [10, 12, 18, 16, 22, 24, 29] },
                { label: 'Storage', value: 24.8, display_value: '24.8 GB', delta: 1.2, unit: 'GB', sparkline: [19, 20, 21, 22, 23, 24, 24.8] }
            ],
            rings: [
                { label: '任务成功率', value: 112, percent: 89, color: '#4f63ff' },
                { label: '模型可用率', value: 6, percent: 86, color: '#47c5e7' },
                { label: '资源健康度', value: 24.8, percent: 78, color: '#18a999' }
            ],
            bars: [
                { label: 'Mon', tasks: 8, exports: 3 },
                { label: 'Tue', tasks: 12, exports: 6 },
                { label: 'Wed', tasks: 18, exports: 9 },
                { label: 'Thu', tasks: 14, exports: 11 },
                { label: 'Fri', tasks: 20, exports: 13 },
                { label: 'Sat', tasks: 16, exports: 8 },
                { label: 'Sun', tasks: 22, exports: 15 }
            ],
            progress: [
                { label: 'GPU', value: 72, color: '#4f63ff' },
                { label: 'Queue', value: 36, color: '#47c5e7' },
                { label: 'Cache', value: 58, color: '#18a999' }
            ],
            categories: [
                { label: '存储使用', value: '24.8 GB', color: '#4f63ff' },
                { label: '模型权重', value: '17.9 GB', color: '#47c5e7' },
                { label: '项目资产', value: '4.6 GB', color: '#18a999' },
                { label: '训练样本', value: '924.5 MB', color: '#f59e0b' }
            ],
            logs: [
                '图片追色任务完成：商业人像追色 Demo',
                '训练样本副本已刷新：storage/training/corpus/',
                '视频导出完成：短片调色 Demo'
            ],
            profile: {
                account_id: PREVIEW_USER.email,
                account_type: isAdmin ? '管理员账号' : '普通用户',
                display_name: PREVIEW_USER.display_name,
                avatar_url: '',
                rating_summary: { rated_count: 16, total_count: 21 }
            },
            task_center: [
                { task_id: 'demo-001', created_at: nowIso(), task_type: '图片追色', status: 'ok', status_label: '成功', summary: 'Warm Cinema 应用完成', user: { display_name: PREVIEW_USER.display_name, email: PREVIEW_USER.email } },
                { task_id: 'demo-002', created_at: nowIso(), task_type: '视频追色', status: 'ok', status_label: '成功', summary: '导出 1080p MP4', user: { display_name: PREVIEW_USER.display_name, email: PREVIEW_USER.email } }
            ]
        };
    };

    var modelStatus = {
        norm_stage_trained: true,
        style_stage_trained: true,
        neural_preset_ready: true,
        modflows_ready: true,
        management: { default_model: 'modflows_b6' },
        models: [
            { key: 'modflows_b6', name: 'ModFlows B6', ready: true, enabled: true, status: 'ready', kind: 'transfer', default_selectable: true, is_default: true, used_by: ['transfer'], files: [{ exists: true, size_mb: 920.4 }], missing_files: [] },
            { key: 'modflows_b0', name: 'ModFlows B0', ready: true, enabled: true, status: 'ready', kind: 'transfer', default_selectable: true, is_default: false, used_by: ['transfer'], files: [{ exists: true, size_mb: 178.2 }], missing_files: [] },
            { key: 'sam2', name: 'SAM2 Subject Mask', ready: true, enabled: true, status: 'ready', kind: 'mask', default_selectable: false, used_by: ['mask'], files: [{ exists: true, size_mb: 856.1 }], missing_files: [] }
        ],
        summary: { total: 3, ready: 3, missing: 0, installed_or_partial: 3 },
        device: 'cuda',
        device_label: '静态预览'
    };

    function json(data, status) {
        return Promise.resolve(new Response(JSON.stringify(data), {
            status: status || 200,
            headers: { 'Content-Type': 'application/json; charset=utf-8' }
        }));
    }

    function text(data, status) {
        return Promise.resolve(new Response(data, {
            status: status || 200,
            headers: { 'Content-Type': 'text/plain; charset=utf-8' }
        }));
    }

    function mockProjectsAssets(projectId) {
        return json([
            {
                id: 'asset-a',
                name: 'portrait-target.jpg',
                path: SAMPLE_SOURCE,
                asset_url: SAMPLE_SOURCE,
                thumbnail: SAMPLE_SOURCE,
                thumbnail_url: SAMPLE_SOURCE,
                source_path: SAMPLE_SOURCE,
                result_url: SAMPLE_RESULT,
                result_path: SAMPLE_RESULT,
                rating: 4
            },
            {
                id: 'asset-b',
                name: 'cinema-result.jpg',
                path: SAMPLE_RESULT,
                asset_url: SAMPLE_RESULT,
                thumbnail: SAMPLE_RESULT,
                thumbnail_url: SAMPLE_RESULT,
                source_path: SAMPLE_RESULT,
                rating: 5
            }
        ]);
    }

    var nativeFetch = window.fetch ? window.fetch.bind(window) : null;
    window.fetch = function(input, init) {
        var rawUrl = typeof input === 'string' ? input : ((input && input.url) || '');
        var url;
        try {
            url = new URL(rawUrl, window.location.href);
        } catch (e) {
            url = { pathname: rawUrl, searchParams: new URLSearchParams() };
        }
        var path = url.pathname.replace(/^\/ColorChase/, '');
        var method = ((init && init.method) || 'GET').toUpperCase();

        if (!path.startsWith('/api/')) {
            return nativeFetch ? nativeFetch(input, init) : Promise.reject(new Error('fetch unavailable'));
        }

        if (path === '/api/auth/me') return json(PREVIEW_USER);
        if (path === '/api/auth/login' || path === '/api/auth/register') return json({ token: 'github-pages-preview-token', user: PREVIEW_USER });
        if (path === '/api/auth/logout') return json({ success: true });
        if (path.indexOf('/api/auth/send_') === 0) return json({ success: true, message: '静态预览验证码已模拟发送' });

        if (path === '/api/portal_messages') {
            return json({
                notice: {
                    version: 3,
                    title: 'ColorChase 静态预览',
                    body: '当前页面复用真实前端资源，所有接口由 mock 数据驱动。',
                    updated_at: nowIso(),
                    items: [
                        { id: 1, title: 'GitHub Pages Preview', body: '不连接后端，不包含运行时数据。', created_at: nowIso() }
                    ]
                },
                contact: { qq: '955749464', notes: '静态预览展示信息' }
            });
        }

        if (path === '/api/projects/' || path === '/api/projects') return json(projects);
        if (path === '/api/projects/trash/') return json([]);
        if (/^\/api\/projects\/\d+\/assets$/.test(path)) return mockProjectsAssets(path.split('/')[3]);
        if (/^\/api\/projects\/\d+\/snapshot$/.test(path)) return json({ success: true });
        if (/^\/api\/projects\/\d+\/upload$/.test(path)) return json({ success: true, asset_url: SAMPLE_SOURCE, thumbnail: SAMPLE_SOURCE });
        if (/^\/api\/projects\/\d+\/rate_asset$/.test(path)) return json({ success: true });
        if (path === '/api/projects/record_export_metric') return json({ success: true });
        if (path === '/api/projects/space_dashboard_v2') return json(dashboard(false));
        if (path === '/api/projects/space_profile') {
            if (method === 'POST') return json({ success: true, nickname: PREVIEW_USER.display_name });
            return json(dashboard(false).profile);
        }
        if (path === '/api/projects/space_profile/avatar') return json({ success: true, avatar_url: SAMPLE_SOURCE });

        if (path === '/api/admin/dashboard') return json(dashboard(true));
        if (path === '/api/admin/models' || /^\/api\/admin\/models\//.test(path)) return json(modelStatus);
        if (path === '/api/admin/task_logs') {
            return json({
                items: dashboard(true).task_center,
                total: 2,
                alerts: []
            });
        }
        if (path.indexOf('/api/admin/task_logs') === 0) return json({ success: true });
        if (path.indexOf('/api/admin/portal_messages') === 0) return json({ success: true });

        if (path === '/api/model_status') return json(modelStatus);
        if (path === '/api/list_styles') return json(styles);
        if (path.indexOf('/api/get_style/') === 0) {
            var id = decodeURIComponent(path.split('/').pop() || '');
            return json(styles.find(function(item) { return item.id === id; }) || styles[0]);
        }
        if (path === '/api/rename_style') return json({ success: true });
        if (path === '/api/apply_style' || path === '/api/apply_profile' || path === '/api/transfer') {
            return json({
                success: true,
                result_b64: SAMPLE_RESULT,
                original_b64: SAMPLE_SOURCE,
                reference_b64: SAMPLE_SOURCE,
                session_id: 'preview_session_001',
                merged_session_id: 'preview_session_001',
                reference_path: SAMPLE_SOURCE
            });
        }
        if (path === '/api/merge_luts') return json({ success: true, merged_session_id: 'preview_merged_001', preview_b64: SAMPLE_RESULT, result_b64: SAMPLE_RESULT });
        if (path === '/api/prepare_lr_preset') return text('ColorChase static preset preview', 200);
        if (path === '/api/capture_style') return json({ success: true, style_id: 'captured_preview', name: 'Captured Preview' });

        if (path === '/api/upload_batch') {
            return json({
                success: true,
                files: [
                    { filename: 'preview-target.jpg', path: SAMPLE_SOURCE, asset_url: SAMPLE_SOURCE, thumbnail: SAMPLE_SOURCE, project_saved: true }
                ]
            });
        }
        if (path === '/api/render_single') return Promise.resolve(new Response('preview image blob', { status: 200, headers: { 'Content-Type': 'image/jpeg' } }));
        if (path === '/api/video_metadata') return json({ fps: 24, duration: 12.5, codec: 'h264', width: 1920, height: 1080, preview: SAMPLE_VIDEO });
        if (path === '/api/video_transfer') return json({ success: true, task_id: 'video-preview-task', message: '静态预览任务已创建' });
        if (path === '/api/export_video') return json({ success: true, url: SAMPLE_VIDEO, output_url: SAMPLE_VIDEO, filename: 'colorchase-preview.mp4' });

        if (/^\/api\/task\/[^/]+\/progress$/.test(path)) {
            return json({ task_id: path.split('/')[3], status: 'done', progress: 100, message: '静态预览任务完成', result_url: SAMPLE_RESULT });
        }
        if (/^\/api\/task\/[^/]+\/(pause|resume|cancel)$/.test(path)) return json({ success: true });
        if (path === '/api/user_config') {
            if (method === 'POST') return json({ success: true });
            return json({
                image_uploads: 'storage/uploads/images',
                image_luts: 'storage/temp/luts',
                image_debug: 'storage/logs/debug_output',
                video_uploads: 'storage/uploads/videos',
                video_results: 'storage/videos',
                video_frames: 'storage/temp/frames'
            });
        }
        if (path === '/api/pick_folder') return json({ success: false, message: '静态预览不访问本地文件夹' });

        if (path === '/api/train/data_stats') return json({ file_count: 128, size_mb: 742.6, image_dir: 'storage/training/uploads/preview' });
        if (path === '/api/train/upload') return json({ success: true, uploaded: 12, failed: 0, file_count: 140, size_mb: 780.2 });
        if (path === '/api/train/clear_uploads' || path === '/api/train/data_clear') return json({ success: true, file_count: 0, size_mb: 0 });
        if (path === '/api/train') return json({ success: true, task_id: 'training-preview-task', message: '静态预览训练任务已启动' });

        if (path === '/api/mask/subject') return json({ success: true, mask_path: 'mock://mask/subject.png', preview_b64: SAMPLE_RESULT });
        if (path === '/api/depth/layers') return json({ success: true, depth_path: 'mock://depth/layers.png', preview_b64: SAMPLE_RESULT });
        if (path === '/api/semantic/match') return json({ success: true, semantic_path: 'mock://semantic/match.png', preview_b64: SAMPLE_RESULT });

        return json({ success: true, preview: true, detail: 'GitHub Pages 静态预览 mock 响应：' + path });
    };

    function MockEventSource(url) {
        var self = this;
        this.url = url;
        this.readyState = 0;
        setTimeout(function() {
            self.readyState = 1;
            if (typeof self.onopen === 'function') self.onopen({});
            var data = JSON.stringify({ status: 'done', progress: 100, message: '静态预览任务完成' });
            if (typeof self.onmessage === 'function') self.onmessage({ data: data });
        }, 350);
    }
    MockEventSource.prototype.close = function() { this.readyState = 2; };
    MockEventSource.prototype.addEventListener = function(type, handler) {
        if (type === 'message') this.onmessage = handler;
        if (type === 'open') this.onopen = handler;
        if (type === 'error') this.onerror = handler;
    };
    window.EventSource = MockEventSource;

    if (!window.showDirectoryPicker) {
        window.showDirectoryPicker = function() {
            return Promise.resolve({
                name: 'ColorChase Preview Export',
                getFileHandle: function() {
                    return Promise.resolve({
                        createWritable: function() {
                            return Promise.resolve({ write: function() {}, close: function() {} });
                        }
                    });
                }
            });
        };
    }

    try {
        if (!localStorage.getItem('cc_token')) localStorage.setItem('cc_token', 'github-pages-preview-token');
        localStorage.setItem('cc_user', JSON.stringify(PREVIEW_USER));
    } catch (e) {}

    window.__COLORCHASE_STATIC_PREVIEW__ = {
        user: PREVIEW_USER,
        projects: projects,
        styles: styles,
        sampleSource: SAMPLE_SOURCE,
        sampleResult: SAMPLE_RESULT
    };
})();
