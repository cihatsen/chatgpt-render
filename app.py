from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv, set_key
import base64
import hashlib
import hmac
import os
import requests
from requests_oauthlib import OAuth1
import secrets
import time
from urllib.parse import urlencode

load_dotenv(override=True)

app = FastAPI()
app.mount("/media", StaticFiles(directory="media"), name="media")


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected_api_key = os.getenv("APP_API_KEY")
    if not expected_api_key:
        raise HTTPException(status_code=503, detail="APP_API_KEY is not configured.")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected_api_key):
        raise HTTPException(status_code=401, detail="Invalid API key.")

@app.get("/health")
def health():
    return {"status": "ok"}


def ensure_upstream_ok(response: requests.Response, platform: str) -> None:
    if response.ok:
        return

    categories = {
        400: "request_rejected",
        401: "authentication_failed",
        403: "permission_denied",
        404: "resource_not_found",
        409: "conflict",
        429: "rate_limited",
    }
    raise HTTPException(
        status_code=502,
        detail={
            "platform": platform,
            "upstream_status": response.status_code,
            "category": categories.get(response.status_code, "upstream_error"),
        },
    )

@app.get("/instagram/status")
def instagram_status():
    token = os.getenv("INSTAGRAM_ACCESS_TOKEN")
    r = requests.get(
        "https://graph.instagram.com/me",
        params={"fields": "id,username", "access_token": token},
        timeout=20,
    )
    return r.json()
from pydantic import BaseModel

class PreviewPost(BaseModel):
    caption: str
    image_url: str | None = None

@app.post("/instagram/preview")
def instagram_preview(post: PreviewPost):
    return {
        "platform": "instagram",
        "dry_run": True,
        "caption": post.caption,
        "image_url": post.image_url,
        "message": "Preview only. Nothing was published."
    }
class PublishPost(BaseModel):
    caption: str
    image_url: str
    confirm: bool = False

@app.post("/instagram/publish")
def instagram_publish(post: PublishPost, _api_key: None = Depends(require_api_key)):
    if not post.confirm:
        return {
            "published": False,
            "reason": "Explicit confirmation required."
        }

    token = os.getenv("INSTAGRAM_ACCESS_TOKEN")

    me = requests.get(
        "https://graph.instagram.com/me",
        params={
            "fields": "id,username",
            "access_token": token
        },
        timeout=20,
    )
    ensure_upstream_ok(me, "instagram")
    ig_user_id = me.json()["id"]

    container = requests.post(
        f"https://graph.instagram.com/{ig_user_id}/media",
        data={
            "image_url": post.image_url,
            "caption": post.caption,
            "access_token": token,
        },
        timeout=30,
    )
    ensure_upstream_ok(container, "instagram")

    creation_id = container.json()["id"]

    # Instagram processes image containers asynchronously. Publishing before
    # the container is ready returns error 9007 (Media ID is not available).
    for _ in range(12):
        status_response = requests.get(
            f"https://graph.instagram.com/{creation_id}",
            params={
                "fields": "status_code,status",
                "access_token": token,
            },
            timeout=20,
        )
        ensure_upstream_ok(status_response, "instagram")
        status_data = status_response.json()
        status_code = status_data.get("status_code")

        if status_code == "FINISHED":
            break
        if status_code in {"ERROR", "EXPIRED"}:
            return {
                "published": False,
                "stage": "container_processing",
                "status_code": status_code,
                "instagram_error": status_data.get("status"),
                "creation_id": creation_id,
            }

        time.sleep(5)
    else:
        return {
            "published": False,
            "stage": "container_processing",
            "status_code": "TIMEOUT",
            "creation_id": creation_id,
        }

    publish = requests.post(
        f"https://graph.instagram.com/{ig_user_id}/media_publish",
        data={
            "creation_id": creation_id,
            "access_token": token,
        },
        timeout=30,
    )

    if not publish.ok:
        return {
            "published": False,
            "stage": "media_publish",
            "status_code": publish.status_code,
            "instagram_error": publish.text,
            "creation_id": creation_id
        }

    media_id = publish.json()["id"]

    media_info = requests.get(
        f"https://graph.instagram.com/{media_id}",
        params={
            "fields": "id,permalink",
            "access_token": token
        },
        timeout=20,
    )
    ensure_upstream_ok(media_info, "instagram")

    return {
        "published": True,
        "platform": "instagram",
        "media_id": media_id,
        "permalink": media_info.json().get("permalink")
    }

