<?php /** 构建产物页。 */ ?>
<section class="page-head">
  <h1>构建产物</h1>
  <p class="mono muted"><?= e($hash) ?></p>
</section>
<section class="card">
  <?php if (!$files): ?>
    <p class="muted">该构建没有产物，可能已被清理。</p>
  <?php else: ?>
    <div class="tbl-wrap">
      <table class="tbl">
        <thead><tr><th>文件</th><th>大小</th><th>时间</th><th>下载</th></tr></thead>
        <tbody>
        <?php foreach ($files as $f): ?>
          <tr>
            <td class="mono sm"><?= e($f['name']) ?></td>
            <td><?= e(bytes_h((float) $f['size'])) ?></td>
            <td class="muted sm"><?= e(ts_h((float) $f['mtime'])) ?></td>
            <td>
              <?php if (!empty($links[$f['name']])): ?>
                <a class="btn btn-xs btn-outline" href="<?= e($links[$f['name']]) ?>">限时链接</a>
              <?php endif; ?>
              <a class="btn btn-xs btn-ghost" href="/store/<?= e($hash) ?>/<?= e(rawurlencode($f['name'])) ?>">直接下载</a>
            </td>
          </tr>
        <?php endforeach; ?>
        </tbody>
      </table>
    </div>
  <?php endif; ?>
</section>
