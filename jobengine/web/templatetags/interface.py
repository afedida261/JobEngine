from django import template
from django.utils.safestring import mark_safe

register = template.Library()
ICONS = {
    "arrow_up": '<path d="M12 20V4m-6 6 6-6 6 6"/>',
    "arrow_left": '<path d="M20 12H4m6 6-6-6 6-6"/>',
    "overview": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "profile": '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
    "plan": '<path d="m3 5 6-2 6 2 6-2v16l-6 2-6-2-6 2zM9 3v16M15 5v16"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    "bookmark": '<path d="M6 4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v17l-6-4-6 4z"/>',
    "arrow": '<path d="M4 12h16m-6-6 6 6-6 6"/>',
    "upload": '<path d="M12 16V3m-5 5 5-5 5 5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "spark": '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z"/>',
    "chat": '<path d="M21 11a8 8 0 0 1-8 8H6l-4 3 1-7a8 8 0 0 1 8-12h2a8 8 0 0 1 8 8Z"/><path d="M7 9h10M7 13h6"/>',
    "settings": '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
    "folder": '<path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/>',
    "external": '<path d="M14 3h7v7m0-7L10 14M10 3H4a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-6"/>',
    "location": '<path d="M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/>',
    "lock": '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "plus": '<path d="M12 4v16M4 12h16"/>',
    "download": '<path d="M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4"/>',
    "leaf": '<path d="M5 19C-1 8 8 2 21 3c0 13-6 20-15 15M3 21 16 8"/>',
}


@register.filter
def words(value):
    return str(value).replace("_", " ").capitalize()


@register.simple_tag
def icon(name):
    return mark_safe(
        '<svg class="icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        + ICONS.get(name, ICONS["spark"])
        + "</svg>"
    )
