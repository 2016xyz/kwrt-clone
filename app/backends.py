#!/usr/bin/env python3
"""
构建后端抽象 —— 本地 ImageBuilder 实编 / GitHub Actions 远程构建。

统一接口：
    backend.dispatch(req, on_progress, handle) -> result dict

结果 dict 形如：
    {"status":"done"|"failed"|"running",
     "files":[{"name","size","sha256","path"}],
     "packages":[...], "stdout":..., "stderr":..., "duration":sec,
     "external":bool}     # external=True 表示产物由远端托管，需回传下载地址
"""
import hashlib
import json
import os
import threading
import urllib.parse
import zipfile
import time
import urllib.error
import urllib.request

from . import builder, sitesettings as SS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# 本地后端
# --------------------------------------------------------------------------- #
class LocalBackend:
    name = "local"
    label = "本机构建（ImageBuilder 实编）"

    def available(self):
        return True

    def dispatch(self, req, on_progress, handle=None):
        return builder.build(req, on_progress, request_hash=(handle or {}).get("request_hash"),
                             handle=handle)


# --------------------------------------------------------------------------- #
# GitHub Actions 后端
# --------------------------------------------------------------------------- #
import threading

#: 轮转游标需要加锁：uvicorn 多个请求线程会并发挑队列
_RR_LOCK = threading.Lock()


def _artifact_pattern() -> str:
    """后台的「产物名匹配」gh.artifact_pattern（默认 openwrt-*）。"""
    return str(SS.get("gh.artifact_pattern") or "openwrt-*").strip()


def _artifact_match(name: str) -> bool:
    """按 gh.artifact_pattern 过滤产物名（glob 语义）。

    空模式或 '*' 表示不过滤（兼容管理员留空的情形）。
    """
    import fnmatch
    pat = _artifact_pattern()
    if pat in ("", "*"):
        return True
    return fnmatch.fnmatch(name or "", pat)


