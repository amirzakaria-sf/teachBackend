from __future__ import annotations

from django.conf import settings


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault('X-Content-Type-Options', 'nosniff')
        response.setdefault('X-Frame-Options', 'SAMEORIGIN')
        if request.path.startswith('/api/student/interactive-visualizers'):
            response.setdefault('Content-Security-Policy', settings.VISUALIZER_CSP)
        return response
