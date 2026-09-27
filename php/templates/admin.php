<?php
/**
 * 管理后台主模板：15 个模块。数据全部由控制器预先装配（服务端渲染）。
 * 写操作交给 admin.js 走 JSON 接口，页面本身刷新即可看到结果。
 */
use Kwrt\Net;

$ov = $overview ?? [];
$t = $tab ?? 'ov';
?>
<h1><?= e($tabs[$t] ?? '总览') ?></h1>

<?php if ($t === 'ov'): ?>
<section class="kpis">
  <?php
  $cards = [
      ['用户', (string) ($ov['users'] ?? 0), '其中管理员 ' . (int) ($ov['admins'] ?? 0)
          . ' · 赞助 ' . (int) ($ov['sponsors'] ?? 0)],
      ['构建任务', (string) ($ov['builds'] ?? 0), '今日 ' . (int) ($ov['builds_today'] ?? 0)],
      ['队列', ($ov['jobs_running'] ?? 0) . ' 运行 / ' . ($ov['jobs_queued'] ?? 0) . ' 排队',
          '并发上限 ' . (int) ($ov['concurrency'] ?? 1) . ' · ' . (string) ($ov['backend'] ?? '')],
      ['磁盘', bytes_h((float) ($ov['disk_free'] ?? 0)), '产物占用 ' . bytes_h((float) ($ov['store_bytes'] ?? 0))],
      ['订单', (string) ($ov['paid'] ?? 0) . ' / ' . (string) ($ov['orders'] ?? 0),
          '已收 ¥' . number_format((float) ($ov['paid_amount'] ?? 0), 2)],
      ['退款待审', (string) ($ov['refunds_pending'] ?? 0), '下载令牌有效 ' . (int) ($ov['tokens_active'] ?? 0)],
      ['插件提议待审', (string) ($ov['proposals_pending'] ?? 0), '封禁 ' . (int) ($ov['bans'] ?? 0) . ' 条'],
      ['邮件通知', ($ov['mail'] ?? false) ? '已开启' : '未开启', '构建引擎 '
          . (($ov['builder_enabled'] ?? false) ? '运行中' : '已暂停')],
  ];
  foreach ($cards as [$label, $value, $sub]): ?>
    <div class="card kpi">
      <div class="kpi-l muted sm"><?= e($label) ?></div>
      <div class="kpi-v"><?= e($value) ?></div>
      <div class="kpi-s muted sm"><?= e($sub) ?></div>
    </div>
  <?php endforeach; ?>
</section>

<section class="card">
  <h2>系统与部署现状</h2>
  <p class="muted sm">这一块直接决定验证邮件链接、下载链接与支付回调地址是否正确。</p>
  <table class="tbl">
    <tbody>
      <?php /* 系统版本：源是仓库根的 VERSION 文件（Version::display() 已在至少 3 处用过，
               此处复用同一个真源，绝不另写常量）。出问题第一时间要确认的就是「跑的是哪一版」。 */ ?>
      <tr><th>系统版本</th><td>
        <span class="tag mono"><?= e(\Kwrt\Version::display()) ?></span>
        <?php /* 检查更新（GitHub）：只查询更新源，不落地任何文件、不重启服务。
                 未配置更新源仓库时按钮禁用，原因写在 title 上，不让管理员猜。
                 结果由 php/public/assets/js/admin.js 里的 #checkUpdateBtn 处理。 */ ?>
        <?php $__updRepo = \Kwrt\Update::repo(); ?>
        <button type="button" class="btn btn-xs btn-outline" id="checkUpdateBtn"
                data-url="/api/v1/admin/update/check"
                data-cache="<?= (int) \Kwrt\Update::cacheMinutes() ?>"
                <?= $__updRepo === '' ? 'disabled' : '' ?>
                title="<?= $__updRepo === ''
                    ? '未配置更新源仓库：站点设置 → 程序更新'
                    : '更新源：' . e($__updRepo) ?>">检查更新</button>
        <button type="button" class="btn btn-xs btn-ghost" id="checkUpdateForce" hidden
                title="忽略缓存，强制回源查询">强制</button>
        <span class="sm muted" id="updateMsg"></span>
        <div id="updateBox" hidden></div>
      </td></tr>
      <tr><th>绑定域名</th><td class="mono"><?= e((string) setting('site.domain', '') ?: '（未绑定，用请求 Host 兜底）') ?></td></tr>
      <tr><th>对外基址</th><td class="mono"><?= e(Net::baseUrl()) ?></td></tr>
      <tr><th>CDN</th><td><?= Net::cdnEnabled() ? '已启用 · <span class="mono">' . e(Net::cdnBase()) . '</span>' : '未启用' ?></td></tr>
      <tr><th>当前请求源 IP</th><td class="mono"><?= e(Net::clientIp()) ?> <span class="muted sm">(直连对端 <?= e(Net::peerIp()) ?>)</span></td></tr>
      <tr><th>可信代理网段</th><td class="mono sm"><?= e(implode(', ', Net::trustedProxies())) ?></td></tr>
    </tbody>
  </table>