def _normalize_queues(raw) -> list:
    """把后台的 gh.queues（JSON 数组）规范化成队列列表。

    每项形如：
        {"name": "主队列", "repo": "owner/repo", "token": "ghp_…",
         "workflow": "build-firmware.yml", "ref": "main", "enabled": true}

    容错原则：任何一项缺 repo 或 token 就**跳过**（而不是让整个后端失效）——
    配置里一行写错不该导致所有队列都不能用。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip().startswith("[") else []
        except (ValueError, TypeError):
            raw = []
    out = []
    for i, q in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(q, dict):
            continue
        repo = str(q.get("repo") or "").strip()
        token = str(q.get("token") or "").strip()
        if not repo or not token:
            continue
        out.append({
            "name": str(q.get("name") or f"队列{i + 1}").strip(),
            "repo": repo,
            "token": token,
            "workflow": str(q.get("workflow") or "build-firmware.yml").strip(),
            "ref": str(q.get("ref") or "main").strip(),
            "enabled": bool(q.get("enabled", True)),
        })
    return out


def list_queues() -> list:
    """当前可用的 GitHub 构建队列。

    兼容旧配置：gh.queues 为空时，用 gh.repo/gh.token/gh.workflow/gh.ref
    合成**一个**默认队列 —— 老用户升级后不必改任何配置就照常工作。
    """
    qs = _normalize_queues(SS.get("gh.queues"))
    if qs:
        return [q for q in qs if q["enabled"]]
    repo = str(SS.get("gh.repo") or "").strip()
    token = str(SS.get("gh.token") or "").strip()
    if repo and token:
        return [{
            "name": "默认队列",
            "repo": repo,
            "token": token,
            "workflow": str(SS.get("gh.workflow") or "build-firmware.yml").strip(),
            "ref": str(SS.get("gh.ref") or "main").strip(),
            "enabled": True,
        }]
    return []


#: 轮询游标 —— 多队列之间轮流派发，避免总压在第一个队列上
_RR = {"i": 0}


def pick_queue():
    """按轮转挑一个队列；没有可用队列返回 None。"""
    qs = list_queues()
    if not qs:
        return None
    with _RR_LOCK:
        idx = _RR["i"] % len(qs)
        _RR["i"] = (_RR["i"] + 1) % max(1, len(qs) * 1000)
    return qs[idx]


class GitHubBackend:
    """
    通过 GitHub Actions workflow_dispatch 触发构建。

    这是**真实**的远端触发：调用 GitHub REST API 派发 workflow，
    轮询 workflow run 状态，完成后取 artifacts 的下载地址。

    前置条件（管理员在控制台配置）：
      · gh.repo      owner/repo
      · gh.workflow  workflow 文件名（如 build-firmware.yml）
      · gh.ref       分支
      · gh.token     具备 repo + workflow 权限的 PAT
    仓库中需存在接受 target/profile/packages 输入的 workflow。
    """
    name = "github"
    label = "GitHub Actions 远程构建"
    API = "https://api.github.com"

    def __init__(self, queue=None):
        """queue 为 None 时读全局设置（单队列/旧配置）；给了就用该队列的参数。

        为什么要绑到实例上：dispatch() 是**同步**的 —— 派发、轮询、取产物
        都在同一次调用里完成，持有同一个 self。因此把队列绑在实例上，
        整个生命周期用的就是同一个 repo/token，不会中途串到别的队列。
        """
        self.queue = queue

    def queue_name(self) -> str:
        return (self.queue or {}).get("name", "默认队列") if self.queue else "默认队列"

    def _cfg(self, key):
        """优先级：实例绑定的队列 > 全局设置。"""
        if self.queue and key in self.queue:
            return self.queue[key]
        return None

    def available(self):
        # ★ 必须尊重 gh.enabled —— 这是后台「启用 GitHub Actions 构建」那个开关。
        #   PHP 版一直查它（AdminController::githubTest 的 github_available），
        #   Python 版原先只看 repo+token → 管理员关掉开关后，Python 仍会把任务派到 GitHub，
        #   开关形同虚设，两版行为也不一致。
        if not bool(SS.get("gh.enabled", False)):
            return False
        return bool(self._repo() and self._token())

    def _repo(self):
        v = self._cfg("repo")
        return (v if v is not None else (SS.get("gh.repo") or "")).strip()

    def _token(self):
        v = self._cfg("token")
        return (v if v is not None else (SS.get("gh.token") or "")).strip()

    def _workflow(self):
        v = self._cfg("workflow")
        return (v if v is not None else (SS.get("gh.workflow") or "build-firmware.yml")).strip()

    def _ref(self):
        v = self._cfg("ref")
        return (v if v is not None else (SS.get("gh.ref") or "main")).strip()

    def _headers(self):
        return {
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Kwrt-Builder/1.0",
        }

    def _request(self, method, path, payload=None):
        url = self.API + path
        data = json.dumps(payload).encode() if payload is not None else None
        r = urllib.request.Request(url, data=data, method=method, headers=self._headers())
        if data:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=45) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            try:
                body = json.loads(body)
            except Exception:
                pass
            return e.code, body
        except Exception as e:
            return 0, {"message": f"{type(e).__name__}: {e}"}

    # ---- 触发 ----
    def dispatch(self, req, on_progress, handle=None):
        if not self.available():
            return {"status": "failed",
                    "stderr": "GitHub 后端未配置完整（需要 repo / workflow / token）",
                    "stdout": "", "detail": "gh-not-configured"}

        workflow = self._workflow()
        ref = self._ref()
        # uci-defaults 脚本可能含换行，workflow_dispatch 的 input 用 base64 传递更稳妥
        defaults_raw = req.get("defaults") or ""
        defaults_b64 = ""
        if defaults_raw:
            import base64 as _b64
            defaults_b64 = _b64.b64encode(defaults_raw.encode("utf-8")).decode("ascii")
        # 版本解析下沉到派发前：把「第三方插件回落 24.10」「新设备回落 SNAPSHOT」
        # 在本地算清楚，workflow 只按最终 release 构建。失败时立即返回，不派发。
        try:
            rinfo = builder.pick_version(
                req.get("target", ""), str(req.get("version") or ""),
                req.get("packages") or [], profile=req.get("profile", ""))
            eff_version = rinfo["release"]
        except RuntimeError as e:
            return {"status": "failed", "stdout": "", "detail": str(e), "stderr": str(e)}
        # apk 后端（SNAPSHOT / 25.12）无第三方 feed：剔除官方源没有的包，
        # 让设备仍能编出固件，并把被忽略的插件如实回传。
        pkgs = list(req.get("packages") or [])
        dropped = []
        if rinfo["backend"] == "apk":
            pkgs, dropped = builder.filter_unavailable(eff_version, req.get("target", ""), pkgs)
        inputs = {
            "target": req.get("target", ""),
            "profile": req.get("profile", ""),
            "packages": " ".join(pkgs),
            "version": eff_version,
            "defaults": defaults_b64,
            "filesystem": req.get("filesystem") or "squashfs",
            "rootfs_size_mb": str(req.get("rootfs_size_mb") or 512),
        }

        on_progress("running",
                    f"向 GitHub 派发构建：{self._repo()} / {workflow}（队列：{self.queue_name()}）")
        # U-3：请求 GitHub 直接回传 run 详情。注意 `return_run_details` 必须放在
        # **请求体**里（不是 query string）—— GitHub 对 query 里的该参数
        # 视而不见，仍返回 204 空响应，只有放 body 才会 200 + workflow_run_id。
        # 有它就能拿到**精确**的 run_id，不再靠「45 秒内 + 同分支」猜测 ——
        # 后者在并发派发时会把两次构建的 run 张冠李戴，导致把 A 的固件发给 B。
        st, body = self._request(
            "POST",
            f"/repos/{self._repo()}/actions/workflows/{workflow}/dispatches",
            {"ref": ref, "inputs": inputs, "return_run_details": True})
        exact_run_id = None
        if isinstance(body, dict):
            exact_run_id = body.get("workflow_run_id") or body.get("run_id")
        if st not in (204, 201, 200):
            msg = body.get("message") if isinstance(body, dict) else str(body)
            return {"status": "failed", "stdout": "", "detail": "gh-dispatch-failed",
                    "stderr": f"GitHub 派发失败 HTTP {st}: {msg}"}

        on_progress("running", "已派发，等待 GitHub 受理…")
        run_id = exact_run_id or self._await_run(workflow, ref, on_progress)
        if not run_id:
            return {"status": "running", "stdout": "", "detail": "gh-run-not-found",
                    "stderr": "派发成功但未找到对应的 workflow run（可能需稍后在 GitHub 查看）",
                    "external": True, "gh_workflow": workflow}

        on_progress("running", f"GitHub run #{run_id} 已启动，等待构建完成…")
        res = self._await_completion(run_id, on_progress,
                                     request_hash=(handle or {}).get("request_hash"))
        # 把「已忽略的不可用插件」如实带回，前端据此提示管理员/用户
        if dropped and isinstance(res, dict):
            res["dropped_packages"] = dropped
            res["warning"] = (f"{eff_version} 官方源中不存在以下插件，已自动忽略："
                              + "、".join(dropped))
        return res

    def _await_run(self, workflow, ref, on_progress, tries=12):
        """派发后 GitHub 需要几秒才创建 run，轮询找出本次 run。

        U-3 修复要点：
          · 候选 run 必须**未被其他构建认领**（_CLAIMED_RUNS 原子占位），
            否则并发派发时两次构建会抢到同一个 run；
          · 时间窗收窄到 45 秒（原 90 秒过宽，跨越了上一次派发）；
          · 取候选中最新的一个，减少拿到旧 run 的概率。
        仍属启发式兜底 —— 正常情况下应走 return_run_details 的精确路径。
        """
        since = time.time() - 45
        for _ in range(tries):
            st, body = self._request(
                "GET", f"/repos/{self._repo()}/actions/workflows/{workflow}/runs?per_page=20")
            if st == 200 and isinstance(body, dict):
                cands = []
                runs = body.get("workflow_runs") or []
                for run in runs:
                    ts = _iso_ts(run.get("created_at", ""))
                    rid = run.get("id")
                    if not ts or not rid or ts < since:
                        continue
                    if run.get("head_branch") != ref:
                        continue
                    cands.append((ts, rid))
                cands.sort(reverse=True)          # 最新优先
                for _, rid in cands:
                    if _claim_run(rid):
                        return rid
            time.sleep(5)
        return None

    def _await_completion(self, run_id, on_progress, request_hash=None, timeout=5400):
        """轮询 run 状态直到完成，然后取 artifacts。"""
        t0 = time.time()
        last = ""
        while time.time() - t0 < timeout:
            st, run = self._request("GET", f"/repos/{self._repo()}/actions/runs/{run_id}")
            if st != 200:
                time.sleep(8)
                continue
            status = run.get("status")
            concl = run.get("conclusion")
            msg = f"GitHub: {status}" + (f" / {concl}" if concl else "")
            if msg != last:
                on_progress("running", msg)
                last = msg

            if status == "completed":
                if concl != "success":
                    logs = self._failed_logs(run_id)
                    return {"status": "failed", "stdout": "", "stderr": logs,
                            "detail": f"gh-{concl}", "external": True,
                            "gh_run_url": run.get("html_url", ""),
                            "duration": time.time() - t0}
                arts = self._artifacts(run_id)
                on_progress("running", f"GitHub 构建完成，取得 {len(arts)} 个产物")
                # 把产物镜像回本地 store，使「限时下载链接」机制对两种后端一致生效
                files = arts
                try:
                    mirrored = self._mirror_artifacts(arts, on_progress, request_hash)
                    if mirrored:
                        files = mirrored
                    else:
                        print("[backend] 产物回传未产出本地文件，回退为 GitHub 直链", flush=True)
                except Exception as e:
                    print(f"[backend] 产物回传异常（不影响构建结果）: {type(e).__name__}: {e}",
                          flush=True)
                return {"status": "done", "files": files, "packages": [],
                        "stdout": "", "stderr": "", "external": True,
                        "gh_run_url": run.get("html_url", ""),
                        "duration": time.time() - t0}
            time.sleep(12)
        return {"status": "failed", "stdout": "", "stderr": f"GitHub 构建超时（>{timeout}s）",
                "detail": "gh-timeout", "external": True, "duration": time.time() - t0}

    def _artifacts(self, run_id):
        st, body = self._request("GET", f"/repos/{self._repo()}/actions/runs/{run_id}/artifacts")
        out = []
        if st == 200 and isinstance(body, dict):
            for a in body.get("artifacts", []):
                if a.get("expired"):
                    continue
                # ★ 使用后台的「产物名匹配」gh.artifact_pattern。
                #   这个键一直存在于 schema（默认 'openwrt-*'）却从无代码使用 ——
                #   管理员改了它没有任何效果，属于「后台能配、代码不用」的死键。
                #   workflow 里可能同时存在多个 artifact，用它可以只镜像目标产物。
                if not _artifact_match(a.get("name", "")):
                    continue
                out.append({
                    "id": a.get("id"),                    # 回传时需要
                    "name": a.get("name", "artifact"),
                    "size": a.get("size_in_bytes", 0),
                    "sha256": "",
                    "url": a.get("archive_download_url", ""),
                    "expired": bool(a.get("expired")),
                    "external": True,
                })
        return out

    def _failed_logs(self, run_id, max_lines=80):
        st, body = self._request("GET", f"/repos/{self._repo()}/actions/runs/{run_id}/jobs")
        lines = []
        if st == 200 and isinstance(body, dict):
            for job in body.get("jobs", []):
                for step in job.get("steps", []):
                    if step.get("conclusion") == "failure":
                        lines.append(f"✗ {job.get('name')} / {step.get('name')}")
                if job.get("conclusion") == "failure":
                    lines.append(f"job 失败: {job.get('name')} — {job.get('html_url','')}")
        if not lines:
            lines = ["GitHub 构建失败，请到 Actions 页面查看完整日志。"]
        return "\n".join(lines[:max_lines])

    def list_workflows(self):
        """管理端「测试连接」用。"""
        if not (self._repo() and self._token()):
            return False, "未配置 repo 或 token"
        st, body = self._request("GET", f"/repos/{self._repo()}/actions/workflows")
        if st != 200:
            msg = body.get("message") if isinstance(body, dict) else str(body)
            return False, f"HTTP {st}: {msg}"
        return True, [{"name": w.get("name"), "path": w.get("path"),
                       "state": w.get("state")} for w in body.get("workflows", [])]

    # ---- 产物镜像回本地 ----
    def _download_artifact(self, artifact_id, dest_zip, deadline=None):
        """
        下载 artifact 归档。

        archive_download_url 会 302 到 Azure Blob 的预签名地址，
        第二跳必须去掉 Authorization 头（否则签名不匹配）。

        deadline: 绝对超时时间戳；到了就中止，避免卡死构建队列。

        关于超时（重要）：
            不能用固定的 socket 读超时。产物动辄上百 MB，慢链路下单次 read()
            卡住超过该值就会被判定失败 —— 而服务端此时仍在发数据，最终表现为
            BrokenPipeError，整包白下。这里改为：
              · 单次 socket 超时按剩余时限动态取（有下限，保证慢速也能推进）
              · 分块失败时断点续传（Range），保留已下载部分
              · 用 Content-Length 校验完整性，截断的包视为失败
        """
        url = f"{self.API}/repos/{self._repo()}/actions/artifacts/{artifact_id}/zip"

        class NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        op = urllib.request.build_opener(NoRedir)
        req = urllib.request.Request(url, headers=self._headers())
        try:
            op.open(req, timeout=60)
            return False, "未发生重定向"
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if not loc:
                return False, f"HTTP {e.code} 且无重定向地址"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

        # U-2：302 的 Location 必须落在白名单内，否则可能是 SSRF / file://
        ok_redir, why = _validate_redirect(loc)
        if not ok_redir:
            return False, f"重定向被拒绝（{why}）"

        # U-1：记录本次下载的身份指纹（URL + 大小 + 修改时间）。
        # 续传时若指纹变了，说明 dest_zip 里残留的是**另一个产物**的分片，
        # 继续追加会拼出一个损坏的 zip。此时丢弃重下。
        fp_path = dest_zip + ".fp"
        ident = ""
        try:
            st, h = self._request(
                "GET", f"/repos/{self._repo()}/actions/artifacts/{artifact_id}")
            a = h.get("artifact") or {} if isinstance(h, dict) else {}
            ident = f"{a.get('id')}|{a.get('size_in_bytes')}|{a.get('updated_at')}|{a.get('name')}"
        except Exception:
            ident = str(artifact_id)

        def _remain():
            if not deadline:
                return 3600
            return max(0, int(deadline - time.time()))

        last_err = ""
        for attempt in range(1, 4):                 # 最多 3 次，失败即续传
            if deadline and _remain() <= 0:
                return False, "回传超时（可调大 gh.mirror_timeout）"

            have = os.path.getsize(dest_zip) if os.path.isfile(dest_zip) else 0
            # U-1：已有分片必须属于同一个产物，否则丢弃重下（避免拼出坏包）
            if have:
                prev = ""
                try:
                    with open(fp_path, encoding="utf-8") as f:
                        prev = f.read().strip()
                except OSError:
                    prev = ""
                if prev != ident:
                    print(f"[backend] 已存在分片与当前产物不匹配，丢弃重下"
                          f"（{prev[:40]!r} != {ident[:40]!r}）", flush=True)
                    try:
                        os.remove(dest_zip)
                    except OSError:
                        pass
                    have = 0
            hdrs = {"User-Agent": "Kwrt-Builder/1.0"}
            if have:
                hdrs["Range"] = f"bytes={have}-"
            r2 = urllib.request.Request(loc, headers=hdrs)
            # 记录本次身份，供下次续传比对
            try:
                with open(fp_path, "w", encoding="utf-8") as f:
                    f.write(ident)
            except OSError:
                pass

            # 单次读超时：取剩余时限，但不超过 300s；下限 60s 兜底
            sock_timeout = max(60, min(300, _remain() or 300))
            try:
                with urllib.request.urlopen(r2, timeout=sock_timeout) as resp:
                    # 206 表示服务端接受了续传；200 表示从头开始（丢弃已有部分）
                    if have and resp.status != 206:
                        have = 0
                    total = resp.headers.get("Content-Length")
                    total = int(total) + have if (total and total.isdigit()) else 0
                    mode = "ab" if (have and resp.status == 206) else "wb"
                    with open(dest_zip, mode) as f:
                        while True:
                            if deadline and time.time() > deadline:
                                return False, "回传超时（可调大 gh.mirror_timeout）"
                            chunk = resp.read(1 << 20)
                            if not chunk:
                                break
                            f.write(chunk)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                print(f"[backend] 产物第 {attempt} 次下载中断（已收 "
                      f"{os.path.getsize(dest_zip) if os.path.isfile(dest_zip) else 0} 字节），"
                      f"准备续传: {last_err}", flush=True)
                time.sleep(min(5 * attempt, 15))
                continue

            # 完整性校验：有 Content-Length 就必须收满；没有的（分块传输）
            # 退回用 zip 自身的 EOCD 判定 —— 原实现无 Content-Length 时直接
            # `return True`，截断的回传包会被当成成功产物（构建"成功"但产物损坏）。
            got = os.path.getsize(dest_zip) if os.path.isfile(dest_zip) else 0
            if total:
                if got < total:
                    last_err = f"下载不完整（{got}/{total} 字节）"
                    print(f"[backend] 产物第 {attempt} 次不完整，续传中: {last_err}", flush=True)
                    continue
            elif not _zip_complete(dest_zip):
                last_err = f"下载可能不完整（无 Content-Length，zip 尾部校验失败，{got} 字节）"
                print(f"[backend] {last_err}，续传中", flush=True)
                continue
            # 成功后清掉身份标记文件，避免残留（失败重试时还会重新写）
            try:
                os.remove(fp_path)
            except OSError:
                pass
            return True, "ok"

        return False, last_err or "下载失败"

    def _mirror_artifacts(self, arts, on_progress, request_hash):
        """
        把 GitHub 产物拉回 store/<request_hash>/ 并解压，返回本地文件清单。
        这样 /dl/t/<token> 限时下载机制对 GitHub 后端同样生效。

        是否启用由管理员配置 gh.mirror_artifacts 决定；关闭或失败时返回 None，
        由上层回退为直接暴露 GitHub 产物地址。
        """
        if not arts or not request_hash:
            return None
        if not SS.get("gh.mirror_artifacts"):
            on_progress("running", "产物回传已关闭，直接使用 GitHub 产物地址")
            return None
        try:
            timeout = int(SS.get("gh.mirror_timeout") or 3600)
        except (TypeError, ValueError):
            timeout = 3600
        dest = os.path.join(ROOT, "store", os.path.basename(request_hash))
        os.makedirs(dest, exist_ok=True)
        deadline = time.time() + timeout
        out = []
        for a in arts:
            aid = a.get("id")
            if not aid:
                continue
            mb = (a.get("size") or 0) / 1048576
            on_progress("running", f"回传产物「{a.get('name')}」({mb:.0f} MB)…")
            # 产物名来自 GitHub，不能直接拼路径（可能含 / 或 ..）
            safe_art = os.path.basename(str(a.get("name") or "artifact"))[:120]
            if not safe_art or safe_art in (".", ".."):
                continue
            zp = os.path.join(dest, f".{safe_art}.zip")
            ok, msg = self._download_artifact(aid, zp, deadline)
            if not ok or not os.path.isfile(zp) or os.path.getsize(zp) == 0:
                print(f"[backend] 产物回传失败 {a.get('name')}: {msg}", flush=True)
                try:
                    os.remove(zp)
                except OSError:
                    pass
                continue
            try:
                # 用项目自带的安全解压（校验条目名/链接/设备文件，拒绝越界整包）
                from . import builder as _b
                _b._safe_extract(zp, dest)
                os.remove(zp)
            except Exception as e:
                print(f"[backend] 解压失败 {a.get('name')}: {e}", flush=True)
                try:
                    os.remove(zp)
                except OSError:
                    pass
                continue
        # 汇总本地文件
        for fn in sorted(os.listdir(dest)):
            fp = os.path.join(dest, fn)
            if not os.path.isfile(fp):
                continue
            h = hashlib.sha256()
            with open(fp, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            out.append({"name": fn, "size": os.path.getsize(fp),
                        "sha256": h.hexdigest(), "external": True})
        if out:
            on_progress("running", f"已回传 {len(out)} 个产物到本站存储")
        return out or None


# 已被构建认领的 GitHub run_id（避免并发派发时两次构建抢同一个 run）
_CLAIMED_RUNS = set()
_CLAIM_LOCK = threading.Lock()


def _claim_run(run_id):
    """原子认领一个 run_id；已被认领则返回 False。

    用于 _await_run 的启发式兜底路径 —— 没有它，两次并发派发会把同一个
    run 各自领走，最终把 A 的固件发给 B（产物错配，用户拿到别人的固件）。
    """
    with _CLAIM_LOCK:
        if run_id in _CLAIMED_RUNS:
            return False
        _CLAIMED_RUNS.add(run_id)
        # 防止集合无限增长：超过 1000 条时丢掉最老的一半
        if len(_CLAIMED_RUNS) > 1000:
            for r in list(_CLAIMED_RUNS)[:500]:
                _CLAIMED_RUNS.discard(r)
        return True


# 允许的重定向目标主机 —— GitHub 产物经 302 指向 Azure Blob 预签名地址
_REDIRECT_HOSTS = (
    "github.com", "api.github.com", "codeload.github.com",
    "objects.githubusercontent.com", "github-releases.githubusercontent.com",
    "release-assets.githubusercontent.com",
)
_REDIRECT_HOST_SUFFIXES = (
    ".githubusercontent.com",
    ".blob.core.windows.net",       # Azure Blob（Actions 产物实际落点）
    ".githubassets.com",
)


def _validate_redirect(loc):
    """校验 302 的 Location，返回 (ok, 原因)。

    为什么必须做：原实现直接 `urllib.request.Request(loc, ...)` 去取第二跳，
    而 loc 来自响应头 —— 若上游（或中间人）返回 `Location: file:///etc/passwd`
    或指向内网地址，就变成 SSRF / 本地文件读取。这里限定必须是 https，
    且主机落在 GitHub 及其产物 CDN 白名单内。
    """
    if not loc:
        return False, "重定向地址为空"
    try:
        u = urllib.parse.urlsplit(loc)
    except Exception as e:
        return False, f"重定向地址无法解析: {e}"
    if u.scheme != "https":
        return False, f"重定向协议不允许: {u.scheme or '(空)'}"
    host = (u.hostname or "").lower()
    if not host:
        return False, "重定向缺少主机名"
    if host in _REDIRECT_HOSTS or host.endswith(_REDIRECT_HOST_SUFFIXES):
        return True, "ok"
    return False, f"重定向目标不在白名单: {host}"


def _zip_complete(path):
    """判定 zip 是否完整 —— 通过定位结尾中央目录（EOCD）签名。

    为什么不用 zipfile：对超大文件读取中央目录成本偏高；这里用轻量的
    尾部探测，只需确认文件末尾存在 PK\x05\x06 记录，避免把截断的回传包
    误判为成功产物（原实现在无 Content-Length 时直接放行）。
    """
    try:
        size = os.path.getsize(path)
        if size < 22:
            return False
        with open(path, "rb") as f:
            tail = min(size, 65557)          # EOCD 最大可能位置
            f.seek(size - tail)
            data = f.read(tail)
        return b"PK\x05\x06" in data
    except Exception:
        return False


def _iso_ts(s):
    """ISO8601 → epoch 秒。"""
    if not s:
        return 0
    try:
        import datetime
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# 调度
# --------------------------------------------------------------------------- #
_LOCAL = LocalBackend()
_GH = GitHubBackend()


def get_backend(name=None):
    """按配置选择后端；github 不可用时安全回落到本地。

    ★ 多队列：每次取后端时按轮转挑一个队列，把参数绑到实例上。
      gh.queues 为空时 pick_queue() 会合成单队列（旧配置照常工作）。
    """
    want = (name or SS.get("builder.backend") or "local").lower()
    if want == "github":
        if _GH.available():
            q = pick_queue()
            return GitHubBackend(q) if q else _GH
        print("[backend] GitHub 后端未配置，回落到本机构建", flush=True)
        return _LOCAL
    return _LOCAL


def info():
    want = (SS.get("builder.backend") or "local").lower()
    gh_ok = _GH.available()
    qs = list_queues()
    return {
        "queues": [{"name": q["name"], "repo": q["repo"], "workflow": q["workflow"],
                    "ref": q["ref"]} for q in qs],
        "queue_count": len(qs),
        "configured": want,
        "effective": "github" if (want == "github" and gh_ok) else "local",
        "github_available": gh_ok,
        "local_available": True,
        "labels": {"local": _LOCAL.label, "github": _GH.label},
    }
