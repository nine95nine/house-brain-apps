#!/usr/bin/env python3
"""Remove the tunnel API credential and ambient config before loading MCP code.

This minimizes inherited data; same-UID processes inside the App are still one
trust domain, not an OS security boundary between native parent and MCP child.
"""
import os

ROOT = '/opt/house-brain-ai-access/runtime'
ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp/house-brain-ai-access',
       'TMPDIR': '/tmp/house-brain-ai-access', 'LANG': 'C.UTF-8',
       'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
       'MAINTENANCE_READ_KEY': os.environ.get('MAINTENANCE_READ_KEY', ''),
       'HOUSE_BRAIN_TUNNEL_PRINCIPAL': 'owner.ha-access',
       'HOUSE_BRAIN_TUNNEL_SCOPES': 'engineering.read.status,engineering.read.platform,engineering.read.backup',
       'HOUSE_BRAIN_AI_CONTRACT': ROOT+'/contracts/house_brain_ai_read_plane.v2.json',
       'HOUSE_BRAIN_AI_KILL_SWITCH_VIEW': '/run/house-brain-ai-kill-switch'}
if __name__ == '__main__':
    os.chdir(ROOT)
    os.execve('/usr/local/bin/python3', ['python3', '-B', '-m', 'tools.house_brain_ai_tunnel_stdio'], ENV)
