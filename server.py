from fastapi import FastAPI, Request
from starlette.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware  # ← ADDED
import jwt
from jwt import PyJWKClient
import httpx
import asyncio
from datetime import datetime
import pytz
import logging
import json
import os

app = FastAPI()

# ==============================
# ✅ CORS (allow frontend calls)  ← ADDED
# ==============================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==============================
# 🪵 LOGGING CONFIG
# ==============================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("MCP_SERVER")

# ==============================
# 🔒 GATEWAY SECRET
# ==============================
GATEWAY_SECRET = os.environ.get("GATEWAY_SECRET", "")

CLERK_JWKS_URL = os.environ.get("CLERK_JWKS_URL", "...")
CLERK_ISSUER = os.environ.get("CLERK_ISSUER", "...")
jwks_client = PyJWKClient(CLERK_JWKS_URL)

# Shared async client (IMPORTANT for performance)
client = httpx.AsyncClient(timeout=10)


# ==============================
# 🔧 SAFE ASYNC REQUEST HELPERS
# ==============================

async def safe_get_json(url, method="GET", json_body=None, timeout=10):
    logger.info(f"➡️ {method} JSON: {url}")

    try:
        if method == "POST":
            res = await client.post(url, json=json_body, timeout=timeout)
        else:
            res = await client.get(url, timeout=timeout)

        logger.info(f"⬅️ Status: {res.status_code}")

        if res.status_code != 200:
            logger.error(f"❌ Bad status: {res.status_code}")
            return None

        if not res.text.strip():
            logger.error("❌ Empty response")
            return None

        return res.json()

    except httpx.ReadTimeout:
        logger.error("⏳ Timeout")
        return None

    except Exception as e:
        logger.exception(f"❌ JSON Error: {e}")
        return None


async def safe_get_text(url, timeout=10):
    logger.info(f"➡️ GET TEXT: {url}")

    try:
        res = await client.get(url, timeout=timeout)

        logger.info(f"⬅️ Status: {res.status_code}")

        if res.status_code != 200:
            logger.error(f"❌ Text status: {res.status_code}")
            return None

        return res.text

    except httpx.ReadTimeout:
        logger.error("⏳ Text timeout")
        return None

    except Exception as e:
        logger.exception(f"❌ TEXT Error: {e}")
        return None


# ==============================
# 🌍 COORDINATES — MULTIPLE SOURCES
# ==============================

async def get_coordinates_openmeteo(city):
    """Primary: Open-Meteo Geocoding"""
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1"
    res = await safe_get_json(url)
    
    if not res or "results" not in res or not res["results"]:
        return None
    
    data = res["results"][0]
    return {
        "city": data.get("name"),
        "country": data.get("country"),
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude"),
        "timezone": data.get("timezone")
    }


async def get_coordinates_nominatim(city):
    """Fallback 1: OpenStreetMap Nominatim"""
    url = f"https://nominatim.openstreetmap.org/search?q={city}&format=json&limit=1"
    headers = {"User-Agent": "GeoBot-MCP/1.0"}
    
    try:
        res = await client.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            return None
        
        data = res.json()
        if not data or len(data) == 0:
            return None
        
        place = data[0]
        return {
            "city": place.get("display_name", "").split(",")[0],
            "country": place.get("display_name", "").split(",")[-1].strip(),
            "latitude": float(place.get("lat")),
            "longitude": float(place.get("lon")),
            "timezone": "UTC"
        }
    except Exception as e:
        logger.warning(f"Nominatim failed: {e}")
        return None


async def get_coordinates_geocode_xyz(city):
    """Fallback 2: Geocode.xyz"""
    url = f"https://geocode.xyz/{city}?json=1"
    
    try:
        res = await client.get(url, timeout=10)
        if res.status_code != 200:
            return None
        
        data = res.json()
        if "error" in data:
            return None
        
        return {
            "city": data.get("standard", {}).get("city", city),
            "country": data.get("standard", {}).get("countryname", "Unknown"),
            "latitude": float(data.get("latt", 0)),
            "longitude": float(data.get("longt", 0)),
            "timezone": "UTC"
        }
    except Exception as e:
        logger.warning(f"Geocode.xyz failed: {e}")
        return None


def _mask_token(token: str) -> str:
    if not token:
        return "[none]"
    if len(token) <= 12:
        return "***"
    return token[:8] + "..."

def verify_clerk_token(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        logger.info("🔐 [AUTH] No Bearer token in request")
        return None
    token = auth.split(" ", 1)[1]
    masked = _mask_token(token)
    logger.info(f"🔐 [AUTH] Verifying token: {masked}")
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=CLERK_ISSUER,
            options={"verify_aud": False, "verify_exp": True}
        )
        logger.info(f"🔐 [AUTH] Token valid for user: {payload.get('email', payload.get('sub', 'unknown'))}")
        return payload
    except Exception as e:
        logger.warning(f"🔐 [AUTH] Token verification failed: {masked} | error: {e}")
        return None

