"""Firebase identity verification for the explicitly enabled cloud workspace."""
import os
from contextvars import ContextVar

actor = ContextVar("invoice_actor", default="local-operator")


class CloudIdentity:
    def __init__(self):
        self.cloud = os.getenv("INV_STUDIO_CLOUD") == "1"
        self.project = os.getenv("GOOGLE_CLOUD_PROJECT", "")
        self.allowed = {x.strip().casefold() for x in os.getenv("INV_STUDIO_ALLOWED_EMAILS", "").split(",") if x.strip()}
        self.app = None
        if self.cloud:
            if not self.project:
                raise ValueError("Cloud project must be configured")
            import firebase_admin
            self.app = firebase_admin.initialize_app(options={"projectId": self.project}, name="invoice-studio-"+os.urandom(6).hex())

    def config(self):
        if not self.cloud:
            return {"cloud": False}
        return {"cloud": True, "firebase": {
            "apiKey": os.getenv("INV_STUDIO_FIREBASE_API_KEY", ""),
            "authDomain": os.getenv("INV_STUDIO_FIREBASE_AUTH_DOMAIN", ""),
            "projectId": self.project,
            "appId": os.getenv("INV_STUDIO_FIREBASE_APP_ID", ""),
            "providers": ["password"],
        }}

    def verify(self, authorization):
        if not authorization or not authorization.startswith("Bearer "):
            raise ValueError("Sign in to Invoice Studio")
        token = authorization[7:]
        if len(token) > 16384:
            raise ValueError("Invalid sign-in token")
        from firebase_admin import auth
        try:
            claims = auth.verify_id_token(token, app=self.app, check_revoked=True)
        except Exception:
            raise ValueError("Sign-in expired or could not be verified. Sign in again.") from None
        email = str(claims.get("email", "")).casefold()
        if claims.get("email_verified") is not True or not email or email not in self.allowed:
            raise PermissionError("This account has not been granted access to this workspace")
        return {"email": email, "uid": claims["uid"]}
