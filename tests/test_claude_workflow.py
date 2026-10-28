import json

import pytest

from enoy.backends.base import CODEX, CLAUDE, BackendError, ChatResult
from enoy.team import TeamRunner
from test_lead import delegate, plan_task
from test_orchestrator import open_project


def setup(orc_factory, project, codex, claude, **settings):
    orc = orc_factory(qwen=codex, claude=claude, agent__native_tools=True,
                      coordinator__claude_reviews=True, coordinator__claude_complex=True, **settings)
    return orc, open_project(orc, project)


def edit(project, calls, provider):
    def run(request):
        calls.append((provider, request.purpose, request.model))
        path = project / 'calculator.py'
        path.write_text(path.read_text() + '\n\ndef multiply(a,b):\n    return a*b\n')
        return ChatResult('Implemented with native tools.', provider, request.model or 'claude-opus-5-5')
    return run


@pytest.mark.parametrize('complex_work', [False, True])
def test_gpt_plans_claude_reviews_then_correct_provider_executes(orc_factory, project, complex_work):
    calls = []
    p = plan_task(model=CODEX)
    if complex_work:
        p.update(title='Refactor architecture across multiple modules', description='Refactor architecture across multiple modules')
    def planner(request):
        calls.append((CODEX, request.purpose, request.model))
        return delegate(p)
    def plan_review(request):
        assert not (project / 'calculator.py').read_text().endswith('return a*b\n')
        calls.append((CLAUDE, request.purpose, request.model))
        return 'PLAN_VERDICT: APPROVE\nThe plan is scoped and testable.'
    def final(request):
        calls.append((CLAUDE, request.purpose, request.model))
        assert '+def multiply' in request.messages[-1].content
        return 'Reviewed the observed changes.\nNEXT: done'
    provider = CLAUDE if complex_work else CODEX
    orc,pid = setup(orc_factory, project, [planner] + ([] if complex_work else [edit(project,calls,provider)]),
                    [plan_review] + ([edit(project,calls,provider)] if complex_work else []) + [final])
    task = orc.start_task(pid, 'Add a simple multiply function')
    assert task['status'] == 'completed', task['summary']
    assert [c[:2] for c in calls] == [(CODEX,'lead'),(CLAUDE,'plan_review'),(provider,'native_edit'),(CLAUDE,'lead_review')]
    if not complex_work:
        assert calls[2][2] == 'gpt-6-luna'
    assert orc.db.messages(task['id'])[-1]['backend'] == CLAUDE
    assert task['state']['team_run']['plan_review_status'] == 'reviewed'


def test_claude_can_revise_plan_before_execution(orc_factory, project):
    revised = plan_task(model=CODEX)
    revised.update(id='T2', title='Add a simple multiply function')
    review = 'PLAN_VERDICT: REVISE\nUse one bounded task.\n```json\n' + json.dumps({'tasks':[revised],'acceptance':['Multiply works']}) + '\n```'
    orc,pid = setup(orc_factory, project, [delegate(plan_task(model=CODEX)), edit(project,[],CODEX)],
                    [review, 'Reviewed.\nNEXT: done'])
    task = orc.start_task(pid, 'Add a simple multiply function')
    assert task['status'] == 'completed', task['summary']
    devs = [t for t in orc.db.team_tasks(task['id']) if t['role']=='developer']
    assert [d['plan_key'] for d in devs] == ['T2']


def test_malformed_claude_review_does_not_start_implementation(orc_factory, project):
    before=(project/'calculator.py').read_text()
    orc,pid = setup(orc_factory, project, [delegate(plan_task(model=CODEX))], ['Looks okay perhaps.'])
    task=orc.start_task(pid,'Add multiply')
    assert task['status']=='failed'
    assert (project/'calculator.py').read_text()==before
    assert not any(t['role']=='developer' for t in orc.db.team_tasks(task['id']))


def test_claude_quota_failure_is_visible_and_gpt_fallback_finishes(orc_factory, project):
    orc,pid = setup(orc_factory, project,
        [delegate(plan_task(model=CODEX)), edit(project,[],CODEX), 'GPT fallback reviewed.\nNEXT: done'],
        [BackendError('limit','Claude weekly quota reached')])
    task=orc.start_task(pid,'Add a simple multiply function')
    assert task['status']=='completed',task['summary']
    assert task['state']['team_run']['plan_review_status']=='unavailable'
    assert len(orc.backends[CLAUDE].requests)==1
    events=orc.db.activity(task['id'],200)
    assert any(e['kind']=='review.unavailable' and 'weekly quota' in e['text'] for e in events)
    assert any(e['kind']=='review.fallback' for e in events)
    assert orc.db.messages(task['id'])[-1]['backend']==CODEX


def test_retired_confirm_setting_does_not_pause_plan_review(orc_factory, project):
    # 090172b retired "Ask before sending to Claude": a requested run uses its providers without a second prompt.
    codex=[delegate(plan_task(model=CODEX)), edit(project,[],CODEX)]
    orc,pid=setup(orc_factory,project,codex,['PLAN_VERDICT: APPROVE\nScoped.','Claude reviewed.\nNEXT: done'],claude__confirm_before_send=True)
    task=orc.start_task(pid,'Add a simple multiply function')
    assert task['status']=='completed', task['summary']
    assert [r.purpose for r in orc.backends[CLAUDE].requests]==['plan_review','lead_review']


