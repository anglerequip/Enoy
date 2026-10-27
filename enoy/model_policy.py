"""Per-request Codex model routing and public progress (never private reasoning)."""
import time

from .routing import classify
from .redact import redact


def choose(cfg, purpose, objective="", file_count=0, repair=False):
    settings = cfg['codex']
    model, effort = settings.get('model', ''), settings.get('effort', '')
    reason = 'Planning, analysis and review use the planning model.'
    if settings.get('adaptive_routing', True) and purpose in ('edit', 'native_edit', 'ask') and not repair:
        task = classify(objective, file_count)
        if (task.kind == 'edit' and task.complexity == 'low' and task.risk != 'high'
                and file_count <= 2 and task.scope != 'multiple modules' and not task.needs.get('multi_step')):
            model = settings.get('execution_model', 'gpt-6-luna')
            effort = settings.get('execution_effort', 'high')
            reason = 'Bounded implementation task: use the economical execution model.'
        else:
            reason = 'Keep the planning model for analysis or work that is complex, broad, or risky.'
    elif repair:
        reason = 'A repair or retry needs the planning model to investigate the failure.'
    if not settings.get('adaptive_routing', True):
        reason = 'Automatic model routing is disabled; use the configured Codex model.'
    return {'model': model, 'effort': effort, 'reason': reason}


def announce(orc, task_id, provider, purpose, profile):
    info = dict(provider=provider, purpose=purpose, model=profile['model'], effort=profile['effort'],
                reason=profile['reason'], started=time.time(), progress=[profile['reason']])
    orc.db.update_task_state(task_id, active_agent=info)
    orc.bus.publish({'type': 'agent_status', 'task_id': task_id, 'agent': info})
    orc._act(task_id, 'model.selected', f"{info['model'] or 'Provider default'} · {info['effort'] or 'default effort'}: {info['reason']}", provider, purpose)


def progress(orc, task_id, text):
    text = redact(str(text)).strip()
    if not text:
        return
    if len(text) > 1500:
        text = text[:1500] + '\n[Progress preview shortened]'
    info = dict(orc.db.get_task(task_id)['state'].get('active_agent') or {})
    info['progress'] = (info.get('progress', []) + [text])[-20:]
    orc.db.update_task_state(task_id, active_agent=info)
    orc.bus.publish({'type': 'agent_status', 'task_id': task_id, 'agent': info})


def validate_start(value):
    """Validate an optional per-message starting Codex profile."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('Starting model must contain a model and effort.')
    model, effort = value.get('model'), value.get('effort', '')
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(c.isspace() for c in model.strip()):
        raise ValueError('Enter a model ID without spaces (up to 200 characters).')
    if effort not in ('', 'low', 'medium', 'high', 'xhigh', 'max'):
        raise ValueError('Choose a valid starting model effort.')
    return {'model': model.strip(), 'effort': effort}


def starting_profile(orc, task_id):
    value = orc.db.get_task(task_id)['state'].get('starting_model')
    if not value:
        return None
    orc.db.update_task_state(task_id, starting_model=None)
    return {**value, 'reason': 'Starting model selected for this message.'}


def chat_profile(orc, task_id, purpose):
    """Every ordinary Chat turn starts light, including clients without a UI override."""
    task = orc.db.get_task(task_id)
    project = orc.db.get_project(task['project_id'])
    if (purpose not in ('ask', 'chat', 'lead') or task['state'].get('team') or
            not (project.get('kind') == 'space' or orc.is_default_project(project))):
        return None
    return {'model': orc.cfg['codex'].get('chat_model', 'gpt-6-luna'),
            'effort': orc.cfg['codex'].get('chat_effort', 'low'),
            'reason': 'Chat uses the lighter model. Requests count toward your Codex allowance.'}
