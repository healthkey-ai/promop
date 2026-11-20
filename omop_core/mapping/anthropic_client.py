"""The one place an Anthropic client is built.

A key created at the organisation level is not scoped to a workspace, and the
API then rejects every request (HTTP 400) unless it names one in the
``anthropic-workspace-id`` header. ``ANTHROPIC_WORKSPACE_ID`` supplies it; a
workspace-scoped key needs nothing.
"""
from django.conf import settings


def anthropic_client():
    import anthropic

    workspace = getattr(settings, 'ANTHROPIC_WORKSPACE_ID', '')
    headers = {'anthropic-workspace-id': workspace} if workspace else None
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, default_headers=headers)