def verify_gateway_secret(request: Request):
    secret = request.headers.get("X-Gateway-Secret", "")
    if not GATEWAY_SECRET:
        logger.warning("⚠️ GATEWAY_SECRET not set — allowing all requests (dev mode)")
        return True
    if secret != GATEWAY_SECRET:
        logger.warning(f"❌ Invalid X-Gateway-Secret: {secret[:8]}...")
        return False
    logger.info("✅ X-Gateway-Secret verified")
    return True

async def get_coordinates(city):
    """Try multiple coordinate sources with fallback"""
    logger.info(f"🌍 Fetching coordinates for: {city}")
    
    sources = [
        ("Open-Meteo", get_coordinates_openmeteo),
        ("Nominatim", get_coordinates_nominatim),
        ("Geocode.xyz", get_coordinates_geocode_xyz)
    ]
    
    for name, fn in sources:
        try:
            logger.info(f"🌍 Trying {name}...")
            result = await fn(city)
            if result and result.get("latitude") and result.get("longitude"):
                logger.info(f"✅ {name} success: {result['city']}, {result['country']}")
                return result
        except Exception as e:
            logger.warning(f"❌ {name} failed: {e}")
    
    logger.error("❌ All coordinate sources failed")
    return None


# ==============================
# 🌡 WEATHER — MULTIPLE SOURCES
# ==============================

async def get_weather_openmeteo(lat, lon):
    """Primary: Open-Meteo Weather"""
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
    res = await safe_get_json(url)
    return res.get("current_weather") if res else None


async def get_weather_openmeteo_archive(lat, lon):
    """Fallback 1: Open-Meteo Archive (uses past data as estimate)"""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    url = f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}&start_date={today}&end_date={today}&daily=temperature_2m_max,temperature_2m_min&timezone=auto"
    
    try:
        res = await safe_get_json(url)
        if not res or "daily" not in res:
            return None
        
        daily = res["daily"]
        return {
            "temperature": (daily.get("temperature_2m_max", [None])[0] + daily.get("temperature_2m_min", [None])[0]) / 2 if daily.get("temperature_2m_max") else None,
            "windspeed": None,
            "winddirection": None,
            "weathercode": None,
            "time": datetime.utcnow().isoformat(),
            "source": "openmeteo_archive"
        }
    except Exception as e:
        logger.warning(f"Open-Meteo archive failed: {e}")
        return None


async def get_weather_7timer(lat, lon):
    """Fallback 2: 7Timer (simple, reliable)"""
    url = f"https://www.7timer.info/bin/api.pl?lon={lon}&lat={lat}&product=civil&output=json"
    
    try:
        res = await safe_get_json(url, timeout=8)
        if not res or "dataseries" not in res or not res["dataseries"]:
            return None
        
        current = res["dataseries"][0]
        temp = current.get("temp2m", 0)
        
        weather_codes = {
            "clear": 0, "cloudy": 1, "rain": 51, "snow": 71, "storm": 95
        }
        weather = current.get("weather", "clear")
        
        return {
            "temperature": temp,
            "windspeed": current.get("wind10m", {}).get("speed", 0),
            "winddirection": current.get("wind10m", {}).get("direction", 0),
            "weathercode": weather_codes.get(weather, 0),
            "time": datetime.utcnow().isoformat(),
            "source": "7timer"
        }
    except Exception as e:
        logger.warning(f"7Timer failed: {e}")
        return None


async def get_weather(lat, lon):
    """Try multiple weather sources with fallback"""
    logger.info(f"🌡 Fetching weather for: {lat}, {lon}")
    
    sources = [
        ("Open-Meteo", get_weather_openmeteo),
        ("Open-Meteo Archive", get_weather_openmeteo_archive),
        ("7Timer", get_weather_7timer)
    ]
    
    for name, fn in sources:
        try:
            logger.info(f"🌡 Trying {name}...")
            result = await fn(lat, lon)
            if result and result.get("temperature") is not None:
                logger.info(f"✅ {name} success: {result['temperature']}°C")
                return result
        except Exception as e:
            logger.warning(f"❌ {name} failed: {e}")
    
    logger.error("❌ All weather sources failed")
    return None


# ==============================
# 🌫 AQI — MULTIPLE SOURCES
# ==============================

async def get_aqi_openmeteo(lat, lon):
    """Primary: Open-Meteo Air Quality"""
    url = f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&current=pm10,pm2_5,us_aqi"
    res = await safe_get_json(url)
    return res.get("current") if res else None


