<?php /** 404。 */ ?>
<section class="err">
  <h1>404</h1>
  <p class="muted">页面不存在或已被移除。</p>
  <?php if (!empty($path)): ?><p class="mono muted sm"><?= e($path) ?></p><?php endif; ?>
  <p><a class="btn btn-primary" href="/">返回首页</a></p>
</section>