</section>

<?php elseif ($t === 'users'): ?>
<section class="card">
  <h2>新建用户</h2>
  <form class="inline-form" data-admin="userCreate">
    <?= csrf_field() ?>
    <input class="input" name="username" placeholder="用户名" required>
    <input class="input" name="password" type="password" placeholder="密码（≥8 位）" required>
    <input class="input" name="email" type="email" placeholder="邮箱（可选）">
    <select class="input" name="role"><option value="user">普通用户</option><option value="admin">管理员</option></select>
    <button class="btn btn-primary" type="submit">创建</button>
  </form>
</section>
<section class="card">
  <h2>用户列表 <span class="muted sm">(<?= e((string) count($users)) ?>)</span></h2>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr><th>用户</th><th>角色</th><th>赞助</th><th>配额</th><th>构建</th><th>邮箱</th><th>最后登录</th><th>操作</th></tr></thead>
      <tbody>
      <?php foreach ($users as $r): ?>
        <tr>
          <td><strong><?= e($r['username']) ?></strong><?= (int) $r['disabled'] === 1 ? ' <span class="tag tag-disabled">停用</span>' : '' ?></td>
          <td><span class="tag <?= $r['is_admin'] ? 'tag-approved' : '' ?>"><?= e($r['role']) ?></span></td>
          <td>
            <?php if (!empty($r['sponsor_active'])): ?>
              <span class="tag tag-approved">有效</span>
              <span class="muted sm"><?= e(ts_h((float) ($r['sponsor_until'] ?? 0), 'Y/m/d')) ?></span>
            <?php else: ?><span class="muted">—</span><?php endif; ?>
          </td>
          <td><?= (int) $r['quota'] ?></td>
          <td><?= (int) $r['builds'] ?></td>
          <td class="sm"><?= (int) ($r['email_verified'] ?? 1) === 1 ? '' : '<span class="tag tag-pending">未验证</span> ' ?><?= e((string) ($r['email'] ?? '')) ?></td>
          <td class="muted sm"><?= e(ago_h((float) ($r['last_login'] ?? 0))) ?></td>
          <td class="nowrap">
            <button class="btn btn-xs btn-outline" data-user="set_role" data-username="<?= e($r['username']) ?>"
                    data-role="<?= $r['is_admin'] ? 'user' : 'admin' ?>"><?= $r['is_admin'] ? '降为用户' : '升为管理员' ?></button>
            <button class="btn btn-xs btn-outline" data-user="set_sponsor" data-username="<?= e($r['username']) ?>"
                    data-value="<?= !empty($r['sponsor_active']) ? '0' : '1' ?>"><?= !empty($r['sponsor_active']) ? '取消赞助' : '设为赞助' ?></button>
            <button class="btn btn-xs btn-outline" data-user="kick" data-username="<?= e($r['username']) ?>">踢下线</button>
            <button class="btn btn-xs btn-outline" data-user="<?= (int) $r['disabled'] === 1 ? 'enable' : 'disable' ?>"
                    data-username="<?= e($r['username']) ?>"><?= (int) $r['disabled'] === 1 ? '启用' : '停用' ?></button>
            <button class="btn btn-xs btn-outline" data-user="reset_password" data-username="<?= e($r['username']) ?>">改密</button>
            <button class="btn btn-xs btn-outline" data-user="delete" data-username="<?= e($r['username']) ?>">删除</button>
            <button class="btn btn-xs btn-primary" data-user="set_quota" data-username="<?= e($r['username']) ?>"
                    data-quota="<?= (int) $r['quota'] ?>">配额</button>
          </td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
