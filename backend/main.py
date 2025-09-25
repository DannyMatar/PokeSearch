# backend/main.py
import os
import json
import re
import requests
from urllib.parse import quote
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from fastapi import FastAPI, Request, Depends, HTTPException, status, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer

from jose import JWTError, jwt
from passlib.context import CryptContext

import databases, sqlalchemy, aioredis
from fastapi_limiter import FastAPILimiter
from fastapi_limiter.depends import RateLimiter

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# Config from env
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
EBAY_OAUTH_TOKEN = os.getenv("EBAY_OAUTH_TOKEN", "")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_CX = os.getenv("GOOGLE_CX", "")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-me")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

SEARCH_LIMIT_PER_GRADE = 3
SAVED_SEARCH_EXPIRY_DAYS = 7

app = FastAPI()
templates = Jinja2Templates(directory=os.path.join(APP_ROOT, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(APP_ROOT, "static")), name="static")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Database (databases + SQLAlchemy)
database = databases.Database(DATABASE_URL)
metadata = sqlalchemy.MetaData()

users = sqlalchemy.Table(
    "users", metadata,
    sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("username", sqlalchemy.String, unique=True, nullable=False),
    sqlalchemy.Column("password_hash", sqlalchemy.String, nullable=False),
)

searches = sqlalchemy.Table(
    "searches", metadata,
    sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("user_id", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("card_name", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("region", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("last_result", sqlalchemy.Text),
    sqlalchemy.Column("last_image", sqlalchemy.String),
    sqlalchemy.Column("last_updated", sqlalchemy.String),
    sqlalchemy.Column("confirmed", sqlalchemy.Boolean, default=False),
    sqlalchemy.UniqueConstraint("user_id", "card_name", name="u_user_card"),
)

# create sync engine to ensure tables exist on startup
engine = sqlalchemy.create_engine(DATABASE_URL.replace("+asyncpg", ""), pool_pre_ping=True)
metadata.create_all(engine)

# Password & JWT helpers
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password, hashed):
    return pwd_context.verify(plain_password, hashed)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                          detail="Could not validate credentials",
                                          headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    query = users.select().where(users.c.username == username)
    user = await database.fetch_one(query)
    if user is None:
        raise credentials_exception
    return user

# --- eBay Browse and image helpers ---
EBAY_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

def duckduckgo_image(card_name: str) -> Optional[str]:
    try:
        url = f"https://duckduckgo.com/i.js?q={quote(card_name)}&iax=images&ia=images"
        headers = {"User-Agent":"Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=8)
        j = r.json()
        if j.get("results"):
            return j["results"][0].get("image")
    except Exception:
        pass
    return None

def google_image_search(card_name: str) -> Optional[str]:
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return None
    try:
        q = quote(card_name)
        url = f"https://www.googleapis.com/customsearch/v1?q={q}&cx={GOOGLE_CX}&searchType=image&key={GOOGLE_API_KEY}&num=1"
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        j = r.json()
        items = j.get("items")
        if items:
            return items[0].get("link")
    except Exception:
        pass
    return None

def detect_grade_from_title(title: str) -> str:
    if not title:
        return "raw"
    t = title.lower()
    patterns = {
        "PSA": re.compile(r"\bpsa[\s\-]?(\d{1,2}(?:\.\d+)?)\b"),
        "BGS": re.compile(r"\bbgs[\s\-]?(\d{1,2}(?:\.\d+)?)\b"),
        "CGC": re.compile(r"\bcgc[\s\-]?(\d{1,2}(?:\.\d+)?)\b"),
    }
    for label, pat in patterns.items():
        if pat.search(t):
            return label
    return "raw"

def ebay_search_items(keyword: str, marketplace: str="EBAY_AU", limit:int=50) -> List[dict]:
    if not EBAY_OAUTH_TOKEN:
        return []
    headers = {"Authorization": f"Bearer {EBAY_OAUTH_TOKEN}", "Accept":"application/json"}
    params = {"q": keyword, "limit": limit, "fieldgroups": "ASPECT_REFINEMENT"}
    # marketplace filter: EBAY_AU or EBAY_US
    params["filter"] = f"marketplaceIds:({marketplace})"
    try:
        r = requests.get(EBAY_BROWSE_URL, headers=headers, params=params, timeout=12)
        r.raise_for_status()
        return r.json().get("itemSummaries", [])
    except Exception as e:
        print("eBay error:", e)
        return []

def gather_prices_by_grade(card_name: str, region: str="AU"):
    grades = {"raw": [], "PSA": [], "CGC": [], "BGS": []}
    image_url = None
    mp = "EBAY_AU" if region.upper()=="AU" else "EBAY_US"
    items = ebay_search_items(card_name, marketplace=mp, limit=50)
    for item in items:
        title = item.get("title","")
        price = None
        try:
            price = float(item.get("price", {}).get("value"))
        except:
            continue
        bucket = detect_grade_from_title(title)
        if bucket not in grades:
            bucket = "raw"
        if len(grades[bucket]) < SEARCH_LIMIT_PER_GRADE:
            grades[bucket].append(price)
        if not image_url:
            img = None
            if item.get("image") and item["image"].get("imageUrl"):
                img = item["image"]["imageUrl"]
            if not img and item.get("thumbnailImages"):
                try:
                    img = item["thumbnailImages"][0].get("imageUrl")
                except:
                    img = None
            if img:
                image_url = img
    if not image_url:
        image_url = google_image_search(card_name) or duckduckgo_image(card_name) or ""
    return grades, image_url

# --- Startup/shutdown ---
@app.on_event("startup")
async def startup():
    await database.connect()
    redis = await aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    await FastAPILimiter.init(redis)

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

# --- Routes / Templates ---
@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    # simple landing; front-end calls API for saved searches
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/login", response_class=HTMLResponse)
async def get_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.get("/register", response_class=HTMLResponse)
async def get_register(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})

# --- Auth & user management ---
@app.post("/register", dependencies=[Depends(RateLimiter(times=5, seconds=60))])
async def register(username: str = Form(...), password: str = Form(...)):
    existing = await database.fetch_one(users.select().where(users.c.username==username))
    if existing:
        raise HTTPException(status_code=400, detail="Username taken")
    pw_hash = get_password_hash(password)
    query = users.insert().values(username=username, password_hash=pw_hash)
    await database.execute(query)
    return RedirectResponse("/login", status_code=302)

@app.post("/token")
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user = await database.fetch_one(users.select().where(users.c.username==form_data.username))
    if not user or not verify_password(form_data.password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    access_token = create_access_token(data={"sub": user["username"]})
    return {"access_token": access_token, "token_type": "bearer"}

# --- API: search & save ---
@app.post("/api/search", dependencies=[Depends(RateLimiter(times=10, seconds=60))])
async def api_search(card_name: str = Form(...), region: str = Form("AU"), token: str = Depends(oauth2_scheme)):
    current = await get_current_user(token)
    grades, image_url = gather_prices_by_grade(card_name, region)
    avg = {k: round(sum(v)/len(v),2) if v else 0 for k,v in grades.items()}
    payload = {"avg": avg, "prices": grades}
    now = datetime.utcnow().isoformat()
    existing = await database.fetch_one(searches.select().where((searches.c.user_id==current["id"]) & (searches.c.card_name==card_name)))
    if existing:
        await database.execute(searches.update().where(searches.c.id==existing["id"]).values(last_result=json.dumps(payload), last_image=image_url, last_updated=now, confirmed=False))
    else:
        await database.execute(searches.insert().values(user_id=current["id"], card_name=card_name, region=region, last_result=json.dumps(payload), last_image=image_url, last_updated=now, confirmed=False))
    return JSONResponse({"ok": True, "result": payload, "image": image_url})

@app.post("/api/refresh", dependencies=[Depends(RateLimiter(times=5, seconds=60))])
async def api_refresh(card_name: str = Form(...), token: str = Depends(oauth2_scheme)):
    current = await get_current_user(token)
    row = await database.fetch_one(searches.select().where((searches.c.user_id==current["id"]) & (searches.c.card_name==card_name)))
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    region = row["region"]
    grades, image_url = gather_prices_by_grade(card_name, region)
    avg = {k: round(sum(v)/len(v),2) if v else 0 for k,v in grades.items()}
    now = datetime.utcnow().isoformat()
    await database.execute(searches.update().where(searches.c.id==row["id"]).values(last_result=json.dumps({"avg":avg,"prices":grades}), last_image=image_url, last_updated=now, confirmed=False))
    return {"ok": True, "avg": avg, "image": image_url}

@app.post("/api/confirm_image", dependencies=[Depends(RateLimiter(times=10, seconds=60))])
async def api_confirm_image(card_name: str = Form(...), image_url: str = Form(...), token: str = Depends(oauth2_scheme)):
    current = await get_current_user(token)
    row = await database.fetch_one(searches.select().where((searches.c.user_id==current["id"]) & (searches.c.card_name==card_name)))
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    now = datetime.utcnow().isoformat()
    await database.execute(searches.update().where(searches.c.id==row["id"]).values(last_image=image_url, confirmed=True, last_updated=now))
    return {"ok": True}

@app.get("/api/saved")
async def api_saved(token: str = Depends(oauth2_scheme)):
    current = await get_current_user(token)
    rows = await database.fetch_all(searches.select().where(searches.c.user_id==current["id"]))
    res = []
    for r in rows:
        last_result = json.loads(r["last_result"]) if r["last_result"] else {}
        last_updated = r["last_updated"]
        expired = not last_updated or (datetime.utcnow() - datetime.fromisoformat(last_updated) > timedelta(days=SAVED_SEARCH_EXPIRY_DAYS))
        res.append({"card_name": r["card_name"], "region": r["region"], "last_result": last_result, "last_image": r["last_image"], "last_updated": last_updated, "confirmed": bool(r["confirmed"]), "expired": expired})
    return res