async def get_aqi_waqi(lat, lon):
    """Fallback 1: WAQI (World Air Quality Index) - no key needed for basic"""
    url = f"https://api.waqi.info/feed/geo:{lat};{lon}/?token=demo"
    
    try:
        res = await safe_get_json(url, timeout=8)
        if not res or res.get("status") != "ok":
            return None
        
        data = res.get("data", {})
        iaqi = data.get("iaqi", {})
        
        return {
            "pm10": iaqi.get("pm10", {}).get("v"),
            "pm2_5": iaqi.get("pm25", {}).get("v"),
            "us_aqi": data.get("aqi"),
            "source": "waqi"
        }
    except Exception as e:
        logger.warning(f"WAQI failed: {e}")
        return None


async def get_aqi(lat, lon):
    """Try multiple AQI sources with fallback"""
    logger.info(f"🌫 Fetching AQI for: {lat}, {lon}")
    
    sources = [
        ("Open-Meteo AQI", get_aqi_openmeteo),
        ("WAQI", get_aqi_waqi)
    ]
    
    for name, fn in sources:
        try:
            logger.info(f"🌫 Trying {name}...")
            result = await fn(lat, lon)
            if result and (result.get("us_aqi") or result.get("pm2_5") or result.get("pm10")):
                logger.info(f"✅ {name} success")
                return result
        except Exception as e:
            logger.warning(f"❌ {name} failed: {e}")
    
    logger.warning("⚠️ All AQI sources failed — returning None (optional field)")
    return None


# ==============================
# 🕒 TIME (sync is fine)
# ==============================

def get_time(timezone):
    try:
        tz = pytz.timezone(timezone)
        return datetime.now(tz).strftime("%d %B %Y, %I:%M %p")
    except Exception as e:
        logger.exception(f"❌ Time error: {e}")
        return None


# ==============================
# 🎉 HOLIDAY — MULTIPLE SOURCES
# ==============================

async def get_today_holiday_nager(country_code="IN"):
    """Primary: Nager.Date API"""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    year = today[:4]

    url = f"https://date.nager.at/api/v3/PublicHolidays/{year}/{country_code}"
    res = await safe_get_json(url)

    if not res:
        return None

    for holiday in res:
        if holiday.get("date") == today:
            return holiday.get("localName")

    return None


async def get_today_holiday(country_code="IN"):
    """Try multiple holiday sources"""
    sources = [
        ("Nager.Date", get_today_holiday_nager)
    ]
    
    for name, fn in sources:
        try:
            logger.info(f"🎉 Trying {name}...")
            result = await fn(country_code)
            if result:
                logger.info(f"✅ {name} success: {result}")
                return result
        except Exception as e:
            logger.warning(f"❌ {name} failed: {e}")
    
    logger.warning("⚠️ All holiday sources failed")
    return None


# ==============================
# 📚 FACT — MULTIPLE SOURCES
# ==============================

async def get_today_fact_numbersapi():
    """Primary: Numbers API"""
    today = datetime.utcnow()
    url = f"http://numbersapi.com/{today.month}/{today.day}/date"
    return await safe_get_text(url)


async def get_today_fact_wikipedia():
    """Fallback: Wikipedia 'On this day'"""
    today = datetime.utcnow()
    month_name = today.strftime("%B")
    day = today.day
    
    url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{month_name}/{day}"
    
    try:
        res = await safe_get_json(url, timeout=8)
        if not res or "events" not in res or not res["events"]:
            return None
        
        event = res["events"][0]
        year = event.get("year", "")
        text = event.get("text", "")
        return f"In {year}, {text}"
    except Exception as e:
        logger.warning(f"Wikipedia facts failed: {e}")
        return None


async def get_today_fact():
    """Try multiple fact sources"""
    sources = [
        ("Numbers API", get_today_fact_numbersapi),
        ("Wikipedia", get_today_fact_wikipedia)
    ]
    
    for name, fn in sources:
        try:
            logger.info(f"📚 Trying {name}...")
            result = await fn()
            if result:
                logger.info(f"✅ {name} success")
                return result
        except Exception as e:
            logger.warning(f"❌ {name} failed: {e}")
    
    logger.warning("⚠️ All fact sources failed")
    return None


# ==============================
# ♻️ KEEP-ALIVE (Self-ping to prevent Render sleep)
# ==============================

SELF_URL =  os.environ.get("SELF_URL", "")
KEEP_ALIVE_INTERVAL = 540  # 9 minutes (in seconds)