</section>

<?php elseif ($t === 'builds'): ?>
<section class="card">
  <h2>构建任务 <span class="muted sm">(<?= e((string) count($builds)) ?>)</span></h2>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr><th>任务</th><th>属主</th><th>目标</th><th>状态</th><th>大小</th><th>时间</th><th>操作</th></tr></thead>
      <tbody>
      <?php foreach ($builds as $b): ?>
        <tr>
          <td class="mono sm"><?= e($b['request_hash']) ?></td>
          <td><?= e((string) $b['username']) ?></td>
          <td class="sm"><?= e($b['target']) ?>/<?= e($b['profile']) ?></td>
          <td><span class="tag tag-<?= e((string) $b['live_status']) ?>"><?= e((string) $b['live_status']) ?></span></td>
          <td><?= e(bytes_h((float) $b['size'])) ?></td>
          <td class="muted sm"><?= e(ts_h((float) $b['created'])) ?></td>
          <td class="nowrap">
            <?php if (!empty($b['has_artifacts'])): ?>
              <a class="btn btn-xs btn-outline" href="/store/<?= e($b['request_hash']) ?>/" target="_blank" rel="noopener">产物</a>
            <?php endif; ?>
            <button class="btn btn-xs btn-outline" data-build="retry" data-hash="<?= e($b['request_hash']) ?>">重试</button>
            <button class="btn btn-xs btn-outline" data-build="cancel" data-hash="<?= e($b['request_hash']) ?>">取消</button>
            <button class="btn btn-xs btn-outline" data-build="resend" data-hash="<?= e($b['request_hash']) ?>">重发链接</button>
            <button class="btn btn-xs btn-outline" data-build="delete" data-hash="<?= e($b['request_hash']) ?>">删除</button>
          </td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
</section>

<?php elseif ($t === 'queue'): ?>
<section class="kpis">
  <div class="card kpi"><div class="kpi-l muted sm">运行中</div><div class="kpi-v"><?= (int) ($queue['running'] ?? 0) ?></div></div>
  <div class="card kpi"><div class="kpi-l muted sm">排队中</div><div class="kpi-v"><?= (int) ($queue['queued'] ?? 0) ?></div></div>
  <div class="card kpi"><div class="kpi-l muted sm">并发上限</div><div class="kpi-v"><?= (int) ($queue['max'] ?? 1) ?></div></div>
  <div class="card kpi"><div class="kpi-l muted sm">磁盘可用</div><div class="kpi-v"><?= e(bytes_h((float) ($queue['disk_free'] ?? 0))) ?></div></div>
</section>
<section class="card">
  <h2>队列与存储</h2>
  <div class="btn-row">
    <button class="btn btn-outline" data-queue="pump">立即调度一次</button>
    <button class="btn btn-outline" data-queue="clear_finished">清空已结束任务</button>
    <button class="btn btn-outline" data-queue="toggle_builder" data-value="<?= ($queue['enabled'] ?? false) ? '0' : '1' ?>">
      <?= ($queue['enabled'] ?? false) ? '暂停构建引擎' : '启动构建引擎' ?>
    </button>
    <button class="btn btn-outline" data-queue="set_concurrency">调整并发</button>
    <button class="btn btn-outline" data-queue="set_backend" data-value="<?= (string) ($queue['backend'] ?? 'local') === 'local' ? 'github' : 'local' ?>">
      切到 <?= (string) ($queue['backend'] ?? 'local') === 'local' ? 'GitHub' : '本机' ?> 后端
    </button>
    <button class="btn btn-outline" data-queue="purge_store">清空构建产物</button>
  </div>
</section>

