<?php
/**
 * 首页：设备检索 → 定制 → 提交构建。
 *
 * 控制器传入：
 *   $devices   设备索引（名称 → 目标/型号）
 *   $branches  分支 → 发行版
 *   $catalog   软件包目录（分组后的预设）
 *   $suites    套件
 *   $myOrders  当前用户订单（$orders=1 时）
 */
use Kwrt\Net;

$hero    = (string) setting('hero_title', '');
$sub     = (string) setting('hero_subtitle', '');
$hint    = (string) setting('devices_hint', '输入设备的名称或型号');
$custT   = (string) setting('customize_title', '');
$u       = $user ?? null;
$sponsor = is_sponsor();
$defVer  = (string) ($defaultVersion ?? '');
?>
<section class="hero">
  <h1><?= e($hero) ?></h1>
  <?php if ($sub !== ''): ?><p class="hero-sub"><?= e($sub) ?></p><?php endif; ?>

  <div class="search" role="search">
    <label class="sr" for="devQ"><?= e($hint) ?></label>
    <input id="devQ" class="input input-lg" type="text" autocomplete="off"
           placeholder="<?= e($hint) ?>" aria-describedby="devHelp">
    <button class="btn btn-primary btn-lg" id="devGo" type="button">检索</button>
    <div id="devSug" class="sug" role="listbox" hidden></div>
  </div>
  <p id="devHelp" class="muted sm">
    共 <strong><?= e((string) count($devices)) ?></strong> 个可用目标 ·
    输入关键字后可用 ↑↓ 选择、回车确认
  </p>
</section>

<?php if (!empty($orders) && $u): ?>
<section class="card" id="orders">
  <h2>我的订单</h2>
  <?php if (!$myOrders): ?>
    <p class="muted">还没有订单。</p>
  <?php else: ?>
    <table class="tbl">
      <thead><tr><th>订单号</th><th>套餐</th><th>金额</th><th>状态</th><th>时间</th><th>操作</th></tr></thead>
      <tbody>
      <?php foreach ($myOrders as $o): ?>
        <tr>
          <td class="mono sm"><?= e($o['out_trade_no']) ?></td>
          <td><?= e($o['tier_name']) ?></td>
          <td>¥<?= e(number_format((float) $o['amount'], 2)) ?></td>
          <td><span class="tag tag-<?= e($o['status']) ?>"><?= e($o['status']) ?></span></td>
          <td class="muted sm"><?= e(ts_h((float) $o['created'])) ?></td>
          <td>
            <?php if (in_array($o['status'], \Kwrt\Pay::PAID_STATES, true)): ?>
              <button class="btn btn-xs btn-outline" data-refund="<?= e($o['out_trade_no']) ?>">申请退款</button>
            <?php endif; ?>
          </td>
        </tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  <?php endif; ?>
</section>
<?php endif; ?>

