from abc import ABC, abstractmethod
from asyncio import iscoroutinefunction
from functools import wraps
from inspect import signature
from types import CoroutineType
from typing import Callable, Dict, List, NamedTuple, Union, Optional
from robyn.authentication import AuthenticationHandler, AuthenticationNotConfiguredError

from robyn.robyn import FunctionInfo, HttpMethod, MiddlewareType, Request, Response
from robyn import status_codes
from robyn.ws import WS
import inspect


class Route(NamedTuple):
    route_type: HttpMethod
    route: str
    function: FunctionInfo
    is_const: bool


class RouteMiddleware(NamedTuple):
    middleware_type: MiddlewareType
    route: str
    function: FunctionInfo


class GlobalMiddleware(NamedTuple):
    middleware_type: MiddlewareType
    function: FunctionInfo


class BaseRouter(ABC):
    @abstractmethod
    def add_route(*args) -> Union[Callable, CoroutineType, WS]:
        ...


class Router(BaseRouter):
    def __init__(self) -> None:
        super().__init__()
        self.routes: List[Route] = []

    def _format_response(self, res):
        response = {}
        if isinstance(res, dict):
            status_code = res.get("status_code", status_codes.HTTP_200_OK)
            headers = res.get("headers", {"Content-Type": "text/plain"})
            body = res.get("body", "")

            if type(status_code) != int:
                status_code = int(status_code)  # status_code can potentially be string

            response = Response(status_code=status_code, headers=headers, body=body)
            file_path = res.get("file_path")
            if file_path is not None:
                response.file_path = file_path
        elif isinstance(res, Response):
            response = res
        elif isinstance(res, bytes):
            response = Response(
                status_code=status_codes.HTTP_200_OK,
                headers={"Content-Type": "application/octet-stream"},
                body=res,
            )
        else:
            response = Response(
                status_code=status_codes.HTTP_200_OK,
                headers={"Content-Type": "text/plain"},
                body=str(res).encode("utf-8"),
            )
        return response

    def add_route(
        self,
        route_type: HttpMethod,
        endpoint: str,
        handler: Callable,
        is_const: bool,
        dependencies: Dict[str,any],
        exception_handler: Optional[Callable],
    ) -> Union[Callable, CoroutineType]:
        @wraps(handler)
        async def async_inner_handler(*args):
            try:
                response = self._format_response(await handler(*args))
            except Exception as err:
                if exception_handler is None:
                    raise
                response = self._format_response(exception_handler(err))
            return response

        @wraps(handler)
        def inner_handler(*args):
            depToPass = ""
            signatureObj = (inspect.signature(handler))
            argsFromHandler = signatureObj.parameters.values() #holds all args from func args
            specificDep = dependencies.get(endpoint, dependencies["all"])
            depToPass = []
            if specificDep != dependencies["all"]: #if specific dep dict is not the one accessible to all
                for a in argsFromHandler:
                    if a.name in specificDep: 
                        depToPass.append(specificDep[a.name])
                    elif a.name not in specificDep: #check all dict
                        for dep_dict in dependencies["all"]:
                            if a.name in dep_dict:
                                depToPass.append(dep_dict[a.name])
            else: #if specified dep dict is the same as the "all" dict
                for a in argsFromHandler:
                    for dep_dict in dependencies["all"]:
                        if a.name in dep_dict:
                            depToPass.append(dep_dict[a.name])
            try:
                if depToPass: #specificDep != dependencies["all"] and inSpecific is True:#depToPass != "" and inSpecific is True:
                    response = self._format_response(handler(*args, *depToPass))#next(iter(depToPass.values()))))
                else:
                    response = self._format_response(handler(*args,))
            except Exception as err:
                if exception_handler is None:
                    raise
                response = self._format_response(exception_handler(err))
            return response

        number_of_params = len(signature(handler).parameters)
        if iscoroutinefunction(handler):
            function = FunctionInfo(async_inner_handler, True, number_of_params)
            self.routes.append(Route(route_type, endpoint, function, is_const))
            return async_inner_handler
        else:
            function = FunctionInfo(inner_handler, False, number_of_params)
            self.routes.append(Route(route_type, endpoint, function, is_const))
            return inner_handler

    def get_routes(self) -> List[Route]:
        return self.routes


class MiddlewareRouter(BaseRouter):
    def __init__(self) -> None:
        super().__init__()
        self.global_middlewares: List[GlobalMiddleware] = []
        self.route_middlewares: List[RouteMiddleware] = []
        self.authentication_handler: Optional[AuthenticationHandler] = None

    def set_authentication_handler(self, authentication_handler: AuthenticationHandler):
        self.authentication_handler = authentication_handler

    def add_route(
        self, middleware_type: MiddlewareType, endpoint: str, handler: Callable
    ) -> Callable:
        number_of_params = len(signature(handler).parameters)
        function = FunctionInfo(handler, iscoroutinefunction(handler), number_of_params)
        self.route_middlewares.append(
            RouteMiddleware(middleware_type, endpoint, function)
        )
        return handler

    def add_auth_middleware(self, endpoint: str):
        """
        This method adds an authentication middleware to the specified endpoint.
        """

        def inner(handler):
            def inner_handler(request: Request, *args):
                if not self.authentication_handler:
                    raise AuthenticationNotConfiguredError()
                identity = self.authentication_handler.authenticate(request)
                if identity is None:
                    return self.authentication_handler.unauthorized_response
                request.identity = identity
                return request

            self.add_route(MiddlewareType.BEFORE_REQUEST, endpoint, inner_handler)
            return inner_handler

        return inner

    # These inner functions are basically a wrapper around the closure(decorator) being returned.
    # They take a handler, convert it into a closure and return the arguments.
    # Arguments are returned as they could be modified by the middlewares.
    def add_middleware(
        self, middleware_type: MiddlewareType, endpoint: Optional[str]
    ) -> Callable[..., None]:
        def inner(handler):
            @wraps(handler)
            async def async_inner_handler(*args):
                return await handler(*args)

            @wraps(handler)
            def inner_handler(*args):
                return handler(*args)

            if endpoint is not None:
                if iscoroutinefunction(handler):
                    self.add_route(middleware_type, endpoint, async_inner_handler)
                else:
                    self.add_route(middleware_type, endpoint, inner_handler)
            else:
                if iscoroutinefunction(handler):
                    self.global_middlewares.append(
                        GlobalMiddleware(
                            middleware_type,
                            FunctionInfo(
                                async_inner_handler,
                                True,
                                len(signature(async_inner_handler).parameters),
                            ),
                        )
                    )
                else:
                    self.global_middlewares.append(
                        GlobalMiddleware(
                            middleware_type,
                            FunctionInfo(
                                inner_handler,
                                False,
                                len(signature(inner_handler).parameters),
                            ),
                        )
                    )

        return inner

    def get_route_middlewares(self) -> List[RouteMiddleware]:
        return self.route_middlewares

    def get_global_middlewares(self) -> List[GlobalMiddleware]:
        return self.global_middlewares


class WebSocketRouter(BaseRouter):
    def __init__(self) -> None:
        super().__init__()
        self.routes = {}

    def add_route(self, endpoint: str, web_socket: WS) -> None:
        self.routes[endpoint] = web_socket

    def get_routes(self) -> Dict[str, WS]:
        return self.routes