<section class="card">
  <h2>本地构建物</h2>
  <p class="muted sm">
    磁盘上实际存在的产物目录，按占用从大到小排列。
    其中 <strong><?= (int) ($artifacts['orphan_count'] ?? 0) ?></strong> 个是孤儿（构建记录已不存在）。
  </p>
  <div class="kpis">
    <div class="card kpi"><div class="kpi-l muted sm">产物合计</div>
      <div class="kpi-v"><?= e(bytes_h((float) ($artifacts['total_bytes'] ?? 0))) ?></div>
      <div class="kpi-s muted sm"><?= (int) ($artifacts['total_files'] ?? 0) ?> 个文件</div></div>
    <div class="card kpi"><div class="kpi-l muted sm">其中孤儿占用</div>
      <div class="kpi-v"><?= e(bytes_h((float) ($artifacts['orphan_bytes'] ?? 0))) ?></div></div>
    <div class="card kpi"><div class="kpi-l muted sm">用户上传</div>
      <div class="kpi-v"><?= e(bytes_h((float) ($artifacts['uploads']['bytes'] ?? 0))) ?></div>
      <div class="kpi-s muted sm"><?= (int) ($artifacts['uploads']['files'] ?? 0) ?> 个文件（不参与清理）</div></div>
  </div>
  <div class="btn-row">
    <button class="btn btn-outline" id="artRefresh">刷新</button>
    <button class="btn btn-outline" data-art="delete_orphans">清理孤儿目录</button>
  </div>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr><th>任务</th><th>属主</th><th>设备</th><th>大小</th><th>文件</th><th>时间</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>
      <?php foreach (($artifacts['items'] ?? []) as $a): ?>
        <tr>
          <td class="mono sm"><?= e($a['request_hash']) ?></td>
          <td><?= e($a['username'] ?: '—') ?></td>
          <td class="sm"><?= $a['target'] ? e($a['target']) . ' / ' . e($a['profile']) : '—' ?></td>
          <td><?= e(bytes_h((float) $a['bytes'])) ?></td>
          <td><?= (int) $a['files'] ?></td>
          <td class="muted sm"><?= e(ts_h((float) $a['mtime'])) ?></td>
          <td><?= $a['orphan'] ? '<span class="tag tag-disabled">孤儿</span>' : '<span class="tag tag-approved">' . e((string) $a['status']) . '</span>' ?></td>
          <td class="nowrap">
            <?php if (!$a['orphan']): ?>
              <a class="btn btn-xs btn-outline" href="/store/<?= e($a['request_hash']) ?>/" target="_blank" rel="noopener">产物</a>
            <?php endif; ?>
            <button class="btn btn-xs btn-outline" data-art="delete" data-hash="<?= e($a['request_hash']) ?>">删除产物</button>
            <button class="btn btn-xs btn-outline" data-art="delete_record" data-hash="<?= e($a['request_hash']) ?>">连记录删除</button>
          </td>
        </tr>
      <?php endforeach; ?>
      <?php if (empty($artifacts['items'])): ?>
        <tr><td colspan="8" class="muted">本地没有构建产物。</td></tr>
      <?php endif; ?>
      </tbody>
    </table>
  </div>
</section>

<?php elseif ($t === 'site'): ?>
<?php foreach ($groups as $g): ?>
<section class="card">
  <h2><?= e($g['label']) ?> <span class="muted sm"><?= e($g['hint']) ?></span></h2>
  <form class="settings-form" data-group="<?= e($g['k']) ?>">
    <?= csrf_field() ?>
    <?php foreach ($g['items'] as $it): ?>
      <div class="field">
        <label for="s_<?= e(str_replace('.', '_', $it['k'])) ?>">
          <?= e($it['label']) ?>
          <?php if (!empty($it['secret'])): ?><span class="tag">密钥</span><?php endif; ?>
        </label>
        <?php if ($it['t'] === 'bool'): ?>
          <label class="chk">
            <input type="checkbox" name="<?= e($it['k']) ?>" value="1"
                   <?= $it['value'] ? 'checked' : '' ?>>
            <span class="muted sm">启用</span>
          </label>
        <?php elseif ($it['t'] === 'select'): ?>
          <select class="input" name="<?= e($it['k']) ?>">
            <?php foreach (($it['opts'] ?? []) as $o): ?>
              <option value="<?= e($o) ?>"<?= (string) $it['value'] === (string) $o ? ' selected' : '' ?>><?= e($o) ?></option>
            <?php endforeach; ?>
          </select>
        <?php elseif ($it['t'] === 'textarea' || $it['t'] === 'json'): ?>
          <textarea class="input" name="<?= e($it['k']) ?>" rows="4"><?= e((string) $it['value']) ?></textarea>
        <?php elseif ($it['t'] === 'color'): ?>
          <input class="input" type="text" name="<?= e($it['k']) ?>" value="<?= e((string) $it['value']) ?>"
                 pattern="#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})" placeholder="#2563eb">
        <?php elseif ($it['t'] === 'number'): ?>
          <input class="input" type="number" name="<?= e($it['k']) ?>" value="<?= e((string) $it['value']) ?>"
                 <?= isset($it['min']) ? 'min="' . (int) $it['min'] . '"' : '' ?>
                 <?= isset($it['max']) ? 'max="' . (int) $it['max'] . '"' : '' ?>>
        <?php elseif ($it['t'] === 'password'): ?>
          <input class="input" type="password" name="<?= e($it['k']) ?>" value=""
                 placeholder="<?= !empty($it['is_set']) ? '已设置（留空则不修改）' : '未设置' ?>">
        <?php else: ?>
          <input class="input" type="text" name="<?= e($it['k']) ?>" value="<?= e((string) $it['value']) ?>"
                 <?= isset($it['max']) ? 'maxlength="' . (int) $it['max'] . '"' : '' ?>>
        <?php endif; ?>
        <?php if (!empty($it['hint'])): ?><p class="muted sm"><?= e($it['hint']) ?></p><?php endif; ?>
      </div>
    <?php endforeach; ?>
    <button class="btn btn-primary" type="submit">保存本组</button>
    <span class="muted sm save-hint"></span>
  </form>
