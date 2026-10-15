"""Agent execution, isolated evaluation and trace inspection CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .agent import AgentLoop, BudgetLimits
from .analysis import analyze_experiment, render_report
from .bundle import export_experiment, verify_bundle
from .context import (
    FullHistory, PythonSymbolContext, RepositoryContext, RepositoryIndex,
    RetrievalSummaryContext, SlidingWindow, SummaryContext,
)
from .docker_runtime import DockerCommandTool, DockerRunner, DockerShellTool, resolve_image_id
from .deepseek import DEFAULT_MODEL, DeepSeekModel, load_api_key, load_pricing
from .evidence import seal_experiment, verify_anchor
from .eval import evaluate, load_case
from .experiment import ExperimentProgress, load_plan, run_experiment
from .experiment_verify import verify_experiment
from .model import CommandModel, ScriptedModel
from .mcp_stdio import McpRemoteTool, McpStdioClient
from .reprice import reprice_experiment
from .resume import workspace_digest
from .runtime import (
    ConfiguredCommandTool, GitTool, PatchFileTool, ReadFileTool, ShellTool,
    ToolRuntime, WorkspaceFiles, WorkspacePolicy, WriteFileTool,
)
from .session import JsonlSession
from .trace import divergence_report, first_difference
from .trace_verify import verify_trace


CONTEXT_NAMES = (
    "full_history", "sliding_window", "summary", "repository", "retrieval_summary", "python_symbols"
)


def _print_batch_progress(update: ExperimentProgress) -> None:
    elapsed = str(timedelta(seconds=round(update.elapsed_s)))
    run_elapsed = str(timedelta(seconds=round(update.run_elapsed_s)))
    eta = "estimating" if update.eta_s is None else f"~{timedelta(seconds=round(update.eta_s))}"
    outcome = ""
    if update.phase == "completed":
        outcome = f" success={'yes' if update.success else 'no'}"
    print(
        f"[{update.index}/{update.total}] {update.phase.upper()}"
        f" case={update.case_id} strategy={update.strategy} repetition={update.repetition + 1}"
        f" run={run_elapsed} elapsed={elapsed} ETA={eta}{outcome}",
        file=sys.stderr, flush=True,
    )


def _context(name: str, workspace: Path):
    if name == "full_history":
        return FullHistory()
    if name == "sliding_window":
        return SlidingWindow()
    if name == "summary":
        return SummaryContext()
    if name == "repository":
        return RepositoryContext(RepositoryIndex(workspace))
    if name == "retrieval_summary":
        return RetrievalSummaryContext(RepositoryIndex(workspace))
    if name == "python_symbols":
        return PythonSymbolContext(workspace)
    raise ValueError(f"unknown context strategy: {name}")


def _argv_json(raw: str) -> list[str]:
    value = json.loads(raw)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError("command must be a nonempty JSON array of strings")
    return value


def _model(args: argparse.Namespace, cwd: Path, seed: int | None = None):
    if args.script is not None:
        responses = json.loads(args.script.read_text(encoding="utf-8"))
        if not isinstance(responses, list):
            raise ValueError("script must contain a JSON array")
        return ScriptedModel(responses)
    if args.deepseek:
        pricing = load_pricing(args.deepseek_pricing) if args.deepseek_pricing else None
        return DeepSeekModel(
            load_api_key(args.deepseek_env_file), args.deepseek_model,
            pricing=pricing,
            max_output_tokens=args.deepseek_max_output_tokens,
        )
    return CommandModel(_argv_json(args.model_argv), cwd, args.model_env, seed=seed)


def _add_model_options(command: argparse.ArgumentParser, script_map: bool = False) -> None:
    group = command.add_mutually_exclusive_group(required=True)
    group.add_argument("--script", type=Path, help="JSON array of scripted model responses")
    if script_map:
        group.add_argument("--script-map", type=Path, help="JSON mapping case IDs to script files; deterministic fixtures only")
    group.add_argument("--model-argv", help="JSON array for a trusted model bridge process")
    group.add_argument("--deepseek", action="store_true", help="use DeepSeek Chat Completions API")
    command.add_argument("--model-env", action="append", default=[], help="environment variable passed to the model bridge")
    command.add_argument("--deepseek-model", default=DEFAULT_MODEL, help="DeepSeek model name")
    command.add_argument("--deepseek-env-file", type=Path, default=Path(".env"), help="file containing DEEPSEEK_API_KEY")
    command.add_argument("--deepseek-pricing", type=Path, help="JSON price snapshot in USD per 1M tokens")
    command.add_argument("--deepseek-max-output-tokens", type=int, help="maximum generated tokens per DeepSeek request")


def _load_mcp(config_path: Path, stack: ExitStack, session: JsonlSession):
    config_path = config_path.resolve(strict=True)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    servers = config.get("servers")
    if not isinstance(servers, list):
        raise ValueError("MCP config must contain a servers array")
    tools = []
    for server in servers:
        if not isinstance(server, dict) or not isinstance(server.get("name"), str):
            raise ValueError("invalid MCP server config")
        argv = server.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
            raise ValueError("MCP argv must be a nonempty array of strings")
        cwd = config_path.parent / server.get("cwd", ".")
        client = McpStdioClient(server["name"], argv, cwd, server.get("env", []))
        try:
            client.start()
        except Exception as exc:
            session.append("mcp_start_error", 0, {"server": server["name"], "error": str(exc)})
            raise
        stack.callback(client.close)
        session.append("mcp_server_started", 0, {
            "server": server["name"], "protocol": "2026-07-28",
            "tools": [definition["name"] for definition in client.tool_definitions],
        })
        effects = server.get("side_effects", {})
        if not isinstance(effects, dict):
            raise ValueError("side_effects must be an object")
        for definition in client.tool_definitions:
            side_effect = effects.get(definition["name"], "write")
            if side_effect not in ("read", "write"):
                raise ValueError("MCP side_effect must be read or write")
            tools.append(McpRemoteTool(client, definition, side_effect))
    return tools


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-harness")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run an agent")
    run.add_argument("--task", required=True)
    _add_model_options(run)
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--traces", type=Path, default=Path(".runs"))
    run.add_argument("--context", default="full_history", choices=CONTEXT_NAMES)
    run.add_argument("--allow-writes", action="store_true")
    run.add_argument("--allow-command", action="append", default=[])
    run.add_argument("--docker-image", help="run shell/test/build commands in a preloaded Docker image")
    run.add_argument("--test-argv", help="JSON array for the run_tests tool")
    run.add_argument("--build-argv", help="JSON array for the build tool")
    run.add_argument("--mcp-config", action="append", type=Path, default=[], help="MCP stdio server config JSON")
    run.add_argument("--steps", type=int, default=20)
    run.add_argument("--tokens", type=int, default=50_000)
    run.add_argument("--tool-calls", type=int, default=40)
    run.add_argument("--wall-seconds", type=float, default=300)

    resume = commands.add_parser("resume", help="continue an interrupted run from a verified workspace checkpoint")
    resume.add_argument("--run-id", required=True)
    _add_model_options(resume)
    resume.add_argument("--workspace", type=Path, default=Path.cwd())
    resume.add_argument("--traces", type=Path, default=Path(".runs"))
    resume.add_argument("--context", default="full_history", choices=CONTEXT_NAMES)
    resume.add_argument("--allow-writes", action="store_true")
    resume.add_argument("--allow-command", action="append", default=[])
    resume.add_argument("--docker-image")
    resume.add_argument("--test-argv")
    resume.add_argument("--build-argv")
    resume.add_argument("--mcp-config", action="append", type=Path, default=[])
    resume.add_argument("--steps", type=int, default=20)
    resume.add_argument("--tokens", type=int, default=50_000)
    resume.add_argument("--tool-calls", type=int, default=40)
    resume.add_argument("--wall-seconds", type=float, default=300)

    evaluation = commands.add_parser("eval", help="run a fixed task and post-run tests in Docker")
    evaluation.add_argument("--manifest", type=Path, required=True)
    _add_model_options(evaluation)
    evaluation.add_argument("--traces", type=Path, default=Path(".runs"))
    evaluation.add_argument("--context", default="full_history", choices=CONTEXT_NAMES)

    batch = commands.add_parser("eval-batch", help="repeat context strategies and aggregate results")
    batch.add_argument("--plan", type=Path, required=True)
    _add_model_options(batch, script_map=True)
    batch.add_argument("--traces", type=Path, default=Path(".runs"))
    batch.add_argument("--output", type=Path, help="new experiment output directory")
    batch.add_argument("--model-label", help="provider and model version label for experiment reports")
    batch.add_argument("--max-estimated-cost-usd", type=float,
                       help="conservative preflight estimate and observed estimate guard; not a billing limit")

    reprice = commands.add_parser("reprice", help="estimate DeepSeek costs from saved experiment traces")
    reprice.add_argument("--experiment", type=Path, required=True)
    reprice.add_argument("--pricing", type=Path, required=True, help="USD per 1M token price snapshot JSON")
    reprice.add_argument("--pricing-model", required=True, help="model that the price snapshot applies to")
    reprice.add_argument("--output", type=Path, help="new output directory")
    reprice.add_argument("--pricing-source", help="source URL or note recorded with the price snapshot")

    diff = commands.add_parser("trace-diff", help="show the first observable difference")
    diff.add_argument("left", type=Path)
    diff.add_argument("right", type=Path)
    diff.add_argument("--report", action="store_true", help="show context, behavior, and accounting differences separately")
    verify = commands.add_parser("trace-verify", help="audit a saved trace and its artifacts")
    verify.add_argument("trace", type=Path)
    experiment_verify = commands.add_parser("experiment-verify", help="audit a batch experiment from saved traces")
    experiment_verify.add_argument("experiment", type=Path)
    experiment_verify.add_argument("--anchor", type=Path, help="independent SHA-256 anchor")
    experiment_seal = commands.add_parser("experiment-seal", help="save file hashes outside a complete experiment")
    experiment_seal.add_argument("experiment", type=Path)
    experiment_seal.add_argument("anchor", type=Path)
    bundle_export = commands.add_parser("experiment-export", help="archive a complete experiment and its trace evidence")
    bundle_export.add_argument("experiment", type=Path)
    bundle_export.add_argument("anchor", type=Path)
    bundle_export.add_argument("archive", type=Path)
    bundle_verify = commands.add_parser("experiment-bundle-verify", help="verify an archived experiment")
    bundle_verify.add_argument("archive", type=Path)
    bundle_verify.add_argument("--trusted-anchor", type=Path, help="separately stored anchor to compare with archive")
    experiment_report = commands.add_parser("experiment-report", help="compare strategies inside each case of a saved experiment")
    experiment_report.add_argument("experiment", type=Path)
    experiment_report.add_argument("--baseline", default="full_history", help="strategy every other strategy is compared against")
    experiment_report.add_argument("--json-output", type=Path, help="also write the machine-readable report to this path")
    args = parser.parse_args()

    if args.command == "experiment-verify":
        report = verify_anchor(args.experiment, args.anchor) if args.anchor else verify_experiment(args.experiment)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ok"]:
            raise SystemExit(1)
        return

    if args.command == "experiment-seal":
        print(json.dumps(seal_experiment(args.experiment, args.anchor), ensure_ascii=False, indent=2))
        return

    if args.command == "experiment-export":
        print(json.dumps(export_experiment(args.experiment, args.anchor, args.archive), ensure_ascii=False, indent=2))
        return

    if args.command == "experiment-bundle-verify":
        report = verify_bundle(args.archive, args.trusted_anchor)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ok"]:
            raise SystemExit(1)
        return

    if args.command == "experiment-report":
        report = analyze_experiment(args.experiment, args.baseline)
        if args.json_output:
            args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        print(render_report(report))
        return

    if args.command == "trace-verify":
        report = verify_trace(args.trace)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ok"]:
            raise SystemExit(1)
        return

    if args.command == "trace-diff":
        result = divergence_report(args.left, args.right) if args.report else first_difference(args.left, args.right)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "reprice":
        output = args.output or args.experiment / "pricing" / uuid4().hex[:8]
        summary = reprice_experiment(
            args.experiment, load_pricing(args.pricing), output,
            args.pricing_source, args.pricing_model,
        )
        print(json.dumps({"output": str(output), "summary": summary}, ensure_ascii=False, indent=2))
        return

    if args.command == "eval":
        source_case = load_case(args.manifest)
        case = replace(source_case, image=resolve_image_id(source_case.image))
        result = evaluate(
            case, _model(args, Path.cwd()),
            lambda workspace: _context(args.context, workspace), args.traces,
        )
        print(json.dumps({
            "case_id": result.case_id,
            "run_id": result.run.run_id,
            "agent_status": result.run.status.value,
            "success": result.success,
            "public_exit_code": result.public_exit_code,
            "holdout_exit_code": result.holdout_exit_code,
            "baseline_hash": result.baseline_hash,
            "trace": str(result.trace),
        }, ensure_ascii=False, indent=2))
        return

    if args.command == "eval-batch":
        if args.max_estimated_cost_usd is not None and not args.deepseek:
            parser.error("--max-estimated-cost-usd requires --deepseek")
        plan = load_plan(args.plan)
        unknown = set(plan.strategies) - set(CONTEXT_NAMES)
        if unknown:
            parser.error(f"unknown context strategies: {', '.join(sorted(unknown))}")
        output = args.output or args.traces / "experiments" / f"{plan.experiment_id}-{uuid4().hex[:8]}"
        scripts = None
        if args.script_map:
            raw_map = json.loads(args.script_map.read_text(encoding="utf-8"))
            if not isinstance(raw_map, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw_map.items()):
                parser.error("script map must be a JSON object mapping case IDs to script paths")
            scripts = {key: (args.script_map.parent / value).resolve(strict=True) for key, value in raw_map.items()}
            case_ids = {load_case(path).case_id for path in plan.cases}
            if set(scripts) != case_ids:
                parser.error(f"script map case IDs must match plan: {sorted(case_ids)}")
            model_spec = {
                "type": "script_map",
                "scripts_sha256": {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in scripts.items()},
                "script_paths": {key: str(path) for key, path in scripts.items()},
            }
            default_label = "script-map:deterministic"
        elif args.script:
            model_spec = {"type": "script", "script_sha256": hashlib.sha256(args.script.read_bytes()).hexdigest(),
                          "script_path": str(args.script.resolve(strict=True))}
            default_label = f"script:{args.script.name}"
        elif args.deepseek:
            model_spec = {"type": "deepseek", "model": args.deepseek_model,
                          "identity": f"deepseek:{args.deepseek_model}:https://api.deepseek.com",
                          "max_output_tokens": args.deepseek_max_output_tokens}
            default_label = f"deepseek:{args.deepseek_model}"
        else:
            model_spec = {"type": "command", "argv_sha256": hashlib.sha256(args.model_argv.encode()).hexdigest(),
                          "env_names": args.model_env}
            default_label = "command"
        pricing_snapshot = asdict(load_pricing(args.deepseek_pricing)) if args.deepseek and args.deepseek_pricing else None
        model_config_hash = hashlib.sha256(json.dumps(model_spec, sort_keys=True).encode()).hexdigest()
        def batch_model(case, strategy, repetition, seed):
            if scripts is not None:
                responses = json.loads(scripts[case.case_id].read_text(encoding="utf-8"))
                if not isinstance(responses, list):
                    raise ValueError(f"script for {case.case_id} must be a JSON array")
                return ScriptedModel(responses)
            return _model(args, Path.cwd(), seed)
        summary = run_experiment(
            plan,
            batch_model,
            _context, args.traces, output,
            args.model_label or default_label,
            model_config_hash,
            model_spec, pricing_snapshot,
            image_resolver=resolve_image_id,
            max_estimated_cost_usd=args.max_estimated_cost_usd,
            max_output_tokens=args.deepseek_max_output_tokens,
            progress=_print_batch_progress,
            progress_interval_s=30,
        )
        print(json.dumps({"output": str(output), "summary": summary}, ensure_ascii=False, indent=2))
        return

    workspace = args.workspace.resolve(strict=True)
    model = _model(args, workspace)
    protected_paths = (args.deepseek_env_file.resolve(),) if args.deepseek else ()
    files = WorkspaceFiles(workspace, protected_paths)
    session_context = (
        JsonlSession.open_existing(args.traces, args.run_id, secrets=getattr(model, "trace_secrets", ()))
        if args.command == "resume" else JsonlSession(args.traces, secrets=getattr(model, "trace_secrets", ()))
    )
    with session_context as session, ExitStack() as stack:
        tools = [ReadFileTool(files), WriteFileTool(files), PatchFileTool(files), GitTool(workspace, session)]
        docker = DockerRunner(args.docker_image, session) if args.docker_image else None
        if args.allow_command:
            tools.append(
                DockerShellTool(docker, workspace, set(args.allow_command)) if docker
                else ShellTool(workspace, set(args.allow_command), session)
            )
        for name, raw in (("run_tests", args.test_argv), ("build", args.build_argv)):
            if raw:
                argv = _argv_json(raw)
                tools.append(
                    DockerCommandTool(name, argv, docker, workspace) if docker
                    else ConfiguredCommandTool(name, argv, workspace, session)
                )
        for config_path in args.mcp_config:
            tools.extend(_load_mcp(config_path, stack, session))
        runtime = ToolRuntime(tools, WorkspacePolicy(args.allow_writes), session)
        loop = AgentLoop(
            model, _context(args.context, workspace), runtime, session,
            BudgetLimits(args.steps, args.tokens, args.tool_calls, args.wall_seconds),
            checkpoint=lambda: workspace_digest(workspace, args.traces),
        )
        result = loop.run(resume=True) if args.command == "resume" else loop.run(args.task)
        print(json.dumps({
            "run_id": result.run_id,
            "status": result.status.value,
            "reason": result.reason,
            "answer": result.answer,
            "trace": str(session.root / "trace.jsonl"),
            "usage": vars(result.usage),
        }, ensure_ascii=False, indent=2))
