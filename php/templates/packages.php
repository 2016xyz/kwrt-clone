<?php
/** 软件包库：服务端渲染的目录 + 搜索表单（无 JS 也能用）。 */
use Kwrt\Net;
$total = count($presets);
?>
<section class="page-head">
  <h1>软件包库</h1>
  <p class="muted">共 <strong><?= e((string) $total) ?></strong> 个常用软件包，按分类浏览。
     提交构建时勾选即可编进固件。</p>
</section>

<form class="filters" method="get" action="/packages/">
  <input class="input" type="search" name="q" value="<?= e($q) ?>" placeholder="按名称或说明筛选…">
  <select class="input" name="arch">
    <option value="">全部架构</option>
    <?php foreach ($archs as $a): ?>
      <option value="<?= e($a) ?>"<?= $a === $arch ? ' selected' : '' ?>><?= e($a) ?></option>
    <?php endforeach; ?>
  </select>
  <button class="btn btn-primary" type="submit">筛选</button>
  <?php if ($q !== '' || $arch !== ''): ?>
    <a class="btn btn-ghost" href="/packages/">重置</a>
  <?php endif; ?>
</form>

<?php
$q = mb_strtolower(trim($q));
foreach ($catalog as $ck => $cat):
    $items = $cat['items'];
    if ($q !== '') {
        $items = array_values(array_filter($items, fn($p) =>
            str_contains(mb_strtolower((string) $p['n']), $q)
            || str_contains(mb_strtolower((string) ($p['l'] ?? '')), $q)
            || str_contains(mb_strtolower((string) ($p['d'] ?? '')), $q)));
    }
    if (!$items) continue;
?>
<section class="card">
  <h2><?= e($cat['label']) ?> <span class="muted sm">(<?= e((string) count($items)) ?>)</span></h2>
  <div class="pkg-grid wide">
    <?php foreach ($items as $p): ?>
      <div class="pkg-row">
        <div>
          <strong><?= e((string) ($p['l'] ?? $p['n'])) ?></strong>
          <span class="mono muted sm"><?= e($p['n']) ?></span>
        </div>
        <p class="muted sm"><?= e((string) ($p['d'] ?? '')) ?></p>
      </div>
    <?php endforeach; ?>
  </div>
</section>
<?php endforeach; ?>
