/**
 * app.js - 面板主界面初始化、全局状态
 * 运行在 AstrBot 插件可视化视图（受限 iframe）中，鉴权由 bridge 统一处理
 */

const App = {
    _currentView: 'tech-tree',
    _initialized: false,
    _configFileName: '',
    _configPanelOpen: false,

    /** 应用入口（视图页面加载时调用） */
    async start() {
        const configMeta = await Api.getConfig();
        if (configMeta && configMeta.ok) {
            this._configFileName = configMeta.config_file_name || '';
        }

        this.showPage('main');
        await this._initMain();
    },

    /**
     * 打开外部链接。
     * 插件视图运行在受限 iframe 中（无 allow-popups），window.open 可能被拦截，
     * 此时回退为提示用户手动复制链接。
     */
    _openExternal(url) {
        let win = null;
        try {
            win = window.open(url, '_blank');
        } catch (_e) {
            win = null;
        }
        if (!win) {
            Utils.alert(`浏览器拦截了新窗口打开，请手动复制以下链接访问：\n${url}`);
        }
    },

    /** 显示指定页面，隐藏其他 */
    showPage(name) {
        document.querySelectorAll('.page').forEach(p => p.classList.add('hidden'));
        const page = document.getElementById(`page-${name}`);
        if (page) page.classList.remove('hidden');
    },

    /** 切换主界面视图 */
    showView(name) {
        this._currentView = name;
        document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
        const view = document.getElementById(`view-${name}`);
        if (view) view.classList.remove('hidden');

        // 更新导航高亮
        document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
        const nav = document.querySelector(`.nav-item[data-view="${name}"]`);
        if (nav) nav.classList.add('active');

        this._updateConfigBadgeVisibility();

        // 缩放按钮仅在流程图页面显示
        const zoomCtrl = document.getElementById('zoom-ctrl');
        if (zoomCtrl) zoomCtrl.classList.toggle('hidden', name !== 'tech-tree');

        // 按需初始化视图
        this._activateView(name);
    },

    /** 激活视图时加载数据 */
    async _activateView(name) {
        // 切换视图时销毁需要清理的模块
        if (name !== 'charts') Charts.destroy();
        if (name !== 'sessions') SessionMgr.destroy();
        if (name !== 'tech-tree' && typeof TechTree !== 'undefined') TechTree.closeAllFloaters();
        if (name !== 'tech-tree') this.setConfigPanelOpen(false);

        switch (name) {
            case 'tech-tree':
                if (!this._initialized) {
                    await TechTree.init();
                    this._initialized = true;
                }
                break;
            case 'charts':
                await Charts.init();
                break;
            case 'sessions':
                await SessionMgr.init();
                break;
            case 'commands':
                this._renderCommands();
                break;
            case 'settings':
                this._renderSettings();
                break;
            case 'files':
                this._renderFileBrowser();
                break;
        }

    },

    /** 初始化主界面 */
    async _initMain() {
        // 侧边栏导航
        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', () => {
                const view = item.dataset.view;
                if (view) this.showView(view);
            });
        });

        this._ensureConfigBadge();
        this._updateConfigBadgeVisibility();

        // 支持作者
        const btnSupportAuthor = document.getElementById('btn-support-author');
        if (btnSupportAuthor) {
            btnSupportAuthor.addEventListener('click', async () => {
                const ok = await Utils.supportAuthorDialog();
                if (ok) {
                    this._openExternal('https://afdian.com/a/chat_plus');
                }
            });
        }

        // 反馈BUG
        const btnFeedback = document.getElementById('btn-feedback');
        if (btnFeedback) {
            btnFeedback.addEventListener('click', async () => {
                const action = await Utils.feedbackDialog();
                if (action === 'github') {
                    this._openExternal('https://github.com/Him666233/astrbot_plugin_group_chat_plus/issues');
                } else if (action === 'group') {
                    Utils.alert('测试群聊号码：QQ群 1021544792');
                }
                // 如果action是'cancel'，则什么都不做
            });
        }

        // 初始化主题切换（仅内存态，不落盘；初始主题由 bridge 注入的 data-theme 决定）
        const themeBtn = document.getElementById('btn-theme-toggle');
        if (themeBtn) {
            const applyThemeLabel = () => {
                const current = document.documentElement.getAttribute('data-theme') || 'dark';
                themeBtn.textContent = current === 'light' ? '🌙 切换深色' : '☀️ 切换浅色';
            };
            themeBtn.addEventListener('click', () => {
                const currentTheme = document.documentElement.getAttribute('data-theme') || 'dark';
                const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
                document.documentElement.setAttribute('data-theme', newTheme);
                applyThemeLabel();
                window.dispatchEvent(new Event('themeChanged'));
            });
            // 初次加载：优先使用 bridge 上下文中的主题
            const bridge = Api.bridge();
            const ctx = bridge && bridge.getContext ? bridge.getContext() : null;
            if (ctx && typeof ctx.isDark === 'boolean') {
                document.documentElement.setAttribute('data-theme', ctx.isDark ? 'dark' : 'light');
            }
            applyThemeLabel();
            if (bridge && typeof bridge.onContext === 'function') {
                bridge.onContext((next) => {
                    if (next && typeof next.isDark === 'boolean') {
                        document.documentElement.setAttribute('data-theme', next.isDark ? 'dark' : 'light');
                        applyThemeLabel();
                    }
                });
            }
        }

        // 初始化默认视图
        await this._activateView(this._currentView);

        // 全局点击：工具提示外部点击时关闭（移动端）
        document.addEventListener('click', (e) => {
        });
    },

    // ===================== 指令执行视图 =====================

    /** 渲染指令执行视图 */
    async _renderCommands() {
        const container = document.getElementById('commands-container');
        if (!container) return;
        container.innerHTML = '';
        container.style.cssText = 'padding:24px;display:flex;flex-direction:column;gap:16px;overflow-y:auto;';

        // 加载会话列表（供 reset-here 使用）
        const sessRes = await Api.sessionList();
        const sessions = sessRes.ok ? Object.keys(sessRes.sessions || {}) : [];

        const commands = [
            {
                id: 'reset',
                name: '全局重置 (gcp_reset)',
                desc: '重置所有插件数据（注意力、情绪、概率等运行时状态）并清除所有会话的聊天记录缓存。不影响配置文件。',
                icon: '🔄',
                color: 'orange',
                exec: async (mode) => {
                    const ok = await Utils.confirm('确认重置所有插件运行时数据？');
                    if (!ok) return;
                    return await Api.cmdReset(mode);
                }
            },
            {
                id: 'reset-here',
                name: '会话重置 (gcp_reset_here)',
                desc: '重置指定会话的插件数据和聊天记录。选择要重置的会话后执行。',
                icon: '🎯',
                color: 'blue',
                needSession: true,
                exec: async (mode, sessionId) => {
                    if (!sessionId) { Utils.toast('请选择要重置的会话', 'warning'); return; }
                    const ok = await Utils.confirm(`确认重置会话「${sessionId}」的数据？`);
                    if (!ok) return;
                    return await Api.cmdResetHere(sessionId, mode);
                }
            },
            {
                id: 'clear-cache',
                name: '清除图片缓存 (gcp_clear_image_cache)',
                desc: '清除所有图片描述的本地缓存。下次遇到相同图片时会重新调用 AI 生成描述。',
                icon: '🗑️',
                color: 'red',
                exec: async (mode) => {
                    const ok = await Utils.confirm('确认清除所有图片描述缓存？');
                    if (!ok) return;
                    return await Api.cmdClearImageCache(mode);
                }
            }
        ];

        commands.forEach(cmd => {
            const card = document.createElement('div');
            card.className = 'cmd-card';

            let sessionSelect = '';
            if (cmd.needSession) {
                const opts = sessions.map(s =>
                    `<option value="${Utils.escapeHtml(s)}">${Utils.escapeHtml(s)}</option>`
                ).join('');
                sessionSelect = `
                    <div class="cmd-field">
                        <label>选择会话</label>
                        <select id="cmd-session-${cmd.id}" class="select-sm" style="width:100%;">
                            <option value="">请选择...</option>
                            ${opts}
                        </select>
                    </div>`;
            }

            card.innerHTML = `
                <div class="cmd-card-header">
                    <span class="cmd-icon">${cmd.icon}</span>
                    <span class="cmd-name">${cmd.name}</span>
                </div>
                <p class="cmd-desc">${cmd.desc}</p>
                ${sessionSelect}
                <div class="cmd-actions">
                    <div class="cmd-field">
                        <label>重启模式</label>
                        <select id="cmd-mode-${cmd.id}" class="select-sm">
                            <option value="reload">仅重载插件</option>
                            <option value="restart">重启整个 AstrBot</option>
                        </select>
                    </div>
                    <button class="btn btn-primary btn-sm" id="cmd-exec-${cmd.id}">执行</button>
                </div>`;

            container.appendChild(card);

            // 绑定执行按钮
            const execBtn = document.getElementById(`cmd-exec-${cmd.id}`);
            execBtn.addEventListener('click', async () => {
                const mode = document.getElementById(`cmd-mode-${cmd.id}`).value;
                const sessionId = cmd.needSession
                    ? document.getElementById(`cmd-session-${cmd.id}`).value
                    : null;
                execBtn.disabled = true;
                execBtn.textContent = '执行中...';
                try {
                    const res = await cmd.exec(mode, sessionId);
                    if (res) {
                        if (res.ok) {
                            Utils.toast(res.msg || '执行成功', 'success');
                            if (mode === 'restart' || mode === 'reload') {
                                App._pollRestartStatus(mode);
                            }
                        } else {
                            Utils.toast(res.msg || '执行失败', 'error');
                        }
                    }
                } catch (e) {
                    Utils.toast(`执行异常: ${e.message}`, 'error');
                }
                execBtn.disabled = false;
                execBtn.textContent = '执行';
            });
        });
    },

    // ===================== 访问日志视图 =====================

    /** 渲染访问日志视图 */
    _renderSettings() {
        const container = document.getElementById('settings-container');
        if (!container) return;
        container.innerHTML = '';
        container.style.cssText = 'padding:24px;overflow-y:auto;display:flex;flex-direction:column;gap:24px;';
        // ---- 桌面端兼容区 ----
        const desktopSection = document.createElement('div');
        desktopSection.className = 'settings-section';
        desktopSection.innerHTML = `
            <h3>🖥️ 桌面端兼容</h3>
            <p style="font-size:12px;color:var(--text-secondary);margin-bottom:12px;line-height:1.7;">
                AstrBot 桌面端（Desktop Edition）使用 Tauri 托管后端进程，重启机制与标准版不同。<br>
                插件支持自动检测运行环境，也可手动指定。修改后需 <strong>保存并重启插件</strong> 生效。
            </p>
            <div id="desktop-loading" class="chart-empty" style="padding:12px;">加载中...</div>
            <div id="desktop-content" class="hidden" style="display:flex;flex-direction:column;gap:12px;max-width:500px;"></div>`;
        container.appendChild(desktopSection);
        this._loadDesktopConfig();

        // ---- Web 面板可调配置区 ----
        const webCfgSection = document.createElement('div');
        webCfgSection.className = 'settings-section';
        webCfgSection.innerHTML = `
            <h3>Web 面板运行配置</h3>
            <p style="font-size:12px;color:var(--text-secondary);margin-bottom:12px;">
                以下配置修改后需 <strong>保存并重启插件</strong> 才能生效。
            </p>
            <div id="webcfg-loading" class="chart-empty" style="padding:12px;">加载中...</div>
            <div id="webcfg-content" class="hidden" style="display:flex;flex-direction:column;gap:16px;max-width:500px;"></div>`;
        container.appendChild(webCfgSection);
        this._loadWebCfg();
    },

    async _loadDesktopConfig() {
        const loading = document.getElementById('desktop-loading');
        const content = document.getElementById('desktop-content');
        if (!loading || !content) return;

        const res = await Api.getConfig();
        if (!res.ok) {
            loading.textContent = '加载失败';
            return;
        }

        loading.classList.add('hidden');
        content.classList.remove('hidden');
        content.style.display = 'flex';

        const cfg = res.config || {};
        const schema = res.schema || {};
        const desktopInfo = res.desktop_info || {};

        // 当前检测状态指示
        const statusRow = document.createElement('div');
        statusRow.style.cssText = 'padding:10px 14px;border-radius:8px;font-size:13px;line-height:1.6;';
        if (desktopInfo.is_desktop) {
            statusRow.style.background = 'rgba(59,130,246,0.08)';
            statusRow.style.border = '1px solid rgba(59,130,246,0.25)';
            statusRow.innerHTML = `
                <strong>🖥️ 当前环境：桌面端</strong><br>
                <span style="font-size:12px;color:var(--text-secondary);">
                    检测模式：${desktopInfo.mode_setting || 'auto'}<br>
                    检测依据：${desktopInfo.detected_env || '—'}
                </span>`;
        } else {
            statusRow.style.background = 'rgba(34,197,94,0.08)';
            statusRow.style.border = '1px solid rgba(34,197,94,0.25)';
            statusRow.innerHTML = `
                <strong>📦 当前环境：标准版</strong><br>
                <span style="font-size:12px;color:var(--text-secondary);">
                    检测模式：${desktopInfo.mode_setting || 'auto'}<br>
                    检测结果：未检测到桌面端特征
                </span>`;
        }
        content.appendChild(statusRow);

        // desktop_mode 可编辑选择
        const modeSchema = schema['desktop_mode'];
        if (modeSchema) {
            const modeRow = document.createElement('div');
            modeRow.className = 'config-field';
            modeRow.style.maxWidth = '500px';
            const currentMode = cfg['desktop_mode'] || 'auto';
            const modeLabels = {
                'auto': 'auto — 自动检测（推荐）',
                'force_desktop': 'force_desktop — 强制桌面端模式',
                'force_standard': 'force_standard — 强制标准版模式'
            };
            const label = document.createElement('div');
            label.className = 'config-field-label';
            label.textContent = '桌面端模式';
            modeRow.appendChild(label);

            const hint = document.createElement('div');
            hint.className = 'config-field-hint';
            hint.textContent = '控制插件如何识别运行环境。auto=多重策略自动检测，force_desktop=强制桌面端，force_standard=强制标准版';
            modeRow.appendChild(hint);

            const select = document.createElement('select');
            select.className = 'config-select';
            select.style.marginTop = '6px';
            (modeSchema.options || ['auto', 'force_desktop', 'force_standard']).forEach(opt => {
                const o = document.createElement('option');
                o.value = opt;
                o.textContent = modeLabels[opt] || opt;
                if (opt === currentMode) o.selected = true;
                select.appendChild(o);
            });
            modeRow.appendChild(select);
            content.appendChild(modeRow);

            // desktop_detected_env 只读
            const detectedRow = document.createElement('div');
            detectedRow.className = 'config-field config-field-readonly';
            detectedRow.style.maxWidth = '500px';
            const detectedVal = cfg['desktop_detected_env'] || '（未检测）';
            detectedRow.innerHTML = `
                <div class="config-field-label">🔒 自动检测结果</div>
                <div class="config-field-readonly-value">当前值：${detectedVal}</div>
                <div class="config-field-readonly-note">此项由插件自动检测并写入，无需手动修改</div>`;
            content.appendChild(detectedRow);

            // 保存按钮
            const saveRow = document.createElement('div');
            saveRow.style.cssText = 'display:flex;gap:8px;align-items:center;margin-top:4px;';
            const saveBtn = document.createElement('button');
            saveBtn.className = 'btn btn-primary btn-sm';
            saveBtn.textContent = '保存并重启插件';
            const statusEl = document.createElement('span');
            statusEl.style.cssText = 'font-size:12px;color:var(--text-secondary);';
            saveRow.appendChild(saveBtn);
            saveRow.appendChild(statusEl);
            content.appendChild(saveRow);

            saveBtn.addEventListener('click', async () => {
                const newMode = select.value;
                saveBtn.disabled = true;
                statusEl.textContent = '保存中...';
                const saveRes = await Api.putConfig({ desktop_mode: newMode });
                if (!saveRes.ok) {
                    Utils.toast('保存失败：' + (saveRes.msg || '未知错误'), 'error');
                    statusEl.textContent = '保存失败';
                    saveBtn.disabled = false;
                    return;
                }
                statusEl.textContent = '正在重启，完成后将自动刷新...';
                statusEl.style.color = 'var(--accent)';
                const reloadRes = await Api.reloadPlugin();
                if (reloadRes && !reloadRes.ok && !reloadRes.network_error) {
                    saveBtn.disabled = false;
                    statusEl.textContent = '重启失败';
                    statusEl.style.color = 'var(--danger)';
                    Utils.toast(reloadRes.msg || '重启失败，请检查配置', 'error');
                    return;
                }
                this._waitForServerAndRefresh();
            });
        }
    },

    /** 加载 Web 面板可调配置（日志清理、防爬虫、信任代理） */
    async _loadWebCfg() {
        const loading = document.getElementById('webcfg-loading');
        const content = document.getElementById('webcfg-content');
        if (!loading || !content) return;

        const res = await Api.getConfig();
        if (!res.ok) {
            loading.textContent = '加载失败';
            return;
        }

        loading.classList.add('hidden');
        content.classList.remove('hidden');

        const cfg = res.config || {};
        const schema = res.schema || {};

        // 可调项定义（key → 覆盖标签，留空则用 schema.description）
        const editableKeys = [
            'web_panel_log_auto_clean',
            'web_panel_log_retention_days',
            'web_panel_log_clean_interval_hours',
        ];

        const pending = {};

        editableKeys.forEach(key => {
            const s = schema[key];
            if (!s) return;
            let val = key in cfg ? cfg[key] : (s.default !== undefined ? s.default : null);

            const row = document.createElement('div');
            row.className = 'config-field';
            row.style.paddingBottom = '12px';
            row.style.borderBottom = '1px solid var(--border-color)';

            const desc = (s.description || key).replace(/^[^\s]+\s/, '');
            const label = document.createElement('div');
            label.className = 'config-field-label';
            label.textContent = desc;
            row.appendChild(label);

            if (s.hint) {
                const hint = document.createElement('div');
                hint.className = 'config-field-hint';
                hint.textContent = s.hint;
                row.appendChild(hint);
                requestAnimationFrame(() => {
                    const lh = parseFloat(getComputedStyle(hint).lineHeight) || 16;
                    if (hint.scrollHeight <= lh * 4 + 4) {
                        hint.classList.add('short');
                    } else {
                        hint.classList.add('collapsible');
                        const fullHeight = hint.scrollHeight;
                        const btn = document.createElement('span');
                        btn.className = 'collapse-toggle';
                        btn.textContent = '▼ 展开';
                        hint.parentNode.insertBefore(btn, hint.nextSibling);
                        btn.addEventListener('click', () => {
                            if (hint.classList.contains('expanded')) {
                                hint.style.maxHeight = hint.scrollHeight + 'px';
                                hint.getBoundingClientRect();
                                hint.classList.remove('expanded');
                                hint.style.maxHeight = '';
                                btn.textContent = '▼ 展开';
                            } else {
                                hint.classList.add('expanded');
                                hint.style.maxHeight = fullHeight + 'px';
                                btn.textContent = '▲ 收起';
                            }
                        });
                        hint.addEventListener('click', () => btn.click());
                    }
                });
            }

            // 控件
            if (s.type === 'bool') {
                const wrap = document.createElement('div');
                wrap.className = 'toggle-wrap';
                const lbl = document.createElement('label');
                lbl.className = 'toggle';
                const input = document.createElement('input');
                input.type = 'checkbox';
                input.checked = !!val;
                input.addEventListener('change', () => { pending[key] = input.checked; });
                const slider = document.createElement('span');
                slider.className = 'toggle-slider';
                lbl.appendChild(input);
                lbl.appendChild(slider);
                wrap.appendChild(lbl);
                row.appendChild(wrap);
            } else if (s.type === 'int' || s.type === 'float') {
                const wrap = document.createElement('div');
                wrap.className = 'number-input-wrap';
                const input = document.createElement('input');
                input.type = 'number';
                input.value = val !== null ? val : '';
                if (s.type === 'float') input.step = '0.01';
                input.addEventListener('change', () => {
                    const v = s.type === 'int' ? parseInt(input.value) : parseFloat(input.value);
                    if (!isNaN(v)) pending[key] = v;
                });
                wrap.appendChild(input);
                row.appendChild(wrap);
            }

            const def = document.createElement('div');
            def.className = 'config-field-default';
            def.textContent = `默认: ${s.default}`;
            row.appendChild(def);

            content.appendChild(row);
        });

        // 保存按钮
        const saveRow = document.createElement('div');
        saveRow.style.cssText = 'display:flex;gap:8px;align-items:center;margin-top:4px;';
        saveRow.innerHTML = `
            <button class="btn btn-primary btn-sm" id="btn-save-webcfg">保存并重启插件</button>
            <span id="webcfg-status" style="font-size:12px;color:var(--text-secondary);"></span>`;
        content.appendChild(saveRow);

        document.getElementById('btn-save-webcfg').addEventListener('click', async () => {
            if (!Object.keys(pending).length) {
                Utils.toast('没有修改任何配置', 'warning');
                return;
            }
            const saveBtn = document.getElementById('btn-save-webcfg');
            const statusEl = document.getElementById('webcfg-status');
            saveBtn.disabled = true;
            saveBtn.textContent = '重启中...';
            statusEl.textContent = '正在重启，完成后将自动刷新...';
            statusEl.style.color = 'var(--accent)';

            const res = await Api.reloadPlugin(pending);
            if (res && !res.ok && !res.network_error) {
                saveBtn.disabled = false;
                saveBtn.textContent = '保存并重启插件';
                statusEl.textContent = '重启失败';
                statusEl.style.color = 'var(--danger)';
                Utils.toast(res.msg || '重启失败', 'error');
                return;
            }
            // 无论成功或网络错误，等待服务器恢复后自动刷新
            Object.assign(cfg, pending);
            Object.keys(pending).forEach(k => delete pending[k]);
            this._waitForServerAndRefresh();
        });
    },

    /** 加载 IP 访问控制配置 */
    async _renderFileBrowser() {
        const container = document.getElementById('files-container');
        if (!container) return;
        container.innerHTML = '';
        container.style.cssText = 'padding:24px;display:flex;gap:16px;height:100%;overflow:hidden;';

        // 左侧：文件列表
        const listPanel = document.createElement('div');
        listPanel.className = 'file-list-panel';
        listPanel.innerHTML = `
            <div class="file-refresh-notice">
                ⚠️ 文件数据可能已被外部更新，请及时点击右侧"刷新"按钮获取最新文件列表和内容。本页面不提供自动刷新功能。
            </div>
            <div class="file-list-header">
                <h3 style="margin:0;font-size:14px;">数据文件</h3>
                <button class="btn btn-sm" id="btn-refresh-files">刷新</button>
            </div>
            <div id="file-tree" class="file-tree"></div>`;
        container.appendChild(listPanel);

        // 右侧：文件内容编辑器
        const editorPanel = document.createElement('div');
        editorPanel.className = 'file-editor-panel';
        editorPanel.innerHTML = `
            <div class="file-editor-header">
                <span id="file-editor-title" style="font-size:14px;font-weight:600;">选择文件查看内容</span>
                <div id="file-editor-actions" class="file-editor-actions hidden">
                    <span id="file-editor-size" style="font-size:12px;color:var(--text-secondary);align-self:center;"></span>
                    <button class="btn btn-sm btn-primary" id="btn-save-file">保存</button>
                    <button class="btn btn-sm btn-danger" id="btn-delete-file">删除</button>
                </div>
            </div>
            <div id="file-editor-content" class="file-editor-content">
                <div class="chart-empty" style="margin:auto;">选择左侧文件查看内容</div>
            </div>`;
        container.appendChild(editorPanel);

        // 事件绑定
        document.getElementById('btn-refresh-files').addEventListener('click', () => this._refreshFileBrowser());
        document.getElementById('btn-save-file').addEventListener('click', () => this._saveCurrentFile());
        document.getElementById('btn-delete-file').addEventListener('click', () => this._deleteCurrentFile());

        await this._loadFileList();
    },

    /** 加载文件列表 */
    async _loadFileList() {
        const tree = document.getElementById('file-tree');
        if (!tree) return;
        tree.innerHTML = '<div class="chart-empty" style="padding:12px;">加载中...</div>';

        const res = await Api.fileList();
        if (!res.ok) {
            tree.innerHTML = '<div class="chart-empty" style="padding:12px;">加载失败</div>';
            return;
        }

        const files = res.files || [];
        if (!files.length) {
            tree.innerHTML = '<div class="chart-empty" style="padding:12px;">暂无数据文件</div>';
            return;
        }

        const statusMeta = {
            protected: { label: '受保护', className: 'file-status--protected' },
            editable: { label: '可编辑', className: 'file-status--editable' },
            delete_only: { label: '仅删除', className: 'file-status--readonly' },
        };

        const groups = {};
        files.forEach(f => {
            const dir = f.directory || '根目录';
            if (!groups[dir]) groups[dir] = [];
            groups[dir].push(f);
        });

        tree.innerHTML = '';
        const sortedDirs = Object.keys(groups).sort();
        sortedDirs.forEach(dir => {
            const dirEl = document.createElement('div');
            dirEl.className = 'file-group';

            const dirHeader = document.createElement('div');
            dirHeader.className = 'file-group-header';
            dirHeader.textContent = dir === '根目录' ? '/' : dir + '/';
            dirHeader.addEventListener('click', () => {
                dirEl.classList.toggle('collapsed');
            });
            dirEl.appendChild(dirHeader);

            const filesList = document.createElement('div');
            filesList.className = 'file-group-items';
            groups[dir].sort((a, b) => a.name.localeCompare(b.name)).forEach(f => {
                const item = document.createElement('div');
                item.className = 'file-item';
                if (f.protected) item.classList.add('file-protected');
                if (this._currentFilePath === f.path) item.classList.add('active');
                const icon = f.protected ? '🔒' : f.is_json ? '{}' : (f.is_text ? '📝' : '📄');
                const meta = statusMeta[f.status] || statusMeta.delete_only;
                item.innerHTML = `
                    <span class="file-icon">${icon}</span>
                    <div class="file-item-main">
                        <span class="file-name" title="${Utils.escapeHtml(f.path)}">${Utils.escapeHtml(f.name)}</span>
                        <div class="file-item-subline">
                            <span class="file-size">${Utils.formatSize(f.size)}</span>
                            <span class="file-status ${meta.className}">${meta.label}</span>
                        </div>
                    </div>`;
                item.addEventListener('click', () => this._openFile(f));
                filesList.appendChild(item);
            });
            dirEl.appendChild(filesList);
            tree.appendChild(dirEl);
        });
    },

    /** 刷新文件浏览器：刷新文件列表 + 重新读取当前打开的文件 */
    async _refreshFileBrowser() {
        // 如果有未保存的修改，先确认
        if (this._fileEditorDirty) {
            const ok = await Utils.confirm('当前文件有未保存的修改，刷新将丢失修改，是否继续？');
            if (!ok) return;
        }

        await this._loadFileList();

        // 如果有当前打开的文件，重新读取内容
        if (this._currentFilePath && this._currentFileMeta) {
            this._fileEditorDirty = false;
            await this._openFile(this._currentFileMeta);
        }

        Utils.toast('文件列表已刷新', 'success', 2000);
    },

    /** 打开文件 */
    async _openFile(fileMeta) {
        const path = fileMeta?.path;
        if (!path) return;
        if (this._fileEditorDirty) {
            const ok = await Utils.confirm('当前文件有未保存的修改，确定放弃？');
            if (!ok) return;
        }

        this._currentFilePath = path;
        this._currentFileMeta = fileMeta;
        this._fileEditorDirty = false;
        const content = document.getElementById('file-editor-content');
        const title = document.getElementById('file-editor-title');
        const actions = document.getElementById('file-editor-actions');
        const sizeEl = document.getElementById('file-editor-size');
        const saveBtn = document.getElementById('btn-save-file');
        const deleteBtn = document.getElementById('btn-delete-file');

        title.textContent = path;

        document.querySelectorAll('.file-item').forEach(el => el.classList.remove('active'));
        document.querySelectorAll('.file-item').forEach(el => {
            if (el.querySelector('.file-name')?.title === path) {
                el.classList.add('active');
            }
        });

        if (fileMeta.protected) {
            actions.classList.add('hidden');
            content.innerHTML = `<div class="chart-empty" style="margin:auto;text-align:center;line-height:1.8;">
                <div style="font-size:32px;margin-bottom:8px;">🔒</div>
                <div style="font-weight:600;">此文件受保护</div>
                <div style="color:var(--text-secondary);font-size:13px;">出于安全考虑，不支持在线查看、编辑或删除。<br>如需处理，请前往服务器本地对应目录手动操作。</div>
            </div>`;
            return;
        }

        content.innerHTML = '<div class="chart-empty" style="margin:auto;">加载中...</div>';

        const res = await Api.fileRead(path);
        if (!res.ok) {
            content.innerHTML = `<div class="chart-empty" style="margin:auto;">${Utils.escapeHtml(res.msg || '读取失败')}</div>`;
            actions.classList.add('hidden');
            return;
        }

        this._currentFileMeta = { ...fileMeta, ...res };
        actions.classList.remove('hidden');
        actions.style.display = 'flex';
        sizeEl.textContent = Utils.formatSize(res.content.length);
        saveBtn.classList.toggle('hidden', !res.can_edit);
        deleteBtn.classList.toggle('hidden', !res.can_delete);

        content.innerHTML = '';
        const textarea = document.createElement('textarea');
        textarea.className = 'file-textarea';
        textarea.spellcheck = false;
        textarea.readOnly = !res.can_edit;

        if (res.is_json && res.parsed !== null) {
            textarea.value = JSON.stringify(res.parsed, null, 2);
        } else {
            textarea.value = res.content;
        }

        if (!res.can_edit) {
            const readonlyHint = document.createElement('div');
            readonlyHint.className = 'file-editor-hint';
            readonlyHint.textContent = '该文件可查看，但当前不支持在线编辑。';
            content.appendChild(readonlyHint);
        }

        textarea.addEventListener('input', () => {
            if (!res.can_edit) return;
            this._fileEditorDirty = true;
            title.textContent = path + ' (已修改)';
        });
        content.appendChild(textarea);
    },

    /** 保存当前文件 */
    async _saveCurrentFile() {
        if (!this._currentFilePath || !this._currentFileMeta?.can_edit) return;
        const textarea = document.querySelector('.file-textarea');
        if (!textarea) return;

        const saveBtn = document.getElementById('btn-save-file');
        saveBtn.disabled = true;
        saveBtn.textContent = '保存中...';

        const res = await Api.fileSave(this._currentFilePath, textarea.value);
        saveBtn.disabled = false;
        saveBtn.textContent = '保存';

        if (res.ok) {
            Utils.toast(res.msg || '保存成功', 'success');
            this._fileEditorDirty = false;
            this._currentFileMeta = { ...this._currentFileMeta, ...res };
            document.getElementById('file-editor-title').textContent = this._currentFilePath;
        } else {
            if (res.msg && (res.msg.includes('不存在') || res.msg.includes('已删除'))) {
                this._currentFileMeta = null;
                this._fileEditorDirty = false;
                document.getElementById('file-editor-title').textContent = '选择文件查看内容';
                document.getElementById('file-editor-content').innerHTML =
                    '<div class="chart-empty" style="margin:auto;">文件已不存在，请刷新列表后重新打开</div>';
                document.getElementById('file-editor-actions').classList.add('hidden');
                await this._loadFileList();
            }
            Utils.toast(res.msg || '保存失败', 'error');
        }
    },

    /** 删除当前文件 */
    async _deleteCurrentFile() {
        if (!this._currentFilePath || !this._currentFileMeta?.can_delete) return;
        const ok = await Utils.confirm(`确认删除文件 "${this._currentFilePath}"？此操作不可恢复。`);
        if (!ok) return;

        const res = await Api.fileDelete(this._currentFilePath);
        if (res.ok) {
            Utils.toast(res.msg || '已删除', 'success');
            this._currentFilePath = null;
            this._currentFileMeta = null;
            this._fileEditorDirty = false;
            document.getElementById('file-editor-title').textContent = '选择文件查看内容';
            document.getElementById('file-editor-content').innerHTML =
                '<div class="chart-empty" style="margin:auto;">文件已删除</div>';
            document.getElementById('file-editor-actions').classList.add('hidden');
            await this._loadFileList();
        } else {
            if (res.msg && (res.msg.includes('不存在') || res.msg.includes('已删除'))) {
                this._currentFilePath = null;
                this._currentFileMeta = null;
                this._fileEditorDirty = false;
                document.getElementById('file-editor-title').textContent = '选择文件查看内容';
                document.getElementById('file-editor-content').innerHTML =
                    '<div class="chart-empty" style="margin:auto;">文件已不存在，请刷新列表后重试</div>';
                document.getElementById('file-editor-actions').classList.add('hidden');
                await this._loadFileList();
            }
            Utils.toast(res.msg || '删除失败', 'error');
        }
    },

    /** 更新 IP 名单标签文字 */
    async _waitForServerAndRefresh(maxWaitMs = 35000, intervalMs = 3000) {
        const startedAt = Date.now();
        // 先等一小段时间让旧插件实例完全停止
        await new Promise(r => setTimeout(r, 1500));

        const poll = async () => {
            if (Date.now() - startedAt >= maxWaitMs) {
                Utils.toast('插件重启耗时较长，请手动刷新页面（F5）确认最新配置已生效', 'warning', 8000);
                return;
            }
            try {
                const res = await Api.dataStatus();
                if (res && res.ok) {
                    Utils.toast('插件已重启，正在刷新页面...', 'success', 2000);
                    setTimeout(() => { window.location.reload(); }, 800);
                    return;
                }
            } catch (_e) {
                // 请求失败 = 插件尚未恢复，继续轮询
            }
            setTimeout(poll, intervalMs);
        };
        poll();
    },

    /** 轮询重启/重载状态，在右上角弹出结果提示。 */
    async _pollRestartStatus(operation, maxWaitMs = 30000, intervalMs = 2500) {
        const startedAt = Date.now();
        const label = operation === 'restart' ? 'AstrBot 重启' : '插件重载';

        return new Promise((resolve) => {
            const poll = async () => {
                if (Date.now() - startedAt >= maxWaitMs) {
                    Utils.toast(`${label}操作超时，请检查服务器日志`, 'warning', 5000);
                    resolve('timeout');
                    return;
                }
                try {
                    const res = await Api.restartStatus();
                    if (res && res.ok && res.status) {
                        if (res.status === 'success') {
                            Utils.toast(`${label}成功`, 'success', 4000);
                            resolve('success');
                            return;
                        }
                        if (res.status === 'failed') {
                            const errMsg = res.error || '未知错误';
                            Utils.toast(`${label}失败: ${errMsg}`, 'error', 6000);
                            resolve('failed');
                            return;
                        }
                    }
                } catch (_e) {
                    // 连接断开在重启过程中是正常现象，继续轮询
                }
                setTimeout(poll, intervalMs);
            };
            setTimeout(poll, 1500);
        });
    },

    _ensureConfigBadge() {
        if (document.getElementById('config-file-badge')) return;
        const badge = document.createElement('div');
        badge.id = 'config-file-badge';
        badge.className = 'config-file-badge hidden';
        badge.innerHTML = `
            <div class="config-file-badge__row">
                <span class="config-file-badge__label">配置文件</span>
                <span class="config-file-badge__value"></span>
            </div>
            <button class="config-file-badge__download" title="点击下载配置文件" aria-label="下载配置文件">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                    <polyline points="7 10 12 15 17 10"/>
                    <line x1="12" y1="15" x2="12" y2="3"/>
                </svg>
            </button>`;
        document.body.appendChild(badge);

        // 绑定下载事件
        const downloadBtn = badge.querySelector('.config-file-badge__download');
        if (downloadBtn) {
            downloadBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this._downloadCurrentConfig();
            });
        }
    },

    _updateConfigBadgeVisibility() {
        const badge = document.getElementById('config-file-badge');
        if (!badge) return;
        const visibleViews = new Set(['tech-tree', 'settings']);
        const shouldShow = visibleViews.has(this._currentView) && !!this._configFileName && !this._configPanelOpen;
        badge.classList.toggle('hidden', !shouldShow);
        const valueEl = badge.querySelector('.config-file-badge__value');
        if (valueEl) valueEl.textContent = this._configFileName || '—';
        // 隐藏状态下禁用下载按钮
        const downloadBtn = badge.querySelector('.config-file-badge__download');
        if (downloadBtn) {
            downloadBtn.disabled = !shouldShow;
        }
    },

    /** 下载当前配置文件（通过 bridge.download 触发浏览器下载） */
    async _downloadCurrentConfig() {
        const downloadBtn = document.querySelector('.config-file-badge__download');
        const resetBtn = () => {
            if (downloadBtn) {
                downloadBtn.disabled = false;
                downloadBtn.classList.remove('config-file-badge__download--loading');
            }
        };

        if (downloadBtn) {
            downloadBtn.disabled = true;
            downloadBtn.classList.add('config-file-badge__download--loading');
        }

        const res = await Api.downloadConfig(this._configFileName || 'config.json');
        resetBtn();

        if (res && res.ok) {
            Utils.toast('配置文件已开始下载', 'success', 2000);
            return;
        }
        Utils.alert(
            '下载失败\n\n' +
            ((res && res.msg) ? res.msg : '未知错误') + '\n\n' +
            '可能原因：\n' +
            '- 插件视图桥接未加载或请求被拒绝\n' +
            '- 配置文件尚未生成（插件尚未完成首次配置加载）\n' +
            '- 插件正在重启或暂时不可用\n\n' +
            '请检查 AstrBot 日志和 Web 面板访问日志获取详细错误信息'
        );
    },

    setConfigPanelOpen(isOpen) {
        this._configPanelOpen = !!isOpen;
        document.body.classList.toggle('config-panel-open', this._configPanelOpen);
        this._updateConfigBadgeVisibility();
    },
};

// 启动应用
document.addEventListener('DOMContentLoaded', () => App.start());

// 移动端：虚拟键盘弹出时确保焦点元素可见
(function() {
    if (!window.visualViewport) return;

    let pendingScroll = null;
    window.visualViewport.addEventListener('resize', () => {
        if (window.innerWidth >= 768) return;
        const active = document.activeElement;
        if (!active) return;
        const tag = active.tagName.toLowerCase();
        if (tag !== 'textarea' && tag !== 'input') return;

        clearTimeout(pendingScroll);
        pendingScroll = setTimeout(() => {
            const vpH = window.visualViewport.height;
            const rect = active.getBoundingClientRect();
            if (rect.bottom > vpH * 0.9 || rect.top > vpH * 0.35) {
                active.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'smooth' });
            }
        }, 200);
    });
})();
