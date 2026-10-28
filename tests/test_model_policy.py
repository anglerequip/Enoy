import json
import sys

import pytest

from enoy import config, model_policy, native
from enoy.backends.base import BackendError, ChatMessage, ChatRequest, ChatResult, CODEX
from enoy.backends.codex import CodexBackend
from test_lead import delegate, plan_task
from test_orchestrator import open_project


@pytest.mark.parametrize('purpose', ['lead', 'plan', 'review', 'lead_review'])
def test_planning_and_review_keep_sol_medium(purpose):
    cfg, _ = config.validate({})
    selected = model_policy.choose(cfg, purpose, 'Add a simple function', 1)
    assert (selected['model'], selected['effort']) == ('gpt-6.1-sol', 'medium')


@pytest.mark.parametrize('description,files,repair,expected', [
    ('Rename a label in README.md', 1, False, 'gpt-6-luna'),
    ('Add a simple multiply function', 1, False, 'gpt-6-luna'),
    ('Implement OAuth authentication', 1, False, 'gpt-6.1-sol'),
    ('Refactor the architecture across all modules', 8, False, 'gpt-6.1-sol'),
    ('Add a simple multiply function', 1, True, 'gpt-6.1-sol'),
    ('Rename a label', 5, False, 'gpt-6.1-sol'),
])
def test_execution_routes_by_scope_risk_and_repairs(description, files, repair, expected):
    cfg, _ = config.validate({})
    selected = model_policy.choose(cfg, 'native_edit', description, files, repair)
    assert selected['model'] == expected
    assert selected['effort'] == ('low' if expected == 'gpt-6-luna' else 'medium')


def test_routing_can_be_disabled_and_does_not_mutate_settings():
    cfg, _ = config.validate({})
    before = json.dumps(cfg)
    model_policy.choose(cfg, 'edit', 'Rename a label', 1)
    assert json.dumps(cfg) == before
    cfg['codex']['adaptive_routing'] = False
    assert model_policy.choose(cfg, 'edit', 'Rename a label', 1)['model'] == 'gpt-6.1-sol'


def test_cli_receives_request_model_and_effort_without_global_mutation():
    settings = {'model':'gpt-6.1-sol', 'effort':'medium'}
    request = ChatRequest([], model='gpt-6-luna', effort='high')
    args = CodexBackend(settings).build_args(request=request)
    assert args[args.index('--model') + 1] == 'gpt-6-luna'
    assert 'model_reasoning_effort="high"' in args
    assert settings == {'model':'gpt-6.1-sol', 'effort':'medium'}


@pytest.mark.parametrize('fallback', [False, True])
def test_real_orchestrator_routes_phases_and_records_visible_progress(orc_factory, project, fallback):
    observed = []
    def worker(request):
        observed.append((request.model, request.effort))
        request.on_thinking('Inspecting calculator.py before applying the small change.')
        (project / 'calculator.py').write_text((project / 'calculator.py').read_text() + '\n\ndef multiply(a,b):\n    return a*b\n')
        return ChatResult('Implemented.', CODEX, request.model, session_id='execution-session')
    def planner(request):
        observed.append((request.model, request.effort))
        return delegate(plan_task(model=CODEX))
    def review(request):
        observed.append((request.model, request.effort))
        return ChatResult('Reviewed.\nNEXT: done', CODEX, request.model)
    replies = [planner]
    if fallback:
        replies.append(BackendError('model_missing', 'Execution model is not available for this account'))
    replies += [worker, review]
    orc = orc_factory(qwen=replies, agent__native_tools=True)
    task = orc.start_task(open_project(orc, project), 'Add a simple multiply function')
    assert task['status'] == 'completed', task['summary']
    assert observed == [('gpt-6.1-sol','medium'),
                        ('gpt-6.1-sol','medium') if fallback else ('gpt-6-luna','low'),
                        ('gpt-6.1-sol','medium')]
    requests = orc.backends[CODEX].requests
    assert requests[1].model == 'gpt-6-luna'
    txs = [json.loads(t['usage']) for t in orc.db.transmissions(task['id']) if t['usage']]
    assert any('Inspecting calculator.py' in ' '.join(t.get('progress', [])) for t in txs)
    assert all(t['model'] and t['effort'] for t in txs)


