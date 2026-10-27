import pytest

from enoy import appearance
from enoy.storage import Storage


def test_preferences_survive_new_database_connection(tmp_path):
    path = tmp_path/'settings.db'
    db=Storage(path)
    appearance.save(db,{'mode':'code','preset':'ocean','custom':{'accent':'#abcdef'}})
    appearance.save(db,{'mode':'chat','preset':'rose','custom':{}})
    appearance.save(db,{'theme':'system'})
    db.close()
    db=Storage(path)
    saved=appearance.read(db)
    assert saved['theme']=='system'
    assert saved['colours']['code']=={'preset':'ocean','custom':{'accent':'#abcdef'}}
    assert saved['colours']['chat']['preset']=='rose'
    db.close()


@pytest.mark.parametrize('value', [[],{'theme':'bad'},{'mode':'code','preset':'clay','custom':{'accent':'url(https://example.com)'}}, {'mode':'code','preset':'other'}])
def test_invalid_preferences_leave_saved_values_unchanged(value):
    db=Storage(':memory:')
    appearance.save(db,{'mode':'code','preset':'clay','custom':{}})
    before=appearance.read(db)
    with pytest.raises(ValueError):
        appearance.save(db,value)
    assert appearance.read(db)==before