</section>
<?php endforeach; ?>

<?php elseif ($t === 'mail'): ?>
<section class="card">
  <h2>发送测试邮件</h2>
  <form class="inline-form" data-admin="mailTest">
    <?= csrf_field() ?>
    <input class="input" name="to" type="email" placeholder="收件人邮箱" required>
    <button class="btn btn-primary" type="submit">发送</button>
    <span class="muted sm" data-hint></span>
  </form>
  <p class="muted sm">SMTP 参数在「站点设置 → 邮件通知」里配置。测试发送走真实 SMTP。</p>
</section>
<section class="card">
  <h2>投递记录</h2>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr><th>时间</th><th>用途</th><th>收件人</th><th>用户</th><th>结果</th><th>说明</th></tr></thead>
      <tbody>
      <?php foreach ($mailLog as $l): ?>
        <tr>
          <td class="muted sm"><?= e(ts_h((float) $l['created'])) ?></td>
          <td><?= e($l['purpose']) ?></td>
          <td class="sm"><?= e($l['to_addr']) ?></td>
          <td class="sm"><?= e((string) ($l['username'] ?? '')) ?></td>
          <td><span class="tag <?= (int) $l['ok'] === 1 ? 'tag-approved' : 'tag-failed' ?>"><?= (int) $l['ok'] === 1 ? '成功' : '失败' ?></span></td>
          <td class="muted sm"><?= e((string) ($l['detail'] ?? '')) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
</section>

<?php elseif ($t === 'verify'): ?>
<section class="card">
  <h2>邮箱验证</h2>
  <p class="muted sm">开启邮件验证后，新注册用户需完成验证。管理员可在此代为标记。</p>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr><th>用户</th><th>邮箱</th><th>验证状态</th><th>操作</th></tr></thead>
      <tbody>
      <?php foreach ($users as $r): ?>
        <tr>
          <td><?= e($r['username']) ?></td>
          <td class="sm"><?= e((string) ($r['email'] ?? '')) ?></td>
          <td><span class="tag <?= (int) ($r['email_verified'] ?? 1) === 1 ? 'tag-approved' : 'tag-pending' ?>">
            <?= (int) ($r['email_verified'] ?? 1) === 1 ? '已验证' : '未验证' ?></span></td>
          <td class="nowrap">
            <button class="btn btn-xs btn-outline" data-user="userVerify" data-username="<?= e($r['username']) ?>">标记已验证</button>
            <button class="btn btn-xs btn-outline" data-user="userReverify" data-username="<?= e($r['username']) ?>">重置为未验证</button>
          </td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
</section>

<?php elseif ($t === 'tokens'): ?>
<section class="kpis">
  <div class="card kpi"><div class="kpi-l muted sm">有效</div><div class="kpi-v"><?= (int) ($dlStats['active'] ?? 0) ?></div></div>
  <div class="card kpi"><div class="kpi-l muted sm">已过期</div><div class="kpi-v"><?= (int) ($dlStats['expired'] ?? 0) ?></div></div>
  <div class="card kpi"><div class="kpi-l muted sm">已吊销</div><div class="kpi-v"><?= (int) ($dlStats['revoked'] ?? 0) ?></div></div>
  <div class="card kpi"><div class="kpi-l muted sm">累计命中</div><div class="kpi-v"><?= (int) ($dlStats['hits'] ?? 0) ?></div></div>
