"""Images must reach the actual provider, not only the chat's saved metadata."""
import json
from pathlib import Path
import sys

from conftest import FakeBackend
from test_images import png
from test_lead import delegate, plan_task
from test_orchestrator import ADD_MULTIPLY, open_project
from enoy import images
from enoy.backends.base import CLAUDE, CODEX, ChatMessage, ChatRequest, ImageData
from enoy.backends.codex import CodexBackend


def make(orc_factory, replies, claude=None):
    orc = orc_factory(claude=claude, permissions__auto_apply_patches=True)
    orc.backends[CODEX] = FakeBackend(CODEX, replies)
    return orc


def test_auto_sees_image_and_followup_keeps_visual_context(orc_factory, project):
    content = png()
    image_id = images.store(content)['id']
    def answer(request):
        user = request.messages[-1]
        assert user.images[0].data == content
        assert image_id in user.content
        return 'MODE: answer\nI can inspect the screenshot.'
    orc = make(orc_factory, [answer, answer])
    task = orc.start_task(open_project(orc, project), 'Explain this screenshot', images=[image_id])
    assert task['status'] == 'completed'
    orc.send_message(task['id'], 'What about the sidebar?')
    assert orc.db.get_task(task['id'])['status'] == 'completed'


def test_retry_of_previously_rejected_image_keeps_attachment(orc_factory, project):
    image_id = images.store(png())['id']
    orc = make(orc_factory, ['MODE: answer\nThe screenshot is visible now.'])
    pid = open_project(orc, project)
    task = orc.db.create_task(pid, 'Rejected screenshot', 'Explain this screenshot', 'ask')
    orc.db.add_message(task['id'], 'user', 'Explain this screenshot', images=[image_id])
    orc.db.update_task(task['id'], status='failed', summary='The coordinator accepts text only.')
    orc.resume(task['id'])
    assert orc.db.get_task(task['id'])['status'] == 'completed'
    assert orc.backends[CODEX].requests[0].messages[-1].images[0].data == png()


def test_workers_reads_and_final_review_keep_the_image(orc_factory, project):
    content = png()
    image_id = images.store(content)['id']
    def checked(text):
        def reply(request):
            assert request.messages[-1].images[0].data == content
            return text
        return reply
    orc = make(orc_factory, [checked('<<<READ calculator.py>>>'),
                            checked(delegate(plan_task(model=CLAUDE))), checked('Done.\nNEXT: done')],
               [checked(ADD_MULTIPLY)])
    task = orc.start_task(open_project(orc, project), 'Add multiply using this reference', images=[image_id])
    assert task['status'] == 'completed', task['summary']
    assert len(orc.backends[CODEX].requests) == 3
    assert len(orc.backends[CLAUDE].requests) == 1


def test_manual_claude_image_is_sent_without_a_second_prompt(orc_factory, project):
    # Choosing Claude is the approval; the retired confirm setting no longer pauses the request (090172b).
    content = png()
    image_id = images.store(content)['id']
    orc = make(orc_factory, [], ['Image description.'])
    orc.cfg['claude']['confirm_before_send'] = True
    task = orc.start_task(open_project(orc, project), 'Describe this image', backend=CLAUDE, images=[image_id])
    assert task['status'] == 'completed', task['summary']
    assert orc.backends[CLAUDE].requests[0].messages[-1].images[0].data == content


def test_cloud_disabled_blocks_images_before_provider_call(orc_factory, project):
    image_id = images.store(png())['id']
    orc = make(orc_factory, [])
    task = orc.start_task(open_project(orc, project), 'Describe this image', local_only=True, images=[image_id])
    assert task['status'] == 'failed'
    assert not orc.backends[CODEX].requests and not orc.backends[CLAUDE].requests


def test_missing_image_fails_without_claiming_to_see_it(orc_factory, project):
    image_id = images.store(png())['id']
    orc = make(orc_factory, [])
    orc.set_dispatch_paused(True)
    task = orc.start_task(open_project(orc, project), 'Describe this image', images=[image_id])
    # Saved attachment IDs can outlive their files after external cleanup.
    from enoy import paths
    (paths.images_dir() / (image_id + '.png')).unlink()
    orc.set_dispatch_paused(False)
    task = orc.db.get_task(task['id'])
    assert task['status'] == 'failed' and not orc.backends[CODEX].requests
    assert 'Reattach' in task['summary']


def test_codex_cli_receives_images_and_cleans_temporary_files(tmp_path, monkeypatch):
    monkeypatch.setattr('tempfile.tempdir', str(tmp_path))
    capture = tmp_path / 'capture.json'
    fake = tmp_path / 'fake_cli.py'
    fake.write_text('''import json, sys
from pathlib import Path
args=sys.argv[1:]
paths=[args[i+1] for i,arg in enumerate(args) if arg=='--image']
assert len(paths)==2
assert [Path(p).suffix for p in paths]==['.png','.jpg']
assert [Path(p).read_bytes() for p in paths]==[b'png-content',b'jpeg-content']
assert all(Path(p).parent==Path.cwd() for p in paths)
assert 'Inspect these images' in sys.stdin.read()
Path(''' + repr(str(capture)) + ''').write_text(json.dumps(paths))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Images received.'}}))
print(json.dumps({'type':'turn.completed','usage':{}}))
''')
    backend = CodexBackend({'enabled':True,'timeout_s':10}, [sys.executable,str(fake)])
    pictures = [ImageData('image/png',b'png-content'),ImageData('image/jpeg',b'jpeg-content')]
    result = backend.chat(ChatRequest([ChatMessage('user','Inspect these images',images=pictures)]))
    assert result.text == 'Images received.'
    assert all(not Path(p).exists() for p in json.loads(capture.read_text()))
