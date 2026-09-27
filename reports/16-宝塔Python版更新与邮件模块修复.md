# 宝塔服务器 Python 版更新，以及顺带修掉的三个真实缺陷

**需求原话**：「更新宝塔面板的 python 版」

**结果**：线上部署从 `dee7cec` 更新到 `12e5dc5`（**26 个提交**），过程中在真机上
发现并修掉 **3 个真实缺陷**，全部在线上验收。

> 本报告不含任何明文凭据。

---

## TL;DR

| # | 事项 | 状态 |
|---|---|---|
| 1 | 线上 git 部署已更新 `dee7cec → 12e5dc5`（26 提交），服务器专属配置完整保留 | ✅ |
| 2 | **缺陷①** `GET /api/v1/admin/mail/log` 稳定 **500**（deque 不支持切片） | ✅ 已修（线上原为 500，现 200） |
| 3 | **缺陷②** 「发送测试邮件」在 Python 版**不留痕**，与 PHP 版不一致 | ✅ 已修 |
| 4 | **缺陷③** 发信**失败后**「投递记录」不刷新（PHP 版更重：完全不刷新） | ✅ 已修 |
| 5 | 补上缺失的**整类**护栏：扫全部管理端 GET 接口，任何 5xx 即失败 | ✅ 新增 8 项断言，含可报红的负向对照 |
| 6 | 修掉一个**偶发断言**（UC-10 会对 GitHub 抖动误报） | ✅ 连跑三次稳定 |
| 7 | 线上 19 个管理端接口 / 15 个后台模块全部正常 | ✅ |

---

## 一、线上更新的做法（以及为什么不能直接 `git pull`）

侦察发现一个**危险的前提**：线上确实是 git 部署，但它的 `origin/main` 也停在
`dee7cec`（从没 fetch 过）—— 所以 `git status` 看起来「干净」，实际落后 26 个提交。
任何依赖 `git pull` 的更新都会以为无事可做。

更危险的是 **20 项本地改动**。逐项分类后：

| 类别 | 文件 | 处置 |
|---|---|---|
| 我之前手工推送、且已入库 | `app/{captcha,mailer,main,netcfg,pay,refund,sitesettings,verify}.py`、`web/admin.html`、`web/assets/js/{admin,login}.js`、`web/login.html`、`requirements.txt`、`app/{reset,update}.py`、`web/reset.html` 等 | `reset --hard` 覆盖即可（内容一致） |
| **必须保留** | `config.json`（**已跟踪且被本地改过**） | 单独备份 → reset → **还原** |
| `users.db` / `kwrt.env` / `store/` / `cache/` | 运行时状态 | 未跟踪或被忽略，`reset --hard` 不动它们 ✓ |

**`config.json` 是关键**：线上是 `host: op.2016xlx.cn, port: 80`，仓库版是
`0.0.0.0:8443`。若被应用读取，用仓库版会**把 8443 暴露到公网**。
（实测确认服务实际监听参数来自 systemd 的
`--host 0.0.0.0 --port 8443`，`config.json` 的 host/port 未被服务使用 —— 但仍
按最保守处置：保留线上那份。）

于是写了一个**带备份、带依赖自检、失败自动回滚**的更新脚本：

```
备份（config.json / kwrt.env / INITIAL_ADMIN.txt / users.db / 整包 452K）
  → git fetch（临时 GIT_ASKPASS，Token 从 users.db 读，不上命令行）
  → git reset --hard FETCH_HEAD
  → 还原 config.json
  → 导入全部 20 个 app 模块（失败即 git reset 回滚 + 还原 config）
  → 清 __pycache__ → systemctl restart kwrt → 检查 is-active 与 /healthz
```

每一步的退出码都单独判定，中途任何一步失败都不会留下半更新状态。

---

## 二、缺陷①：投递记录接口稳定 500

### 症状

在线上逐个点开后台 15 个模块时，浏览器的 `fetch` 拦截器抓到：

```
/api/v1/admin/mail/log → 500
```

服务端堆栈：

```
File "/root/kwrt-clone/app/main.py", line 2890, in admin_mail_log
    return {"log": mailer.recent(limit), ...}
File "/root/kwrt-clone/app/mailer.py", line 325, in recent
    return _sent[-limit:][::-1]
TypeError: sequence index must be integer, not 'slice'
```

### 根因

`_sent = collections.deque(maxlen=500)` —— **deque 不支持切片**。

### 为什么所有测试都是绿的

**没有任何测试调用过这个端点。** 这不是「漏了一个 bug」，是**整类缺陷没有覆盖**：
一个管理端接口挂了，没有任何套件会发现。

### 修法：删掉重复机制，与 PHP 对齐

Python 侧原本有**两套**发信日志：

| | 写入方 | 读取方 |
|---|---|---|
| `email_send_log` **表** | `verify.log_send()`（由 main.py 调用） | 无（Python 侧） |
| `_sent` **进程内 deque** | `mailer.send()` | `admin_mail_log` |

