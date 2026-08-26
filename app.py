from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv, set_key
import hmac
import os
import requests
import time

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
    me.raise_for_status()
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
    container.raise_for_status()

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
        status_response.raise_for_status()
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
    media_info.raise_for_status()

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
    r.raise_for_status()
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
@app.get("/linkedin/login")
def linkedin_login():
    client_id = os.getenv("LINKEDIN_CLIENT_ID")
    redirect_uri = os.getenv("LINKEDIN_REDIRECT_URI")

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile w_member_social",
    }

    return {
        "authorization_url":
            "https://www.linkedin.com/oauth/v2/authorization?"
            + urlencode(params)
    }


@app.get("/linkedin/callback")
@app.get("/linkedin/callback")
def linkedin_callback(
    code: str | None = None,
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

    token_response.raise_for_status()
    data = token_response.json()

    access_token = data.get("access_token")
    if access_token:
        set_key(".env", "LINKEDIN_ACCESS_TOKEN", access_token)

    return {
        "access_token_received": bool(access_token),
        "expires_in": data.get("expires_in"),
        "scope": data.get("scope"),
    }
class LinkedInPost(BaseModel):
    text: str
    image_url: str | None = None
    confirm: bool = False


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
        init.raise_for_status()

        init_data = init.json()["value"]
        upload_url = init_data["uploadUrl"]
        image_urn = init_data["image"]

        image_response = requests.get(
            post.image_url,
            timeout=30,
        )
        image_response.raise_for_status()

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
        upload.raise_for_status()

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

    r.raise_for_status()

    post_id = r.headers.get("x-restli-id") or r.headers.get("X-RestLi-Id")

    return {
        "published": True,
        "platform": "linkedin",
        "post_id": post_id
    }
import base64
import hashlib
import secrets
from urllib.parse import urlencode

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

    token_response.raise_for_status()
    data = token_response.json()

    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")

    if access_token:
        set_key(".env", "X_ACCESS_TOKEN", access_token)

    if refresh_token:
        set_key(".env", "X_REFRESH_TOKEN", refresh_token)

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

    r.raise_for_status()
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
@app.post("/x/publish")
def x_publish(post: XPost, _api_key: None = Depends(require_api_key)):
    if not post.confirm:
        return {
            "published": False,
            "reason": "Explicit confirmation required."
        }

    token = os.getenv("X_ACCESS_TOKEN")

    if not token:
        return {
            "published": False,
            "reason": "X access token is missing."
        }

    def send_post(access_token: str):
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

    if r.status_code == 401:
        refresh_x_access_token()
        token = os.getenv("X_ACCESS_TOKEN")
        r = send_post(token)

    r.raise_for_status()
    result = r.json()

    tweet_id = result.get("data", {}).get("id")

    return {
        "published": True,
        "platform": "x",
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
    if not post.confirm:
        return {
            "published": False,
            "reason": "Explicit confirmation required.",
            "results": {}
        }

    results = {}

    try:
        results["instagram"] = instagram_publish(
            PublishPost(
                caption=post.instagram_caption,
                image_url=post.image_url,
                confirm=True
            )
        )
    except Exception as e:
        results["instagram"] = {
            "published": False,
            "error": str(e)
        }

    try:
        results["facebook"] = facebook_publish(
            FacebookPost(
                message=post.facebook_message,
                image_url=post.image_url,
                confirm=True
            )
        )
    except Exception as e:
        results["facebook"] = {
            "published": False,
            "error": str(e)
        }

    try:
        results["linkedin"] = linkedin_publish(
            LinkedInPost(
                text=post.linkedin_text,
                image_url=post.image_url,
                confirm=True
            )
        )
    except Exception as e:
        results["linkedin"] = {
            "published": False,
            "error": str(e)
        }

    try:
        results["x"] = x_publish(
            XPost(
                text=post.x_text,
                confirm=True
            )
        )
    except Exception as e:
        results["x"] = {
            "published": False,
            "error": str(e)
        }

    all_success = all(
        result.get("published") is True
        for result in results.values()
    )

    return {
        "published": all_success,
        "results": results
    }
