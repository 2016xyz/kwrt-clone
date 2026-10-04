<?php
/**
 * 公开页面控制器。
 *
 * 所有页面都是**服务端渲染**（PHP 模板），不再依赖旧版 Vue 单页。
 * 页面需要的设备/软件包数据在服务端组装好再交给模板，减少首屏请求数。
 */
declare(strict_types=1);

namespace Kwrt\Controllers;

use Kwrt\Catalog;
use Kwrt\Config;
use Kwrt\Db;
use Kwrt\Net;
use Kwrt\Settings;
use Kwrt\View;

final class PublicController
{
    /** 首页：设备检索 + 在线定制。 */
    public function index(array $p): string
    {
        $branches = $this->branches();
        return View::page('index', [
            'pageTitle'      => (string) Settings::get('site_name', 'Kwrt'),
            'devices'        => $this->devices(),
            'branches'       => $branches,
            'defaultVersion' => array_key_first($branches) ?? '25.12',
            'catalog'        => Catalog::grouped(),
            'presets'        => Catalog::presets(),
            'suites'         => Catalog::suites(),
            'orders'         => isset($_GET['orders']),
            'myOrders'       => $this->myOrders(),
        ]);
    }

    /** 软件包库。 */
    public function packages(array $p): string
    {
        return View::page('packages', [
            'pageTitle' => '软件包库 · ' . (string) Settings::get('site_short', 'Kwrt'),
            'catalog'   => Catalog::grouped(),
            'presets'   => Catalog::presets(),
            'archs'     => $this->archs(),
            'q'         => (string) ($_GET['q'] ?? ''),
            'arch'      => (string) ($_GET['arch'] ?? ''),
        ]);
    }

    /** 插件提议。 */
    public function newpkg(array $p): string
    {
        $rows = Db::all('SELECT id, name, url, note, status, reply, created FROM proposals '
            . "WHERE status != 'rejected' ORDER BY created DESC LIMIT 100");
        return View::page('newpkg', [
            'pageTitle' => '插件提议 · ' . (string) Settings::get('site_short', 'Kwrt'),
            'proposals' => $rows,
        ]);
    }

    /** 为爱发电（赞助）。 */
    public function fadian(array $p): string
    {
        $tiers = $this->tiers();
        $u = \Kwrt\Auth::currentUser();
        $mine = $u ? Db::all('SELECT * FROM sponsor_claims WHERE username=? ORDER BY created DESC LIMIT 50',
            [$u['username']]) : [];
        return View::page('fadian', [
            'pageTitle' => '为爱发电 · ' . (string) Settings::get('site_short', 'Kwrt'),
            'tiers'     => $tiers,
            'claims'    => $mine,
            'orders'    => $u ? $this->myOrders() : [],
            // ★ 这三项原先从未传给模板 —— 后台配了「赞助说明」「收款码图片」
            //   「货币单位」，PHP 的赞助页全部看不见（Python 版正常显示）。
            'note'      => (string) Settings::get('sponsor.note', ''),
            'payQr'     => (string) Settings::get('sponsor.pay_qr', ''),
            'currency'  => (string) Settings::get('sponsor.currency', 'CNY'),
            // ★ 动态收款码：用户输入金额 → 点击 → 服务端实时生成图片地址。
            //   模板据此画金额输入框并做即时校验；真值仍由服务端 Sponsor::resolve 决定。
            'customAmount' => Settings::bool('sponsor.custom_amount', true),
            'minAmount'    => \Kwrt\Sponsor::amountRange()[0],
            'maxAmount'    => \Kwrt\Sponsor::amountRange()[1],
            'perDay'       => \Kwrt\Sponsor::perDayPrice(),
            'payAvailable' => \Kwrt\Pay::available(),
        ]);
    }

    /** 联系我们。 */
    public function contact(array $p): string
    {
        return View::page('contact', [
            'pageTitle' => '联系我们 · ' . (string) Settings::get('site_short', 'Kwrt'),
        ]);
    }

    /**
     * 固件索引页 /firmware/{target}/{id}/。
     * target 里含斜杠，所以路由用 {target:path} 兜底整段。
     */
    public function firmware(array $p): string
    {
        $seg = trim((string) ($p['target'] ?? ''), '/');
        $parts = explode('/', $seg);
        $id = array_pop($parts) ?? '';
        $target = implode('/', $parts);
        // 规范化：把 target 还原成 overview.json 里的写法
        $known = [];
        $f = Config::path('data', 'json', 'v1', 'overview.json');
        if (is_file($f)) {
            $ov = json_decode((string) file_get_contents($f), true);
            foreach (($ov['branches'] ?? []) as $b) {
                foreach (array_keys($b['targets'] ?? []) as $t) {
                    $known[$t] = true;
                }
            }
        }
        if (!isset($known[$target]) && str_contains($seg, '/')) {
            $head = substr($seg, 0, (int) strrpos($seg, '/'));
            if (isset($known[$head])) {
                $target = $head;
                $id = substr($seg, (int) strrpos($seg, '/') + 1);
            }
        }
        if ($target === '') {
            $target = 'x86/64';
        }
        if ($id === '') {
            $id = 'generic';
        }
        return View::page('firmware', [
            'pageTitle' => $target . '/' . $id . ' · 固件下载',
            'target' => $target,
            'id' => $id,
            'info' => $this->targetInfo($target),
        ]);
    }