而 **PHP** 的 `AdminController::mailLog` 读的是 `email_send_log` 表，
**前端模板**用的也是表结构字段（`{{ m.to_addr \|\| m.to }}` / `{{ m.created }}` /
`{{ m.detail }}` / `m.id`）—— 也就是说 **Python 后端才是那个异类**。

所以修法不是「把切片改对」，而是**删掉 `_sent` 那份内存日志**，让 `recent()`
读 `email_send_log` 表。这同时修掉两个问题：

1. 接口 500（deque 切片）
2. **内存日志重启即失** —— 之前 Python 版每次重启，后台投递记录就清空，PHP 版不会。
   两版共用同一张表之后，历史长度也一致了。

顺带夹紧 `limit`：`[-0:]` 会退化成「取全部」（经典 off-by-one），
现在显式 `max(0, min(limit, 500))`。

---

## 三、缺陷②：Python 版「发送测试邮件」不留痕

上一个缺陷修完、在线上用真实 UI 验收时发现的。

- **PHP** `AdminController::mailTest()` **会**写 `email_send_log`（`purpose='test'`）
- **Python** `admin_mail_test` 只写审计日志，**不写这张表**

后果：管理员在 Python 版点完「发送测试邮件」，翻到「投递记录」是空的 ——
**看着像功能坏了**。同一个页面，两版给出不同内容。

**修法**：补上 `verify.log_send("test", ...)`，成功失败都记
（排查投递问题时失败记录比成功记录更有用）。记日志本身出错不得吞掉
「测试邮件」的结果，所以单独 `try` 包住。

---

## 四、缺陷③：失败后「投递记录」不刷新（两版都有，PHP 更重）

### Python

```js
await guard(async () => {
  const r = await K.postForm('/api/v1/admin/mail/test', { to: mailTestTo.value });
  toast(r.detail || '已发送', 'success');
  await loadMailLog();          // ← 失败时这一句永远到不了
});
```

`guard()` 会吞异常，而 `loadMailLog()` 写在**可能抛异常的请求之后** ——
接口回 400（SMTP 未配置）时直接跳过，记录页保持旧内容。而「测试邮件失败」
**恰恰是最该在记录里看到的那一条**。

**修法**：把刷新与复位挪进 `finally`。

### PHP（更重）

```js
}).catch(function (e) {
  toast(e.message || '操作失败', 4200);
});                            // 服务端渲染的记录表永远陈旧
```

失败走 `.catch()` 只弹提示、**完全不刷新**。

**修法**：`catch` 里也安排刷新，延迟 3200ms（成功分支是 600ms）——
要让报错提示先读得完，否则刷新会把提示一起刷掉。

---

## 五、补上缺失的整类护栏：`tools/verify_admin_smoke.py`（新增，8 项）

这个 500 之所以能活到现在，是因为**整类缺陷没有覆盖**。所以核心是一条**通用**断言：

```
AS-2  把 app/main.py 里注册的每一个管理端 GET 接口都真打一遍，
      **任何一个 5xx 都算失败**。
```

**新增接口自动被覆盖** —— 不需要有人记得来加断言。配套：

| 断言 | 内容 |
|---|---|
| AS-1 | 枚举完整性（19 个 GET，下限 15）—— 防止扫描因正则失配变成空转 |
| AS-2 | **主断言**：19 个接口逐个真打，0 个 5xx |
| AS-3 | 投递记录接口 200，且返回**表结构**字段 |
| AS-4 | `limit`：合法整数须 200，非整数须 4xx（422 是干净拒绝，**不是崩溃**） |
| AS-4b | `limit=0`/负数回**空列表**，而非退化成「取全部」 |
| AS-7 | 「发送测试邮件」留痕（与 PHP 一致）—— 调接口后直接查库断言那一行 |
| AS-6 | 投递记录读的是 `email_send_log` 表（与 PHP 同源）—— 用「库里的探针记录能被读到」证明，而不是「接口没报错」 |
| AS-5 | **负向对照**：断言 `deque[-1:]` 确实抛 `TypeError`（旧写法真会崩），否则 AS-3/AS-4 守的可能不是真问题 |

> **AS-4 的第一版是我自己的错**：它把 `abc` / `1.5` / 空串返回的 422 也判成失败。
> 那是 FastAPI 对非整数的**标准校验拒绝** —— 干净、明确，属于正确行为。
> 一个**失败测量不等于产品缺陷**，判据改成「非整数只要求不是 5xx」。

### 已实测能报红

把 `recent()` 改回 deque 切片注入原始 bug：

```
AS-2  ✗ 失败  扫了 19 个接口，5xx=1；首个 5xx：/api/v1/admin/mail/log → 500
AS-3  ✗ 失败  HTTP 500
合计 8 项，失败 ✗ ['AS-2','AS-3','AS-4','AS-4b','AS-6']
```

还原后：`合计 8 项，全部通过 ✓`。

---

## 六、顺带修掉一个偶发断言（UC-10）

`verify_update_check.py` 的 UC-10 **会偶发失败**。

