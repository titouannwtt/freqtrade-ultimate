from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.exceptions import HTTPException
from starlette.responses import FileResponse, Response

from freqtrade.rpc.api_server.ui_static import static_response


router_ui = APIRouter(include_in_schema=False, tags=["Web UI"])


@router_ui.get("/favicon.ico")
async def favicon():
    return FileResponse(str(Path(__file__).parent / "ui/favicon.ico"))


@router_ui.get("/fallback_file.html")
async def fallback():
    return FileResponse(str(Path(__file__).parent / "ui/fallback_file.html"))


@router_ui.get("/ui_version")
async def ui_version():
    from freqtrade.commands.deploy_ui import read_ui_version

    uibase = Path(__file__).parent / "ui/installed/"
    version = read_ui_version(uibase)

    return {
        "version": version if version else "not_installed",
    }


@router_ui.get("/{rest_of_path:path}")
async def index_html(rest_of_path: str, request: Request) -> Response:
    """
    Emulate path fallback to index.html.
    """
    if rest_of_path.startswith("api") or rest_of_path.startswith("."):
        raise HTTPException(status_code=404, detail="Not Found")
    uibase = (Path(__file__).parent / "ui/installed/").resolve()
    filename = (uibase / rest_of_path).resolve()
    accept_encoding = request.headers.get("accept-encoding")
    if_none_match = request.headers.get("if-none-match")
    # It's security relevant to check "relative_to".
    # Without this, Directory-traversal is possible.
    if filename.is_file() and filename.is_relative_to(uibase):
        try:
            return static_response(
                filename,
                rest_of_path,
                accept_encoding=accept_encoding,
                if_none_match=if_none_match,
            )
        except FileNotFoundError:
            pass  # Raced with an install-ui - fall through to index.html.

    index_file = uibase / "index.html"
    if not index_file.is_file():
        return FileResponse(str(uibase.parent / "fallback_file.html"))
    # Fall back to index.html, as indicated by vue router docs
    return static_response(
        index_file,
        "index.html",
        accept_encoding=accept_encoding,
        if_none_match=if_none_match,
    )
