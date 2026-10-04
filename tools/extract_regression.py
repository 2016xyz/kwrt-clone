"""_safe_extract 完整回归：攻击向量应全拦，合法归档应可用。"""
import io, os, shutil, stat, sys, tarfile, tempfile, zipfile
# ★ 不能用开发机的绝对路径：脚本要在任意部署目录（开发机、服务器）都能跑。
#   原先写死 sys.path.insert/os.chdir 到某个 /root/.hermes/... 路径，
#   换台机器就 FileNotFoundError —— 一个跑不起来的回归测试等于没有。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
from app.builder import _safe_extract

BASE = tempfile.mkdtemp(prefix="sx_")
results = []


def case(name, build, expect_block, ext=".tar.gz"):
    dest = os.path.join(BASE, name.replace(" ", "_"), "files")
    sib = os.path.join(BASE, name.replace(" ", "_"), "filesX")
    os.makedirs(dest); os.makedirs(sib, exist_ok=True)
    arc = os.path.join(BASE, name.replace(" ", "_"), "a" + ext)
    build(arc, dest)
    try:
        _safe_extract(arc, dest)
        blocked = False
        err = ""
    except Exception as e:
        blocked = True
        err = str(e)[:56]
    # 检查越界落点
    escaped = []
    for root, dirs, names in os.walk(BASE):
        for n in names:
            p = os.path.join(root, n)
            if os.path.realpath(p).startswith(os.path.realpath(sib)):
                escaped.append(n)
    ok = (blocked == expect_block) and (not escaped or expect_block)
    results.append((name, blocked, expect_block, escaped, err, ok))


# ── 攻击向量 ──
def t_dotdot(arc, dest):
    with tarfile.open(arc, "w:gz") as t:
        d = b"x"; i = tarfile.TarInfo("../evil.txt"); i.size = len(d)
        t.addfile(i, io.BytesIO(d))
case("tar ../ 穿越", t_dotdot, True)

def t_abs(arc, dest):
    with tarfile.open(arc, "w:gz") as t:
        d = b"x"; i = tarfile.TarInfo("/tmp/abs_evil.txt"); i.size = len(d)
        t.addfile(i, io.BytesIO(d))
case("tar 绝对路径", t_abs, True)

def t_sym_prefix(arc, dest):
    with tarfile.open(arc, "w:gz") as t:
        i = tarfile.TarInfo("link"); i.type = tarfile.SYMTYPE
        i.linkname = "../filesX"; t.addfile(i)
        d = b"PWNED"; f = tarfile.TarInfo("link/escaped.txt"); f.size = len(d)
        t.addfile(f, io.BytesIO(d))
case("tar 符号链接前缀绕过", t_sym_prefix, True)

def t_sym_root(arc, dest):
    with tarfile.open(arc, "w:gz") as t:
        i = tarfile.TarInfo("link"); i.type = tarfile.SYMTYPE
        i.linkname = "/etc"; t.addfile(i)
case("tar 符号链接 /etc", t_sym_root, True)

def t_hardlink(arc, dest):
    with tarfile.open(arc, "w:gz") as t:
        i = tarfile.TarInfo("h"); i.type = tarfile.LNKTYPE
        i.linkname = "../../../etc/passwd"; t.addfile(i)
case("tar 硬链接越界", t_hardlink, True)

def t_dev(arc, dest):
    with tarfile.open(arc, "w:gz") as t:
        i = tarfile.TarInfo("dev"); i.type = tarfile.CHRTYPE
        i.devmajor = 1; i.devminor = 3; t.addfile(i)
case("tar 设备文件", t_dev, True)

def z_dotdot(arc, dest):
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("../evil.txt", "x")
case("zip ../ 穿越", z_dotdot, True, ".zip")

def z_symlink(arc, dest):
    with zipfile.ZipFile(arc, "w") as z:
        i = zipfile.ZipInfo("link"); i.create_system = 3
        i.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(i, "../filesX")
case("zip 符号链接", z_symlink, True, ".zip")

# ── 合法归档 ──
def ok_tar(arc, dest):
    with tarfile.open(arc, "w:gz") as t:
        d = b"config"; i = tarfile.TarInfo("etc/config/net"); i.size = len(d)
        t.addfile(i, io.BytesIO(d))
case("合法 tar.gz", ok_tar, False)

def ok_nested(arc, dest):
    with tarfile.open(arc, "w:gz") as t:
        for n in ("etc/uci-defaults/99-x", "root/.ssh/authorized_keys"):
            d = b"k"; i = tarfile.TarInfo(n); i.size = len(d)
            t.addfile(i, io.BytesIO(d))
case("合法 多层目录", ok_nested, False)

def ok_zip(arc, dest):
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("etc/config/x", "y"); z.writestr("root/f", "z")
case("合法 zip", ok_zip, False, ".zip")

def ok_symlink_inside(arc, dest):
    with tarfile.open(arc, "w:gz") as t:
        i = tarfile.TarInfo("a/b"); i.type = tarfile.SYMTYPE
        i.linkname = "c"; t.addfile(i)
        d = b"x"; f = tarfile.TarInfo("a/c"); f.size = len(d)
        t.addfile(f, io.BytesIO(d))
case("符号链接指向 dest 内", ok_symlink_inside, False)

print("=" * 96)
print(f"{'用例':<26}{'被拦截':<9}{'期望拦截':<11}{'越界写出':<12}{'判定'}")
print("=" * 96)
allok = True
for name, blocked, expect, escaped, err, ok in results:
    if not ok:
        allok = False
    print(f"{name:<26}{str(blocked):<9}{str(expect):<11}{str(escaped):<12}{'✓' if ok else '✗'}  {err}")
print("=" * 96)
print(f"合计 {len(results)} 项，{'全部符合预期 ✓' if allok else '存在不符 ✗'}")
shutil.rmtree(BASE, ignore_errors=True)
