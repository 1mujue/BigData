"""Fine-screening commands for the paper library."""

import json
from pathlib import Path
import subprocess
import sys

from . import screening


def register_commands(parser):
    commands = parser.add_subparsers(dest="fine_command", required=True)

    command = commands.add_parser("prepare-inputs", help="从粗筛名单和 PDF 入库记录准备正文")
    command.add_argument("--selection", type=Path, required=True)
    command.add_argument("--pdf-run", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)

    command = commands.add_parser("prepare", help="保存本次的论文、读者配置和评分规则")
    command.add_argument("--stage", choices=("triage", "detail"), required=True)
    command.add_argument("--input", type=Path, required=True)
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--reader", choices=("codex", "api"), default="codex")
    command.add_argument("--readers", type=Path, required=True)
    command.add_argument("--batch", type=Path, required=True)

    command = commands.add_parser("run", help="逐篇执行 Codex 或 API 评估")
    command.add_argument("--run", type=Path, required=True)
    command.add_argument("--runtime", type=Path)
    command.add_argument("--auth-file", type=Path, help="Codex 认证文件")
    command.add_argument("--codex", default="codex")
    command.add_argument("--env-file", type=Path, help="API 凭据文件")
    command.add_argument("--timeout", type=float, default=1800)
    command.add_argument("--limit", type=int)

    commands.add_parser("check", help="检查收取状态").add_argument("--run", type=Path, required=True)
    for name, help_text in (
        ("collect", "汇总有效评估记录"), ("analyze", "用 Spark SQL 分析评估结果"),
        ("export", "导出选定的论文和理由"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--run", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        if name == "analyze":
            command.add_argument("--master", default="local[2]")
        if name == "export":
            command.add_argument("--selection", type=Path, required=True)
    commands.add_parser("rubric", help="显示四维评分表")


def execute(args):
    code = 0
    try:
        match args.fine_command:
            case "prepare-inputs":
                from .prepare_inputs import prepare_inputs
                result = prepare_inputs(args.selection, args.pdf_run, args.output)
            case "prepare":
                result = screening.prepare(args.input, args.out, args.stage, batch_path=args.batch,
                                           readers_path=args.readers, reader=args.reader)
            case "run":
                from .run import run_readers
                args.runtime = args.runtime or args.run / "runtime"
                result = run_readers(args)
                if result.get("cancelled"):
                    code = 130
                elif not result.get("attempts_succeeded", result["complete"]):
                    code = 2
            case "check":
                _, result = screening.collect_records(args.run)
                code = 0 if result["complete"] else 2
            case "collect":
                result = screening.collect(args.run, args.out)
            case "analyze":
                from .analyze import analyze
                result = analyze(args.run, args.out, args.master)
            case "export":
                result = screening.export_selection(args.run, args.selection, args.out)
            case "rubric":
                rubric = screening.load_json(screening.RUBRIC_FILE)
                screening.validate_rubric(rubric)
                print(screening.render_rubric(rubric), end="")
                return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except (ValueError, KeyError, OSError, ImportError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
