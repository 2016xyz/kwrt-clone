<?php
/** 为爱发电：赞助套餐（配置来自后台 sponsor.tiers）。 */
$u = $user ?? null;
$note = $note ?? '';
$payQr = $payQr ?? '';
// ★ 币种符号必须跟着后台的「货币单位」走（sponsor.currency），
//   与 Python 前端 app.js 用的是同一张表。原先这里硬编码 ¥，
//   管理员选了 USD/EUR/JPY/HKD 也照旧显示 ¥ —— 价格数字对、符号错。
$SYM = ['CNY' => '¥', 'USD' => '$', 'EUR' => '€', 'JPY' => '¥', 'HKD' => 'HK$'];
$sym = $SYM[$currency ?? 'CNY'] ?? '¥';
?>
<section class="page-head">
  <h1>为爱发电</h1>
  <p class="muted">赞助用于覆盖服务器与带宽成本。赞助用户可解锁高级定制项。</p>
</section>

<?php if ($note !== ''): ?>
  <section class="card sponsor-note"><p><?= nl2br(e($note)) ?></p></section>
<?php endif; ?>

<?php if (!$tiers): ?>
  <section class="card"><p class="muted">管理员尚未配置赞助套餐。</p></section>
<?php else: ?>
<section class="tiers">
  <?php foreach ($tiers as $t): ?>
    <div class="tier card">
      <h3><?= e((string) ($t['name'] ?? '')) ?></h3>
      <p class="price"><?= e($sym) ?><?= e(number_format((float) ($t['amount'] ?? 0), 2)) ?></p>
      <p class="muted sm"><?= e((string) ($t['days'] ?? '')) ?> 天</p>
      <?php if (!empty($t['perks'])): ?>
        <ul class="perks">
          <?php foreach ((array) $t['perks'] as $perk): ?><li><?= e((string) $perk) ?></li><?php endforeach; ?>
        </ul>
      <?php endif; ?>
      <?php if ($u): ?>
        <button class="btn btn-primary btn-block" data-buy="<?= e((string) ($t['name'] ?? '')) ?>">
          立即赞助
        </button>
      <?php else: ?>
        <a class="btn btn-outline btn-block" href="/login/">登录后赞助</a>
      <?php endif; ?>
    </div>
  <?php endforeach; ?>
</section>
<?php endif; ?>

<?php if (!empty($orders)): ?>
<section class="card">
  <h2>我的订单</h2>
  <table class="tbl">
    <thead><tr><th>订单号</th><th>套餐</th><th>金额</th><th>状态</th><th>时间</th></tr></thead>
    <tbody>
    <?php foreach ($orders as $o): ?>
      <tr>
        <td class="mono sm"><?= e($o['out_trade_no']) ?></td>
        <td><?= e($o['tier_name']) ?></td>
        <td>¥<?= e(number_format((float) $o['amount'], 2)) ?></td>
        <td><span class="tag tag-<?= e($o['status']) ?>"><?= e($o['status']) ?></span></td>
        <td class="muted sm"><?= e(ts_h((float) $o['created'])) ?></td>
      </tr>
    <?php endforeach; ?>
    </tbody>
  </table>
</section>
<?php endif; ?>

<div class="modal" id="payModal" hidden>
  <div class="modal-mask" data-close></div>
  <div class="modal-box">
    <button class="modal-x" type="button" data-close aria-label="关闭">×</button>
    <h3>扫码支付</h3>
    <div id="payQr" class="pay-qr"></div>
    <p class="mono sm" id="payNo"></p>
    <p class="muted sm" id="payState">等待支付…</p>
  </div>
</div>
