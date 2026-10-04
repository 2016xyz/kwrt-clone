<?php
/**
 * 全站布局。所有页面模板都被它包住（View::page() 负责把内容渲成 $content）。
 *
 * 注意所有变量输出都过 e()；只有 $content 是 raw() —— 它来自我们自己的模板。
 */
use Kwrt\Net;

$siteName  = (string) setting('site_name', 'Kwrt');
$siteShort = (string) setting('site_short', 'Kwrt');
$logo      = (string) setting('logo_url', '');
$favicon   = (string) setting('favicon_url', '') ?: $logo;
$theme     = (string) setting('theme_default', 'auto');
$accent    = (string) setting('primary_color', '#2563eb');
$navItems  = $nav ?? [];
$u         = $user ?? null;
$entryApp    = (bool) setting('entry.app_enabled', false);
$entryWechat = (bool) setting('entry.wechat_enabled', false);
?>
<!DOCTYPE html>
<html lang="zh-CN" data-theme="<?= e($theme) ?>">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title><?= e($pageTitle ?? $siteName) ?></title>
<meta name="description" content="<?= e((string) setting('site_desc', '')) ?>">
<?php if ($favicon !== ''): ?>
<link rel="icon" href="<?= e(Net::asset($favicon)) ?>">
<?php endif; ?>
<link rel="stylesheet" href="<?= e(asset('assets/css/app.css')) ?>">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="<?= e($accent) ?>">
<?php if (t_bool('entry.pwa_enabled', true)): ?>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="<?= e((string) setting('entry.pwa_short_name', $siteShort)) ?>">
<?php endif; ?>
<style>:root{--accent:<?= e($accent) ?>}</style>
<script>
// 传给前端的最小运行态（不塞任何敏感值）
window.KWRT = <?= js([
    'base'     => Net::baseUrl(),
    'csrf'     => csrf(),
    'user'     => $u ? ['username' => $u['username'], 'admin' => is_admin(), 'sponsor' => is_sponsor()] : null,
    'nav'      => $navItems,
    'pollMs'   => (int) setting('pay.poll_seconds', 3) * 1000,
    'pwdMin'   => 8,
]) ?>;
</script>
</head>
<body>
<a class="skip" href="#main">跳到主要内容</a>

<header class="site-head">
  <div class="wrap head-in">
    <a class="brand" href="/">
      <?php if ($logo !== ''): ?>
        <img src="<?= e(Net::asset($logo)) ?>" alt="" width="28" height="28">
      <?php endif; ?>
      <span><?= e($siteShort) ?></span>
    </a>

    <button class="burger" id="burger" type="button" aria-label="打开菜单" aria-expanded="false">
      <span></span><span></span><span></span>
    </button>

    <nav class="main-nav" id="mainNav" aria-label="主导航">
      <?php if (!empty($navItems['home'])): ?>
        <a href="/"<?= ($current_path ?? '') === '/' ? ' class="on" aria-current="page"' : '' ?>>首页</a>
      <?php endif; ?>
      <?php if (!empty($navItems['packages'])): ?>
        <a href="/packages/"<?= str_starts_with((string) ($current_path ?? ''), '/packages') ? ' class="on"' : '' ?>>软件包</a>
      <?php endif; ?>
      <?php if (!empty($navItems['newpkg'])): ?>
        <a href="/newpkg/"<?= str_starts_with((string) ($current_path ?? ''), '/newpkg') ? ' class="on"' : '' ?>>插件提议</a>
      <?php endif; ?>
      <?php if (!empty($navItems['fadian'])): ?>
        <a href="/fadian/"<?= str_starts_with((string) ($current_path ?? ''), '/fadian') ? ' class="on"' : '' ?>>为爱发电</a>
      <?php endif; ?>
      <?php if (!empty($navItems['contact'])): ?>
        <a href="/contact/"<?= str_starts_with((string) ($current_path ?? ''), '/contact') ? ' class="on"' : '' ?>>联系我们</a>
      <?php endif; ?>
      <?php if ($entryApp): ?>
        <a href="#app" data-open="app">App 下载</a>
      <?php endif; ?>
      <?php if ($entryWechat): ?>
        <a href="#wechat" data-open="wechat">公众号</a>
      <?php endif; ?>
      <?php foreach (\Kwrt\Net::homepageLinks() as $lnk): ?>
        <a href="<?= e($lnk['url']) ?>"><?= e($lnk['label']) ?></a>
      <?php endforeach; ?>
      </nav>

    <div class="head-act">
      <button class="btn btn-ghost btn-sm" id="themeBtn" type="button" aria-label="切换主题">◑</button>
      <?php if ($u): ?>
        <a class="btn btn-ghost btn-sm" href="/?orders=1">我的订单</a>
        <?php if (is_admin()): ?><a class="btn btn-outline btn-sm" href="/admin/">管理后台</a><?php endif; ?>
        <button class="btn btn-ghost btn-sm" id="logoutBtn" type="button">退出</button>
      <?php elseif (!empty($navItems['login'])): ?>
        <a class="btn btn-primary btn-sm" href="/login/">登录 / 注册</a>
      <?php endif; ?>
    </div>
  </div>