@app.get("/instagram/media/{media_id}")
def instagram_media(media_id: str):
    token = os.getenv("INSTAGRAM_ACCESS_TOKEN")

    r = requests.get(
        f"https://graph.instagram.com/{media_id}",
        params={
            "fields": "id,permalink,media_type,timestamp",
            "access_token": token
        },
        timeout=20,
    )
    ensure_upstream_ok(r, "facebook")
    return r.json()
class FacebookPost(BaseModel):
    message: str
    image_url: str | None = None
    confirm: bool = False


@app.post("/facebook/preview")
def facebook_preview(post: FacebookPost):
    return {
        "platform": "facebook",
        "dry_run": True,
        "message": post.message,
        "image_url": post.image_url,
        "published": False
    }


@app.post("/facebook/publish")
def facebook_publish(post: FacebookPost, _api_key: None = Depends(require_api_key)):
    if not post.confirm:
        return {
            "published": False,
            "reason": "Explicit confirmation required."
        }

    page_id = os.getenv("FACEBOOK_PAGE_ID")
    page_token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")

    if not page_id or not page_token:
        return {
            "published": False,
            "reason": "Facebook Page credentials are missing."
        }

    if post.image_url:
        r = requests.post(
            f"https://graph.facebook.com/v24.0/{page_id}/photos",
            data={
                "url": post.image_url,
                "caption": post.message,
                "access_token": page_token
            },
            timeout=30,
        )
    else:
        r = requests.post(
            f"https://graph.facebook.com/v24.0/{page_id}/feed",
            data={
                "message": post.message,
                "access_token": page_token
            },
            timeout=30,
        )

    if not r.ok:
        return {
            "published": False,
            "status_code": r.status_code,
            "facebook_error": r.text
        }

    result = r.json()
    post_id = result.get("post_id") or result.get("id")

    permalink = None

    if post_id:
        info = requests.get(
            f"https://graph.facebook.com/v24.0/{post_id}",
            params={
                "fields": "permalink_url",
                "access_token": page_token
            },
            timeout=20,
        )

        if info.ok:
            permalink = info.json().get("permalink_url")

    return {
        "published": True,
        "platform": "facebook",
        "post_id": post_id,
        "permalink": permalink
    }
linkedin_oauth_states: set[str] = set()


@app.get("/linkedin/login")
def linkedin_login():
    client_id = os.getenv("LINKEDIN_CLIENT_ID")
    redirect_uri = os.getenv("LINKEDIN_REDIRECT_URI")

    if not client_id or not redirect_uri:
        raise HTTPException(
            status_code=503,
            detail="LinkedIn OAuth settings are not configured.",
        )

    state = secrets.token_urlsafe(32)
    linkedin_oauth_states.add(state)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile w_member_social",
        "state": state,
    }

    return {
        "authorization_url":
            "https://www.linkedin.com/oauth/v2/authorization?"
            + urlencode(params)
    }


