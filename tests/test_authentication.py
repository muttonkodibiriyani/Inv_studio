import pytest
from app.authentication import CloudIdentity


def identity():
    instance=object.__new__(CloudIdentity)
    instance.cloud=True
    instance.allowed={'owner@example.test'}
    instance.app=None
    return instance


def test_cloud_verification_requires_bearer():
    for value in (None,'','Basic value','Bearer '+('a'*17000)):
        with pytest.raises(ValueError):identity().verify(value)


@pytest.mark.parametrize('claims',[
    {'uid':'u','email':'other@example.test','email_verified':True},
    {'uid':'u','email':'owner@example.test','email_verified':False},
    {'uid':'u','email_verified':True},
])
def test_cloud_denies_unapproved_identity(monkeypatch,claims):
    auth=pytest.importorskip('firebase_admin.auth')
    monkeypatch.setattr(auth,'verify_id_token',lambda *a,**k:claims)
    with pytest.raises(PermissionError):identity().verify('Bearer token')


def test_cloud_checks_revocation_and_returns_only_identity(monkeypatch):
    auth=pytest.importorskip('firebase_admin.auth')
    def verify(value,**kwargs):
        assert value=='test-token'
        assert kwargs['check_revoked'] is True
        return {'uid':'u','email':'OWNER@example.test','email_verified':True,'private_claim':'hidden'}
    monkeypatch.setattr(auth,'verify_id_token',verify)
    assert identity().verify('Bearer test-token')=={'email':'owner@example.test','uid':'u'}
    empty=identity();empty.allowed=set()
    with pytest.raises(PermissionError):empty.verify('Bearer test-token')


def test_cloud_provider_error_does_not_echo_token(monkeypatch):
    auth=pytest.importorskip('firebase_admin.auth')
    def fail(*a,**k):raise RuntimeError('private-token-marker')
    monkeypatch.setattr(auth,'verify_id_token',fail)
    with pytest.raises(ValueError) as exc:identity().verify('Bearer private-token-marker')
    assert 'private-token-marker' not in str(exc.value)


def test_cloud_api_auth_guard_and_public_config(tmp_path,monkeypatch):
    from app import main,cloud_store
    from app.store import Store
    from fastapi.testclient import TestClient
    class FakeIdentity:
        cloud=True
        def config(self):return {'cloud':True,'firebase':{'providers':['password']}}
        def verify(self,value):
            if value=='Bearer valid':return {'email':'owner@example.test','uid':'u'}
            if value=='Bearer unapproved':raise PermissionError('Not allowed')
            raise ValueError('Sign in')
    class FakeStore(Store):
        def sync_templates(self):pass
    monkeypatch.setattr(main,'CloudIdentity',FakeIdentity)
    monkeypatch.setattr(cloud_store,'PostgresStore',FakeStore)
    app=main.create_app(tmp_path)
    with TestClient(app) as client:
        assert client.get('/api/public-config').json()['cloud'] is True
        assert client.get('/api/state').status_code==401
        assert client.get('/api/state',headers={'Authorization':'Bearer unapproved'}).status_code==403
        assert client.get('/api/session',headers={'Authorization':'Bearer valid'}).json()['uid']=='u'
        assert client.get('/api/jobs/unknown/document').status_code==401
        assert client.get('/api/samples/reference.xlsx').status_code==401
        assert client.get('/auth/callback').status_code==404
    app.state.pool.shutdown(wait=True)
