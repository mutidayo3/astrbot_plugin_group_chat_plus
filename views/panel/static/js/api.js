/**
 * api.js - 插件视图后端调用封装（AstrBotPluginView bridge）
 *
 * 视图运行在受限 iframe 中（无 allow-same-origin），不能直接 fetch，
 * 也不能使用浏览器本地存储与 Cookie。
 * 所有请求都通过 window.AstrBotPluginView 的 apiGet / apiPost / download 转发。
 */

const Api = {
    /** 惰性获取 bridge（bridge SDK 可能晚于本脚本执行） */
    bridge() {
        return window.AstrBotPluginView || window.AstrBotPluginPage || null;
    },

    /**
     * 统一请求入口
     * @param {'get'|'post'} method
     * @param {string} endpoint 插件内相对端点（不含插件名前缀）
     * @param {object} [bodyOrParams]
     * @returns {Promise<object>} 后端 JSON（形如 {ok:true, ...}）或 {ok:false, msg}
     */
    async _call(method, endpoint, bodyOrParams) {
        const bridge = this.bridge();
        if (!bridge) {
            return { ok: false, msg: '插件视图桥接未加载' };
        }
        try {
            if (method === 'post') {
                const data = await bridge.apiPost(endpoint, bodyOrParams || {});
                return data && typeof data === 'object' ? data : { ok: true, data };
            }
            const data = await bridge.apiGet(endpoint, bodyOrParams || {});
            return data && typeof data === 'object' ? data : { ok: true, data };
        } catch (error) {
            return {
                ok: false,
                network_error: true,
                msg: (error && error.message) || '请求失败',
            };
        }
    },

    get(endpoint, params) { return this._call('get', endpoint, params); },
    post(endpoint, body) { return this._call('post', endpoint, body); },

    // ===================== 配置 =====================
    getConfig()              { return this.get('config'); },
    putConfig(config)        { return this.post('config', { config }); },
    reloadPlugin(config)     { return this.post('config/reload', config ? { config } : {}); },

    /** 下载当前配置文件（bridge.download 会直接触发浏览器下载） */
    async downloadConfig(filename) {
        const bridge = this.bridge();
        if (!bridge) {
            return { ok: false, msg: '插件视图桥接未加载' };
        }
        try {
            await bridge.download('config/download', {}, filename || 'config.json');
            return { ok: true };
        } catch (error) {
            return {
                ok: false,
                network_error: true,
                msg: (error && error.message) || '下载失败',
            };
        }
    },

    // ===================== 数据可视化 =====================
    dataSessions()          { return this.get('data/sessions'); },
    dataAttention(session)  { return this.get(`data/attention/${session}`); },
    dataMood(session)       { return this.get(`data/mood/${session}`); },
    dataProbability(session) { return this.get(`data/probability/${session}`); },
    dataProactive()         { return this.get('data/proactive'); },
    dataOverview()          { return this.get('data/overview'); },
    dataStatus()            { return this.get('data/status'); },
    sessionDetail(session)  { return this.get(`data/session-detail/${session}`); },

    // ===================== 会话管理 =====================
    sessionList()            { return this.get('session/list'); },
    sessionReset(session)    { return this.post(`session/reset/${session}`, {}); },
    clearImageCache()        { return this.post('session/clear-image-cache', {}); },
    sessionCleanGhosts()     { return this.post('session/clean-ghosts', {}); },
    getChatHistory(session)  { return this.get(`session/chat-history/${session}`); },
    putChatHistory(session, messages) {
        return this.post(`session/chat-history/${session}`, { messages });
    },
    getImageCache()          { return this.get('session/image-cache'); },

    // ===================== 指令执行 =====================
    cmdReset(restart_mode)                  { return this.post('commands/reset', { restart_mode }); },
    cmdResetHere(session_id, restart_mode)  { return this.post('commands/reset-here', { session_id, restart_mode }); },
    cmdClearImageCache(restart_mode)        { return this.post('commands/clear-image-cache', { restart_mode }); },
    restartStatus()                         { return this.get('commands/restart-status'); },

    // ===================== 安全 / 访问控制 =====================
    fileList()               { return this.get('files/list'); },
    fileRead(path)           { return this.get('files/read', { path }); },
    fileSave(path, content)  { return this.post('files/save', { path, content }); },
    fileDelete(path)         { return this.post('files/delete', { path }); },
};
