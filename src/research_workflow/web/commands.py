"""工作台可提交的研究命令白名单。

浏览器/客户端只能提交“命令名 + 结构化参数”；这里负责校验并拼成 run_research.py 的 argv
（列表传参，不经 shell）。任何文件参数只接受本项目已上传文件的 ID，绝不接受服务器路径。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

MONTH = re.compile(r"^\d{4}-\d{2}$")
MONTHS = re.compile(r"^\d{4}-\d{2}(,\d{4}-\d{2}){0,59}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:\-/ ]{1,200}$")  # 节点、槽位等标识符
MAX_TEXT = 4000


@dataclass(frozen=True)
class Arg:
    name: str
    kind: str  # str | int | float | bool | list | file | enum | month | months | ident
    required: bool = False
    choices: tuple[str, ...] = ()
    lo: float | None = None
    hi: float | None = None
    live: bool = False  # 需要访问外部供应商/本机浏览器的选项


@dataclass(frozen=True)
class CommandSpec:
    name: str
    args: tuple[Arg, ...]
    needs: str = "editor"  # 最低项目角色
    timeout: int = 3600
    check: Callable[[dict[str, Any]], None] | None = None
    # 由服务端注入的输出路径参数名（如 output），客户端不可指定
    server_output: str | None = None


def _need(flag: str, *names: str) -> Callable[[dict[str, Any]], None]:
    def check(a: dict[str, Any]) -> None:
        if not a.get(flag) and not any(a.get(n) for n in names):
            raise ValueError(f"缺少必要参数：需要 {names} 之一，或启用 {flag}")
    return check


SPECS: dict[str, CommandSpec] = {s.name: s for s in [
    CommandSpec("ingest", (
        Arg("file", "list"), Arg("month", "month"), Arg("node", "list"), Arg("top_n", "int", lo=1, hi=500),
        Arg("no_metric_dedupe", "bool"), Arg("dedupe_title", "bool"), Arg("live", "bool", live=True),
        Arg("keyword", "str"), Arg("months", "months"), Arg("page_size", "int", lo=1, hi=100),
        Arg("max_pages", "int", lo=1, hi=20)), check=_need("live", "file")),
    CommandSpec("nodes-discover", (Arg("file", "file"), Arg("live", "bool", live=True), Arg("keyword", "str"),
                                   Arg("month", "month"), Arg("max_pages", "int", lo=1, hi=20)),
                check=_need("live", "file")),
    CommandSpec("nodes-confirm", (Arg("node", "list", required=True), Arg("primary", "ident"),
                                  Arg("allow_unlisted", "bool"))),
    CommandSpec("enrich", (Arg("live", "bool", live=True), Arg("details_file", "file"),
                           Arg("concurrency", "int", lo=1, hi=10)), check=_need("live", "details_file")),
    CommandSpec("classify", (Arg("label", "list", required=True), Arg("dimension", "ident"), Arg("rule", "list"),
                             Arg("provider", "enum", choices=("rules", "llm")), Arg("model", "ident"),
                             Arg("batch_size", "int", lo=1, hi=100), Arg("min_bullets_coverage", "float", lo=0, hi=100))),
    CommandSpec("analyze", (Arg("new_products_file", "file"), Arg("sales_threshold", "int", lo=0),
                            Arg("consecutive_months", "int", lo=1, hi=24), Arg("early_window", "int", lo=1, hi=24),
                            Arg("survival_threshold", "int", lo=0), Arg("promotion_threshold", "int", lo=0),
                            Arg("promotion_window", "int", lo=1, hi=24), Arg("pair", "list"), Arg("period", "list"))),
    CommandSpec("new-products-select", (Arg("as_of", "month"), Arg("window_days", "int", lo=1, hi=365),
                                        Arg("include_excluded", "bool")), server_output="output"),
    CommandSpec("table-export", (Arg("table", "int", required=True, lo=1, hi=3),
                                 Arg("format", "enum", choices=("xlsx", "csv"))), needs="viewer", server_output="output"),
    CommandSpec("table-import", (Arg("table", "int", required=True, lo=1, hi=1), Arg("file", "file", required=True),
                                 Arg("allow_month_change", "bool"))),
    CommandSpec("evidence-add", (
        Arg("slot", "ident", required=True), Arg("claim", "str", required=True), Arg("value", "str"),
        Arg("unit", "ident"), Arg("period", "ident"), Arg("geography", "ident"), Arg("scope", "ident"),
        Arg("source_name", "str", required=True), Arg("source_url", "str"), Arg("source_file", "file"),
        Arg("confidence", "enum", choices=("high", "medium", "low")))),
    CommandSpec("finding-add", (Arg("module", "ident", required=True), Arg("text", "str", required=True),
                                Arg("evidence", "list", required=True))),
    CommandSpec("reconcile", (Arg("reference", "file", required=True),), needs="viewer", server_output="output"),
    CommandSpec("dossier", (), server_output="output"),
]}

# resolve 单独建模：必须由具名用户裁决，并写审计。
RESOLVE_ARGS = (Arg("checkpoint", "ident", required=True), Arg("approve", "bool"), Arg("reject", "bool"),
                Arg("edit", "list"), Arg("select", "str"), Arg("edits_file", "file"))


RESOLVE_SPEC = CommandSpec("resolve", RESOLVE_ARGS)


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def _validate_value(arg: Arg, value: Any, resolve_file: Callable[[str], str]) -> list[str]:
    """返回该参数对应的 argv 片段。resolve_file 把上传 ID 换成服务器路径（并校验归属）。"""
    flag = _flag(arg.name)
    k = arg.kind
    if k == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{arg.name} 必须是布尔值")
        return [flag] if value else []
    if k == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{arg.name} 必须是整数")
    elif k == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{arg.name} 必须是数字")
    if k in {"int", "float"}:
        if arg.lo is not None and value < arg.lo or arg.hi is not None and value > arg.hi:
            raise ValueError(f"{arg.name} 超出允许范围 [{arg.lo}, {arg.hi}]")
        return [f"{flag}={value}"]
    if not isinstance(value, str):
        raise ValueError(f"{arg.name} 必须是字符串")
    if len(value) > MAX_TEXT:
        raise ValueError(f"{arg.name} 过长")
    if k == "month" and not MONTH.match(value):
        raise ValueError(f"{arg.name} 必须是 YYYY-MM")
    if k == "months" and not MONTHS.match(value):
        raise ValueError(f"{arg.name} 必须是逗号分隔的 YYYY-MM（最多 60 个）")
    if k == "ident" and not TOKEN.match(value):
        raise ValueError(f"{arg.name} 含非法字符或过长")
    if k == "enum" and value not in arg.choices:
        raise ValueError(f"{arg.name} 必须是 {arg.choices}")
    if k == "file":
        return [f"{flag}={resolve_file(value)}"]
    if "\x00" in value:
        raise ValueError(f"{arg.name} 含非法字符")
    return [f"{flag}={value}"]


def build_argv(
    spec: CommandSpec, params: dict[str, Any], *, resolve_file: Callable[[str], str],
    output_path: str | None = None, allow_live: bool = True, forced: dict[str, str] | None = None,
    args: tuple[Arg, ...] | None = None,
) -> list[str]:
    """校验参数并生成 argv；未知参数一律拒绝。"""
    arg_specs = args if args is not None else spec.args
    by_name = {a.name: a for a in arg_specs}
    unknown = set(params) - set(by_name)
    if unknown:
        raise ValueError(f"不支持的参数: {sorted(unknown)}")
    if spec.check:
        spec.check(params)
    argv: list[str] = []
    for arg in arg_specs:
        if arg.name == "format":
            continue  # 仅用于决定服务端输出文件后缀
        value = params.get(arg.name)
        if value is None or value == [] or value == "":
            if arg.required:
                raise ValueError(f"缺少必要参数: {arg.name}")
            continue
        if arg.live and value is True and not allow_live:
            raise ValueError("该服务未开启 live 供应商访问")
        if arg.kind == "list":
            if not isinstance(value, list) or len(value) > 200:
                raise ValueError(f"{arg.name} 必须是列表（最多 200 项）")
            for item in value:
                argv += _validate_value(Arg(arg.name, "file" if arg.name == "file" else "str"), item, resolve_file)
        else:
            argv += _validate_value(arg, value, resolve_file)
    for flag, val in (forced or {}).items():
        argv.append(f"{_flag(flag)}={val}")
    if spec.server_output:
        if output_path is None:
            raise ValueError("内部错误：缺少服务端输出路径")
        argv.append(f"--{spec.server_output}={output_path}")
    return argv
