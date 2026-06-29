from __future__ import annotations

import re


ALLOWED_SCRIPT_HOSTS = (
    'https://cdnjs.cloudflare.com',
    'https://cdn.jsdelivr.net',
)

BANNED_PATTERNS = [
    r'document\.cookie',
    r'localStorage',
    r'sessionStorage',
    r'XMLHttpRequest',
    r'fetch\s*\(',
    r'window\.open',
    r'eval\s*\(',
    r'new\s+Function\s*\(',
]


def sanitize_visualizer_code(code: str) -> tuple[str, dict]:
    original = code or ''
    sanitized = original
    removed_patterns: list[str] = []

    for pattern in BANNED_PATTERNS:
        if re.search(pattern, sanitized, flags=re.IGNORECASE):
            sanitized = re.sub(pattern, '/* blocked */', sanitized, flags=re.IGNORECASE)
            removed_patterns.append(pattern)

    def _replace_script(match: re.Match[str]) -> str:
        tag = match.group(0)
        src_match = re.search(r'src=["\']([^"\']+)["\']', tag, flags=re.IGNORECASE)
        if not src_match:
            return tag
        src = src_match.group(1)
        if any(src.startswith(host) for host in ALLOWED_SCRIPT_HOSTS):
            return tag
        removed_patterns.append(f'external_script:{src}')
        return '<!-- blocked external script -->'

    sanitized = re.sub(r'<script\b[^>]*>.*?</script>', _replace_script, sanitized, flags=re.IGNORECASE | re.DOTALL)
    return sanitized.strip(), {
        'sanitized': sanitized != original,
        'removed_patterns': removed_patterns,
    }
