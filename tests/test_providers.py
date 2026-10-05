import base64
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

import app.engines as engines
import app.oauth as oauth
import app.providers as provider_module
from app.models import ProcessingOptions
from app.oauth import ChatGPTAuth
from app.providers import Providers, safe_error
from app.store import Store
from app.subscriptions import CLAUDE_SECRET, ClaudeSubscription


ROOT = Path(__file__).resolve().parents[1]


def sample_invoice():
    return json.loads((ROOT / "samples" / "invoice.json").read_text())


class FakeResponse:
    def __init__(self, payload=None, status_code=200, lines=None):
        self.payload = payload or {}
        self.status_code = status_code
        self._lines = lines or []

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_lines(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeClient:
    def __init__(self, *, post=None, get=None, stream=None):
        self.post_response = post or FakeResponse()
        self.get_response = get or FakeResponse()
        self.stream_response = stream or FakeResponse()
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.post_response

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.get_response

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.stream_response


class FakeChatGPT:
    def token(self):
        return "chatgpt-access-token"


class IdentityClient(FakeClient):
    def __init__(self, jwk):
        super().__init__()
        self.jwk = jwk

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/.well-known/openid-configuration"):
            return FakeResponse({"jwks_uri": oauth.AUTH + "/.well-known/jwks.json"})
        return FakeResponse({"keys": [self.jwk]})


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def options(provider="openai"):
    return ProcessingOptions(engine="ai", ai_fallback=True, provider=provider, model="test-model")


def test_openai_mocked_success_uses_strict_schema_and_disables_storage(tmp_path, store, monkeypatch):
    extracted = sample_invoice()
    extracted["lines"][0]["net_amount"] = "599.99"
    extracted["lines"][0]["tax_amount"] = "30.00"
    store.secret("openai", "sk-test-provider-secret")
    payload = {
        "status": "completed",
        "output": [
            {
                "content": [
                    {"type": "output_text", "text": json.dumps(extracted)}
                ]
            }
        ],
        "usage": {"input_tokens": 7, "output_tokens": 11},
    }
    fake = FakeClient(post=FakeResponse(payload))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *args, **kwargs: fake)
    path = tmp_path / "invoice.txt"
    path.write_text("visible invoice text")

    invoice, usage = Providers(store, FakeChatGPT()).extract(
        path, "visible invoice text", options()
    )

    assert invoice.number == "DEMO-2026-001"
    assert invoice.lines[0].gtin == "00012345678905"
    assert invoice.lines[0].price == Decimal("60")
    assert invoice.lines[0].net_amount == Decimal("599.99")
    assert invoice.lines[0].tax_amount == Decimal("30.00")
    assert usage == {"input_tokens": 7, "output_tokens": 11}
    _, url, request = fake.calls[0]
    assert url == "https://api.openai.com/v1/responses"
    assert request["headers"] == {"Authorization": "Bearer sk-test-provider-secret"}
    assert request["json"]["store"] is False
    assert "must never be back-calculated or altered" in request["json"]["instructions"]
    assert request["json"]["text"]["format"]["strict"] is True
    assert request["json"]["text"]["format"]["schema"]["additionalProperties"] is False
    line_schema = request["json"]["text"]["format"]["schema"]["properties"]["lines"]["items"]
    assert {"net_amount", "tax_amount"}.issubset(line_schema["properties"])


def test_chatgpt_mocked_stream_requires_a_completed_response(tmp_path, store, monkeypatch):
    completed = {
        "type": "response.completed",
        "response": {
            "output": [
                {
                    "content": [
                        {"type": "output_text", "text": json.dumps(sample_invoice())}
                    ]
                }
            ],
            "usage": {"input_tokens": 3},
        },
    }
    stream = FakeResponse(lines=["data: " + json.dumps(completed), "data: [DONE]"])
    fake = FakeClient(stream=stream)
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *args, **kwargs: fake)
    path = tmp_path / "invoice.txt"
    path.write_text("visible")

    invoice, usage = Providers(store, FakeChatGPT()).extract(
        path, "visible", options("chatgpt")
    )

    assert invoice.number == "DEMO-2026-001"
    assert usage == {"input_tokens": 3}
    assert fake.calls[0][2]["headers"] == {
        "Authorization": "Bearer chatgpt-access-token"
    }

    fake.stream_response = FakeResponse(lines=["data: [DONE]"])
    with pytest.raises(ValueError, match="without a completed response"):
        Providers(store, FakeChatGPT()).extract(path, "visible", options("chatgpt"))


