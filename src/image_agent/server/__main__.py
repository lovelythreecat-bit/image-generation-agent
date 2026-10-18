"""Explicit single-process service and atomic configuration import commands."""
import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from .settings import ServerError, ServerSettings


async def import_config_file(path: Path, name: str, settings: ServerSettings | None = None):
    try:
        mapping = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(mapping, dict):
            raise ValueError("not an object")
    except (OSError, UnicodeError, ValueError):
        raise ServerError(422, "invalid_config_file", "无法将指定文件读取为配置 JSON 对象") from None
    if not name.strip():
        raise ServerError(422, "invalid_name", "配置方案名不能为空")
    settings = settings or ServerSettings.from_file()
    from .config_service import ConfigService
    from .database import Database
    from .secrets import SecretCipher
    database = Database(settings.database_url)
    try:
        await database.initialize()
        service = ConfigService(database.session_factory, SecretCipher(settings.secret_key))
        return await service.import_config(mapping, name)
    finally:
        await database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="image-agent 单进程 HTTP 服务")
    parser.add_argument("--config", type=Path, default=Path("server.config.json"),
                        help="服务配置 JSON；默认当前目录的 server.config.json")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="启动 HTTP 服务（默认命令）")
    serve.add_argument("--host", help="显式覆盖监听地址；默认 127.0.0.1")
    serve.add_argument("--port", type=int, help="显式覆盖监听端口；默认 8000")
    load = commands.add_parser("import-config", help="原子导入明确指定的配置文件")
    load.add_argument("--file", type=Path, required=True)
    load.add_argument("--name", required=True)
    for subcommand in (serve, load):
        subcommand.add_argument("--config", type=Path, default=argparse.SUPPRESS,
                                help="服务配置 JSON；默认当前目录的 server.config.json")
    args = parser.parse_args(argv)
    try:
        if args.command == "import-config":
            settings = ServerSettings.from_file(args.config)
            result = asyncio.run(import_config_file(args.file, args.name, settings))
            print(json.dumps({"agent_config_id": result["id"]}, ensure_ascii=False))
        else:
            import uvicorn

            from .app import create_app
            settings = ServerSettings.from_file(args.config)
            overrides = {key: getattr(args, key) for key in ("host", "port")
                         if getattr(args, key, None) is not None}
            if overrides:
                settings = replace(settings, **overrides)
            uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                        workers=1, access_log=False,
                        loop=('image_agent.server.event_loop:selector_loop_factory'
                              if sys.platform == 'win32' else 'auto'))
        return 0
    except ServerError as exc:
        print(json.dumps({"code": exc.code, "message": "服务命令执行失败；请检查配置与数据库状态"},
                         ensure_ascii=False), file=sys.stderr)
    except ValueError:
        print("服务配置无效；请检查 server.config.json 或 --config 指定的 JSON 文件、"
              "必填字段、MySQL 地址、Fernet 密钥和参数", file=sys.stderr)
    except Exception:
        print("服务命令执行失败；请检查配置与数据库状态", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
