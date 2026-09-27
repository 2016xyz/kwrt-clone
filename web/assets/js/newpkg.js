/* 插件提议页：提交 + 列表 + 撤回 */
(function () {
  const { createApp, ref, computed, onMounted } = Vue;

  const STATUS = {
    pending:  { text: '待审核', cls: 'badge-warning' },
    approved: { text: '已收录', cls: 'badge-success' },
    rejected: { text: '未通过', cls: '' },
  };

  const app = createApp({
    setup() {
      const ready = ref(false);
      const site = ref({});
      const user = ref({});
      const menu = ref(false);
      const year = new Date().getFullYear();

      const f = ref({ name: '', url: '', note: '' });
      const busy = ref(false);
      const err = ref('');
      const okMsg = ref('');

      const list = ref([]);
      const total = ref(0);

      const canSubmit = computed(() => !!(f.value.name && f.value.url));

      const statusText = s => (STATUS[s || 'pending'] || STATUS.pending).text;
      const statusClass = s => (STATUS[s || 'pending'] || STATUS.pending).cls;

      function fmtDate(ts) {
        if (!ts) return '';
        const d = new Date(ts * 1000);
        const p = n => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
               `${p(d.getHours())}:${p(d.getMinutes())}`;
      }

      function reset() {
        f.value = { name: '', url: '', note: '' };
        err.value = '';
        okMsg.value = '';
      }

      async function load() {
        try {
          const d = await K.api('/api/v1/proposals?limit=50');
          // 公开列表不展示被驳回的条目数量细节，但保留状态徽章
          list.value = d.proposals || [];
          total.value = d.count || 0;
        } catch (e) {
          /* 列表加载失败不阻塞提交 */
        }
      }

      async function submit() {
        err.value = ''; okMsg.value = '';
        if (!f.value.name) { err.value = '请填写插件名'; return; }
        if (!/^https?:\/\/\S+$/.test(f.value.url)) {
          err.value = '仓库地址必须是 http(s) 链接'; return;
        }
        busy.value = true;
        try {
          const r = await K.postForm('/api/v1/propose', f.value);
          okMsg.value = r.detail || '提交成功，等待管理员审核';
          K.toast(okMsg.value, 'success');
          f.value = { name: '', url: '', note: '' };
          await load();
        } catch (e) {
          err.value = e.message || '提交失败';
          K.toast(err.value, 'error');
        } finally {
          busy.value = false;
        }
      }

      async function withdraw(p) {
        if (!confirm(`确定撤回「${p.name}」这条提议？`)) return;
        try {
          await K.api('/api/v1/propose/' + p.id, { method: 'DELETE' });
          K.toast('已撤回', 'success');
          await load();
        } catch (e) {
          K.toast(e.message || '撤回失败', 'error');
        }
      }

      onMounted(async () => {
        try { site.value = await K.api('/api/v1/site'); } catch (e) { }
        try { user.value = await K.api('/api/v1/user'); } catch (e) { }
        await load();
        ready.value = true;
      });

      return {
        ready, site, user, menu, year,
        f, busy, err, okMsg, list, total, canSubmit,
        statusText, statusClass, fmtDate, reset, submit, withdraw,
      };
    }
  });

  app.mount('#app');
})();