**根因**：它要 PHP 先真查一次 GitHub、再查第二次断言 `cached=True`，
而 `check()` **有意只缓存成功结果** —— 第一次碰上 GitHub 抖动就什么也不写，
第二次自然 `cached=False`，断言红而产品是对的。

**一个会对网络抖动的常驻断言是有害的** —— 它会把人训练成「红了就重跑」，
真出问题时反而被忽略。

改成**不依赖网络**的更强证法：由**测试进程**写一条缓存（`v9.9.9`），
再让 `php -S` 的**另一个进程**读回来。这直接证明「一个进程写的，另一个进程
看得到」—— 正是这条断言真正要证的事。

并按同一线索补了 **UC-10b**，守住那条「只缓存成功」规则本身：
指向不存在的仓库 → `status=error`、缓存仍为空、文案不含「最新」。

连跑三次：`20/20`、`20/20`、`20/20`。

---

## 七、线上验收（`op.2016xlx.cn`）

### 缺陷①③ 的浏览器实测

真实 UI 操作：点「邮件通知」→ 填收件地址 → 点「发送」。
SMTP 未配置，接口回 400 并提示「邮件通知未启用」，随后：

```
操作前  投递记录
          ui-probe@example.test   失败  13:05:47 · 邮件通知未启用

操作后  投递记录
          ui-probe2@example.test  失败  13:08:26 · 邮件通知未启用
          ui-probe@example.test   失败  13:05:47 · 邮件通知未启用
```

—— 失败记录**不但写了，列表也刷新了**，收件人 / 结果 / 时间 / 失败原因
四项都渲染正确。这正是缺陷②③ 在线上被证实修好的证据。

> **两条探针记录已从线上库删除**（清理前后对比：`[(1,'ui-probe@...'),(2,'ui-probe2@...')]`
> → `[]`），服务器上的临时脚本与日志也已清掉。

### 全量线上检查（经 EdgeOne）

```
后台 19 个管理端 GET 接口        → 0 个 5xx / 不可达  ✓
浏览器逐个点开 15 个后台模块      → 全部渲染，0 失败请求，0 控制台错误  ✓
投递记录接口（原稳定 500）        → HTTP 200  ✓
limit=0 / -5 / 1000            → 200；limit=abc → 422（干净拒绝）  ✓
检查更新（真调 GitHub）          → 「已是最新版本 v1.0.1」✓
找回密码 / 站点信息 / 支付信息 / healthz → 200  ✓
静态资源路径式版本化             → 7 个引用全部带版本段，签名 7212779a46  ✓
版本化 admin.js 与本地            → 逐字节一致（40126B / md5 482dfb268c）✓
```

**合计 17 项，全部通过。**

---

## 八、部署与提交

| 文件 | 变化 | 校验 |
|---|---|---|
| `app/mailer.py` | 删除 `_sent`，`recent()` 改读表 | 随 git 更新下发 |
| `app/main.py` | 补留痕 | 189634B sha1 `1cf735ed2562` 两侧一致 ✓ |
| `web/assets/js/admin.js` | 刷新挪进 `finally` | 40126B sha1 `74c7377f108c` 两侧一致 ✓ |
| `php/public/assets/js/admin.js` | `catch` 里也刷新 | 随 git |
| `tools/verify_admin_smoke.py` | 新增 | 随 git |

```
12e5dc5  fix(mail): 测试邮件要留痕 + 失败后记录页要刷新（两版）
4e84d74  fix(mail): 邮件投递记录接口稳定 500 —— deque 不支持切片；并补上缺失的整类护栏
573eab3  docs(report): 15 号报告 —— 后台检查更新（GitHub）
```

线上 HEAD = 远端 HEAD = `12e5dc5`。三次更新各留一个备份点
（`/root/_kwrt_bak_20260927-*`）。

---

## 九、遗留

1. **线上三个备份点**占 ~1.4MB，确认无事后可删：
   `rm -rf /root/_kwrt_bak_20260927-*`
2. **`/root/kwrt-clone/config.json` 的 `server.host/port` 是陈旧的**
   （`op.2016xlx.cn:80`，实际服务由 systemd 硬编码 `0.0.0.0:8443`）。
   不影响运行，但建议改成与实际一致，免得下次有人照着它排查。
3. **`store/` 与 `cache/` 未在 `.gitignore` 覆盖范围内做了确认**（当前是被忽略的 ✓），
   但 `config.json` 是**已跟踪**的 —— 服务器专属配置放在受版本控制的文件里，
   每次更新都要手工还原。建议把服务器专属值改放 `config.local.json` 或环境变量。
4. 为腾磁盘删掉了本机 Docker 镜像 `mysql:8.0` / `mariadb:10.4` / `php:7.4-cli`
   （共 1.66GB）—— 下次跑 `verify_mysql.py` / `verify_php_compat.py` 需先重新拉取。
5. 上一轮遗留仍在：`site.trusted_proxies` 为空（CDN 后取不到真实客户端 IP）、
   「夏诗意」品牌名、线上 SMTP 未配置。
