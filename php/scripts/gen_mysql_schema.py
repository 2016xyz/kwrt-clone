#!/usr/bin/env python3
"""从 SQLite DDL 生成 MySQL DDL（php/src/Schema.php 的 MYSQL 常量）。

为什么要生成而不是手写两份：
    本项目已有一次教训 —— 同一份设置 schema 手写两遍，结果两版漂移
    （见 reports/09）。DDL 同理：手写两套建表语句，字段类型/默认值/索引
    迟早对不上，而且是**静默**对不上（一方能跑，另一方只在新装时出错）。
    因此这里沿用同一模式：SQLite DDL 是真源，MySQL 由脚本派生，
    `--check` 供 CI 校验两者同步。

用法：
    python3 php/scripts/gen_mysql_schema.py           # 生成/更新 Schema.php 的 MYSQL
    python3 php/scripts/gen_mysql_schema.py --check   # 只校验是否同源（CI 用）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_PHP = ROOT / "php" / "src" / "Schema.php"

#: MySQL 下字符串索引列的最大长度（utf8mb4 下 191*4=764 字节，落在 767 字节旧限制内，
#: 兼容 MySQL 5.7 / MariaDB 的老配置；8.0 其实可以 3072 字节，但取小值最省心）
INDEX_STRLEN = 191

#: 带默认值的 TEXT 列降级后的长度（MySQL 不允许 TEXT 带 DEFAULT）。
DEFAULT_STRLEN = 255


def _unquote_ident(name: str) -> str:
    return name.strip().strip('`"').strip()


def parse_sqlite_ddl(php_src: str) -> tuple[dict[str, str], list[str]]:
    """从 Schema.php 的 SQLITE 常量里取出建表语句与索引语句。"""
    m = re.search(r"public const SQLITE = \[(.*?)\n    \];", php_src, re.S)
    if not m:
        raise SystemExit("✗ 在 Schema.php 里找不到 SQLITE 常量")
    body = m.group(1)

    tables: dict[str, str] = {}
    # 结尾既可能是 "',\n" 也可能是 "',\n    ];"（最后一项）
    for tm in re.finditer(r"'(\w+)'\s*=>\s*'(CREATE TABLE(?:[^'\\]|\\.)*)'", body, re.S):
        name = tm.group(1)
        if name in tables:
            continue
        tables[name] = tm.group(2).replace("\\'", "'")

    mi = re.search(r"public const INDEX_SQLITE = \[(.*?)\n    \];", php_src, re.S)
    indexes = []
    if mi:
        indexes = [x.replace("\\'", "'") for x in re.findall(r"'(CREATE INDEX[^']*)'", mi.group(1))]
    return tables, indexes


def sqlite_table_to_mysql(table: str, ddl: str, indexed: set[str]) -> str:
    """把一条 SQLite 建表语句转成 MySQL 版本。

    规则（都是 SQLite→MySQL 的硬性差异，不是风格偏好）：
      · INTEGER PRIMARY KEY AUTOINCREMENT → BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY
        （MySQL 里自增列必须是键，且 INNODB 不支持「不是主键的自增」）
      · 被索引的 TEXT 列 → VARCHAR(191)：MySQL **不允许**对 TEXT 建无前缀索引
      · REAL → DOUBLE；INTEGER → INT
      · 双引号默认值 → 单引号（避免依赖 ANSI_QUOTES 设置）
      · 行级 UNIQUE / PRIMARY KEY 约束改写成表级，便于给约束起名（排查时报错更清楚）
    """
    inner = re.search(r"\((.*)\)\s*$", ddl, re.S)
    assert inner, f"无法解析表 {table} 的建表语句"
    # ★ 必须按**顶层逗号**切分，不能按行：SQLite 的 DDL 常把多列写在一行
    #   （如 `role TEXT DEFAULT "user", disabled INTEGER DEFAULT 0, last_login REAL,`），
    #   按行切会把它当成"一个列定义"，生成的 MySQL DDL 直接语法错误。
    raw = inner.group(1)
    parts, buf, depth, quote = [], "", 0, None
    for ch in raw:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch; buf += ch; continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf); buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    lines = [re.sub(r"\s+", " ", p).strip() for p in parts]
    lines = [l for l in lines if l]

    cols: list[str] = []
    table_level: list[str] = []
    auto_col: str | None = None

    for line in lines:
        low = line.lower()

        # ① 自增主键
        if re.match(r"^\w+\s+integer\s+primary\s+key\s+autoincrement$", low):
            col = _unquote_ident(line.split()[0])
            cols.append(f"`{col}` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT")
            table_level.append(f"PRIMARY KEY (`{col}`)")
            auto_col = col
            continue

        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        col = _unquote_ident(parts[0])
        rest = parts[1]
        # 同一行里除首列外的其余列名也要加反引号（如 "value TEXT, updated DOUBLE"）
        rest = re.sub(r"(?<=,)\s*(\w+)\s+(?=(?:TEXT|VARCHAR|INT|BIGINT|DOUBLE|REAL|NUMERIC)\b)",
                      lambda m: " `" + m.group(1) + "` ", rest)

        # ② 列级 UNIQUE / PRIMARY KEY 提到表级
        is_unique = bool(re.search(r"\bunique\b", rest, re.I))
        is_pk = bool(re.search(r"\bprimary\s+key\b", rest, re.I))
        rest = re.sub(r"\s*\bunique\b", "", rest, flags=re.I)
        rest = re.sub(r"\s*\bprimary\s+key\b", "", rest, flags=re.I)

        # ③ 类型映射
        rest = re.sub(r"\bTEXT\b", "TEXT", rest, flags=re.I)
        if re.search(r"\bTEXT\b", rest, re.I):
            # TEXT 什么时候**必须**降成 VARCHAR：
            #   ① 被索引 —— MySQL 不允许对 TEXT 建无前缀索引；
            #   ② 带 DEFAULT —— MySQL 不允许 TEXT/BLOB/JSON 列有默认值
            #      （错误 1101: BLOB, TEXT, GEOMETRY or JSON column 'x'
            #       can't have a default value）。
            # ★ ② 这条极易漏：**MariaDB 允许** TEXT DEFAULT，所以只用 MariaDB
            #   测会全绿，一上真 MySQL 建表就炸。真实现场：宝塔 + MySQL，
            #   向导第 2 步选 MySQL 提交后报 1101（列 users.role）。
            if col in indexed:
                rest = re.sub(r"\bTEXT\b", f"VARCHAR({INDEX_STRLEN})", rest, count=1, flags=re.I)
            elif re.search(r"\bDEFAULT\b", rest, re.I):
                rest = re.sub(r"\bTEXT\b", f"VARCHAR({DEFAULT_STRLEN})", rest, count=1, flags=re.I)
        rest = re.sub(r"\bREAL\b", "DOUBLE", rest, flags=re.I)
        # 只把**独立的** INTEGER 转 INT（AUTOINCREMENT 已在上面处理）
        rest = re.sub(r"\bINTEGER\b", "INT", rest, flags=re.I)
        # 默认值引号
        rest = re.sub(r'"([^"]*)"', r"'\1'", rest)
        rest = re.sub(r"\bNOT\s+NULL\b", "NOT NULL", rest, flags=re.I)
        rest = re.sub(r"\s+", " ", rest).strip()

        # 列名一律加反引号：`key`/`value`/`status`/`days` 等在 MySQL 里是保留字或易冲突
        cols.append(f"`{col}` {rest}")

        if is_pk:
            table_level.append(f"PRIMARY KEY (`{col}`)")
        elif is_unique:
            table_level.append(f"UNIQUE KEY `uk_{table}_{col}` (`{col}`)")

    body = ",\n    ".join(cols + table_level)
    return (f"CREATE TABLE IF NOT EXISTS `{table}` (\n    {body}\n) "
            f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")


def _mysql_columns(mysql_tables: dict[str, str]):
    """逐列产出 (表, 列, 列定义)。

    ★ 必须审计**生成的 MySQL DDL**，不能审计 SQLite 真源 —— SQLite 那边一行
      写多列（`role TEXT DEFAULT "user", disabled INTEGER DEFAULT 0, …`），
      按行匹配会把邻居列的 DEFAULT 算到 TEXT 列头上。第一版就是这么误报的，
      对着 10 个无辜的列报警。
    """
    for table, ddl in mysql_tables.items():
        for line in ddl.split("\n"):
            m = re.match(r"\s*`(\w+)`\s+(.+?),?\s*$", line)
            if m:
                yield table, m.group(1), m.group(2)


def audit_text_defaults(mysql_tables: dict[str, str]) -> list[str]:
    """自检：MySQL DDL 里**不得**出现「TEXT 列带 DEFAULT」。

    MySQL 会以错误 1101 拒绝这种列（BLOB, TEXT, GEOMETRY or JSON column
    'x' can't have a default value），而 **MariaDB 允许** —— 只用 MariaDB 测
    永远发现不了（真实现场：宝塔 + MySQL，向导第 2 步选 MySQL 提交后报
    1101，列 users.role）。做成生成期断言，问题在开发期就红，而不是等装上才炸。
    """
    return [f"{t}.{c}" for t, c, rest in _mysql_columns(mysql_tables)
            if re.search(r"\bTEXT\b", rest, re.I) and re.search(r"\bDEFAULT\b", rest, re.I)]


def audit_indexed_not_text(mysql_tables: dict[str, str],
                           indexed: dict[str, set[str]]) -> list[str]:
    """自检：被索引的列不得是 TEXT/BLOB（MySQL 错误 1170）。

    MySQL/MariaDB 都不允许 TEXT 直接进 key spec：
        1170 BLOB/TEXT column 'x' used in key specification without a key length
    真实现场：`email_verifications` 的真源是**一行多列** ——
        id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE, ...
    旧的 indexed_columns() 按行取 `line.split()[0]`，把 UNIQUE 记在了 `id` 头上，
    token_hash 漏掉、没降成 VARCHAR，建表直接 1170。改成按列切分后正常，
    这道断言则保证以后不会再漏。
    """
    bad: list[str] = []
    for t, c, rest in _mysql_columns(mysql_tables):
        col = c.strip("`")
        if col in indexed.get(t, set()) and re.search(r"\b(TEXT|BLOB)\b", rest, re.I):
            bad.append(f"{t}.{col}")
    return bad


def audit_default_fits(mysql_tables: dict[str, str]) -> list[str]:
    """自检：VARCHAR(n) 的默认值不能比 n 还长（否则写入即被截断/报错）。"""
    bad: list[str] = []
    for t, c, rest in _mysql_columns(mysql_tables):
        m = re.search(r"VARCHAR\((\d+)\)", rest, re.I)
        d = re.search(r"DEFAULT\s+'([^']*)'", rest)
        if m and d and len(d.group(1)) > int(m.group(1)):
            bad.append(f"{t}.{c}（默认值 {len(d.group(1))} 字 > VARCHAR({m.group(1)})）")
    return bad

def sqlite_index_to_mysql(sql: str) -> str:
    """CREATE INDEX IF NOT EXISTS → MySQL 无 IF NOT EXISTS，靠 Ddl 层 try/catch。"""
    return re.sub(r"CREATE INDEX IF NOT EXISTS", "CREATE INDEX", sql, flags=re.I)


def split_top_commas(body: str) -> list[str]:
    """按**顶层逗号**把建表体切成一个个列/约束定义。

    ★ 必须切分，不能按行取 —— SQLite 真源常常**一行写多列**：
        id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE,
        username TEXT NOT NULL, ...
      按行处理时 `line.split()[0]` 只拿到第一个列名，UNIQUE / PRIMARY KEY 会被
      错记到它头上，真正被索引的 token_hash 则漏掉、得不到 VARCHAR 长度，
      于是 MySQL 建表报 1170
      （BLOB/TEXT column 'token_hash' used in key specification without a key length）。
      真实现场：宝塔 + MySQL，向导第 2 步建表失败。
    """
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


def indexed_columns(tables: dict[str, str], indexes: list[str]) -> dict[str, set[str]]:
    """算出每张表里「被索引的列」——用于决定哪些 TEXT 必须给长度。

    来源有三处，漏掉任何一处都会在建表时炸：
      · 列级 PRIMARY KEY / UNIQUE（来自列定义）
      · 表级 PRIMARY KEY (...) / UNIQUE (...)
      · INDEX_SQLITE 里的 CREATE INDEX 列

    ★ 一律按**列**判断，不按行 —— 真源经常一行多列（见 split_top_commas 的说明）。
    """
    out: dict[str, set[str]] = {t: set() for t in tables}

    for table, ddl in tables.items():
        body = ddl[ddl.index("(") + 1: ddl.rindex(")")]
        for item in split_top_commas(body):
            s = item.strip()
            if not s:
                continue
            low = s.lower()
            if low.startswith("primary key") or low.startswith("unique"):
                # 表级约束：PRIMARY KEY (`a`, `b`) / UNIQUE (`c`)
                m = re.search(r"\((.*?)\)", s, re.S)
                if m:
                    for c in m.group(1).split(","):
                        c = c.strip().strip("`\"[]").strip()
                        if c:
                            out[table].add(c)
                continue
            # 列定义：`name` TYPE ... [PRIMARY KEY|UNIQUE]
            cols_in_item = [x for x in re.findall(r"`(\w+)`", s)]
            names = cols_in_item or [re.sub(r"^[`\"\[]|[`\"\]]$", "", s.split()[0])]
            if "primary key" in low or re.search(r"\bunique\b", low):
                # 约束属于**本项**；一行多列时 split_top_commas 已切开，所以本项只谈一列
                out[table].add(names[0])

    for sql in indexes:
        m = re.search(r"\bON\s+(?:`?)(\w+)(?:`?)\s*\((.*?)\)", sql, re.I | re.S)
        if not m:
            continue
        table, cols = m.group(1), m.group(2)
        if table in out:
            for c in cols.split(","):
                c = c.strip().strip("`\"[]").strip()
                if c:
                    out[table].add(c)
    return out


def render_mysql_const(tables: dict[str, str], indexes: list[str]) -> str:
    idx = indexed_columns(tables, indexes)
    def esc(s: str) -> str:
        """PHP 单引号字符串里必须转义反斜杠与单引号 —— 
        DEFAULT 'user' 这类写法不转义会直接把 Schema.php 变成语法错误。"""
        return s.replace("\\", "\\\\").replace("'", "\\'")

    lines = []
    for name, ddl in tables.items():
        mysql = sqlite_table_to_mysql(name, ddl, idx[name])
        lines.append(f"        '{name}' => '{esc(mysql)}',")
    idx_lines = [f"        '{esc(sqlite_index_to_mysql(s))}'," for s in indexes]
    return ("    /**\n"
            "     * MySQL 建表语句 —— **由 php/scripts/gen_mysql_schema.py 从 SQLITE 生成**，\n"
            "     * 不要手改：改 SQLITE 后重新生成，并用 --check 校验同步。\n"
            "     */\n"
            "    public const MYSQL = [\n"
            + "\n".join(lines) + "\n    ];\n\n"
            "    /** MySQL 索引语句（同上，由生成器派生）。 */\n"
            "    public const INDEX_MYSQL = [\n"
            + "\n".join(idx_lines) + "\n    ];\n")


def main() -> int:
    check = "--check" in sys.argv
    src = SCHEMA_PHP.read_text(encoding="utf-8")
    tables, indexes = parse_sqlite_ddl(src)
    if not tables:
        print("✗ 没解析出任何建表语句", file=sys.stderr)
        return 2
    generated = render_mysql_const(tables, indexes)

    # ---- 自检：这两条只有真 MySQL 才会报，MariaDB 会放过，所以必须静态兜住 ----
    _idx = indexed_columns(tables, indexes)
    _gen = {t: sqlite_table_to_mysql(t, d, _idx.get(t, set())) for t, d in tables.items()}
    bad_td = audit_text_defaults(_gen)
    if bad_td:
        print("✗ 生成的 MySQL DDL 里有「TEXT 列带 DEFAULT」—— MySQL 会以 1101 拒绝：\n    "
              + "\n    ".join(bad_td)
              + "\n  修法：别动生成器，改这里的类型映射（TEXT→VARCHAR），"
                "或在 SQLite 真源里把该列改成非 TEXT。", file=sys.stderr)
        return 3
    bad_ix = audit_indexed_not_text(_gen, _idx)
    if bad_ix:
        print("✗ 被索引的列还是 TEXT —— MySQL 会以 1170 拒绝（key specification "
              "without a key length）：\n    " + "\n    ".join(bad_ix), file=sys.stderr)
        return 3
    bad_fit = audit_default_fits(_gen)
    if bad_fit:
        print("✗ 默认值比列长度还长：\n    " + "\n    ".join(bad_fit), file=sys.stderr)
        return 3

    m = re.search(r"(\n    /\*\*\n     \* MySQL 建表语句.*?public const INDEX_MYSQL = \[.*?\n    \];\n)",
                  src, re.S)
    if m:
        new = src[:m.start(1)] + "\n" + generated + src[m.end(1):]
    else:
        # 首次：插到类的末尾（最后一个 } 之前）
        pos = src.rstrip().rfind("}")
        new = src[:pos] + "\n" + generated + "\n" + src[pos:]

    if check:
        if new != src:
            print("✗ MySQL schema 与 SQLite 真源不同步 —— 请运行："
                  "python3 php/scripts/gen_mysql_schema.py", file=sys.stderr)
            return 1
        print(f"✓ MySQL schema 与 SQLite 同源（{len(tables)} 张表 / {len(indexes)} 个索引）")
        return 0

    if new == src:
        print(f"✓ 无需改动（{len(tables)} 张表 / {len(indexes)} 个索引）")
        return 0
    SCHEMA_PHP.write_text(new, encoding="utf-8")
    print(f"✓ 已生成 {SCHEMA_PHP.relative_to(ROOT)}（{len(tables)} 张表 / {len(indexes)} 个索引）")
    return 0


if __name__ == "__main__":
    sys.exit(main())