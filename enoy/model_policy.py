"""Per-request Codex model routing and public progress (never private reasoning)."""
import time
import re

from .routing import classify
from .redact import redact


def choose(cfg, purpose, objective="", file_count=0, repair=False):
    settings = cfg['codex']
    model, effort = settings.get('model', ''), settings.get('effort', '')
    reason = 'Planning, analysis and review use the planning model.'
    quick_request = re.search(r"\b(quick review|quick code review|triage|route this task)\b", str(objective).lower())
    routable = purpose in ('edit', 'native_edit', 'ask') or (quick_request and purpose in ('review', 'lead_review', 'plan', 'lead'))
    if settings.get('adaptive_routing', True) and routable and not repair:
        task = classify(objective, file_count)
        lowered = objective.lower()
        if re.search(r"\b(game concept|game mechanics|progression|game architecture|game design|mechanics design)\b", lowered):
            return {'model': 'claude-opus-5-5', 'effort': 'high' if re.search(r"\b(complex|difficult|large|multiple systems)\b", lowered) else 'medium',
                    'reason': 'Game design, mechanics and progression use Claude Opus at medium effort; complex architecture uses high effort.'}
        if re.search(r"\b(difficult debugging|debugging|final review|difficult review)\b", lowered):
            return {'model': 'claude-opus-5-5', 'effort': 'high', 'reason': 'Difficult debugging and final review use Claude Opus at high effort.'}
        # Role stack supplied in Enoy's recommended model matrix. These explicit
        # specialties take precedence over the generic small-edit optimization.
        if re.search(r"\b(blender|geometry nodes|procedural model)\b", lowered) and re.search(r"\b(visual|inspect|look|screenshot|feedback|scene)\b", lowered):
            model, effort = 'gpt-6.1-terra', 'medium'
            reason = 'Visual inspection and Blender feedback use GPT-6.1 Terra at medium effort.'
        elif re.search(r"\b(exceptional|unresolved|still failing|still broken|same error again)\b", lowered):
            model, effort = 'gpt-6-astra', 'high'
            reason = 'An unresolved technical problem uses GPT-6 Astra at high effort.'
        elif quick_request:
            model, effort = 'gpt-6-luna', 'low'
            reason = 'Quick review and routing use GPT-6 Luna at low effort.'
        elif re.search(r"\b(difficult|complex|advanced)\b", lowered) and re.search(r"\b(procedural model|geometry nodes|procedural modeling)\b", lowered):
            model, effort = 'claude-opus-5-5', 'high'
            reason = 'Difficult procedural modeling uses Claude Opus or GPT-6.1 Sol at high effort.'
        elif re.search(r"\b(luau|roblox|blender python|python automation|vfx scripting|vfx script)\b", lowered):
            model, effort = settings.get('model', 'gpt-6.1-sol'), 'medium'
            reason = 'Daily Luau, Blender Python and Roblox VFX scripting use GPT-6.1 Sol at medium effort.'
        elif re.search(r"\b(vfx|visual effects|art direction|visual feedback)\b", lowered):
            model, effort = 'sonnet', 'medium'
            reason = 'VFX art direction and visual feedback use Claude Sonnet at medium effort.'
        elif (purpose in ('edit', 'native_edit') and task.kind == 'edit' and task.complexity == 'low' and task.risk != 'high'
                and file_count <= 2 and task.scope != 'multiple modules' and not task.needs.get('multi_step')):
            model = settings.get('execution_model', 'gpt-6-luna')
            effort = settings.get('execution_effort', 'low')
            reason = 'Simple, repetitive implementation uses GPT-6 Luna at low effort.'
        else:
            reason = 'Keep the planning model for analysis or work that is complex, broad, or risky.'
    elif repair:
        if re.search(r"\b(exceptional|unresolved|still failing|still broken|same error again)\b", str(objective).lower()):
            model, effort = 'gpt-6-astra', 'high'
            reason = 'An unresolved technical repair uses GPT-6 Astra at high effort.'
        else:
            reason = 'A repair or retry needs the planning model to investigate the failure.'
    if not settings.get('adaptive_routing', True):
        reason = 'Automatic model routing is disabled; use the configured Codex model.'
    return {'model': model, 'effort': effort, 'reason': reason}


def claude_profile(cfg, objective='', purpose='', repair=False):
    """Choose a Claude role profile from the supplied model stack."""
    settings = cfg['claude']
    text = (str(objective) + ' ' + str(purpose)).lower()
    model = settings.get('model', 'claude-opus-5-5')
    effort = settings.get('effort', 'medium')
    reason = 'Claude Opus handles concept and complex design work at medium effort.'
    if re.search(r"\b(vfx|visual effects|art direction|visual feedback)\b", text):
        model, effort = 'sonnet', 'medium'
        reason = 'VFX art direction uses Claude Code’s current Sonnet alias at medium effort.'
    elif repair or 'review' in str(purpose).lower() or re.search(r"\b(debug|debugging|unresolved|still failing|same error)\b", text):
        effort = 'high'
        reason = 'Difficult debugging and final review use Claude Opus at high effort.'
    elif re.search(r"\b(architect|architecture|system design|game concept|mechanics|progression|geometry nodes|procedural model)\b", text):
        effort = 'high' if re.search(r"\b(complex|difficult|large|multiple systems)\b", text) else 'medium'
        reason = 'Game design and architecture use Claude Opus at medium or high effort by scope.'
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