def test_session_profiles_preserve_planner_and_worker_sessions(orc_factory, project):
    orc = orc_factory(qwen=['MODE: answer\nReady.'], agent__native_tools=True)
    task = orc.start_task(open_project(orc, project), 'Inspect')
    for model, effort, sid in [('gpt-6.1-sol','medium','planner'), ('gpt-6-luna','low','worker')]:
        profile = {'model':model,'effort':effort}
        options = native.options(orc, task['id'], CODEX, 'native_edit', profile)
        native.remember(orc, task['id'], CODEX, options, ChatResult('Done', CODEX, model, session_id=sid))
    assert native.options(orc, task['id'], CODEX, 'ask')['resume_session'] == 'planner'
    assert native.options(orc, task['id'], CODEX, 'native_edit', {'model':'gpt-6-luna','effort':'low'})['resume_session'] == 'worker'


def test_public_progress_excludes_raw_reasoning_items(tmp_path):
    script = tmp_path / 'fake.py'
    script.write_text('''import sys,json
sys.stdin.read()
for item in [dict(type='reasoning',text='PRIVATE_THOUGHT'),dict(type='agent_message',text='Inspecting files.'),dict(type='agent_message',text='Done.')]:
 print(json.dumps(dict(type='item.completed',item=item)),flush=True)
''')
    seen = []
    CodexBackend({'timeout_s':10}, [sys.executable,str(script)]).chat(
        ChatRequest([ChatMessage('user','Inspect')], on_thinking=seen.append))
    assert seen and 'PRIVATE_THOUGHT' not in ''.join(seen)


def test_starting_model_applies_to_new_message_and_resets_on_followup(orc_factory, project):
    orc = orc_factory(qwen=['MODE: answer\nReady.', 'MODE: answer\nAgain.', 'MODE: answer\nDefault.'])
    cfg_before = json.dumps(orc.cfg)
    task = orc.start_task(open_project(orc, project), 'Hello',
                          starting_model={'model': 'custom-light', 'effort': 'low'})
    assert task['status'] == 'completed', task['summary']
    assert orc.backends[CODEX].requests[-1].model == 'custom-light'
    assert orc.backends[CODEX].requests[-1].effort == 'low'
    orc.send_message(task['id'], 'Hello again', starting_model={'model': 'another-model', 'effort': 'medium'})
    assert orc.backends[CODEX].requests[-1].model == 'another-model'
    orc.send_message(task['id'], 'Back to default')
    assert orc.backends[CODEX].requests[-1].model == orc.cfg['codex']['model']
    assert json.dumps(orc.cfg) == cfg_before


@pytest.mark.parametrize('value', ['', {'model': ''}, {'model': 'bad model'}, {'model': 'valid', 'effort': 'bad'}])
def test_starting_model_rejects_invalid_profiles(value):
    with pytest.raises(ValueError):
        model_policy.validate_start(value)


def test_chat_starts_light_on_each_turn_and_respects_override(orc_factory):
    from enoy import paths
    orc = orc_factory(qwen=['MODE: answer\nHello.', 'MODE: answer\nAgain.', 'MODE: answer\nCustom.'])
    project = orc.open_project(str(paths.general_chat_dir()))
    task = orc.start_task(project['id'], 'Hello')
    assert task['status'] == 'completed', task['summary']
    request = orc.backends[CODEX].requests[-1]
    assert (request.model, request.effort) == ('gpt-6-luna', 'low')
    orc.send_message(task['id'], 'Hello again')
    request = orc.backends[CODEX].requests[-1]
    assert (request.model, request.effort) == ('gpt-6-luna', 'low')
    orc.send_message(task['id'], 'Use my chosen model', starting_model={'model': 'custom-model', 'effort': 'medium'})
    assert orc.backends[CODEX].requests[-1].model == 'custom-model'
