<?php /** 500。生产环境不回显堆栈。 */ ?>
<section class="err">
  <h1>500</h1>
  <p class="muted">服务器内部错误，已经记录到错误日志。</p>
  <?php if (!empty($detail)): ?>
    <pre class="mono err-detail"><?= e($detail) ?></pre>
  <?php endif; ?>
  <p><a class="btn btn-primary" href="/">返回首页</a></p>
</section>