async def keep_alive_loop():
    """
    Background task that pings this server every 9 minutes
    to prevent Render.com from spinning down the free tier instance.
    """
    logger.info(f"♻️ Keep-alive started — pinging {SELF_URL} every {KEEP_ALIVE_INTERVAL // 60} minutes")
    
    # Wait a bit on first startup so the server is fully ready
    await asyncio.sleep(30)
    
    while True:
        try:
            async with httpx.AsyncClient(timeout=15) as ping_client:
                # Ping /ping (public, no auth) instead of /health (protected)
                ping_url = SELF_URL.replace("/tool", "/ping")
                response = await ping_client.get(ping_url, timeout=15)
                
                if response.status_code == 200:
                    logger.info("♻️ Keep-alive ping successful — /ping is awake")
                else:
                    logger.warning(f"♻️ Keep-alive ping returned status {response.status_code}")
                    
        except Exception as e:
            logger.warning(f"♻️ Keep-alive ping failed: {e}")
        
        await asyncio.sleep(KEEP_ALIVE_INTERVAL)


@app.on_event("startup")
async def startup_event():
    """
    Start the keep-alive background task when the server boots up.
    """
    asyncio.create_task(keep_alive_loop())
    logger.info("🚀 MCP Server startup complete — keep-alive task registered")


## ==============================
# 🏓 PUBLIC PING (for keep-alive, no auth)
# ==============================

@app.get("/ping")
@app.head("/ping")
def ping():
    return {"status": "ok"}


# ==============================
# ❤️ HEALTH (protected — used by gateway/agent)
# ==============================

@app.get("/health")
@app.head("/health")
def health(request: Request):
    user = verify_clerk_token(request)
    if not user:
        return JSONResponse(
            {"error": "Unauthorized — valid Bearer token required"},
            status_code=401
        )
    logger.info(f"🔐 [AUTH] /health accessed by: {user.get('email', user.get('sub', 'unknown'))}")
    return {"status": "ok"}


# ==============================
# 🧠 TOOL HANDLER (protected)
# ==============================

@app.post("/tool")
async def tool_handler(request: Request):
    # 1. Verify Gateway Secret FIRST
    if not verify_gateway_secret(request):
        logger.warning("❌ [GATEWAY] /tool rejected — invalid gateway secret")
        return JSONResponse(
            {"error": "Unauthorized — Invalid gateway secret"},
            status_code=401
        )
    
    # 2. Verify Bearer token from backend
    user = verify_clerk_token(request)
    if not user:
        logger.warning("🔐 [AUTH] /tool rejected — no valid token")
        return JSONResponse(
            {"error": "Unauthorized — valid Bearer token required"},
            status_code=401
        )
    logger.info(f"🔐 [AUTH] /tool accessed by: {user.get('email', user.get('sub', 'unknown'))}")

    try:
        payload = await request.json()

        logger.info("🔥 MCP SERVER HIT")
        logger.info(json.dumps(payload, indent=2))

        tool = payload.get("tool")
        city = payload.get("input")

        # ❤️ HEALTH CHECK
        if tool == "healthCheck":
            return {
                "status": "ok",
                "server": "MCP ASYNC RUNNING",
                "version": "V8-FALLBACK",
                "features": {
                    "coordinates": ["openmeteo", "nominatim", "geocode_xyz"],
                    "weather": ["openmeteo", "openmeteo_archive", "7timer"],
                    "aqi": ["openmeteo_aqi", "waqi"],
                    "holiday": ["nager"],
                    "facts": ["numbersapi", "wikipedia"]
                }
            }

        if not city:
            return {"error": "No city provided"}

        coord = await get_coordinates(city)

        if not coord:
            return {"error": "City not found — tried Open-Meteo, Nominatim, and Geocode.xyz"}

        lat = coord["latitude"]
        lon = coord["longitude"]

        # 🚀 PARALLEL EXECUTION (KEY BOOST)
        weather_task = get_weather(lat, lon)
        aqi_task = get_aqi(lat, lon)
        holiday_task = get_today_holiday("IN")
        fact_task = get_today_fact()

        weather, aqi, holiday, fact = await asyncio.gather(
            weather_task,
            aqi_task,
            holiday_task,
            fact_task
        )

        result = {
            "source": "MCP_SERVER_V8_FALLBACK",
            "city": coord["city"],
            "country": coord["country"],
            "latitude": lat,
            "longitude": lon
        }

        if weather:
            result["weather"] = weather

        if aqi:
            result["aqi"] = aqi

        current_time = get_time(coord["timezone"])
        if current_time:
            result["current_time"] = current_time

        special = {}
        if holiday:
            special["holiday"] = holiday
        if fact:
            special["fact"] = fact

        if special:
            result["today_special"] = special

        logger.info("✅ Final Response:")
        logger.info(json.dumps(result, indent=2))

        return result

    except Exception as e:
        logger.exception(f"💥 CRITICAL ERROR: {e}")
        return {"error": "Internal server error"}