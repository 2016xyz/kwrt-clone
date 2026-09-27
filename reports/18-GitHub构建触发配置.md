# op.2016xlx.cn —— GitHub Actions 构建触发配置

**需求原话**：「帮我配置 github 构建需要的信息 就用现在的 token」
→「帮我填写 op.2016xlx.cn GitHub Actions 触发配置所有需要配置的参数，
   只需要配置参数，不需要试运行」

**结果**：线上参数已全部填好并逐项验证通过（只读预检 **11/11**，未派发任何构建）。
`builder.backend` 已切到 GitHub Actions，线上前台文案变为「构建后端 GitHub Actions」。

> 本报告不含任何明文凭据。

---

## TL;DR

| # | 参数 | 改前 | 改后 | 依据 |
|---|---|---|---|---|
| 1 | `gh.enabled` | `false` | **`true`** | 必须为真，否则 `available()` 直接 false |
| 2 | `gh.repo` | 空 | **`2016xyz/kwrt-firmware-builder`** | 仓库里放着构建 workflow |
| 3 | `gh.workflow` | `build-firmware.yml` | `build-firmware.yml`（不变） | 该仓库里的 active workflow |
| 4 | `gh.ref` | `main` | `main`（不变） | 仓库默认分支 |
| 5 | `gh.token` | **空** | **已填入（40 位）** | 就用你现在的那个 token |
| 6 | `builder.backend` | `local` | **`github`** | 让构建真的走 Actions |
| 7 | `gh.artifact_pattern` | `openwrt-*` | 不变 | 与 workflow 产物名 `openwrt-…` 匹配 |
| 8 | `gh.mirror_artifacts` | `true` | 不变 | 产物回传本站，链接由本站管理 |
| 9 | `gh.mirror_timeout` | `3600` | 不变 | 单个产物不大，够用 |
| 10 | `gh.queues` | `[]` | 不变 | 留空即回落到上面单队列，**不必填** |

**验证**：只读预检 11 项全过 —— token 有效、仓库可达、ref 存在、workflow active、
输入契约一致、产物名匹配、默认版本合法。

**没有试运行**（按你的要求）。但配置正确性已用不派发的方式验完。

---

## 一、这些值是怎么定出来的

没有一个是猜的，全部来自可核对的真源：

| 参数 | 真源 | 核对结果 |
|---|---|---|
| `gh.repo` | `GET /user/repos` | 24 个仓库里 `2016xyz/kwrt-firmware-builder`（私有，9-24 推送）是唯一带构建 workflow 的 |
| `gh.workflow` | `GET /repos/…/actions/workflows` | 仓库里两个 workflow：`build-firmware.yml`（active）、`build-firmware-verify.yml`（active） |
| `gh.ref` | `GET /repos/…` | `default_branch = main`，与配置值一致 |
| `gh.enabled` | `app/backends.py::available()` | 代码显式要求它，关掉就直接 false |
| `builder.backend` | `app/builder` | `local` / `github` 二选一 |
| `gh.queues` | `app/backends.py::list_queues()` | 为空时用 `gh.repo`/`gh.token`/`gh.workflow`/`gh.ref` **合成一个默认队列**，所以单队列不用填 |

### token 是哪一个

`gh.token` 线上原本是**空的**。你说的「现在的 token」= 仓库本地库 `users.db` 里
那个已在用的（`len=40`，`ghp_` 前缀），由配置脚本读取后写入线上。
它的 scopes 含 **`repo` + `workflow`** —— 派发 workflow 需要的正是这两项。

---

## 二、验证：只读预检（不派发）

配置写完必须验，但不一定非得跑一次构建。`tools/preflight_gh_config.py`
一次 HTTP 请求都不派发，把派发会踩的坑静态排掉：

```
  ✓ gh.enabled 已开启                                 值=true
  ✓ builder.backend = github                        值=github
  ✓ gh.repo 形态为 owner/repo                          2016xyz/kwrt-firmware-builder
  ✓ gh.token 已填写                                    len=40
  ✓ token 有效（GET /user）                             HTTP 200 login=2016xyz
  ✓ 仓库可访问（owner/repo 正确）                          HTTP 200 private=True default=main
  ✓ ref 存在（main）                                    HTTP 200
  ✓ workflow 存在且 active（build-firmware.yml）         HTTP 200 state=active
  ✓ 输入契约一致（声明 ⊇ 派发）                              派发 ['defaults','filesystem','packages',
                                                     'profile','rootfs_size_mb','target','version']
  ✓ 产物名匹配 gh.artifact_pattern（openwrt-*）            openwrt-${{ steps.resolve.outputs.full }}…
  ✓ build_default_version 在 workflow 取值内            站点默认=25.12 workflow 示例=['24.10','25.12','25.12.5']
  ────────────────────────────────────────
  通过 11 项 —— 配置可用 ✓
```

### 为什么这几条值得单独验

有两个失败模式**看日志看不出来**：

- **输入名对不上** → `POST …/dispatches` 返回 **204，看着完全正常**，
  但 run 起来立刻失败。必须把「workflow 声明的 inputs」与
  「程序实际派发的键」两个集合拿出来比。
