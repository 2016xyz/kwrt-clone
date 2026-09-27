<?php
/** 登录 / 注册 / 验证邮箱。 */
$tab = ($_GET['tab'] ?? 'login') === 'reg' ? 'reg' : 'login';
?>
<section class="auth-wrap">
  <div class="card auth-card">
    <div class="auth-tabs" role="tablist">
      <button type="button" class="tab<?= $tab === 'login' ? ' on' : '' ?>" data-tab="login"
              role="tab" aria-selected="<?= $tab === 'login' ? 'true' : 'false' ?>">登录</button>
      <?php if (!empty($nav['register'])): ?>
      <button type="button" class="tab<?= $tab === 'reg' ? ' on' : '' ?>" data-tab="reg"
              role="tab" aria-selected="<?= $tab === 'reg' ? 'true' : 'false' ?>">注册</button>
      <?php endif; ?>
    </div>

    <form id="loginForm" class="stack"<?= $tab === 'reg' ? ' hidden' : '' ?>>
      <?= csrf_field() ?>
      <div class="field">
        <label for="lu">用户名</label>
        <input id="lu" name="username" class="input" autocomplete="username" required maxlength="32">
      </div>
      <div class="field">
        <label for="lp">密码</label>
        <div class="input-group">
          <input id="lp" name="password" class="input" type="password"
                 autocomplete="current-password" required>
          <button type="button" class="btn btn-outline" data-toggle-pw="lp"
                  aria-label="显示密码">显示</button>
        </div>
      </div>
      <div class="field captcha-field" data-captcha="login" hidden>
        <label for="lc">验证码</label>
        <div class="input-group">
          <input id="lc" name="captcha_code" class="input" autocomplete="off"
                 maxlength="6" placeholder="请输入图中字符">
          <button type="button" class="captcha-btn" data-captcha-refresh="login"
                  aria-label="看不清，换一张"><span class="captcha-svg"></span></button>
        </div>
        <p class="muted sm">看不清？点击图片换一张（不区分大小写）</p>
        <input type="hidden" name="captcha_id" value="">
      </div>
      <button class="btn btn-primary btn-lg btn-block" type="submit">登录</button>
      <p class="muted sm" id="loginMsg"></p>
      <?php /* 找回密码入口：page.reset_enabled 关闭时 /reset/ 已被 Pages 拦掉，
               这里一并隐藏，避免给用户一条点了就 404 的死链 */ ?>
      <?php if (!empty($nav['reset'])): ?>
      <p class="muted sm"><a href="/reset/">忘记密码？</a></p>
      <?php endif; ?>
    </form>

    <?php if (!empty($nav['register'])): ?>
    <form id="regForm" class="stack"<?= $tab === 'reg' ? '' : ' hidden' ?>>
      <?= csrf_field() ?>
      <div class="field">
        <label for="ru">用户名</label>
        <input id="ru" name="username" class="input" autocomplete="username" required
               maxlength="32" pattern="[A-Za-z0-9_\-]{3,32}">
        <p class="muted sm">3–32 位，字母/数字/下划线/短横线</p>
      </div>
      <div class="field">
        <label for="re">邮箱</label>
        <input id="re" name="email" class="input" type="email" autocomplete="email"
               required maxlength="120">
        <p class="muted sm">用于接收构建完成通知与找回密码</p>
      </div>
      <div class="field">
        <label for="rp">密码</label>
        <div class="input-group">
          <input id="rp" name="password" class="input" type="password"
                 autocomplete="new-password" required minlength="8">
          <button type="button" class="btn btn-outline" data-toggle-pw="rp">显示</button>
        </div>
        <p class="muted sm">至少 8 位</p>
      </div>
      <div class="field">
        <label for="rp2">确认密码</label>
        <input id="rp2" name="password2" class="input" type="password"
               autocomplete="new-password" required minlength="8">
      </div>
      <div class="field captcha-field" data-captcha="register" hidden>
        <label for="rc">验证码</label>
        <div class="input-group">
          <input id="rc" name="captcha_code" class="input" autocomplete="off"
                 maxlength="6" placeholder="请输入图中字符">
          <button type="button" class="captcha-btn" data-captcha-refresh="register"
                  aria-label="看不清，换一张"><span class="captcha-svg"></span></button>
        </div>
        <p class="muted sm">看不清？点击图片换一张（不区分大小写）</p>
        <input type="hidden" name="captcha_id" value="">
      </div>
      <button class="btn btn-primary btn-lg btn-block" type="submit">注册</button>
      <p class="muted sm" id="regMsg"></p>
    </form>
    <?php endif; ?>

    <p class="muted sm auth-foot">
      <?php if (!$nav['login'] && !$nav['register']): ?>
        本站已关闭登录与注册。
      <?php else: ?>
        登录即表示同意本站的使用条款。请勿滥用构建资源。
      <?php endif; ?>
    </p>
  </div>
</section>
