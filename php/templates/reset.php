<?php
/**
 * 找回密码。
 *
 * 三种初始状态由服务端决定（不是靠前端猜）：
 *   request —— 申请重置（默认）
 *   setpw   —— 带合法 ?token= 进来，直接进入「设置新密码」
 *   bad     —— 带令牌但无效/过期/已用过，明确告知原因
 * POST 成功后的「已发送」「已完成」两态由 JS 就地切换（见 app.js 的 reset 段）。
 */
$step     = $step     ?? 'request';
$token    = $token    ?? '';
$username = $username ?? '';
$bad      = $bad      ?? '';
$ttlHours = $ttlHours ?? 2;
?>
<section class="auth-wrap">
  <div class="card auth-card">

    <?php /* ---------------- 申请重置 ---------------- */ ?>
    <div id="rstRequest"<?= $step === 'request' ? '' : ' hidden' ?>>
      <h1 class="auth-title">找回密码</h1>
      <p class="muted sm">输入你的用户名或注册邮箱，我们会向该账号绑定的邮箱发送一封重置邮件。</p>
      <form id="resetForm" class="stack">
        <?= csrf_field() ?>
        <div class="field">
          <label for="ra">用户名或邮箱</label>
          <input id="ra" name="account" class="input" autocomplete="username" required
                 maxlength="120" placeholder="用户名 或 name@example.com">
        </div>
        <div class="field captcha-field" data-captcha="reset" hidden>
          <label for="rc">验证码</label>
          <div class="input-group">
            <input id="rc" name="captcha_code" class="input" autocomplete="off"
                   maxlength="6" placeholder="请输入图中字符">
            <button type="button" class="captcha-btn" data-captcha-refresh="reset"
                    aria-label="看不清，换一张"><span class="captcha-svg"></span></button>
          </div>
          <p class="muted sm">看不清？点击图片换一张（不区分大小写）</p>
          <input type="hidden" name="captcha_id" value="">
        </div>
        <button class="btn btn-primary btn-lg btn-block" type="submit">发送重置邮件</button>
        <p class="muted sm" id="resetMsg"></p>
        <p class="muted sm">为保护账号，无论该账号是否存在，我们都会返回同样的提示。</p>
        <p class="muted sm">
          想起来了？<a href="/login/">返回登录</a>
        </p>
      </form>
    </div>

    <?php /* ---------------- 已发送 ---------------- */ ?>
    <div id="rstSent" hidden>
      <h1 class="auth-title">重置邮件已发送</h1>
      <p class="muted sm" id="rstSentDetail">如果该账号存在且已绑定邮箱，重置邮件已经发出。</p>
      <p class="muted sm">
        <b>没收到？</b>请检查垃圾邮件目录；确认账号绑定的邮箱是否正确；
        链接 <?= e((string) $ttlHours) ?> 小时内有效，过期后可重新申请。
      </p>
      <p>
        <button type="button" class="btn btn-outline" id="rstAgain">重新申请</button>
        <a class="btn btn-outline" href="/login/">返回登录</a>
      </p>
    </div>

    <?php /* ---------------- 设置新密码 ---------------- */ ?>
    <div id="rstSetpw"<?= $step === 'setpw' ? '' : ' hidden' ?>>
      <h1 class="auth-title">设置新密码</h1>
      <p class="muted sm">
        正在为账号 <b><?= e($username) ?></b> 设置新密码。
        设置成功后，该账号在其它设备上的登录状态会被全部注销。
      </p>
      <form id="resetConfirmForm" class="stack">
        <?= csrf_field() ?>
        <input type="hidden" name="token" value="<?= e($token) ?>">
        <div class="field">
          <label for="rp1">新密码</label>
          <div class="input-group">
            <input id="rp1" name="password" class="input" type="password"
                   autocomplete="new-password" required minlength="6" placeholder="至少 6 位">
            <button type="button" class="btn btn-outline" data-toggle-pw="rp1">显示</button>
          </div>
        </div>
        <div class="field">
          <label for="rp2">确认新密码</label>
          <input id="rp2" name="password2" class="input" type="password"
                 autocomplete="new-password" required minlength="6" placeholder="再输一次">
        </div>
        <button class="btn btn-primary btn-lg btn-block" type="submit">设置新密码并登录</button>
        <p class="muted sm" id="resetConfirmMsg"></p>
      </form>
    </div>

    <?php /* ---------------- 链接无效 / 过期 ---------------- */ ?>
    <div id="rstBad"<?= $step === 'bad' ? '' : ' hidden' ?>>
      <h1 class="auth-title">链接无法使用</h1>
      <p class="muted sm" id="rstBadDetail"><?= e($bad !== '' ? $bad : '重置链接无效或已过期。') ?></p>
      <p>
        <button type="button" class="btn btn-primary" id="rstRestart">重新申请重置</button>
        <a class="btn btn-outline" href="/login/">返回登录</a>
      </p>
    </div>

    <?php /* ---------------- 完成 ---------------- */ ?>
    <div id="rstDone" hidden>
      <h1 class="auth-title">密码已重置</h1>
      <p class="muted sm">
        账号 <b id="rstDoneUser"></b> 的新密码已生效，你已自动登录。
      </p>
      <p>
        <a class="btn btn-primary" href="/">开始使用</a>
        <a class="btn btn-outline" href="/login/">前往登录页</a>
      </p>
    </div>

  </div>
</section>
