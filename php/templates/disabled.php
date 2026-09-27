<?php
/** 页面被后台开关关闭时的提示页。 */
$mode = (string) setting('page.disabled_behavior', '404');
?>
<section class="err">
  <h1>暂时下线</h1>
  <p><?= e($message ?? setting('page.disabled_message', '该功能已暂时下线，请稍后再试。')) ?></p>
  <p><a class="btn btn-primary" href="/">返回首页</a>
     <?php if (!empty($nav['contact'])): ?>
       <a class="btn btn-outline" href="/contact/">联系我们</a>
     <?php endif; ?>
  </p>
  <?php if (is_admin()): ?>
    <p class="muted sm">你是管理员：可在「管理后台 → 站点设置 → 页面与入口」重新开启该页面。</p>
  <?php endif; ?>
</section>
