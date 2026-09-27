<?php /** 邮箱验证结果页。 */ ?>
<section class="err">
  <h1><?= !empty($ok) ? '✓' : '✕' ?></h1>
  <p><?= e($message ?? '') ?></p>
  <p>
    <?php if (!empty($ok)): ?>
      <a class="btn btn-primary" href="/">开始定制固件</a>
    <?php else: ?>
      <a class="btn btn-primary" href="/login/">重新登录</a>
      <a class="btn btn-outline" href="/login/?tab=reg">重新注册</a>
    <?php endif; ?>
  </p>
</section>