</section>
<section class="card">
  <h2>下载令牌</h2>
  <div class="btn-row"><button class="btn btn-outline" data-token="cleanup">清理超过 7 天的过期令牌</button></div>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr><th>令牌</th><th>文件</th><th>属主</th><th>过期</th><th>命中</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>
      <?php foreach ($tokens as $k): ?>
        <tr>
          <td class="mono sm"><?= e(substr((string) $k['token'], 0, 18)) ?>…</td>
          <td class="sm"><?= e((string) $k['filename']) ?></td>
          <td><?= e((string) $k['username']) ?></td>
          <td class="sm"><?= e(ts_h((float) $k['expires'], 'Y/m/d H:i')) ?></td>
          <td><?= (int) $k['hits'] ?><?= (int) $k['max_hits'] > 0 ? ' / ' . (int) $k['max_hits'] : '' ?></td>
          <td>
            <?php if ((int) $k['revoked'] === 1): ?><span class="tag tag-failed">已吊销</span>
            <?php elseif ((float) $k['expires'] < time()): ?><span class="tag tag-expired">已过期</span>
            <?php else: ?><span class="tag tag-approved">有效</span><?php endif; ?>
          </td>
          <td class="nowrap">
            <button class="btn btn-xs btn-outline" data-token="revoke" data-token-id="<?= e($k['token']) ?>">吊销</button>
            <button class="btn btn-xs btn-outline" data-token="extend" data-token-id="<?= e($k['token']) ?>">续期 72h</button>
          </td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
</section>

<?php elseif ($t === 'sponsor'): ?>
<section class="card">
  <h2>赞助申请</h2>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr><th>用户</th><th>套餐</th><th>金额</th><th>状态</th><th>时间</th><th>操作</th></tr></thead>
      <tbody>
      <?php foreach ($claims as $c): ?>
        <tr>
          <td><?= e($c['username']) ?></td>
          <td><?= e($c['tier']) ?></td>
          <td>¥<?= e(number_format((float) $c['amount'], 2)) ?></td>
          <td><span class="tag tag-<?= e($c['status']) ?>"><?= e($c['status']) ?></span></td>
          <td class="muted sm"><?= e(ts_h((float) $c['created'])) ?></td>
          <td class="nowrap">
            <button class="btn btn-xs btn-primary" data-claim="approve" data-id="<?= (int) $c['id'] ?>">通过</button>
            <button class="btn btn-xs btn-outline" data-claim="reject" data-id="<?= (int) $c['id'] ?>">拒绝</button>
          </td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
</section>
<section class="card">
  <h2>发放赞助</h2>
  <form class="inline-form" data-admin="userSponsor">
    <?= csrf_field() ?>
    <input class="input" name="username" placeholder="用户名" required>
    <input class="input" name="days" type="number" value="30" min="1" max="3650">
    <input class="input" name="tier" placeholder="套餐名（可自定义）">
    <button class="btn btn-primary" type="submit">发放</button>
  </form>
</section>

<?php elseif ($t === 'pay'): ?>
<section class="card">
  <h2>支付状态</h2>
  <table class="tbl">
    <tbody>
      <tr><th>是否可用</th><td><?= $payEnabled ? '<span class="tag tag-approved">已启用</span>' : '<span class="tag tag-pending">未启用</span>' ?></td></tr>
      <tr><th>AppID</th><td class="mono"><?= e((string) setting('pay.alipay_app_id', '') ?: '（未配置）') ?></td></tr>
      <tr><th>回调地址</th><td class="mono sm"><?= e($notifyUrl) ?></td></tr>
      <tr><th>沙箱</th><td><?= t_bool('pay.sandbox', false) ? '是' : '否（生产）' ?></td></tr>
      <tr><th>私钥 / 公钥</th><td><?= setting('pay.alipay_private_key', '') !== '' ? '私钥已配置' : '<span class="tag tag-failed">私钥缺失</span>' ?>
        · <?= setting('pay.alipay_public_key', '') !== '' ? '公钥已配置' : '<span class="tag tag-failed">公钥缺失</span>' ?></td></tr>
    </tbody>
  </table>
  <p class="muted sm">
    回调地址必须公网可访问，且要与「域名与 CDN」里绑定的域名一致，否则支付宝回调会打到错误地址。
    <button class="btn btn-xs btn-outline" id="copyNotify" type="button">复制地址</button>
  </p>
  <div class="btn-row">
    <button class="btn btn-outline" data-pay="test">真实调用网关下测试单（0.01 元）</button>
  </div>
