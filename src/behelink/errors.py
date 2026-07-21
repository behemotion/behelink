"""RFC 9457 application/problem+json errors."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_TYPE_BY_STATUS = {
    400: "usage",
    401: "auth",
    403: "auth",
    404: "not_found",
    405: "usage",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
}


class ProblemError(Exception):
    def __init__(
        self,
        status: int,
        type_: str,
        title: str,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(detail or title)
        self.status = status
        self.type = type_
        self.title = title
        self.detail = detail
        self.headers = headers


def problem_response(
    status: int,
    type_: str,
    title: str,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {"type": type_, "title": title, "status": status}
    if detail:
        body["detail"] = detail
    return JSONResponse(
        body,
        status_code=status,
        media_type="application/problem+json",
        headers=headers,
    )


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(exc.status, exc.type, exc.title, exc.detail, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        type_ = _TYPE_BY_STATUS.get(exc.status_code, "internal")
        return problem_response(exc.status_code, type_, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        return problem_response(422, "validation_error", "Validation Error", detail)