    // ------------------------------------------------------------- PWA / App

    /** Web App Manifest —— 「网站打包成 App」的第一步（可安装到桌面）。 */
    public function manifest(array $p): string
    {
        // ★ 必须受 entry.pwa_enabled 管辖：否则后台关掉 PWA 在 PHP 侧毫无效果
        //   （Python 侧有 gate、PHP 侧没有 —— 由 tools/verify_switch_parity.py 查出）
        if (!Settings::bool('entry.pwa_enabled', true)) {
            http_response_code(404);
            header('Content-Type: application/json; charset=utf-8');
            return json_encode(['detail' => 'Not Found']);
        }
        header('Content-Type: application/manifest+json; charset=utf-8');
        // manifest 跟随站点版本号做短缓存：改了图标/名称能较快生效
        header('Cache-Control: public, max-age=300');
        $name = (string) Settings::get('site_name', 'Kwrt');
        $short = (string) Settings::get('entry.pwa_short_name', (string) Settings::get('site_short', 'Kwrt'));
        $color = (string) Settings::get('entry.pwa_theme_color', '#2563eb');
        $logo = (string) Settings::get('logo_url', '');
        $icons = [];
        if ($logo !== '') {
            $icons[] = ['src' => Net::asset($logo), 'sizes' => '192x192', 'type' => 'image/png'];
            $icons[] = ['src' => Net::asset($logo), 'sizes' => '512x512', 'type' => 'image/png'];
        }
        return json_encode([
            'name' => $name,
            'short_name' => $short,
            'description' => (string) Settings::get('site_desc', ''),
            'start_url' => '/?from=pwa',
            'scope' => '/',
            'display' => 'standalone',
            'background_color' => '#ffffff',
            'theme_color' => $color,
            'orientation' => 'portrait-primary',
            'icons' => $icons,
        ], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    }

    /** Service Worker：离线兜底 + 静态资源缓存（PWA 必需）。 */
    public function serviceWorker(array $p): string
    {
        if (!Settings::bool('entry.pwa_enabled', true)) {
            http_response_code(404);
            header('Content-Type: application/json; charset=utf-8');
            return json_encode(['detail' => 'Not Found']);
        }
        header('Content-Type: application/javascript; charset=utf-8');
        // SW 本身绝不能长缓存，否则新版发不出去
        header('Cache-Control: no-cache, must-revalidate');
        $ver = (string) Settings::get('site.asset_version', '1');
        return <<<JS
// Kwrt Service Worker v{$ver}
const CACHE = 'kwrt-{$ver}';
const SHELL = ['/', '/offline.html'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  // 接口一律走网络，绝不缓（避免拿到过期的构建状态）
  if (url.pathname.startsWith('/api/')) return;
  // 静态资源：缓存优先
  if (/\\.(css|js|png|jpe?g|svg|webp|ico|woff2?)$/i.test(url.pathname)) {
    e.respondWith(
      caches.match(req).then(hit => hit || fetch(req).then(res => {
        const cp = res.clone();
        caches.open(CACHE).then(c => c.put(req, cp));
        return res;
      }).catch(() => hit))
    );
    return;
  }
  // 页面：网络优先，断网回落离线页
  e.respondWith(fetch(req).catch(() => caches.match('/offline.html')));
});
JS;
    }

    /**
     * /sitemap.xml —— 只收录仍开启的公开页面。
     *
     * ★ Python 版一直有这条，PHP 版漏了 —— 而 PHP 自己的 robots.txt 里
     *   还写着 `Sitemap: .../sitemap.xml`，等于对外宣告了一个 404。
     *   （由 tools/verify_integrity.py 的「路由对称」检查发现。）
     */
    public function sitemap(array $p): string
    {
        header('Content-Type: application/xml; charset=utf-8');
        header('Cache-Control: public, max-age=3600');
        $urls = [];
        foreach (['/' => 'page.index_enabled', '/packages/' => 'page.packages_enabled',
                  '/newpkg/' => 'page.newpkg_enabled', '/fadian/' => 'page.fadian_enabled',
                  '/contact/' => 'page.contact_enabled'] as $path => $key) {
            if (Settings::bool($key, true)) {
                $urls[] = $path;
            }
        }
        $out = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            . "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n";
        foreach ($urls as $u) {
            $out .= '  <url><loc>' . htmlspecialchars(Net::url($u), ENT_XML1) . "</loc></url>\n";
        }
        return $out . "</urlset>\n";
    }

