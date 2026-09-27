/* 软件库页：架构索引 + 包名检索 */
(function () {
  const { createApp, ref, computed, onMounted, watch } = Vue;

  const app = createApp({
    setup() {
      const ready = ref(false);
      const site = ref({});
      const user = ref({});
      const menu = ref(false);
      const year = new Date().getFullYear();

      const archs = ref([]);
      const releases = ref([]);
      const relFilter = ref('');
      const cur = ref('');
      const q = ref('');
      const result = ref(null);
      const searching = ref(false);

      const totalPkgs = computed(() =>
        archs.value.reduce((s, a) => s + (a.count || 0), 0));

      const filteredArchs = computed(() =>
        relFilter.value ? archs.value.filter(a => a.release === relFilter.value) : archs.value);

      async function doSearch() {
        if (!cur.value) return;
        searching.value = true;
        try {
          const [src, arch] = cur.value.split('/');
          const d = await K.api('/api/v1/packages/search?q=' + encodeURIComponent(q.value) +
                                '&src=' + encodeURIComponent(src) +
                                '&arch=' + encodeURIComponent(arch) + '&limit=100');
          result.value = d;
          if (d.detail) K.toast(d.detail, 'error');
        } catch (e) {
          K.toast(e.message || '检索失败', 'error');
        } finally {
          searching.value = false;
        }
      }

      // 切换版本时，若当前架构不属于该版本则自动选中第一个
      watch([relFilter, archs], () => {
        const list = filteredArchs.value;
        if (!list.length) { cur.value = ''; result.value = null; return; }
        if (!list.find(a => a.src + '/' + a.arch === cur.value)) {
          cur.value = list[0].src + '/' + list[0].arch;
          result.value = null;
        }
      }, { deep: true });

      onMounted(async () => {
        try { site.value = await K.api('/api/v1/site'); } catch (e) { }
        try { user.value = await K.api('/api/v1/user'); } catch (e) { }
        try {
          const d = await K.api('/api/v1/packages/archs');
          archs.value = d.archs || [];
          releases.value = d.releases || [];
          if (archs.value.length) cur.value = archs.value[0].src + '/' + archs.value[0].arch;
        } catch (e) {
          K.toast(e.message || '索引加载失败', 'error');
        }
        ready.value = true;
      });

      return {
        ready, site, user, menu, year,
        archs, releases, relFilter, cur, q, result, searching,
        totalPkgs, filteredArchs, doSearch,
      };
    }
  });

  app.mount('#app');
})();