def test_provider_refusal_incomplete_output_and_invalid_schema_are_rejected(tmp_path, store, monkeypatch):
    path = tmp_path / "invoice.txt"
    path.write_text("visible")
    store.secret("anthropic", "anthropic-test-secret")
    refusal = FakeClient(
        post=FakeResponse({"stop_reason": "refusal", "content": [], "usage": {}})
    )
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *args, **kwargs: refusal)
    with pytest.raises(ValueError, match="incomplete or refused"):
        Providers(store, FakeChatGPT()).extract(path, "visible", options("anthropic"))

    store.secret("openai", "openai-test-secret")
    incomplete = FakeClient(post=FakeResponse({"status": "incomplete", "output": []}))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *args, **kwargs: incomplete)
    with pytest.raises(ValueError, match="output was incomplete"):
        Providers(store, FakeChatGPT()).extract(path, "visible", options())

    invalid = FakeClient(
        post=FakeResponse(
            {
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {"type": "output_text", "text": '{"lines": "not-a-list"}'}
                        ]
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *args, **kwargs: invalid)
    with pytest.raises(ValueError, match="invalid invoice structure"):
        Providers(store, FakeChatGPT()).extract(path, "visible", options())


def test_missing_credential_and_http_error_are_safe(tmp_path, store, monkeypatch):
    path = tmp_path / "invoice.txt"
    path.write_text("visible")
    providers = Providers(store, FakeChatGPT())
    with pytest.raises(ValueError, match="Connect openai with an API key first"):
        providers.extract(path, "visible", options())

    secret = "sk-must-not-appear-in-errors"
    store.secret("openai", secret)
    fake = FakeClient(post=FakeResponse(status_code=401))
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *args, **kwargs: fake)
    with pytest.raises(ValueError) as error:
        providers.extract(path, "visible", options())
    assert str(error.value) == safe_error(401)
    assert secret not in str(error.value)


def test_model_listing_filters_non_text_openai_models(store, monkeypatch):
    store.secret("openai", "openai-test-secret")
    fake = FakeClient(
        get=FakeResponse(
            {
                "data": [
                    {"id": "gpt-5"},
                    {"id": "gpt-realtime"},
                    {"id": "text-embedding-3-large"},
                    {"id": "o3"},
                ]
            }
        )
    )
    monkeypatch.setattr(provider_module.httpx, "Client", lambda *args, **kwargs: fake)

    assert Providers(store, FakeChatGPT()).models("openai") == [
        {"id": "gpt-5", "name": "gpt-5"},
        {"id": "o3", "name": "o3"},
    ]


def test_engine_ai_fallback_selects_complete_candidate_and_keeps_local_on_failure(
    tmp_path, monkeypatch
):
    path = tmp_path / "invoice.txt"
    path.write_text("visible")
    local = sample_invoice()
    local["tax"] = None
    complete = sample_invoice()
    monkeypatch.setattr(
        engines,
        "capabilities",
        lambda: [{"id": "invoice2data", "installed": True}],
    )
    monkeypatch.setattr(
        engines,
        "local_read",
        lambda *args, **kwargs: {"invoice": local, "text": "OCR", "boxes": []},
    )
    runtime = SimpleNamespace(root=tmp_path)
    configured = ProcessingOptions(
        engine="auto", ai_fallback=True, provider="openai", model="test-model"
    )

    result = engines.process(
        path,
        configured,
        runtime,
        lambda *args: (engines.Invoice.model_validate(complete), {"tokens": 2}),
    )
    # The AI fills the field the local read left empty; the local read stays the selected one.
    assert result["selected_engine"] == "invoice2data"
    assert result["invoice"]["tax"] == "40.0"
    assert result["readers"]["header"]["tax"] == "ai"
    assert result["readers"]["ai"]["status"] == "gap_fill"

    def failed_reader(*args):
        raise ValueError("invalid provider schema")

    result = engines.process(path, configured, runtime, failed_reader)
    assert result["selected_engine"] == "invoice2data"
    assert result["invoice"]["tax"] is None
    assert result["trace"][-1]["status"] == "failed"
    assert result["trace"][-1]["reason"] == "invalid provider schema"