</header>

<?php if (($announce = trim((string) setting('announcement', ''))) !== ''): ?>
<div class="notice" role="status"><div class="wrap"><?= e($announce) ?></div></div>
<?php endif; ?>

<?php if (!empty($flash)): ?>
<div class="flash" role="status"><div class="wrap"><?= e($flash) ?></div></div>
<?php endif; ?>

<main id="main" class="wrap page">
  <?= raw($content) ?>
</main>

<footer class="site-foot">
  <div class="wrap foot-in">
    <div class="foot-main">
      <div class="foot-brand">
        <strong><?= e($siteShort) ?></strong>
        <p><?= e((string) setting('footer_text', '')) ?></p>
      </div>
      <?php if (trim((string) setting('footer_moat_title', '')) !== ''): ?>
      <div class="foot-col">
        <h4><?= e((string) setting('footer_moat_title', '')) ?></h4>
        <p><?= e((string) setting('footer_moat_text', '')) ?></p>
      </div>
      <?php endif; ?>
      <div class="foot-col">
        <h4>入口</h4>
        <ul>
          <?php if (!empty($navItems['packages'])): ?><li><a href="/packages/">软件包库</a></li><?php endif; ?>
          <?php if (!empty($navItems['newpkg'])): ?><li><a href="/newpkg/">插件提议</a></li><?php endif; ?>
          <?php if (!empty($navItems['fadian'])): ?><li><a href="/fadian/">为爱发电</a></li><?php endif; ?>
          <?php if (!empty($navItems['contact'])): ?><li><a href="/contact/">联系我们</a></li><?php endif; ?>
        </ul>
      </div>
      <?php if ($entryWechat || $entryApp): ?>
      <div class="foot-col">
        <h4>关注</h4>
        <ul>
          <?php if ($entryApp): ?><li><a href="#app" data-open="app">App 下载</a></li><?php endif; ?>
          <?php if ($entryWechat): ?><li><a href="#wechat" data-open="wechat">公众号</a></li><?php endif; ?>
        </ul>
      </div>
      <?php endif; ?>
    </div>
    <div class="foot-bot">
      <span>© <?= e(date('Y')) ?> <?= e($siteName) ?> · <?= e(\Kwrt\Version::display()) ?></span>
      <?php /* 不显示 PHP 版本：精确版本号会告诉攻击者该试哪些 CVE，属信息泄露；
               Python 版也从不显示语言/框架版本 —— 去掉正好让两版一致。 */ ?>
      <span class="muted">构建由 OpenWrt 官方 ImageBuilder 完成</span>
    </div>
  </div>
</footer>

<?php require __DIR__ . '/partials/entry_modals.php'; ?>

<div id="toast" class="toast" role="status" aria-live="polite" hidden></div>
<script src="<?= e(asset('assets/js/app.js')) ?>" defer></script>
<script src="<?= e(asset('assets/js/ads.js')) ?>" defer></script>
<script src="<?= e(asset('assets/js/ads-boot.js')) ?>" defer></script>
</body>
</html>
