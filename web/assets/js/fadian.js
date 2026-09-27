/* 为爱发电页：套餐 + 权益明细 + 当前赞助状态 */
(function () {
  const { createApp, ref, computed, onMounted } = Vue;

  const IC = {
    unlock: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="10.5" width="16" height="10" rx="2"/><path d="M8 10.5V7.6A4 4 0 0 1 15.7 6"/></svg>',
    vip:    '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7.5l4 3.5 4.5-6 4.5 6 4-3.5-1.6 11H5.1z"/></svg>',
    sign:   '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19c3.5 0 5-9 8-9s3.5 5 5 5 2-1.5 2-1.5"/><path d="M4 21h16"/></svg>',
    pkg:    '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/></svg>',
  };

  const app = createApp({
    setup() {
      const ready = ref(false);
      const site = ref({});
      const user = ref({});
      const menu = ref(false);
      const year = new Date().getFullYear();
      const tiers = ref([]);
      const currency = ref('CNY');

      const SYM = { CNY: '¥', USD: '$', EUR: '€', JPY: '¥', GBP: '£' };
      const currencySymbol = computed(() => SYM[currency.value] || '¥');

      const perks = ref([
        { title: '解锁全部定制项', icon: IC.unlock,
          desc: '主机名、签名、根分区容量、文件系统、VMDK、ROOTFS、自定义文件包等全部打开。' },
        { title: 'VIP 构建通道', icon: IC.vip,
          desc: '队列优先，减少排队等待。' },
        { title: '自定义主机名与签名', icon: IC.sign,
          desc: '固件里的主机名与作者签名由你自己定。' },
        { title: '插件数量不限', icon: IC.pkg,
          desc: '免费用户受 luci-app 插件配额限制，赞助后不限。' },
      ]);

      // 与首页定制表单里的锁定项保持一致
      const detailRows = ref([
        ['插件数量', 'luci-app-* 插件可勾选的数量上限', '12 个', '不限'],
        ['主机名', '固件默认主机名（hostname）', '锁定', '可改'],
        ['自定义签名', '固件里的作者/站点签名', '锁定', '可改'],
        ['根分区容量', 'rootfs 分区大小', '默认', '可调'],
        ['文件系统', 'squashfs / ext4 等', '默认', '可选'],
        ['ROOTFS', '单独生成 rootfs 产物', '锁定', '可用'],
        ['VMDK', '生成 VMware 虚拟磁盘', '锁定', '可用'],
        ['自定义文件包', '额外打入自备 ipk', '锁定', '可用'],
        ['去除作者外链', '清掉固件内的作者推广链接', '锁定', '可用'],
        ['USB 无线网卡', '预置 USB WiFi 驱动', '默认', '可选'],
      ]);

      function fmtDate(ts) {
        if (!ts) return '';
        const d = new Date(ts * 1000);
        const p = n => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
      }

      onMounted(async () => {
        try { site.value = await K.api('/api/v1/site'); } catch (e) { }
        try { user.value = await K.api('/api/v1/user'); } catch (e) { }
        try {
          const d = await K.api('/api/v1/sponsor/tiers');
          tiers.value = d.tiers || [];
          currency.value = d.currency || 'CNY';
        } catch (e) { }
        ready.value = true;
      });

      return {
        ready, site, user, menu, year, tiers, currencySymbol,
        perks, detailRows, fmtDate,
      };
    }
  });

  app.mount('#app');
})();
