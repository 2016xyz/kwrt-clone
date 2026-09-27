<?php
/**
 * 「App 下载」与「公众号」弹层 —— 后台「公众号与 App」分组控制。
 *
 * 二维码策略：
 *   · 后台配了图片地址 → 直接用图片（最省事也最可控）
 *   · 只配了下载链接 → 前端用内置的离线二维码生成器画出来，
 *     **不请求任何第三方接口**（默认 entry.qrcode_api 为空）。
 *     若管理员显式填了第三方生成服务，则按配置请求。
 */
use Kwrt\Net;

$appOn    = (bool) setting('entry.app_enabled', false);
$wechatOn = (bool) setting('entry.wechat_enabled', false);
if (!$appOn && !$wechatOn) {
    return;
}
$appName   = (string) setting('entry.app_name', 'Kwrt 助手');
$appDesc   = (string) setting('entry.app_desc', '');
$apk       = (string) setting('entry.app_android_url', '');
$ios       = (string) setting('entry.app_ios_url', '');
$appQr     = (string) setting('entry.app_qrcode_url', '');
$wxName    = (string) setting('entry.wechat_official_name', '');
$wxQr      = (string) setting('entry.wechat_qrcode_url', '');
$wxHint    = (string) setting('entry.wechat_follow_hint', '');
// entry.wechat_appid：schema 里 hint 写着「仅用于展示/后续扩展」，
// 但四处（含 Python 版）都从未渲染 → 后台填了前台看不见。
// 公众号 AppID 是用户在微信里能搜到该号的唯一标识，比二维码更耐用（二维码会过期）。
$wxAppId   = trim((string) setting('entry.wechat_appid', ''));
$qrApi     = trim((string) setting('entry.qrcode_api', ''));
?>
<?php if ($appOn): ?>
<div class="modal" id="appModal" hidden>
  <div class="modal-mask" data-close></div>
  <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="appModalT">
    <button class="modal-x" type="button" data-close aria-label="关闭">×</button>
    <h3 id="appModalT"><?= e($appName) ?></h3>
    <?php if ($appDesc !== ''): ?><p class="muted"><?= e($appDesc) ?></p><?php endif; ?>

    <div class="app-grid">
      <div class="app-qr">
        <?php if ($appQr !== ''): ?>
          <img src="<?= e(Net::asset($appQr)) ?>" alt="App 下载二维码" width="168" height="168">
        <?php else: ?>
          <canvas class="qr" width="168" height="168"
                  data-qr="<?= e($apk !== '' ? $apk : $ios) ?>"
                  data-qr-api="<?= e($qrApi) ?>"></canvas>
          <noscript><p class="muted">请开启 JavaScript 以显示二维码</p></noscript>
        <?php endif; ?>
        <p class="muted sm">扫码安装</p>
      </div>
      <div class="app-links">
        <?php if ($apk !== ''): ?>
          <a class="btn btn-primary btn-block" href="<?= e($apk) ?>" rel="noopener">Android 下载 APK</a>
        <?php endif; ?>
        <?php if ($ios !== ''): ?>
          <a class="btn btn-outline btn-block" href="<?= e($ios) ?>" rel="noopener">iPhone / iPad 下载</a>
        <?php endif; ?>
        <?php if ($apk === '' && $ios === ''): ?>
          <p class="muted">管理员尚未配置下载地址。</p>
        <?php endif; ?>
        <p class="muted sm">
          App 本体即本站在 WebView 中的封装，功能与网页版一致；
          也可直接在浏览器里「添加到主屏幕」，效果等同。
        </p>
      </div>
    </div>
  </div>
</div>
<?php endif; ?>

<?php if ($wechatOn): ?>
<div class="modal" id="wechatModal" hidden>
  <div class="modal-mask" data-close></div>
  <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="wxModalT">
    <button class="modal-x" type="button" data-close aria-label="关闭">×</button>
    <h3 id="wxModalT"><?= e($wxName !== '' ? $wxName : '官方公众号') ?></h3>
    <?php if ($wxHint !== ''): ?><p><?= e($wxHint) ?></p><?php endif; ?>
    <div class="wx-qr">
      <?php if ($wxQr !== ''): ?>
        <img src="<?= e(Net::asset($wxQr)) ?>" alt="公众号二维码" width="180" height="180">
      <?php else: ?>
        <p class="muted">管理员尚未配置公众号二维码。</p>
      <?php endif; ?>
      </div>
      <?php if ($wxAppId !== ''): ?>
      <p class="muted sm">公众号 ID：<code><?= e($wxAppId) ?></code></p>
      <?php endif; ?>
      </div>
      </div>
      <?php endif; ?>
