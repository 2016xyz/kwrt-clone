<?php /** 联系我们。 */ ?>
<section class="page-head">
  <h1>联系我们</h1>
</section>
<section class="card prose">
  <?php $c = (string) setting('contact_text', ''); ?>
  <?php if ($c !== ''): ?>
    <?= nl2br(e($c)) ?>
  <?php else: ?>
    <p>可通过以下方式联系我们：</p>
    <ul>
      <?php $mail = (string) setting('contact_email', ''); ?>
      <?php if ($mail !== ''): ?><li>邮箱：<a href="mailto:<?= e($mail) ?>"><?= e($mail) ?></a></li><?php endif; ?>
      <?php $qq = (string) setting('contact_qq', ''); ?>
      <?php if ($qq !== ''): ?><li>QQ 群：<?= e($qq) ?></li><?php endif; ?>
      <?php $tg = (string) setting('contact_telegram', ''); ?>
      <?php if ($tg !== ''): ?><li>Telegram：<a href="<?= e($tg) ?>" rel="noopener"><?= e($tg) ?></a></li><?php endif; ?>
    </ul>
    <?php if (trim((string) setting('contact_text', '')) === ''
              && trim((string) setting('contact_email', '')) === ''
              && trim((string) setting('contact_qq', '')) === ''): ?>
      <p class="muted">管理员尚未配置联系方式，可在后台「页脚与联系」中填写。</p>
    <?php endif; ?>
  <?php endif; ?>
</section>
