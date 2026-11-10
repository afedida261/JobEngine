import uuid

from django.utils.cache import patch_cache_control


class PrivateWorkspaceMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if "workspace" not in request.session:
            request.session["workspace"] = uuid.uuid4().hex
        response = self.get_response(request)
        patch_cache_control(response, private=True, no_store=True)
        response["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response
