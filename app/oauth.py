"""Official Sign in with ChatGPT flow for an open-source, loopback-hosted app.

Tokens stay encrypted on the server. This module never uses ChatGPT backend-api
endpoints or copies credentials from another application.
"""
import base64
import hashlib
import secrets
import threading
import time
import uuid
from urllib.parse import urlencode, urlparse
import httpx
import jwt

AUTH="https://auth.openai.com"
RESOURCE="https://api.openai.com/v1"
SCOPES="openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


class ChatGPTAuth:
    def __init__(self,store):
        self.store=store; self.pending={}; self.lock=threading.RLock()
        if not store.get("host_id"):store.set("host_id","urn:uuid:"+str(uuid.uuid4()))

    def accounts(self):
        return self.store.get("chatgpt_accounts",[])

    def start(self,port,account_id=None):
        verifier=secrets.token_urlsafe(48);state=secrets.token_urlsafe(32);nonce=secrets.token_urlsafe(32)
        saved=self.store.secret("chatgpt:"+account_id) if account_id else None
        account=next((a for a in self.accounts() if a["id"]==account_id),None)
        if account_id and not account:raise ValueError("Unknown ChatGPT account")
        client=account["client_id"] if account else "dynamic_agent_client"
        redirect=f"http://127.0.0.1:{port}/auth/callback"
        query={"client_id":client,"ext_agent_host_id":self.store.get("host_id"),"response_type":"code",
               "redirect_uri":redirect,"scope":SCOPES,"resource":RESOURCE,"state":state,"nonce":nonce,
               "code_challenge_method":"S256","code_challenge":base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
        if not account:query["agent_name_hint"]="Inv Studio"
        elif saved and saved.get("id_token"):query["id_token_hint"]=saved["id_token"]
        with self.lock:
            self.pending={k:v for k,v in self.pending.items() if v["expires"]>time.time()}
            self.pending[state]={"verifier":verifier,"nonce":nonce,"redirect":redirect,"client":client,
                                 "account":account,"expires":time.time()+600}
        return AUTH+"/api/accounts/authorize?"+urlencode(query)

    def finish(self,params):
        with self.lock:pending=self.pending.pop(params.get("state",""),None)
        if not pending or pending["expires"]<time.time():raise ValueError("Sign-in expired or state was invalid. Start again.")
        if params.get("error"):raise ValueError("ChatGPT sign-in was declined or cancelled.")
        client=params.get("client_id") or pending["client"]
        if client=="dynamic_agent_client" or not params.get("code"):raise ValueError("Registration did not return an issued client and code.")
        if pending["account"] and client!=pending["client"]:raise ValueError("Registration changed during sign-in.")
        with httpx.Client(timeout=30,follow_redirects=False) as http:
            r=http.post(AUTH+"/api/accounts/oauth/token",data={"grant_type":"authorization_code","client_id":client,
                "code":params["code"],"code_verifier":pending["verifier"],"redirect_uri":pending["redirect"],"resource":RESOURCE})
            if r.status_code!=200:raise ValueError("Token exchange failed. Start a new sign-in.")
            tokens=r.json()
            config=http.get(AUTH+"/.well-known/openid-configuration");config.raise_for_status()
            jwks_url=config.json()["jwks_uri"]
            if urlparse(jwks_url).scheme!="https" or urlparse(jwks_url).hostname!="auth.openai.com":raise ValueError("Unexpected identity key endpoint")
            keys=http.get(jwks_url);keys.raise_for_status()
        header=jwt.get_unverified_header(tokens["id_token"])
        key=next((k for k in keys.json()["keys"] if k.get("kid")==header.get("kid")),None)
        if not key:raise ValueError("Unknown identity signing key")
        claims=jwt.decode(tokens["id_token"],jwt.PyJWK.from_dict(key).key,algorithms=["RS256"],
                          audience=client,issuer=AUTH,options={"require":["exp","sub","nonce","aud","iss"]})
        if not secrets.compare_digest(str(claims["nonce"]),pending["nonce"]):raise ValueError("Identity nonce did not match")
        if pending["account"] and claims["sub"]!=pending["account"]["subject"]:raise ValueError("Signed-in account changed")
        scopes=tokens.get("scope","").split()
        if "chatgpt.tokens.use.direct" not in scopes:raise ValueError("ChatGPT plan usage permission was not granted")
        aid=hashlib.sha256((client+claims["sub"]).encode()).hexdigest()[:20]
        tokens.update(client_id=client,expires_at=time.time()+int(tokens.get("expires_in",3600)),scopes=scopes)
        self.store.secret("chatgpt:"+aid,tokens)
        accounts=[a for a in self.accounts() if a["id"]!=aid]
        accounts.append({"id":aid,"email":claims.get("email","ChatGPT account"),"subject":claims["sub"],"client_id":client,"connected":True})
        self.store.set("chatgpt_accounts",accounts);self.store.set("chatgpt_active",aid)
        return aid

    def token(self):
        with self.lock:
            aid=self.store.get("chatgpt_active")
            t=self.store.secret("chatgpt:"+aid) if aid else None
            if not t:raise ValueError("Connect a ChatGPT account first")
            if t["expires_at"]<time.time()+60:
                with httpx.Client(timeout=30) as http:
                    r=http.post(AUTH+"/api/accounts/oauth/token",data={"grant_type":"refresh_token","client_id":t["client_id"],"refresh_token":t.get("refresh_token",""),"resource":RESOURCE})
                if r.status_code!=200:raise ValueError("ChatGPT session expired. Reconnect the selected account.")
                new=r.json();t.update(new);t["expires_at"]=time.time()+int(new.get("expires_in",3600))
                if "scope" in new:t["scopes"]=new["scope"].split()
                if "chatgpt.tokens.use.direct" not in t["scopes"]:raise ValueError("ChatGPT plan usage permission is no longer granted")
                self.store.secret("chatgpt:"+aid,t)
            return t["access_token"]

    def disconnect(self,aid):
        with self.lock:
            t=self.store.secret("chatgpt:"+aid);revoked=False
            if t:
                try:
                    with httpx.Client(timeout=20) as http:
                        cfg=http.get(AUTH+"/.well-known/openid-configuration").json()
                        endpoint=cfg["revocation_endpoint"]
                        if urlparse(endpoint).scheme=="https" and urlparse(endpoint).hostname=="auth.openai.com":
                            r=http.post(endpoint,data={"token":t.get("refresh_token",""),"token_type_hint":"refresh_token","client_id":t["client_id"]})
                            revoked=r.status_code==200
                except Exception:pass
            self.store.secret("chatgpt:"+aid,delete=True)
            accounts=self.accounts()
            for a in accounts:
                if a["id"]==aid:a["connected"]=False
            self.store.set("chatgpt_accounts",accounts)
            if self.store.get("chatgpt_active")==aid:self.store.set("chatgpt_active",None)
            return "Disconnected" if revoked else "Disconnected locally. Remote revocation was not confirmed; disconnect Inv Studio in ChatGPT Settings."