@pytest.mark.parametrize("suffix", [".csv", ".txt"])
def test_explicit_ai_prepares_digital_text_before_calling_provider(tmp_path, monkeypatch, suffix):
    path = tmp_path / ("invoice" + suffix)
    path.write_text("number,net\nTEXT-1,10.00\n")
    prepared_text = "native text with invoice number TEXT-1"
    calls = []
    monkeypatch.setattr(
        engines,
        "capabilities",
        lambda: [{"id": "invoice2data", "installed": True}],
    )
    monkeypatch.setattr(
        engines,
        "local_read",
        lambda engine, *args, **kwargs: {
            "invoice": None,
            "text": prepared_text,
            "boxes": [],
        },
    )

    def ai_reader(received_path, text, received_options):
        calls.append((received_path, text, received_options))
        return engines.Invoice.model_validate(sample_invoice()), {"input_tokens": 4}

    configured = ProcessingOptions(
        engine="ai", ai_fallback=True, provider="openai", model="test-model"
    )
    result = engines.process(
        path,
        configured,
        SimpleNamespace(root=tmp_path),
        ai_reader,
    )

    assert calls == [(path, prepared_text, configured)]
    assert result["selected_engine"] == "openai / test-model"
    assert result["trace"][0]["engine"] == "invoice2data"
    assert result["trace"][0]["status"] == "text_only"


def test_oauth_start_binds_loopback_state_nonce_scope_and_pkce(store):
    auth = ChatGPTAuth(store)
    url = auth.start(8765)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "https",
        "auth.openai.com",
        "/api/accounts/authorize",
    )
    assert query["redirect_uri"] == ["http://127.0.0.1:8765/auth/callback"]
    assert query["resource"] == [oauth.RESOURCE]
    assert query["code_challenge_method"] == ["S256"]
    assert "chatgpt.tokens.use.direct" in query["scope"][0].split()
    state = query["state"][0]
    pending = auth.pending[state]
    assert pending["nonce"] == query["nonce"][0]
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(pending["verifier"].encode()).digest()
    ).decode().rstrip("=")
    assert query["code_challenge"] == [expected]

    with pytest.raises(ValueError, match="state was invalid"):
        auth.finish({"state": "not-the-issued-state", "code": "unused"})


def test_oauth_refresh_rejects_loss_of_direct_usage_scope(store, monkeypatch):
    auth = ChatGPTAuth(store)
    account_id = "account-1"
    store.set(
        "chatgpt_accounts",
        [
            {
                "id": account_id,
                "email": "person@example.test",
                "subject": "subject-1",
                "client_id": "issued-client",
                "connected": True,
            }
        ],
    )
    store.set("chatgpt_active", account_id)
    store.secret(
        "chatgpt:" + account_id,
        {
            "client_id": "issued-client",
            "access_token": "expired-access",
            "refresh_token": "refresh-token",
            "expires_at": time.time() - 1,
            "scopes": ["chatgpt.tokens.use.direct"],
        },
    )
    fake = FakeClient(
        post=FakeResponse(
            {
                "access_token": "new-access",
                "expires_in": 3600,
                "scope": "openid profile",
            }
        )
    )
    monkeypatch.setattr(oauth.httpx, "Client", lambda *args, **kwargs: fake)

    with pytest.raises(ValueError, match="permission is no longer granted"):
        auth.token()


