"""Transactional configuration management and the sole DB-to-core config boundary."""

from pydantic import SecretStr, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from image_agent.config import AgentConfig, ServiceConfig
from image_agent.errors import ConfigurationError

from .config_repository import ConfigRepository
from .db_models import AgentConfigRecord, AgentModelRoute, ModelConfig, ProviderService
from .schemas import (
    AgentConfigCreate,
    AgentConfigPatch,
    AgentParameters,
    ModelConfigCreate,
    ModelConfigPatch,
    ServiceCreate,
    ServicePatch,
)
from .settings import ServerError


def invalid():
    return ServerError(422, "invalid_config", "配置无效，请检查字段、模型协议及阶段路由")


def conflict():
    return ServerError(409, "config_conflict", "配置版本已改变或配置仍被引用")


def validate_input(schema, data):
    try:
        value = data if isinstance(data, schema) else schema.model_validate(data)
        result = value.model_dump(exclude_unset=True)
        for key, item in result.items():
            if isinstance(item, SecretStr):
                result[key] = item.get_secret_value()
        return result
    except (ValidationError, TypeError, ValueError):
        raise invalid() from None


def timestamps(row):
    return {
        "id": row.id,
        "enabled": row.enabled,
        "version": row.version,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def public_service(row):
    return {
        **timestamps(row),
        **{
            key: getattr(row, key)
            for key in ("name", "base_url", "protocol", "generation_path", "edit_path", "chat_path")
        },
        "has_api_key": bool(row.api_key_encrypted),
    }


def public_model(row):
    return {
        **timestamps(row),
        **{key: getattr(row, key) for key in ("service_id", "model", "description")},
    }


class ConfigService:
    def __init__(self, session_factory, cipher):
        self.session_factory = session_factory
        self.cipher = cipher

    async def _transaction(self, operation):
        try:
            async with self.session_factory() as session, session.begin():
                return await operation(session)
        except IntegrityError:
            raise conflict() from None
        except SQLAlchemyError:
            raise ServerError(503, "database_unavailable", "数据库暂时不可用") from None

    @staticmethod
    def _version(row, fields):
        if fields.pop("version") != row.version:
            raise conflict()
        row.version += 1

    @staticmethod
    def _apply(row, fields, nullable=()):
        for key, value in fields.items():
            if value is None and key not in nullable:
                raise invalid()
            setattr(row, key, value)

    def _service_fields(self, fields):
        try:
            core = ServiceConfig.model_validate(
                {key: value for key, value in fields.items() if key not in {"name", "enabled"}}
            )
            core.credential()
            return core
        except (ValidationError, ConfigurationError):
            raise invalid() from None

    async def create_service(self, data):
        fields = validate_input(ServiceCreate, data)

        async def operation(session):
            return public_service(await self._create_service(session, fields))

        return await self._transaction(operation)

    async def _create_service(self, session, fields):
        core = self._service_fields(fields)
        row = ProviderService(
            name=fields["name"],
            enabled=fields.get("enabled", True),
            base_url=core.base_url,
            protocol=core.protocol,
            generation_path=core.generation_path,
            edit_path=core.edit_path,
            chat_path=core.chat_path,
            api_key_encrypted=self.cipher.encrypt(core.credential()),
        )
        session.add(row)
        await session.flush()
        return row

    async def update_service(self, identifier, data):
        fields = validate_input(ServicePatch, data)

        async def operation(session):
            repo = ConfigRepository(session)
            row = await repo.get(ProviderService, identifier)
            self._version(row, fields)
            referenced = await repo.referenced(ModelConfig, ModelConfig.service_id, identifier)
            if referenced and (
                fields.get("enabled") is False
                or ("protocol" in fields and fields["protocol"] != row.protocol)
            ):
                raise conflict()
            combined = {
                key: getattr(row, key)
                for key in (
                    "name",
                    "enabled",
                    "base_url",
                    "protocol",
                    "generation_path",
                    "edit_path",
                    "chat_path",
                )
            }
            combined["api_key"] = fields.get("api_key", self.cipher.decrypt(row.api_key_encrypted))
            combined.update(fields)
            core = self._service_fields(combined)
            if "api_key" in fields:
                row.api_key_encrypted = self.cipher.encrypt(fields.pop("api_key"))
            self._apply(row, fields, {"generation_path", "edit_path"})
            row.base_url = core.base_url
            await session.flush()
            return public_service(row)

        return await self._transaction(operation)

    async def create_model_config(self, data):
        fields = validate_input(ModelConfigCreate, data)

        async def operation(session):
            return public_model(await self._create_model(session, fields))

        return await self._transaction(operation)

    async def _create_model(self, session, fields):
        service = await ConfigRepository(session).get(ProviderService, fields["service_id"])
        if not service.enabled:
            raise conflict()
        row = ModelConfig(**fields)
        session.add(row)
        await session.flush()
        return row

    async def update_model_config(self, identifier, data):
        fields = validate_input(ModelConfigPatch, data)

        async def operation(session):
            repo = ConfigRepository(session)
            row = await repo.get(ModelConfig, identifier)
            self._version(row, fields)
            if fields.get("enabled") is False and await repo.referenced(
                AgentModelRoute, AgentModelRoute.model_config_id, identifier
            ):
                raise conflict()
            self._apply(row, fields, {"description"})
            service = await repo.get(ProviderService, row.service_id)
            if not service.enabled:
                raise conflict()
            await session.flush()
            # A service change must preserve every existing route's protocol.
            agents = (
                (
                    await session.execute(
                        select(AgentModelRoute.agent_config_id).where(
                            AgentModelRoute.model_config_id == identifier
                        )
                    )
                )
                .scalars()
                .all()
            )
            for agent_id in sorted(set(agents)):
                await self._resolve(session, agent_id, require_enabled=False)
            return public_model(row)

        return await self._transaction(operation)

    async def _routes(self, session, identifier):
        return list(
            (
                await session.execute(
                    select(AgentModelRoute)
                    .where(AgentModelRoute.agent_config_id == identifier)
                    .order_by(AgentModelRoute.route_kind, AgentModelRoute.route_name)
                    .with_for_update()
                )
            ).scalars()
        )

    async def _public_agent(self, session, row):
        routes = await self._routes(session, row.id)
        return {
            **timestamps(row),
            "name": row.name,
            "parameters": row.parameters,
            "image_models": {
                route.route_name: route.model_config_id
                for route in routes
                if route.route_kind == "image_models"
            },
            "stages": {
                route.route_name: route.model_config_id
                for route in routes
                if route.route_kind == "stages"
            },
        }

    async def _replace_routes(self, session, identifier, fields):
        for kind in ("image_models", "stages"):
            if kind in fields:
                if fields[kind] is None:
                    raise invalid()
                await session.execute(
                    delete(AgentModelRoute).where(
                        AgentModelRoute.agent_config_id == identifier,
                        AgentModelRoute.route_kind == kind,
                    )
                )
                for name, model_id in fields[kind].items():
                    await ConfigRepository(session).get(ModelConfig, model_id)
                    session.add(
                        AgentModelRoute(
                            agent_config_id=identifier,
                            model_config_id=model_id,
                            route_kind=kind,
                            route_name=name,
                        )
                    )
        await session.flush()

    async def _create_agent(self, session, fields):
        row = AgentConfigRecord(
            name=fields["name"],
            parameters=fields.get("parameters", {}),
            enabled=fields.get("enabled", True),
        )
        session.add(row)
        await session.flush()
        await self._replace_routes(session, row.id, fields)
        await self._resolve(session, row.id, require_enabled=False)
        return await self._public_agent(session, row)

    async def create_agent_config(self, data):
        fields = validate_input(AgentConfigCreate, data)
        return await self._transaction(lambda session: self._create_agent(session, fields))

    async def update_agent_config(self, identifier, data):
        fields = validate_input(AgentConfigPatch, data)

        async def operation(session):
            row = await ConfigRepository(session).get(AgentConfigRecord, identifier)
            self._version(row, fields)
            self._apply(
                row,
                {
                    key: value
                    for key, value in fields.items()
                    if key not in {"image_models", "stages"}
                },
            )
            await self._replace_routes(session, identifier, fields)
            await self._resolve(session, identifier, require_enabled=False)
            await session.flush()
            return await self._public_agent(session, row)

        return await self._transaction(operation)

    async def _get(self, model, identifier, serialize):
        async def operation(session):
            row = await ConfigRepository(session).get(model, identifier)
            if serialize is None:
                return await self._public_agent(session, row)
            return serialize(row)

        return await self._transaction(operation)

    async def get_service(self, identifier):
        return await self._get(ProviderService, identifier, public_service)

    async def get_model_config(self, identifier):
        return await self._get(ModelConfig, identifier, public_model)

    async def get_agent_config(self, identifier):
        return await self._get(AgentConfigRecord, identifier, None)

    async def _list(self, model, serialize, limit, offset):
        if not 1 <= limit <= 100 or offset < 0:
            raise invalid()

        async def operation(session):
            rows = await ConfigRepository(session).list(model, limit, offset)
            if serialize is None:
                return [await self._public_agent(session, row) for row in rows]
            return [serialize(row) for row in rows]

        return await self._transaction(operation)

    async def list_services(self, limit=20, offset=0):
        return await self._list(ProviderService, public_service, limit, offset)

    async def list_model_configs(self, limit=20, offset=0):
        return await self._list(ModelConfig, public_model, limit, offset)

    async def list_agent_configs(self, limit=20, offset=0):
        return await self._list(AgentConfigRecord, None, limit, offset)

    async def _delete(self, model, identifier, reference_model=None, reference_column=None):
        async def operation(session):
            repo = ConfigRepository(session)
            row = await repo.get(model, identifier)
            if reference_model is not None and await repo.referenced(
                reference_model, reference_column, identifier
            ):
                raise conflict()
            if model is AgentConfigRecord:
                await session.execute(
                    delete(AgentModelRoute).where(AgentModelRoute.agent_config_id == identifier)
                )
            await session.delete(row)
            await session.flush()

        return await self._transaction(operation)

    async def delete_service(self, identifier):
        await self._delete(ProviderService, identifier, ModelConfig, ModelConfig.service_id)

    async def delete_model_config(self, identifier):
        await self._delete(
            ModelConfig, identifier, AgentModelRoute, AgentModelRoute.model_config_id
        )

    async def delete_agent_config(self, identifier):
        await self._delete(AgentConfigRecord, identifier)

    async def _resolve(self, session, identifier, *, require_enabled=True):
        repo = ConfigRepository(session)
        agent = await repo.get(AgentConfigRecord, identifier)
        if require_enabled and not agent.enabled:
            raise conflict()
        routes = await self._routes(session, identifier)
        models = {}
        services = {}
        for model_id in sorted({route.model_config_id for route in routes}):
            model = await repo.get(ModelConfig, model_id)
            if not model.enabled:
                raise conflict()
            models[model_id] = model
        for service_id in sorted({model.service_id for model in models.values()}):
            service = await repo.get(ProviderService, service_id)
            if not service.enabled:
                raise conflict()
            services[service_id] = service
        mapping = {**agent.parameters, "services": {}, "image_models": {}, "stages": {}}
        for service_id, service in services.items():
            mapping["services"][service_id] = {
                key: getattr(service, key)
                for key in ("base_url", "protocol", "generation_path", "edit_path", "chat_path")
            }
            mapping["services"][service_id]["api_key"] = self.cipher.decrypt(
                service.api_key_encrypted
            )
        for route in routes:
            model = models[route.model_config_id]
            mapping[route.route_kind][route.route_name] = {
                "service": model.service_id,
                "model": model.model,
            }
        try:
            # No from_env fallback. Validation is identical to the public core API.
            config = AgentConfig.from_mapping(mapping)
            config.validate_credentials()
        except ConfigurationError:
            raise invalid() from None
        versions = {
            "agent_config": {agent.id: agent.version},
            "model_configs": {key: row.version for key, row in models.items()},
            "services": {key: row.version for key, row in services.items()},
        }
        return config, versions

    async def resolve_agent_config_in_session(self, session, identifier):
        """Lock the graph until the caller commits the encrypted task snapshot."""
        return await self._resolve(session, identifier)

    async def resolve_agent_config(self, identifier):
        return await self._transaction(lambda session: self._resolve(session, identifier))

    async def import_config(self, mapping, name):
        try:
            config = AgentConfig.from_mapping(mapping)
            config.validate_credentials()
            runtime = {field: getattr(config, field) for field in AgentParameters.model_fields}
            if not config.services:
                key = config.openrouter_api_key.get_secret_value()
                services = {
                    "images": {
                        "base_url": config.openrouter_base_url,
                        "protocol": "openrouter_images",
                        "api_key": key,
                    },
                    "vision": {
                        "base_url": config.openrouter_base_url,
                        "protocol": "chat_completions",
                        "api_key": key,
                    },
                }
                image_models = {
                    alias: {"service": "images", "model": config.model_name(alias)}
                    for alias in ("pro", "fast", "base")
                }
                stages = {
                    "vision": {"service": "vision", "model": config.quality_model},
                    "describe_style": {"service": "vision", "model": config.style_model},
                }
            else:
                services = {
                    key: {
                        **service.model_dump(exclude={"api_key", "api_key_env"}),
                        "api_key": service.credential(),
                    }
                    for key, service in config.credential_services().items()
                }
                image_models = {
                    key: route.model_dump() for key, route in config.image_models.items()
                }
                stages = {
                    key: route.model_dump() for key, route in config.stages.items()
                    if key != "validate_evidence"
                }
            # Validate name and runtime before making any database changes.
            validate_input(
                AgentConfigCreate,
                {
                    "name": name,
                    "parameters": runtime,
                    "image_models": {key: "pending" for key in image_models},
                    "stages": {key: "pending" for key in stages},
                },
            )
        except (ConfigurationError, AttributeError, TypeError, ValueError):
            raise invalid() from None

        async def operation(session):
            service_ids = {}
            for key, fields in services.items():
                row = await self._create_service(
                    session, validate_input(ServiceCreate, {"name": key, **fields})
                )
                service_ids[key] = row.id
            model_ids = {}
            routes = {"image_models": {}, "stages": {}}
            for kind, raw in (("image_models", image_models), ("stages", stages)):
                for route_name, route in raw.items():
                    pair = (route["service"], route["model"])
                    if pair not in model_ids:
                        row = await self._create_model(
                            session,
                            validate_input(
                                ModelConfigCreate,
                                {"service_id": service_ids[pair[0]], "model": pair[1]},
                            ),
                        )
                        model_ids[pair] = row.id
                    routes[kind][route_name] = model_ids[pair]
            return await self._create_agent(
                session, {"name": name, "parameters": runtime, **routes}
            )

        return await self._transaction(operation)
