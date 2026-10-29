"""Browser executable asset types, independent of Windows registry MIME entries."""

from pathlib import PurePosixPath

from fastapi.staticfiles import StaticFiles


class WebStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        media_type = {
            ".js": "text/javascript; charset=utf-8",
            ".mjs": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".html": "text/html; charset=utf-8",
        }.get(PurePosixPath(path).suffix.lower())
        if media_type and response.status_code in {200, 206, 304}:
            response.headers["content-type"] = media_type
        return response