def test_claude_subscription_is_encrypted_and_cli_run_is_isolated(tmp_path, store, monkeypatch):
    token = "sk-ant-oat01-" + "s" * 64
    subscription = ClaudeSubscription(store)
    assert subscription.connect(token) == {"connected": True, "provider": "claude_local"}
    assert subscription.connected() is True
    assert token.encode() not in store.path.read_bytes()

    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"structured_output": sample_invoice(), "usage": {"input_tokens": 5}}),
            stderr="",
        )

    monkeypatch.setattr(provider_module.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(provider_module.subprocess, "run", run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("INV_STUDIO_DATABASE_URL", "must-not-leak")
    path = tmp_path / "invoice.txt"
    path.write_text("visible invoice")

    invoice, usage = Providers(store, FakeChatGPT()).extract(
        path, "visible invoice", options("claude_local")
    )

    assert invoice.number == "DEMO-2026-001"
    assert usage == {"input_tokens": 5}
    assert "--bare" not in captured["command"]
    for switch in ("--safe-mode", "--restricted", "--disable-slash-commands", "--no-session-persistence"):
        assert switch in captured["command"]
    assert captured["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == token
    assert captured["env"]["HOME"] == captured["cwd"]
    assert captured["env"]["CLAUDE_CONFIG_DIR"].startswith(captured["cwd"])
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert "INV_STUDIO_DATABASE_URL" not in captured["env"]

    subscription.disconnect()
    assert store.secret(CLAUDE_SECRET) is None


def _registration_bundle():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="test-key", use="sig", alg="RS256")
    now = int(time.time())
    client_id = "issued-inv-studio-client"
    subject = "chatgpt-subject-1"
    scopes = oauth.SCOPES.split()
    access = jwt.encode(
        {
            "iss": oauth.AUTH,
            "aud": oauth.RESOURCE,
            "sub": subject,
            "client_id": client_id,
            "scope": " ".join(scopes),
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
        },
        private,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    identity = jwt.encode(
        {
            "iss": oauth.AUTH,
            "aud": client_id,
            "sub": subject,
            "email": "owner@example.test",
            "iat": now,
            "exp": now + 3600,
        },
        private,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    return {
        "format": oauth.BUNDLE_FORMAT,
        "version": 1,
        "exported_at": now,
        "account": {"subject": subject, "email": "owner@example.test"},
        "credentials": {
            "client_id": client_id,
            "access_token": access,
            "refresh_token": "refresh-token-for-inv-studio-only",
            "id_token": identity,
            "token_type": "Bearer",
            "expires_at": now + 3600,
            "earliest_refresh_at": now + 60,
            "scopes": scopes,
        },
    }, jwk


def test_chatgpt_bundle_import_verifies_tokens_encrypts_secrets_and_preserves_host(store, monkeypatch):
    auth = ChatGPTAuth(store)
    target_host_id = store.get("host_id")
    bundle, jwk = _registration_bundle()
    fake = IdentityClient(jwk)
    monkeypatch.setattr(oauth.httpx, "Client", lambda *args, **kwargs: fake)

    account = auth.import_bundle(bundle)

    assert account == {"id": account["id"], "email": "owner@example.test", "connected": True}
    assert store.get("host_id") == target_host_id
    assert auth.accounts() == [
        {
            "id": account["id"],
            "email": "owner@example.test",
            "subject": "chatgpt-subject-1",
            "client_id": "issued-inv-studio-client",
            "connected": True,
        }
    ]
    database = store.path.read_bytes()
    assert bundle["credentials"]["refresh_token"].encode() not in database
    assert bundle["credentials"]["access_token"].encode() not in database
    assert auth.token() == bundle["credentials"]["access_token"]


def test_chatgpt_bundle_rejects_scope_mismatch_without_echoing_or_storing_secret(store, monkeypatch):
    auth = ChatGPTAuth(store)
    bundle, jwk = _registration_bundle()
    secret = bundle["credentials"]["refresh_token"]
    bundle["credentials"]["scopes"] = ["openid", "offline_access"]
    monkeypatch.setattr(oauth.httpx, "Client", lambda *args, **kwargs: IdentityClient(jwk))

    with pytest.raises(ValueError) as error:
        auth.import_bundle(bundle)

    assert str(error.value) == "ChatGPT credential bundle is invalid or expired. Sign in again locally and retry."
    assert secret not in str(error.value)
    assert auth.accounts() == []
    assert secret.encode() not in store.path.read_bytes()


def test_chatgpt_bundle_export_is_own_registration_and_excludes_host_id(store):
    auth = ChatGPTAuth(store)
    bundle, _ = _registration_bundle()
    account_id = "account-1"
    store.set("chatgpt_accounts", [{"id": account_id, "email": "owner@example.test",
        "subject": bundle["account"]["subject"], "client_id": bundle["credentials"]["client_id"],
        "connected": True}])
    store.secret("chatgpt:" + account_id, bundle["credentials"])

    exported = auth.export_bundle(account_id)

    assert exported["format"] == oauth.BUNDLE_FORMAT
    assert exported["credentials"]["client_id"] == "issued-inv-studio-client"
    assert "host_id" not in json.dumps(exported)
    assert store.secret("chatgpt:"+account_id) is None
    assert auth.accounts()[0]["connected"] is False


def test_chatgpt_refresh_is_serialized_across_workers(store, monkeypatch):
    auth = ChatGPTAuth(store)
    account_id = "account-1"
    store.set("chatgpt_active", account_id)
    store.secret("chatgpt:" + account_id, {"client_id": "issued-client", "access_token": "expired",
        "refresh_token": "refresh", "expires_at": time.time() - 1,
        "scopes": ["chatgpt.tokens.use.direct"]})
    fake = FakeClient(post=FakeResponse({"access_token": "fresh", "refresh_token": "replacement",
        "expires_in": 3600, "scope": "openid offline_access chatgpt.tokens.use.direct"}))
    monkeypatch.setattr(oauth.httpx, "Client", lambda *args, **kwargs: fake)

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: auth.token(), range(8))) == ["fresh"] * 8

    assert len([call for call in fake.calls if call[0] == "POST"]) == 1


def test_local_reader_does_not_inherit_cloud_database_or_vault_credentials(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from app import engines
    (tmp_path/"work").mkdir()
    for key in ("INV_STUDIO_DATABASE_URL","INV_STUDIO_VAULT_KEY","GOOGLE_APPLICATION_CREDENTIALS","OTHER_SECRET"):
        monkeypatch.setenv(key,"synthetic-private-marker")
    def reader(command,**kwargs):
        assert "synthetic-private-marker" not in kwargs["env"].values()
        assert kwargs["env"]["OMP_NUM_THREADS"]=="2"
        output=Path(command[command.index("--output")+1])
        output.write_text(json.dumps({"text":"synthetic","boxes":[],"invoice":None}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(engines.subprocess,"run",reader)
    assert engines.local_read("invoice2data",tmp_path/"invoice.pdf",tmp_path)["text"]=="synthetic"


def test_prompt_asks_for_the_printed_description_only():
    assert "description is the product name exactly as printed" in provider_module.PROMPT
    assert "a barcode belongs in gtin" in provider_module.PROMPT
    assert "sku is null and a code that is part of the name text stays in the description" in provider_module.PROMPT


def test_prompt_appends_learned_examples_and_survives_a_broken_store(store):
    class Learned:
        def prompt_examples(self, text):
            return "\n\nVerified examples for " + text

    class Broken:
        def prompt_examples(self, text):
            raise RuntimeError("store unavailable")

    assert Providers(store, FakeChatGPT()).prompt("doc") == provider_module.PROMPT
    assert Providers(store, FakeChatGPT(), Learned()).prompt("doc") == provider_module.PROMPT + "\n\nVerified examples for doc"
    assert Providers(store, FakeChatGPT(), Broken()).prompt("doc") == provider_module.PROMPT