</section>
<section class="card">
  <h2>订单</h2>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr><th>订单号</th><th>用户</th><th>套餐</th><th>金额</th><th>状态</th><th>时间</th><th>操作</th></tr></thead>
      <tbody>
      <?php foreach ($orders as $o): ?>
        <tr>
          <td class="mono sm"><?= e($o['out_trade_no']) ?></td>
          <td><?= e($o['username']) ?></td>
          <td><?= e($o['tier_name']) ?></td>
          <td>¥<?= e(number_format((float) $o['amount'], 2)) ?></td>
          <td><span class="tag tag-<?= e($o['status']) ?>"><?= e($o['status']) ?></span></td>
          <td class="muted sm"><?= e(ts_h((float) $o['created'])) ?></td>
          <td class="nowrap">
            <button class="btn btn-xs btn-outline" data-order="query" data-no="<?= e($o['out_trade_no']) ?>">查单</button>
            <?php if ($o['status'] === 'paid_pending'): ?>
              <button class="btn btn-xs btn-outline" data-order="mark_paid" data-no="<?= e($o['out_trade_no']) ?>">确认发放权益</button>
            <?php endif; ?>
          </td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
</section>

<?php elseif ($t === 'refund'): ?>
<section class="card">
  <h2>退款审核</h2>
  <p class="muted sm">
    通过时会**真实调用支付宝退款**；网关失败则退回待审，绝不标记成功。
    确实已线下退款的，勾选「线下」直接撤销权益。
  </p>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr><th>订单号</th><th>用户</th><th>金额</th><th>原因</th><th>状态</th><th>时间</th><th>操作</th></tr></thead>
      <tbody>
      <?php foreach ($refunds as $r): ?>
        <tr>
          <td class="mono sm"><?= e($r['out_trade_no']) ?></td>
          <td><?= e($r['username']) ?></td>
          <td>¥<?= e(number_format((float) $r['amount'], 2)) ?></td>
          <td class="sm"><?= e((string) $r['reason']) ?></td>
          <td><span class="tag tag-<?= e($r['status']) ?>"><?= e($r['status']) ?></span></td>
          <td class="muted sm"><?= e(ts_h((float) $r['created'])) ?></td>
          <td class="nowrap">
            <?php if ($r['status'] === 'pending'): ?>
              <button class="btn btn-xs btn-primary" data-refund-op="approve" data-rid="<?= (int) $r['id'] ?>">线上退款</button>
              <button class="btn btn-xs btn-outline" data-refund-op="approve_offline" data-rid="<?= (int) $r['id'] ?>">线下已退</button>
              <button class="btn btn-xs btn-outline" data-refund-op="reject" data-rid="<?= (int) $r['id'] ?>">拒绝</button>
            <?php endif; ?>
          </td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
</section>

<?php elseif ($t === 'catalog'): ?>
<section class="card">
  <h2>插件目录
    <span class="muted sm"><?= (string) count($catalog['presets'] ?? []) ?> 预设 ·
      <?= (string) count($catalog['cats'] ?? []) ?> 分类 · <?= (string) count($catalog['suites'] ?? []) ?> 套件</span></h2>
  <form class="inline-form" data-catalog="add_preset">
    <?= csrf_field() ?>
    <input class="input" name="name" placeholder="包名，如 luci-app-xxx" required>
    <input class="input" name="label" placeholder="显示名">
    <input class="input" name="desc" placeholder="说明">
    <select class="input" name="cat">
      <?php foreach (($catalog['cats'] ?? []) as $c): ?>
        <option value="<?= e($c['k']) ?>"><?= e($c['l']) ?></option>
      <?php endforeach; ?>
      <option value="other">其他</option>
    </select>
    <button class="btn btn-primary" type="submit">新增预设</button>
  </form>
  <div class="btn-row">
    <button class="btn btn-outline" data-catalog="reset">恢复默认目录</button>
  </div>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr><th>包名</th><th>显示名</th><th>分类</th><th>说明</th><th>操作</th></tr></thead>
      <tbody>
      <?php foreach (($catalog['presets'] ?? []) as $p): ?>
        <tr>
          <td class="mono sm"><?= e($p['n']) ?></td>
          <td><?= e((string) ($p['l'] ?? '')) ?></td>
          <td><?= e((string) ($p['c'] ?? '')) ?></td>
          <td class="muted sm"><?= e((string) ($p['d'] ?? '')) ?></td>
          <td><button class="btn btn-xs btn-outline" data-catalog="remove_preset" data-name="<?= e($p['n']) ?>">删除</button></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
