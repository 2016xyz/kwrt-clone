<?php
/** 管理后台布局。与前台共用设计系统，但独立的导航与更宽的容器。 */
$siteShort = (string) setting('site_short', 'Kwrt');
$u = $user ?? null;
?>
<!DOCTYPE html>
<html lang="zh-CN" data-theme="<?= e((string) setting('theme_default', 'auto')) ?>">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><?= e($pageTitle ?? '管理控制台') ?> · <?= e($siteShort) ?></title>
<link rel="stylesheet" href="<?= e(asset('assets/css/app.css')) ?>">
<link rel="stylesheet" href="<?= e(asset('assets/css/admin.css')) ?>">
<meta name="robots" content="noindex,nofollow">
<script>
window.KWRT = <?= js([
    'base' => Kwrt\Net::baseUrl(),
    'csrf' => csrf(),
    'user' => $u ? ['username' => $u['username'], 'admin' => true] : null,
]) ?>;
</script>
</head>
<body class="admin-body">
<header class="site-head">
  <div class="wrap head-in">
    <a class="brand" href="/admin/">
      <strong><?= e($siteShort) ?></strong>
      <span class="tag">管理控制台</span>
      <span class="tag" title="当前版本"><?= e(\Kwrt\Version::display()) ?></span>
    </a>
    <div class="head-act">
      <a class="btn btn-ghost btn-sm" href="/" target="_blank" rel="noopener">返回站点</a>
      <button class="btn btn-ghost btn-sm" id="themeBtn" type="button" aria-label="切换主题">◑</button>
      <span class="muted sm"><?= e((string) ($u['username'] ?? '')) ?></span>
      <button class="btn btn-ghost btn-sm" id="logoutBtn" type="button">退出</button>
    </div>
  </div>
</header>

<div class="admin-wrap">
  <aside class="admin-side">
    <nav aria-label="后台模块">
      <?php foreach (($tabs ?? []) as $k => $label): ?>
        <a class="admin-nav-item<?= $k === ($tab ?? '') ? ' active' : '' ?>"
           href="/admin/?tab=<?= e($k) ?>"><?= e($label) ?></a>
      <?php endforeach; ?>
    </nav>
  </aside>
  <main class="admin-main">
    <?php if (!empty($flash)): ?><div class="flash"><?= e($flash) ?></div><?php endif; ?>
    <?= raw($content) ?>
  </main>
</div>

<div id="toast" class="toast" role="status" aria-live="polite" hidden></div>
<script src="<?= e(asset('assets/js/app.js')) ?>" defer></script>
<script src="<?= e(asset('assets/js/admin.js')) ?>" defer></script>
</body>
</html>