def test_first_repair_moves_from_luna_to_claude(orc_factory, project, monkeypatch):
    orc,pid=setup(orc_factory,project,[delegate(plan_task(model=CODEX)),edit(project,[],CODEX)],
                   ['PLAN_VERDICT: APPROVE\nScoped.','Reviewed.\nNEXT: done'])
    task=orc.start_task(pid,'Add a simple multiply function')
    assert task['status']=='completed'
    runner=TeamRunner(orc,task['id'])
    monkeypatch.setattr(runner,'continue_run',lambda:None)
    runner._repair('A verification test failed',[])
    repair=next(t for t in orc.db.team_tasks(task['id']) if t['plan_key']=='FIX1')
    assert repair['provider']==CLAUDE


def test_final_claude_quota_failure_falls_back_to_gpt(orc_factory, project):
    orc,pid=setup(orc_factory,project,
        [delegate(plan_task(model=CODEX)),edit(project,[],CODEX),'GPT reviewed after quota failure.\nNEXT: done'],
        ['PLAN_VERDICT: APPROVE\nScoped.',BackendError('limit','Weekly quota reached during final review')])
    task=orc.start_task(pid,'Add a simple multiply function')
    assert task['status']=='completed',task['summary']
    assert orc.backends[CLAUDE].requests[-1].purpose=='lead_review'
    assert orc.db.messages(task['id'])[-1]['backend']==CODEX
    assert any(e['kind']=='review.fallback' and 'quota' in e['text'] for e in orc.db.activity(task['id'],200))


def test_full_access_general_chat_can_delegate_to_claude(orc_factory, project):
    from enoy import paths
    p=plan_task(model=CODEX)
    p.update(title='Refactor architecture across multiple modules',description='Refactor architecture across multiple modules')
    orc=orc_factory(qwen=[delegate(p)],claude=['PLAN_VERDICT: APPROVE\nScoped.',edit(project,[],CLAUDE),'Reviewed.\nNEXT: done'],
                    agent__native_tools=True,agent__access_mode='full',coordinator__claude_reviews=True,coordinator__claude_complex=True)
    pid=orc.open_project(str(paths.general_chat_dir()))['id']
    task=orc.start_task(pid,'Refactor the application architecture')
    assert task['status']=='completed',task['summary']
    assert any(r.native_tools and r.purpose=='native_edit' for r in orc.backends[CLAUDE].requests)


def test_blocked_native_worker_is_not_reported_as_complete(orc_factory, project):
    orc,pid=setup(orc_factory,project,[delegate(plan_task(model=CODEX)), 'I did not edit the file. Access was denied.'],
                   ['PLAN_VERDICT: APPROVE\nScoped.','The implementation is blocked.\nNEXT: done'])
    task=orc.start_task(pid,'Add a simple multiply function')
    assert task['status']=='failed'
    dev=next(t for t in orc.db.team_tasks(task['id']) if t['role']=='developer')
    assert dev['status']=='failed' and 'blocked' in dev['error']


def test_missing_final_review_verdict_cannot_claim_completion(orc_factory, project):
    orc,pid=setup(orc_factory,project,[delegate(plan_task(model=CODEX)),edit(project,[],CODEX)],
                   ['PLAN_VERDICT: APPROVE\nScoped.','I cannot determine whether all acceptance criteria were met.'])
    task=orc.start_task(pid,'Add a simple multiply function')
    assert task['status']=='failed'
    assert 'explicit completion verdict' in task['summary']


def test_every_claude_and_gpt_request_carries_the_self_reliance_rule(orc_factory, project):
    from enoy.native import SELF_RELIANCE
    orc, pid = setup(orc_factory, project, [delegate(plan_task(model=CODEX)), edit(project, [], CODEX)],
                     ['PLAN_VERDICT: APPROVE\nScoped.', 'Claude reviewed.\nNEXT: done'])
    task = orc.start_task(pid, 'Add a simple multiply function')
    assert task['status'] == 'completed', task['summary']
    sent = orc.backends[CODEX].requests + orc.backends[CLAUDE].requests
    assert {r.purpose for r in orc.backends[CLAUDE].requests} >= {'plan_review'}
    for request in sent:
        assert request.messages[0].content.count(SELF_RELIANCE) == 1, request.purpose


def test_team_claude_requests_outside_the_coordinator_get_the_rule(orc_factory, project):
    from enoy.native import SELF_RELIANCE
    orc = orc_factory(claude=['MODE: answer\nOk.', 'Done.'])
    t = orc.start_task(open_project(orc, project), 'Is divide thread-safe? Explain.', backend=CLAUDE)
    orc._claude_direct(t['id'], None, 'edit', 'Make the change.', 'Task: ...', [], set())
    assert orc.backends[CLAUDE].requests[-1].messages[0].content.count(SELF_RELIANCE) == 1
