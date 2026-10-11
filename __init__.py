"""Hermes plugin entry point. Importing it never starts the health service."""
import json
import os
from pathlib import Path

from .personal_health import register_tools


def register(ctx):
    home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    path = Path(os.environ.get('PERSONAL_AGENT_HEALTH_CONFIG',
                               str(home / 'personal-agent-health.json')))
    config = json.loads(path.read_text(encoding='utf-8'))
    register_tools(ctx, config)
    ctx.register_skill('personal-health',
                       Path(__file__).parent / 'skills' / 'personal-health' / 'SKILL.md')
