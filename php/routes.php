<?php
/**
 * 路由表。
 *
 * 与 Python 版 app/main.py 的路由逐条对应（90 条）。
 * 命名刻意与 Python 侧保持一致，便于两边对照排查。
 *
 * 返回一个「注册路由」的闭包，由 public/index.php 调用。
 */
declare(strict_types=1);

use Kwrt\Router;
use Kwrt\Controllers\AdminController;
use Kwrt\Controllers\ApiController;
use Kwrt\Controllers\AuthController;
use Kwrt\Controllers\PublicController;

return static function (Router $r): void {

    // ---------------------------------------------------------------- 页面
    $r->get('/',                    [PublicController::class, 'index']);
    $r->get('/packages/',           [PublicController::class, 'packages']);
    $r->get('/newpkg/',             [PublicController::class, 'newpkg']);
    $r->get('/fadian/',             [PublicController::class, 'fadian']);
    $r->get('/contact/',            [PublicController::class, 'contact']);
    $r->get('/firmware/{target:path}', [PublicController::class, 'firmware']);
    $r->get('/login/',              [AuthController::class, 'loginPage']);
    // 少斜杠的别名 —— 站内链接都用 '/login/'，但用户手打 '/login' 很常见，
    // 没有这条就会掉进 404（与 '/admin' + '/admin/' 的既有约定保持一致）。
    $r->get('/login',               [AuthController::class, 'loginPage']);
    $r->get('/verify/',             [AuthController::class, 'verifyPage']);
    // 找回密码：带 ?token= 进来就是「设置新密码」态，不带就是「申请」态。
    // 与 /login/ 一样登记少斜杠别名 —— 用户手打 /reset 很常见。
    $r->get('/reset/',              [AuthController::class, 'resetPage']);
    $r->get('/reset',               [AuthController::class, 'resetPage']);

    // PWA / App（「打包成 App」的 Web 侧支撑）
    $r->get('/manifest.webmanifest', [PublicController::class, 'manifest']);
    $r->get('/service-worker.js',    [PublicController::class, 'serviceWorker']);
    $r->get('/offline.html',         [PublicController::class, 'offline']);
    $r->get('/robots.txt',           [PublicController::class, 'robots']);
    $r->get('/sitemap.xml',          [PublicController::class, 'sitemap']);
    // 历史路径（Python 版有，两版行为需一致）
    $r->get('/index.css',            [PublicController::class, 'legacyCss']);
    $r->get('/index.js',             [PublicController::class, 'legacyJs']);
    $r->get('/form-storage.js',      [PublicController::class, 'legacyFormStorage']);
    $r->get('/healthz',              [ApiController::class, 'healthz']);

    // 站点数据（离线元数据）
    $r->get('/json/v1/{path:path}', [ApiController::class, 'jsonData']);
    $r->get('/langs/{name}',        [ApiController::class, 'lang']);

    // ---------------------------------------------------------------- 鉴权
    $r->post('/api/v1/login',          [AuthController::class, 'loginPost']);
    $r->post('/api/v1/register',       [AuthController::class, 'registerPost']);
    $r->post('/api/v1/logout',         [AuthController::class, 'logoutPost']);
    $r->post('/api/v1/verify_email',   [AuthController::class, 'verifyEmail']);
    $r->post('/api/v1/resend_verify',  [AuthController::class, 'resendVerify']);
    // 找回密码。★ 这三条**不受页面开关管辖**（在 /api/ 前缀下，由各自的鉴权管），
    //   否则关掉 /reset/ 页面会把接口一起关掉，后台都无法自查。
    $r->post('/api/v1/reset_request',  [AuthController::class, 'resetRequest']);
    $r->post('/api/v1/reset_check',    [AuthController::class, 'resetCheck']);
    $r->post('/api/v1/reset_confirm',  [AuthController::class, 'resetConfirm']);
    $r->get('/api/v1/user',            [ApiController::class, 'user']);

    // ---------------------------------------------------------------- 站点信息
    $r->get('/api/v1/site',           [ApiController::class, 'site']);
    $r->get('/api/v1/captcha',        [ApiController::class, 'captcha']);
    $r->get('/api/v1/announcement',    [ApiController::class, 'announcement']);
    $r->get('/api/v1/sponsor/tiers',   [ApiController::class, 'sponsorTiers']);
    $r->get('/api/v1/pay/info',        [ApiController::class, 'payInfo']);

    // ---------------------------------------------------------------- 软件库
    $r->get('/api/v1/packages/catalog', [ApiController::class, 'packagesCatalog']);
    $r->get('/api/v1/packages/archs',   [ApiController::class, 'packagesArchs']);
    $r->get('/api/v1/packages/search',  [ApiController::class, 'packagesSearch']);
    $r->get('/api/v1/proposals',        [ApiController::class, 'proposals']);
    $r->post('/api/v1/propose',         [ApiController::class, 'propose']);
    $r->del('/api/v1/propose/{pid}',    [ApiController::class, 'proposeDelete']);

    // ---------------------------------------------------------------- 构建
    $r->post('/api/v1/build',            [ApiController::class, 'buildSubmit']);
    $r->get('/api/v1/build/{hash_}',     [ApiController::class, 'buildStatus']);
    $r->post('/api/v1/build/{hash_}/files', [ApiController::class, 'buildAttachFiles']);
    $r->post('/api/v1/upload',           [ApiController::class, 'upload']);

    // ---------------------------------------------------------------- 下载
    $r->get('/api/v1/downloads',      [ApiController::class, 'downloads']);
    $r->get('/dl/mirror',             [ApiController::class, 'dlMirror']);
    $r->get('/dl/t/{token}',          [ApiController::class, 'dlToken']);
    $r->get('/dl/{path:path}',        [ApiController::class, 'dlPath']);
    $r->get('/store/{hash_}/',        [ApiController::class, 'storeIndex']);
    $r->get('/store/{hash_}/{name}',  [ApiController::class, 'storeFile']);

    // ---------------------------------------------------------------- 赞助 / 支付
    $r->post('/api/v1/sponsor/claim',   [ApiController::class, 'sponsorClaim']);
    $r->post('/api/v1/sponsor/pay',     [ApiController::class, 'sponsorPay']);
    $r->get('/api/v1/sponsor/orders',   [ApiController::class, 'sponsorOrders']);
    $r->get('/api/v1/sponsor/pay/{out_trade_no}', [ApiController::class, 'sponsorPayStatus']);
    $r->get('/api/v1/sponsor/pay/{out_trade_no}/qr.png', [ApiController::class, 'sponsorPayQr']);
    $r->post('/api/v1/sponsor/pay/{out_trade_no}/cancel', [ApiController::class, 'sponsorPayCancel']);
    $r->get('/api/v1/sponsor/refunds',  [ApiController::class, 'sponsorRefunds']);
    $r->post('/api/v1/sponsor/refund',  [ApiController::class, 'sponsorRefund']);
    $r->post('/api/v1/alipay/notify',   [ApiController::class, 'alipayNotify']);
    // 兼容 Python 版曾用的下划线写法
    $r->post('/api/v1/sponsor',         [ApiController::class, 'sponsorDisabled']);

    // ---------------------------------------------------------------- 管理后台
    $r->get('/admin/', [AdminController::class, 'page']);
    $r->get('/admin',  [AdminController::class, 'page']);

    $r->get('/api/v1/admin/overview',   [AdminController::class, 'overview']);
    // 检查本程序自身更新（GitHub）。GET 因为它是只读操作 —— 只查询更新源，
    // 不改动任何文件、不重启服务；也因此不需要 CSRF。
    $r->get('/api/v1/admin/update/check', [AdminController::class, 'updateCheck']);
    $r->get('/api/v1/admin/users',      [AdminController::class, 'users']);
    $r->post('/api/v1/admin/user',      [AdminController::class, 'userOp']);
    $r->post('/api/v1/admin/user/create', [AdminController::class, 'userCreate']);
    $r->post('/api/v1/admin/user/sponsor', [AdminController::class, 'userSponsor']);
    $r->post('/api/v1/admin/user/verify', [AdminController::class, 'userVerify']);
    $r->post('/api/v1/admin/user/reverify', [AdminController::class, 'userReverify']);
    $r->get('/api/v1/admin/builds',     [AdminController::class, 'builds']);
    $r->post('/api/v1/admin/build',     [AdminController::class, 'buildOp']);
    $r->get('/api/v1/admin/artifacts',  [AdminController::class, 'artifacts']);
    $r->post('/api/v1/admin/artifact',  [AdminController::class, 'artifactOp']);
    $r->post('/api/v1/admin/queue',     [AdminController::class, 'queueOp']);
    $r->get('/api/v1/admin/build_backend', [AdminController::class, 'buildBackend']);
    $r->get('/api/v1/admin/proposals',  [AdminController::class, 'proposals']);
    $r->post('/api/v1/admin/proposal',  [AdminController::class, 'proposalOp']);
    $r->get('/api/v1/admin/logs',       [AdminController::class, 'logs']);
    $r->post('/api/v1/admin/logs/clear', [AdminController::class, 'logsClear']);
    $r->get('/api/v1/admin/bans',       [AdminController::class, 'bans']);
    $r->post('/api/v1/admin/ban',       [AdminController::class, 'banOp']);
    $r->get('/api/v1/admin/settings',   [AdminController::class, 'settings']);
    $r->post('/api/v1/admin/settings',  [AdminController::class, 'settingsPost']);
    $r->get('/api/v1/admin/site',       [AdminController::class, 'siteGet']);
    $r->post('/api/v1/admin/site',      [AdminController::class, 'sitePost']);
    $r->get('/api/v1/admin/tokens',     [AdminController::class, 'tokens']);
    $r->post('/api/v1/admin/tokens',    [AdminController::class, 'tokensOp']);
    $r->get('/api/v1/admin/catalog',    [AdminController::class, 'catalogGet']);
    $r->post('/api/v1/admin/catalog',   [AdminController::class, 'catalogOp']);
    $r->get('/api/v1/admin/refunds',    [AdminController::class, 'refunds']);
    $r->post('/api/v1/admin/refund',    [AdminController::class, 'refundOp']);
    $r->get('/api/v1/admin/pay/info',   [AdminController::class, 'payInfo']);
    $r->post('/api/v1/admin/pay/test',  [AdminController::class, 'payTest']);
    $r->get('/api/v1/admin/pay/orders', [AdminController::class, 'payOrders']);
    $r->post('/api/v1/admin/pay/order', [AdminController::class, 'payOrderOp']);
    $r->get('/api/v1/admin/pay/verify_stats', [AdminController::class, 'payVerifyStats']);
    $r->post('/api/v1/admin/mail/test', [AdminController::class, 'mailTest']);
    $r->get('/api/v1/admin/mail/log',   [AdminController::class, 'mailLog']);
    $r->post('/api/v1/admin/github/test', [AdminController::class, 'githubTest']);
    $r->get('/api/v1/admin/sponsor/claims', [AdminController::class, 'sponsorClaims']);
    $r->post('/api/v1/admin/sponsor/claim', [AdminController::class, 'sponsorClaimOp']);
};
