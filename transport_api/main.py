import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from .provider import ROOT, Provider, ProviderError, Result, create_provider

load_dotenv(ROOT / ".env", override=False)


class Meta(BaseModel):
    source: str = "Pallium / Kahramankart"
    fetched_at: str
    cached: bool


class Envelope(BaseModel):
    data: Any
    meta: Meta


def wrap(result: Result, data: Any = None) -> Envelope:
    return Envelope(data=result.data if data is None else data,
                    meta=Meta(fetched_at=result.fetched_at, cached=result.cached))


def require_field(result: Result, name: str, expected_type: type):
    value = result.data.get(name)
    if not isinstance(value, expected_type):
        raise ProviderError("UPSTREAM_SCHEMA_CHANGED", f"Ulaşım servisinin {name} alanı beklenen biçimde değil.")
    return value


def create_app(provider_factory=create_provider) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        app.state.provider = provider_factory()
        try:
            yield
        finally:
            await app.state.provider.close()

    app = FastAPI(
        title="Kahramankart Ulaşım API",
        version="1.0.0",
        description="Hat, durak, güzergâh ve ulaşım verileri. Resmî olmayan sunucu adaptörü. "
                    "Boş araç listesi veri yokluğunu gösterir; araç konumu uydurulmaz.",
        lifespan=lifespan,
    )
    origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",") if x.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins,
                       allow_methods=["GET"], allow_headers=["X-API-Key", "Content-Type"])
    local_api_key = os.getenv("API_ACCESS_KEY", "")
    key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    async def access(key: Annotated[str | None, Depends(key_header)]):
        if local_api_key and (not key or not secrets.compare_digest(key, local_api_key)):
            raise HTTPException(401, detail={"code": "INVALID_API_KEY", "message": "API anahtarı gerekli."})

    async def provider(request: Request) -> Provider:
        return request.app.state.provider

    P = Annotated[Provider, Depends(provider)]
    ID = Annotated[int, Path(gt=0)]
    Direction = Annotated[int, Query(ge=1, le=3, description="Arama sonucundaki direction değeri.")]

    @app.exception_handler(ProviderError)
    async def provider_error(request, error):
        return JSONResponse(status_code=error.status,
                            content={"error": {"code": error.code, "message": error.message}})

    @app.get("/healthz", tags=["Sistem"])
    async def health():
        return {"status": "ok", "version": "1.0.0", "upstream_checked": False}

    router = APIRouter(prefix="/v1", dependencies=[Depends(access)])

    @router.get("/capabilities", tags=["Sistem"])
    async def capabilities():
        return {
            "available": ["search", "nearby_stops", "map_stops", "stop_details", "stop_lines",
                          "arrivals", "line_details", "line_stops", "line_shape", "vehicles",
                          "timetables", "announcements", "journey_points", "journey_planning"],
            "live_data": {"vehicles": "22 Ekim 2026: H/1 üzerinde 3 araç; iki örnekte aynı plakalar ve değişen koordinatlar doğrulandı.",
                          "arrivals": "22 Ekim 2026: 6968 durağında dolu buses yanıtı ve kaynak tahmini remaining_time doğrulandı.",
                          "gps_timestamp": "Gözlenen yanıtta GPS ölçüm zamanı yok; fetched_at yalnız API alınma zamanıdır.",
                          "citywide_fleet": "Tüm araçları tek çağrıda veren bir uç doğrulanmadı."},
            "client_features": ["favorites", "map_rendering", "location_permission"],
            "journey_calendar_date": "Kaynak yalnızca haftanın günü, saat ve dakika alır; tam takvim tarihi göndermez.",
            "not_implemented": ["push_notifications", "alarm_service", "payments", "user_accounts"],
        }

    @router.get("/search", response_model=Envelope, tags=["Arama"])
    async def search(p: P, q: Annotated[str, Query(min_length=1, max_length=100)],
                     stops: bool = True, lines: bool = True, addresses: bool = False):
        if not any((stops, lines, addresses)):
            raise HTTPException(422, detail="En az bir arama türü seçin.")
        return wrap(await p.get("Search", {"SearchString": q, "busStops": int(stops),
                                          "adress": int(addresses), "busLines": int(lines)}))

    @router.get("/stops/nearby", response_model=Envelope, tags=["Duraklar"])
    async def nearby(p: P, latitude: Annotated[float, Query(ge=-90, le=90)],
                     longitude: Annotated[float, Query(ge=-180, le=180)]):
        return wrap(await p.get("NearestStops", {"latitude": latitude, "longitude": longitude}, ttl=30))

    @router.get("/stops/in-bounds", response_model=Envelope, tags=["Duraklar"])
    async def bounds(p: P, south: Annotated[float, Query(ge=-90, le=90)],
                     west: Annotated[float, Query(ge=-180, le=180)],
                     north: Annotated[float, Query(ge=-90, le=90)],
                     east: Annotated[float, Query(ge=-180, le=180)]):
        if south >= north or west >= east:
            raise HTTPException(422, detail="south < north ve west < east olmalıdır.")
        # Preserve the provider's observed corner mapping, despite its unusual names.
        params = dict(topLeftLatitude=south, topLeftLongitude=east,
                      topRightLatitude=north, topRightLongitude=east,
                      bottomLeftLatitude=south, bottomLeftLongitude=west,
                      bottomRightLatitude=north, bottomRightLongitude=west)
        return wrap(await p.get("BusStopLocationInfo", params, ttl=300))

    @router.get("/stops/{stop_id}", response_model=Envelope, tags=["Duraklar"])
    async def stop(p: P, stop_id: ID):
        return wrap(await p.get("BusStop", {"stopId": stop_id}, ttl=10))

    @router.get("/stops/{stop_id}/lines", response_model=Envelope, tags=["Duraklar"])
    async def stop_lines(p: P, stop_id: ID):
        return wrap(await p.get("StopLines", {"stopId": stop_id}, ttl=300))

    @router.get("/stops/{stop_id}/arrivals", response_model=Envelope, tags=["Canlı veriler"])
    async def arrivals(p: P, stop_id: ID):
        result = await p.get("BusStop", {"stopId": stop_id}, ttl=10)
        return wrap(result, require_field(result, "buses", list))

    @router.get("/lines/{line_id}", response_model=Envelope, tags=["Hatlar"])
    async def line(p: P, line_id: ID, direction: Direction = 1):
        return wrap(await p.get("BusLine", {"lineId": line_id, "direction": direction}, ttl=10))

    @router.get("/lines/{line_id}/vehicles", response_model=Envelope, tags=["Canlı veriler"])
    async def vehicles(p: P, line_id: ID, direction: Direction = 1):
        result = await p.get("BusLine", {"lineId": line_id, "direction": direction}, ttl=10)
        info = require_field(result, "busLineInfo", dict)
        if not isinstance(info.get("liveBuses"), list):
            raise ProviderError("UPSTREAM_SCHEMA_CHANGED", "Canlı araç yanıtının biçimi değişmiş.")
        return wrap(result, info["liveBuses"])

    @router.get("/lines/{line_id}/stops", response_model=Envelope, tags=["Hatlar"])
    async def line_stops(p: P, line_id: ID, direction: Direction = 1):
        return wrap(await p.get("LineStop", {"lineId": line_id, "direction": direction}, ttl=300))

    @router.get("/lines/{line_id}/shape", response_model=Envelope, tags=["Hatlar"])
    async def shape(p: P, line_id: ID, direction: Direction = 1, format: Literal["json", "geojson"] = "json"):
        result = await p.get("BusLinePoints", {"lineId": line_id, "direction": direction}, ttl=300)
        if format == "json":
            return wrap(result)
        coordinates = require_field(result, "lineCoordinates", list)
        try:
            points = [[float(x["longitude"]), float(x["latitude"])] for x in coordinates]
            if any(not (-180 <= lon <= 180 and -90 <= lat <= 90) for lon, lat in points):
                raise ValueError()
        except (TypeError, KeyError, ValueError):
            raise ProviderError("UPSTREAM_SCHEMA_CHANGED", "Güzergâh koordinatlarının biçimi değişmiş.") from None
        return wrap(result, {"type": "Feature", "properties": {"line_id": line_id, "direction": direction},
                             "geometry": {"type": "LineString", "coordinates": points} if len(points) >= 2 else None})

    @router.get("/timetables", response_model=Envelope, tags=["Seferler"])
    async def timetables(p: P, line_code: Annotated[str, Query(min_length=1, max_length=40)]):
        return wrap(await p.get("TimeTableAllDirection", {"lineCode": line_code}, ttl=300))

    @router.get("/announcements", response_model=Envelope, tags=["Duyurular"])
    async def announcements(p: P):
        return wrap(await p.get("Announcements", ttl=60))

    @router.get("/journey-points", response_model=Envelope, tags=["Yolculuk"])
    async def journey_points(p: P, q: Annotated[str, Query(min_length=2, max_length=100)]):
        return wrap(await p.post("GetJourneyPoint", {"searchText": q}, ttl=300))

    @router.get("/journeys", response_model=Envelope, tags=["Yolculuk"])
    async def journeys(p: P, from_stop_id: Annotated[int, Query(gt=0)],
                       to_stop_id: Annotated[int, Query(gt=0)],
                       day: Annotated[int | None, Query(ge=0, le=6, description="0 Pazar, 1 Pazartesi … 6 Cumartesi.")] = None,
                       hour: Annotated[int | None, Query(ge=0, le=23)] = None,
                       minute: Annotated[int | None, Query(ge=0, le=59)] = None,
                       walking_meters: Annotated[int, Query(ge=0, le=5000)] = 1000,
                       waiting_minutes: Annotated[int, Query(ge=0, le=120)] = 20):
        if from_stop_id == to_stop_id:
            raise HTTPException(422, detail="Başlangıç ve varış durakları farklı olmalıdır.")
        origin, destination = await asyncio.gather(
            p.get("BusStop", {"stopId": from_stop_id}, ttl=10),
            p.get("BusStop", {"stopId": to_stop_id}, ttl=10))
        loc_from, loc_to = require_field(origin, "location", dict), require_field(destination, "location", dict)
        try:
            lat1, lng1 = float(loc_from["latitude"]), float(loc_from["longitude"])
            lat2, lng2 = float(loc_to["latitude"]), float(loc_to["longitude"])
            if not (-90 <= lat1 <= 90 and -90 <= lat2 <= 90 and -180 <= lng1 <= 180 and -180 <= lng2 <= 180):
                raise ValueError()
        except (TypeError, KeyError, ValueError):
            raise ProviderError("UPSTREAM_SCHEMA_CHANGED", "Durak koordinatları beklenen biçimde değil.") from None
        now = datetime.now(ZoneInfo("Europe/Istanbul"))
        body = {"modes": 1, "day": now.isoweekday() % 7 if day is None else day,
                "hour": f"{now.hour if hour is None else hour:02d}",
                "minute": f"{now.minute if minute is None else minute:02d}",
                "waiting": waiting_minutes, "walking": walking_meters,
                "latForFrom": lat1, "lngForFrom": lng1, "latForTo": lat2, "lngForTo": lng2,
                "fromId": from_stop_id, "toId": to_stop_id}
        return wrap(await p.post("GetJourneyPlanner", body, ttl=30))

    app.include_router(router)
    return app


app = create_app()
