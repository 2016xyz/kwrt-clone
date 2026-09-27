<?php /** 固件索引页。 */ ?>
<section class="page-head">
  <h1><?= e($target) ?> / <?= e($id) ?></h1>
  <p class="muted">预编译镜像与在线定制入口</p>
</section>
<section class="card">
  <h2>可用构建</h2>
  <?php if (!empty($info['branch'])): ?>
    <p>分支：<span class="mono"><?= e((string) $info['branch']) ?></span></p>
  <?php endif; ?>
  <p class="muted">该型号的镜像由 OpenWrt 官方 ImageBuilder 在本站实时编译，
     也可回到<a href="/">首页</a>按需定制软件包。</p>
  <a class="btn btn-primary" href="/?target=<?= e(urlencode($target)) ?>&id=<?= e(urlencode($id)) ?>">去定制</a>
</section>