</section>

<?php elseif ($t === 'prop'): ?>
<section class="card">
  <h2>插件提议</h2>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr><th>插件</th><th>链接</th><th>说明</th><th>状态</th><th>时间</th><th>操作</th></tr></thead>
      <tbody>
      <?php foreach ($proposals as $r): ?>
        <tr>
          <td><strong><?= e($r['name']) ?></strong></td>
          <td class="sm"><?php if ($r['url']): ?><a href="<?= e($r['url']) ?>" rel="noopener nofollow">链接</a><?php endif; ?></td>
          <td class="muted sm"><?= e((string) $r['note']) ?></td>
          <td><span class="tag tag-<?= e($r['status']) ?>"><?= e($r['status']) ?></span></td>
          <td class="muted sm"><?= e(ts_h((float) $r['created'])) ?></td>
          <td class="nowrap">
            <button class="btn btn-xs btn-primary" data-prop="approve" data-id="<?= (int) $r['id'] ?>">采纳</button>
            <button class="btn btn-xs btn-outline" data-prop="reject" data-id="<?= (int) $r['id'] ?>">拒绝</button>
            <button class="btn btn-xs btn-outline" data-prop="reply" data-id="<?= (int) $r['id'] ?>">回复</button>
          </td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
</section>

<?php elseif ($t === 'bans'): ?>
<section class="card">
  <h2>新增封禁</h2>
  <form class="inline-form" data-admin="banAdd">
    <?= csrf_field() ?>
    <select class="input" name="kind"><option value="ip">IP</option><option value="user">用户名</option></select>
    <input class="input" name="value" placeholder="IP 或用户名" required>
    <input class="input" name="reason" placeholder="原因">
    <button class="btn btn-primary" type="submit">封禁</button>
  </form>
  <p class="muted sm">
    挂 CDN 时封禁依据的是**回源头**（见「总览 → 当前请求源 IP」）。
    若可信代理网段配错，封禁会把 CDN 节点整个封掉。
  </p>
</section>
<section class="card">
  <h2>封禁列表</h2>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr><th>类型</th><th>对象</th><th>原因</th><th>时间</th><th>操作</th></tr></thead>
      <tbody>
      <?php foreach ($bans as $b): ?>
        <tr>
          <td><span class="tag"><?= e($b['kind']) ?></span></td>
          <td class="mono sm"><?= e($b['value']) ?></td>
          <td class="muted sm"><?= e((string) $b['reason']) ?></td>
          <td class="muted sm"><?= e(ts_h((float) $b['created'])) ?></td>
          <td><button class="btn btn-xs btn-outline" data-ban-remove="<?= (int) $b['id'] ?>">解除</button></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
</section>

<?php elseif ($t === 'logs'): ?>
<section class="card">
  <h2>审计日志 <span class="muted sm">(<?= e((string) count($logs)) ?>)</span></h2>
  <div class="btn-row"><button class="btn btn-outline" data-logs="clear">清空日志</button></div>
  <div class="tbl-wrap">
    <table class="tbl">
      <thead><tr><th>时间</th><th>管理员</th><th>操作</th><th>对象</th><th>详情</th><th>IP</th></tr></thead>
      <tbody>
      <?php foreach ($logs as $l): ?>
        <tr>
          <td class="muted sm"><?= e(ts_h((float) $l['created'])) ?></td>
          <td><?= e((string) $l['admin']) ?></td>
          <td class="mono sm"><?= e($l['action']) ?></td>
          <td class="mono sm"><?= e((string) $l['target']) ?></td>
          <td class="muted sm"><?= e(mb_substr((string) $l['detail'], 0, 120)) ?></td>
          <td class="mono sm"><?= e((string) $l['ip']) ?></td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
</section>
<?php endif; ?>
