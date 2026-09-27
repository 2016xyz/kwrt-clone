<?php /** 插件提议：公开提案 + 列表。 */ ?>
<section class="page-head">
  <h1>插件提议</h1>
  <p class="muted">希望固件里加入某个插件？在这里提出来，管理员会审核采纳。</p>
</section>

<section class="card">
  <h2>提交提议</h2>
  <form id="proposeForm" class="stack">
    <?= csrf_field() ?>
    <div class="field">
      <label for="pName">插件名称</label>
      <input id="pName" name="name" class="input" maxlength="80" required placeholder="如 luci-app-xxx">
    </div>
    <div class="field">
      <label for="pUrl">项目地址</label>
      <input id="pUrl" name="url" class="input" maxlength="300" placeholder="https://github.com/…">
    </div>
    <div class="field">
      <label for="pNote">说明</label>
      <textarea id="pNote" name="note" class="input" maxlength="600" rows="3"
                placeholder="它做什么、为什么需要它"></textarea>
    </div>
    <button class="btn btn-primary" type="submit">提交</button>
    <span class="muted sm" id="proposeHint">同一 IP 限速：5 次 / 10 分钟</span>
  </form>
</section>

<section class="card">
  <h2>已收到的提议 <span class="muted sm">(<?= e((string) count($proposals)) ?>)</span></h2>
  <?php if (!$proposals): ?>
    <p class="muted">还没有提议，来提第一个吧。</p>
  <?php else: ?>
    <ul class="props">
      <?php foreach ($proposals as $r): ?>
        <li>
          <div class="prop-h">
            <strong><?= e($r['name']) ?></strong>
            <span class="tag tag-<?= e($r['status']) ?>"><?= e($r['status']) ?></span>
            <span class="muted sm"><?= e(ago_h((float) $r['created'])) ?></span>
            <?php if (is_admin()): ?>
              <button class="btn btn-xs btn-outline" data-prop-del="<?= (int) $r['id'] ?>">删除</button>
            <?php endif; ?>
          </div>
          <?php if (!empty($r['url'])): ?>
            <a class="sm" href="<?= e($r['url']) ?>" rel="noopener nofollow"><?= e($r['url']) ?></a>
          <?php endif; ?>
          <?php if (!empty($r['note'])): ?><p class="muted sm"><?= e($r['note']) ?></p><?php endif; ?>
          <?php if (!empty($r['reply'])): ?>
            <p class="reply">管理员回复：<?= e($r['reply']) ?></p>
          <?php endif; ?>
        </li>
      <?php endforeach; ?>
    </ul>
  <?php endif; ?>
</section>