<section class="card build-card">
  <h2><?= e($custT) ?></h2>

  <form id="buildForm" class="build-form" novalidate>
    <?= csrf_field() ?>
    <div class="grid-2">
      <div class="field">
        <label for="fTarget">目标设备（target）</label>
        <input id="fTarget" name="target" class="input" required placeholder="x86/64"
               value="<?= e($preTarget ?? 'x86/64') ?>">
        <p class="muted sm">从上方检索结果点选会自动填入</p>
      </div>
      <div class="field">
        <label for="fProfile">型号标识（profile）</label>
        <input id="fProfile" name="profile" class="input" required placeholder="generic"
               value="<?= e($preProfile ?? 'generic') ?>">
      </div>
    </div>

    <div class="grid-3">
      <div class="field">
        <label for="fVersion">固件分支</label>
        <select id="fVersion" name="version" class="input">
          <?php foreach ($branches as $b => $v): ?>
            <option value="<?= e((string) $b) ?>"<?= ((string) $b === $defVer) ? ' selected' : '' ?>>
              <?= e((string) $b) ?><?= $v !== '' ? ' (' . e((string) $v) . ')' : '' ?>
            </option>
          <?php endforeach; ?>
        </select>
      </div>
      <div class="field">
        <label for="fFs">文件系统</label>
        <select id="fFs" name="filesystem" class="input">
          <option value="squashfs">squashfs（可恢复出厂）</option>
          <option value="ext4">ext4（可写、可扩容）</option>
        </select>
      </div>
      <div class="field">
        <label for="fRoot">根分区容量 (MB)</label>
        <input id="fRoot" name="rootfs_size_mb" class="input" type="number"
               min="128" max="4096" step="1" value="512">
        <?php if (!$sponsor): ?>
          <p class="muted sm">赞助用户可调更大容量</p>
        <?php endif; ?>
      </div>
    </div>

    <div class="field">
      <label>预置软件包
        <span class="muted sm">（勾选即编进固件；共 <?= e((string) count($presets)) ?> 个预设）</span>
      </label>
      <div class="pkg-tools">
        <?php foreach ($suites as $s): ?>
          <button type="button" class="chip chip-suite" data-suite="<?= e($s['k']) ?>"
                  title="<?= e(implode(' ', $s['pkgs'] ?? [])) ?>"><?= e($s['l']) ?></button>
        <?php endforeach; ?>
        <input id="pkgFilter" class="input input-sm" type="search" placeholder="筛选软件包…">
      </div>
      <div class="pkg-cats" id="pkgCats">
        <?php foreach ($catalog as $ck => $cat): ?>
          <fieldset class="pkg-cat" data-cat="<?= e($ck) ?>">
            <legend><?= e($cat['label']) ?></legend>
            <div class="pkg-grid">
              <?php foreach ($cat['items'] as $p): ?>
                <label class="pkg-item" title="<?= e((string) ($p['d'] ?? '')) ?>">
                  <input type="checkbox" name="packages[]" value="<?= e($p['n']) ?>">
                  <span class="pkg-l"><?= e((string) ($p['l'] ?? $p['n'])) ?></span>
                  <span class="pkg-n mono"><?= e($p['n']) ?></span>
                </label>
              <?php endforeach; ?>
            </div>
          </fieldset>
        <?php endforeach; ?>
      </div>
    </div>

    <div class="field">
      <label for="fExtra">额外软件包 / 移除
        <span class="muted sm">空格分隔；前缀 - 表示移除</span>
      </label>
      <input id="fExtra" name="extra" class="input" placeholder="如 luci-app-ttyd -luci-app-ddns">
    </div>

    <div class="grid-2">
      <div class="field">
        <label for="fHost">主机名</label>
        <input id="fHost" name="hostname" class="input" placeholder="Kwrt">
      </div>
      <div class="field">
        <label for="fLan">LAN IP</label>
        <input id="fLan" name="lan_ip" class="input" placeholder="192.168.1.1">
      </div>
    </div>

    <details class="adv">
      <summary>高级选项<?= $sponsor ? '' : '（部分仅赞助用户可用）' ?></summary>
      <div class="grid-3">
        <label class="chk"><input type="checkbox" name="ipv6" value="1"> 启用 IPv6</label>
        <label class="chk"><input type="checkbox" name="dhcp" value="1" checked> 启用 DHCP</label>
        <label class="chk" <?= $sponsor ? '' : 'data-need-sponsor' ?>>
          <input type="checkbox" name="eflasher" value="1"> 包含 eflasher 镜像
        </label>
        <label class="chk"><input type="checkbox" name="usb_net" value="1"> USB 网卡支持</label>
        <label class="chk" <?= $sponsor ? '' : 'data-need-sponsor' ?>>
          <input type="checkbox" name="usb_wireless" value="1"> USB 无线网卡
        </label>
        <label class="chk"><input type="checkbox" name="wanlan" value="1"> WAN/LAN 互换</label>
      </div>
      <div class="field">
        <label for="fFiles">自定义文件包（tar.gz，解压进 rootfs）</label>
        <input id="fFiles" type="file" class="input" accept=".tar.gz,.tgz,.zip,.7z"
               <?= $sponsor ? '' : 'disabled' ?>>
        <p class="muted sm" id="filesHint">
          <?= $sponsor ? '上传后会随构建一起注入' : '仅赞助用户可上传；可先提交构建，再单独上传' ?>
        </p>
      </div>
    </details>

    <div class="build-act">
      <button class="btn btn-primary btn-lg" type="submit" id="buildBtn">
        <?= $u ? '开始构建' : '登录后构建' ?>
      </button>
      <span class="muted sm" id="buildHint"></span>
    </div>
  </form>

  <div id="buildProg" class="prog" hidden>
    <div class="prog-bar"><i id="progBar"></i></div>
    <p id="progText" class="mono sm"></p>
    <div id="progFiles" class="prog-files"></div>
  </div>
</section>

<?php if (!empty($nav['packages'])): ?>
<section class="cta">
  <a class="btn btn-outline" href="/packages/">浏览全部软件包 →</a>
</section>
<?php endif; ?>