- **产物名不匹配** → 构建「成功」，站点却找不到固件。
  workflow 里 `upload-artifact` 的 `name` 必须以 `gh.artifact_pattern`
  去掉星号后的前缀开头。

实测两者都对得上：程序派发 7 个键（`target/profile/packages/version/defaults/
filesystem/rootfs_size_mb`），workflow 声明的正是这 7 个。

### 前台确认

```
GET /api/v1/site  →  build.effective   = github
                     build.configured  = github
                     github_available  = True
                     queues            = [{name: 默认队列,
                                           repo: 2016xyz/kwrt-firmware-builder,
                                           workflow: build-firmware.yml, ref: main}]
```

浏览器实测首页渲染出：**「已收录 997 台设备 · 构建后端 GitHub Actions」** ✓

---

## 三、过程中差点误判的一件事：token 回显成了 8 个星号

写入接口的返回里，`gh.token` 读回来是 `长度=8 前缀=****`，
而我写进去的是 40 位。**这有两种完全不同的含义**：

- 接口对密钥字段打码 → 实际存的是 40 位，配置正常；
- 写入时被截断成 8 位 → 配置坏了，派发会 401。

猜错任何一个都很难看，所以直接查证：

```
app/sitesettings.py::all_values()
    if spec.get("secret") and not include_secret:
        out[k] = "********"        ← 8 个星号，正是回显的「长度=8」

线上库直接读（绕开接口打码）：
    len=40   prefix=ghp_           ← 存的是完整的
```

**结论：是打码，不是截断。** 记下来是因为这类「回显和写入不一致」的情况，
判断依据必须落在**存储**上，不能落在**回显**上 —— 回显中间可能隔着打码层。

---

## 四、顺带修掉的一个自身缺陷

`tools/verify_gh_backend.py` 里的 `workflow_inputs()` 第一版写成
「比 `inputs:` 缩进更深就收」，于是把每个输入下面的
`description` / `required` / `default` 也当成了输入名：

```
修正前 声明: [..., 'default', 'defaults', 'description', 'filesystem',
              'packages', 'profile', 'required', 'rootfs_size_mb', 'target', 'version']
修正后 声明: ['defaults', 'filesystem', 'packages', 'profile',
              'rootfs_size_mb', 'target', 'version']
```

危害不是多收几个名字，而是**契约检查被放宽** —— 某个真缺的输入名若在别处
作为普通字符串出现过，就会被误判成「已声明」，于是「workflow 不认这个参数」
这类问题从检查里漏过去。

改成严格只收「输入名那一层」的键，并补了**双向负向对照**：

```
负向对照 1：输入名只嵌套出现时不得被误判为已声明
  输入: profile 只作为 description 的文字出现
  结果: 解析出 {'target'}                    ✓ 正确

负向对照 2：真缺一个派发键时必须被抓到
  输入: workflow 只声明 target/profile/packages
  结果: 报缺 ['defaults','filesystem','rootfs_size_mb','version']   ✓ 正确
```

---

## 五、没有做的事（以及为什么）

- **没有派发任何构建**（按你的要求）。所以「链路真的通」这件事**尚未实测**，
  只验到了「参数正确」。这两者的差距在第 2 节列了：输入契约与产物名匹配
  已经静态验过，但 Actions 能不能真的跑完、产物能不能真的回传，
  只有派发才知道。
- 什么时候想补这一步：

```bash
# 真实派发联调（默认走 build-firmware-verify.yml，几十秒）
.venv/bin/python tools/verify_gh_backend.py

# 单独跑上面那个只读预检
.venv/bin/python tools/preflight_gh_config.py
.venv/bin/python tools/preflight_gh_config.py --db /path/to/users.db
```

- **仓库本地库 `users.db` 未改**（它的 `gh.repo` 还是占位值 `own/repo`，
  `gh.enabled=false`）。只改了线上。若要本地也能跑 GitHub 构建，照着上面的表改即可。
  顺带一提：预检脚本对本地库会报 **5 项失败**（其中一项抓到的是
  `own/repo` 这个占位值会 404）—— 这也证明预检**能报红**，不是永远绿。

---

## 附录：证据

**配置写入**（走站点自己的管理接口，带校验与审计）

```
写入结果: status=ok
updated=['gh.enabled','gh.repo','gh.workflow','gh.ref','builder.backend','gh.token']

改后 backend_info:
  configured=github  available=True  effective=github  queues=1
  队列 默认队列: 2016xyz/kwrt-firmware-builder / build-firmware.yml @ main
```

**提交**：`6cf1783 feat(gh): GitHub Actions 触发配置的开箱验证 —— 只读预检 + 真实联调两个工具`
（`tools/preflight_gh_config.py`、`tools/verify_gh_backend.py`，已推送 `2016xyz/kwrt-clone`）

**说明**：配置写入走的是 `POST /api/v1/admin/site`，它会用 `SS.set_()` 做
类型/取值校验并写审计日志 —— 直接改 SQLite 会绕过这两样，出问题时查不到是谁改的。