@app.get("/linkedin/callback")
def linkedin_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    if error:
        return {
            "oauth_error": error,
            "error_description": error_description
        }

    if not code:
        return {
            "oauth_error": "missing_code"
        }

    if not state or state not in linkedin_oauth_states:
        return {
            "oauth_error": "invalid_state"
        }

    linkedin_oauth_states.remove(state)

    client_id = os.getenv("LINKEDIN_CLIENT_ID")
    client_secret = os.getenv("LINKEDIN_CLIENT_SECRET")
    redirect_uri = os.getenv("LINKEDIN_REDIRECT_URI")

    token_response = requests.post(
        "https://www.linkedin.com/oauth/v2/accessToken",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )

    ensure_upstream_ok(token_response, "linkedin")
    data = token_response.json()

    access_token = data.get("access_token")
    if access_token:
        set_key(".env", "LINKEDIN_ACCESS_TOKEN", access_token)
        os.environ["LINKEDIN_ACCESS_TOKEN"] = access_token

    return {
        "access_token_received": bool(access_token),
        "expires_in": data.get("expires_in"),
        "scope": data.get("scope"),
    }
class LinkedInPost(BaseModel):
    text: str
    image_url: str | None = None
    confirm: bool = False


@app.get("/linkedin/status")
def linkedin_status(_api_key: None = Depends(require_api_key)):
    access_token = os.getenv("LINKEDIN_ACCESS_TOKEN")
    client_id = os.getenv("LINKEDIN_CLIENT_ID")
    client_secret = os.getenv("LINKEDIN_CLIENT_SECRET")

    if not access_token or not client_id or not client_secret:
        raise HTTPException(status_code=503, detail="LinkedIn credentials are missing.")

    response = requests.post(
        "https://www.linkedin.com/oauth/v2/introspectToken",
        data={
            "token": access_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if not response.ok:
        raise HTTPException(
            status_code=502,
            detail=f"LinkedIn credential check failed with status {response.status_code}.",
        )

    data = response.json()
    scopes = {
        scope
        for scope in str(data.get("scope", "")).replace(",", " ").split()
        if scope
    }

    expires_at = data.get("expires_at")
    days_remaining = None
    if expires_at is not None:
        try:
            expires_timestamp = float(expires_at)
            if expires_timestamp > 10_000_000_000:
                expires_timestamp /= 1000
            days_remaining = max(0, int((expires_timestamp - time.time()) // 86400))
        except (TypeError, ValueError):
            pass

    return {
        "connected": bool(data.get("active")),
        "w_member_social": "w_member_social" in scopes,
        "days_remaining": days_remaining,
        "renewal_required": days_remaining is not None and days_remaining <= 7,
    }


@app.post("/linkedin/preview")
def linkedin_preview(post: LinkedInPost):
    return {
        "platform": "linkedin",
        "dry_run": True,
        "text": post.text,
        "published": False
    }


@app.post("/linkedin/publish")
def linkedin_publish(post: LinkedInPost, _api_key: None = Depends(require_api_key)):
    if not post.confirm:
        return {
            "published": False,
            "reason": "Explicit confirmation required."
        }

    token = os.getenv("LINKEDIN_ACCESS_TOKEN")
    person_id = os.getenv("LINKEDIN_PERSON_ID")

    if not token or not person_id:
        return {
            "published": False,
            "reason": "LinkedIn credentials are missing."
        }

    author = f"urn:li:person:{person_id}"

    image_urn = None
    payload = {
        "author": author,
        "commentary": post.text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": []
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False
    }

    if post.image_url:
        api_headers = {
            "Authorization": f"Bearer {token}",
            "Linkedin-Version": "202604",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json"
        }

        init = requests.post(
            "https://api.linkedin.com/rest/images?action=initializeUpload",
            headers=api_headers,
            json={
                "initializeUploadRequest": {
                    "owner": author
                }
            },
            timeout=30,
        )
        ensure_upstream_ok(init, "linkedin")

        init_data = init.json()["value"]
        upload_url = init_data["uploadUrl"]
        image_urn = init_data["image"]

        image_response = requests.get(
            post.image_url,
            timeout=30,
        )
        ensure_upstream_ok(image_response, "image_source")

        upload = requests.put(
            upload_url,
            data=image_response.content,
            headers={
                "Content-Type": image_response.headers.get(
                    "Content-Type",
                    "application/octet-stream"
                )
            },
            timeout=60,
        )
        ensure_upstream_ok(upload, "linkedin")

    if image_urn:
        payload["content"] = {
            "media": {
                "id": image_urn
            }
        }
    headers = {
        "Authorization": f"Bearer {token}",
        "Linkedin-Version": "202604",
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json"
    }

    r = requests.post(
        "https://api.linkedin.com/rest/posts",
        headers=headers,
        json=payload,
        timeout=30,
    )

    ensure_upstream_ok(r, "linkedin")

    post_id = r.headers.get("x-restli-id") or r.headers.get("X-RestLi-Id")

    return {
        "published": True,
        "platform": "linkedin",
        "post_id": post_id
    }
x_oauth_state = {}
@app.get("/x/login")
def x_login():
    client_id = os.getenv("X_CLIENT_ID")
    redirect_uri = os.getenv("X_REDIRECT_URI")

    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)

    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip("=")

    x_oauth_state[state] = code_verifier

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
       "scope": "tweet.read tweet.write users.read offline.access",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    return {
        "authorization_url":
            "https://x.com/i/oauth2/authorize?"
            + urlencode(params)
    }


@app.get("/x/callback")
def x_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if error:
        return {"oauth_error": error}

    if not code or not state:
        return {"oauth_error": "missing_code_or_state"}

    code_verifier = x_oauth_state.pop(state, None)

    if not code_verifier:
        return {"oauth_error": "invalid_state"}

    client_id = os.getenv("X_CLIENT_ID")
    client_secret = os.getenv("X_CLIENT_SECRET")
    redirect_uri = os.getenv("X_REDIRECT_URI")

    token_response = requests.post(
        "https://api.x.com/2/oauth2/token",
        auth=(client_id, client_secret),
        data={
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        },
        timeout=30,
    )

    ensure_upstream_ok(token_response, "x")
    data = token_response.json()

    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")

    if access_token:
        set_key(".env", "X_ACCESS_TOKEN", access_token)
        os.environ["X_ACCESS_TOKEN"] = access_token

    if refresh_token:
        set_key(".env", "X_REFRESH_TOKEN", refresh_token)
        os.environ["X_REFRESH_TOKEN"] = refresh_token

    return {
        "access_token_received": bool(access_token),
        "refresh_token_received": bool(refresh_token),
        "expires_in": data.get("expires_in"),
        "scope": "tweet.read tweet.write users.read",
    }
class XPost(BaseModel):
    text: str
    confirm: bool = False


@app.post("/x/preview")
def x_preview(post: XPost):
    return {
        "platform": "x",
        "dry_run": True,
        "text": post.text,
        "published": False
    }

def refresh_x_access_token():
    client_id = os.getenv("X_CLIENT_ID")
    client_secret = os.getenv("X_CLIENT_SECRET")
    refresh_token = os.getenv("X_REFRESH_TOKEN")

    if not client_id or not client_secret or not refresh_token:
        raise RuntimeError("X OAuth refresh credentials are missing.")

    r = requests.post(
        "https://api.x.com/2/oauth2/token",
        auth=(client_id, client_secret),
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        },
        timeout=30,
    )

    ensure_upstream_ok(r, "x")
    data = r.json()

    new_access_token = data.get("access_token")
    new_refresh_token = data.get("refresh_token")

    if new_access_token:
        set_key(".env", "X_ACCESS_TOKEN", new_access_token)
        os.environ["X_ACCESS_TOKEN"] = new_access_token

    if new_refresh_token:
        set_key(".env", "X_REFRESH_TOKEN", new_refresh_token)
        os.environ["X_REFRESH_TOKEN"] = new_refresh_token

    return {
        "access_token_updated": bool(new_access_token),
        "refresh_token_updated": bool(new_refresh_token),
        "expires_in": data.get("expires_in")
    }


@app.post("/x/refresh-token")
def x_refresh_token(_api_key: None = Depends(require_api_key)):
    return refresh_x_access_token()


def x_oauth1_auth() -> OAuth1 | None:
    consumer_key = os.getenv("X_OAUTH1_CONSUMER_KEY")
    consumer_secret = os.getenv("X_OAUTH1_CONSUMER_SECRET")
    access_token = os.getenv("X_OAUTH1_ACCESS_TOKEN")
    access_token_secret = os.getenv("X_OAUTH1_ACCESS_TOKEN_SECRET")

    credentials = (
        consumer_key,
        consumer_secret,
        access_token,
        access_token_secret,
    )
    if not all(credentials):
        return None

    return OAuth1(
        consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=access_token,
        resource_owner_secret=access_token_secret,
    )


@app.get("/x/status")
def x_status(_api_key: None = Depends(require_api_key)):
    oauth1 = x_oauth1_auth()
    if oauth1:
        response = requests.get(
            "https://api.x.com/2/users/me",
            auth=oauth1,
            timeout=30,
        )
        auth_mode = "oauth1"
    else:
        access_token = os.getenv("X_ACCESS_TOKEN")
        if not access_token:
            raise HTTPException(status_code=503, detail="X credentials are missing.")
        response = requests.get(
            "https://api.x.com/2/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        auth_mode = "oauth2"

    if not response.ok:
        raise HTTPException(
            status_code=502,
            detail=f"X credential check failed with status {response.status_code}.",
        )

    data = response.json().get("data", {})
    return {
        "connected": bool(data.get("id")),
        "auth_mode": auth_mode,
        "user_id_present": bool(data.get("id")),
    }


@app.post("/x/publish")
def x_publish(post: XPost, _api_key: None = Depends(require_api_key)):
    if not post.confirm:
        return {
            "published": False,
            "reason": "Explicit confirmation required."
        }

    oauth1 = x_oauth1_auth()
    token = os.getenv("X_ACCESS_TOKEN")

    if not oauth1 and not token:
        raise HTTPException(status_code=503, detail="X credentials are missing.")

    def send_post(access_token: str | None = None):
        if oauth1:
            return requests.post(
                "https://api.x.com/2/tweets",
                auth=oauth1,
                json={"text": post.text},
                timeout=30,
            )
        return requests.post(
            "https://api.x.com/2/tweets",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            },
            json={
                "text": post.text
            },
            timeout=30,
        )

    r = send_post(token)

    if not oauth1 and r.status_code == 401:
        refresh_x_access_token()
        token = os.getenv("X_ACCESS_TOKEN")
        r = send_post(token)

    ensure_upstream_ok(r, "x")
    result = r.json()

    tweet_id = result.get("data", {}).get("id")

    return {
        "published": True,
        "platform": "x",
        "auth_mode": "oauth1" if oauth1 else "oauth2",
        "post_id": tweet_id,
        "url": f"https://x.com/i/web/status/{tweet_id}" if tweet_id else None
    }
class PublishAllPost(BaseModel):
    instagram_caption: str
    facebook_message: str
    linkedin_text: str
    x_text: str
    image_url: str
    confirm: bool = False


@app.post("/publish-all")
def publish_all(post: PublishAllPost, _api_key: None = Depends(require_api_key)):
    if post.confirm:
        raise HTTPException(
            status_code=409,
            detail={
                "category": "bulk_publish_disabled",
                "message": "Publish each platform separately to avoid partial or duplicate posts.",
            },
        )

    return {
        "published": False,
        "dry_run": True,
        "reason": "Bulk preview only. Publish each platform separately.",
        "previews": {
            "instagram": {
                "caption": post.instagram_caption,
                "image_url": post.image_url,
            },
            "facebook": {
                "message": post.facebook_message,
                "image_url": post.image_url,
            },
            "linkedin": {
                "text": post.linkedin_text,
                "image_url": post.image_url,
            },
            "x": {"text": post.x_text},
        },
    }
