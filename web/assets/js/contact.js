/* 联系页 */
(function () {
  const { createApp, ref, onMounted } = Vue;

  const app = createApp({
    setup() {
      const ready = ref(false);
      const site = ref({});
      const user = ref({});
      const menu = ref(false);
      const year = new Date().getFullYear();

      const faqs = ref([
        { q: '构建失败了怎么办？',
          a: '先看构建记录里的日志输出。常见原因是所选插件与目标平台不兼容，' +
             '或该插件当前上游源码有编译错误。换掉个别插件重试通常就能过。' },
        { q: '为什么只能选 12 个插件？',
          a: '服务器资源有限，免费用户对 luci-app-* 插件有数量限制。' +
             '赞助后可解除限制，详见 <a href="/fadian/">为爱发电</a>。' },
        { q: '想要某个插件被收录？',
          a: '到 <a href="/newpkg/">插件提议</a> 提交仓库地址，审核通过后会并入软件源。' },
        { q: '下载链接过期了？',
          a: '链接有有效期限制。登录后在账户页的构建记录里可以重新生成下载链接。' },
        { q: '固件刷写后有问题？',
          a: '本站按所选配置如实编译官方源码，不额外改动固件行为。' +
             '刷写前请确认设备型号与目标平台一致，并保留原厂固件以便回退。' },
      ]);

      onMounted(async () => {
        try { site.value = await K.api('/api/v1/site'); } catch (e) { }
        try { user.value = await K.api('/api/v1/user'); } catch (e) { }
        ready.value = true;
      });

      return { ready, site, user, menu, year, faqs };
    }
  });

  app.mount('#app');
})();