    /**
     * 历史路径 → 301。
     *
     * 早期版本的桩页面引用过 /index.css、/index.js、/form-storage.js，
     * 文件早已不存在。保留 301 可避免遗留链接或浏览器缓存拿到 404。
     * Python 版一直有，PHP 版补齐以保证两版行为一致。
     */
    public function legacyCss(array $p): string
    {
        header('Location: /assets/css/app.css', true, 301);
        return '';
    }

    public function legacyJs(array $p): string
    {
        header('Location: /assets/js/app.js', true, 301);
        return '';
    }

    public function legacyFormStorage(array $p): string
    {
        header('Location: /assets/js/common.js', true, 301);
        return '';
    }

    public function offline(array $p): string
    {
        return View::page('offline', ['pageTitle' => '离线']);
    }

    public function robots(array $p): string
    {
        header('Content-Type: text/plain; charset=utf-8');
        $lines = ["User-agent: *"];
        $lines = ["User-agent: *"];
        // 被后台关掉的页面不该被搜索引擎收录 —— 与 Python 版 robots_txt() 一致
        foreach (['packages' => '/packages/', 'newpkg' => '/newpkg/',
                  'fadian' => '/fadian/', 'contact' => '/contact/'] as $k => $path) {
            if (!Settings::bool('page.' . $k . '_enabled', true)) {
                $lines[] = 'Disallow: ' . $path;
            }
        }
        $lines[] = 'Disallow: /admin/';
        $lines[] = 'Disallow: /store/';
        $lines[] = 'Disallow: /dl/';
        $lines[] = 'Sitemap: ' . Net::url('/sitemap.xml');
        return implode("\n", $lines) . "\n";
    }

    // ------------------------------------------------------------- 数据装配

    /** 设备索引：device_box.json（型号别名）+ overview.json（target/profile）。 */
    public function devices(): array
    {
        $out = [];
        $f = Config::path('data', 'json', 'v1', 'overview.json');
        if (is_file($f)) {
            $ov = json_decode((string) file_get_contents($f), true);
            foreach (($ov['branches'] ?? []) as $bname => $b) {
                foreach (($b['targets'] ?? []) as $target => $t) {
                    foreach (($t['profiles'] ?? []) as $pid => $prof) {
                        $title = (string) ($prof['titles'][0] ?? $prof['name'] ?? $pid);
                        $out[] = [
                            'name'    => $title,
                            'id'      => $pid,
                            'target'  => $target,
                            'branch'  => (string) $bname,
                            'vendor'  => (string) ($prof['vendor'] ?? ''),
                        ];
                    }
                }
            }
        }
        // 型号别名（Phicomm-N1 → s905d 等），补齐搜索面
        $g = Config::path('data', 'json', 'v1', 'device_box.json');
        if (is_file($g)) {
            $box = json_decode((string) file_get_contents($g), true);
            foreach ((array) $box as $alias => $slug) {
                $out[] = ['name' => (string) $alias, 'id' => (string) $slug,
                          'target' => '', 'branch' => '', 'vendor' => ''];
            }
        }
        return $out;
    }

    /** 可用分支 → 默认发行版号。 */
    public function branches(): array
    {
        $out = [];
        $f = Config::path('data', 'json', 'v1', 'overview.json');
        if (is_file($f)) {
            $ov = json_decode((string) file_get_contents($f), true);
            $def = Config::get('builder.download_default_version', []);
            foreach (($ov['branches'] ?? []) as $name => $b) {
                if (isset($b['enabled']) && !$b['enabled']) {
                    continue;
                }
                $out[(string) $name] = (string) ($def[$name] ?? '');
            }
        }
        if (!$out) {
            $out = ['25.12' => '', '24.10' => ''];
        }
        return $out;
    }

    public function archs(): array
    {
        $dir = Config::path('data', 'json', 'v1', 'releases', 'packages-25.12');
        if (!is_dir($dir)) {
            return [];
        }
        $out = [];
        foreach (scandir($dir) ?: [] as $f) {
            if (str_ends_with($f, '-index.json')) {
                $out[] = str_replace('-index.json', '', $f);
            }
        }
        sort($out);
        return $out;
    }

    /** 某 target 下的可用型号与版本（固件索引页用）。 */
    private function targetInfo(string $target): array
    {
        $f = Config::path('data', 'json', 'v1', 'overview.json');
        if (!is_file($f)) {
            return [];
        }
        $ov = json_decode((string) file_get_contents($f), true);
        foreach (($ov['branches'] ?? []) as $bname => $b) {
            if (isset($b['targets'][$target])) {
                return ['branch' => (string) $bname] + (array) $b['targets'][$target];
            }
        }
        return [];
    }

    private function tiers(): array
    {
        $raw = Settings::get('sponsor.tiers', []);
        if (is_string($raw)) {
            $raw = json_decode($raw, true) ?: [];
        }
        return is_array($raw) ? $raw : [];
    }

    private function myOrders(): array
    {
        $u = \Kwrt\Auth::currentUser();
        if (!$u) {
            return [];
        }
        return Db::all('SELECT * FROM pay_orders WHERE username=? ORDER BY created DESC LIMIT 50',
            [$u['username']]);
    }
}
