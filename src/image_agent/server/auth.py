"""Bearer authentication shared by all business routers."""
import secrets
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .settings import ServerError

bearer = HTTPBearer(auto_error=False, scheme_name="ServerBearer", description="服务部署者提供的 Bearer Token")


async def require_token(request: Request,
                        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    expected = request.app.state.settings.token
    if (credentials is None or credentials.scheme.lower() != "bearer" or
            not secrets.compare_digest(credentials.credentials.encode(), expected.encode())):
        raise ServerError(401, "unauthorized", "需要有效的 Bearer Token")
