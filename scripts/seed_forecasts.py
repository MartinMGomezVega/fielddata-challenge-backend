"""Script para sembrar datos de prueba y simular la ingesta climática con upsert condicional.

Re-ejecutable e idempotente. Crea usuarios, campos en diferentes husos horarios
y un abanico completo de casos de prueba climáticos (borde 59.99/60.00, stale,
fechas pasadas, etc.).
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from agrobot.config import settings
from agrobot.domain.enums import AlertStatus, WeatherEvent
from agrobot.infrastructure.db.models import Alert, Field, User, WeatherForecast
from agrobot.infrastructure.db.session import dispose_engine, get_engine, get_session_factory
from agrobot.logging import configure_logging, get_logger

logger = get_logger("scripts.seed")

# IDs fijos deterministas para permitir re-ejecución idempotente
USER_1_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_2_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

FIELD_CORDOBA_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
FIELD_TOKYO_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

ALERT_FROST_60_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
ALERT_RAIN_50_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
ALERT_RAIN_80_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")


async def seed() -> None:
    configure_logging(settings.log_level)
    logger.info("seed_starting")

    engine = get_engine()
    session_factory = get_session_factory(engine)

    async with session_factory() as session:
        # 1. Usuarios
        users_data = [
            {"id": USER_1_ID, "name": "Juan Perez", "phone_number": "+5491122334455"},
            {"id": USER_2_ID, "name": "Akira Tanaka", "phone_number": "+819012345678"},
        ]
        for u in users_data:
            stmt = pg_insert(User).values(**u).on_conflict_do_nothing(index_elements=["id"])
            await session.execute(stmt)

        # 2. Campos en 2 zonas horarias distintas
        fields_data = [
            {
                "id": FIELD_CORDOBA_ID,
                "owner_id": USER_1_ID,
                "name": "Campo Los Molles (Córdoba)",
                "latitude": Decimal("-31.42010"),
                "longitude": Decimal("-64.18880"),
                "timezone": "America/Argentina/Cordoba",
            },
            {
                "id": FIELD_TOKYO_ID,
                "owner_id": USER_2_ID,
                "name": "Paddy Field (Kanto)",
                "latitude": Decimal("35.67620"),
                "longitude": Decimal("139.65030"),
                "timezone": "Asia/Tokyo",
            },
        ]
        for f in fields_data:
            stmt = pg_insert(Field).values(**f).on_conflict_do_nothing(index_elements=["id"])
            await session.execute(stmt)

        # 3. Alertas iniciales para pruebas
        alerts_data = [
            {
                "id": ALERT_FROST_60_ID,
                "user_id": USER_1_ID,
                "field_id": FIELD_CORDOBA_ID,
                "event_type": WeatherEvent.FROST,
                "threshold": Decimal("60.00"),
                "status": AlertStatus.ACTIVE,
                "version": 1,
            },
            {
                "id": ALERT_RAIN_50_ID,
                "user_id": USER_1_ID,
                "field_id": FIELD_CORDOBA_ID,
                "event_type": WeatherEvent.RAIN,
                "threshold": Decimal("50.00"),
                "status": AlertStatus.ACTIVE,
                "version": 1,
            },
            {
                "id": ALERT_RAIN_80_ID,
                "user_id": USER_1_ID,
                "field_id": FIELD_CORDOBA_ID,
                "event_type": WeatherEvent.RAIN,
                "threshold": Decimal("80.00"),
                "status": AlertStatus.ACTIVE,
                "version": 1,
            },
        ]
        for a in alerts_data:
            stmt = pg_insert(Alert).values(**a).on_conflict_do_nothing(index_elements=["id"])
            await session.execute(stmt)

        # 4. Pronósticos con upsert condicional
        now_utc = datetime.now(UTC)
        today_cba = now_utc.astimezone(ZoneInfo("America/Argentina/Cordoba")).date()
        today_tokyo = now_utc.astimezone(ZoneInfo("Asia/Tokyo")).date()

        forecasts_data = [
            # Caso 1: Borde exacto 60.00 para mañana en Córdoba (disparará ALERT_FROST_60_ID)
            {
                "field_id": FIELD_CORDOBA_ID,
                "forecast_date": today_cba + timedelta(days=1),
                "event_type": WeatherEvent.FROST,
                "probability": Decimal("60.00"),
                "source": "mock_seeder",
                "issued_at": now_utc - timedelta(hours=1),
            },
            # Caso 2: Borde 59.99 (no disparará)
            {
                "field_id": FIELD_CORDOBA_ID,
                "forecast_date": today_cba + timedelta(days=2),
                "event_type": WeatherEvent.FROST,
                "probability": Decimal("59.99"),
                "source": "mock_seeder",
                "issued_at": now_utc - timedelta(hours=1),
            },
            # Caso 3: Rain 85% (disparará ALERT_RAIN_50 y ALERT_RAIN_80)
            {
                "field_id": FIELD_CORDOBA_ID,
                "forecast_date": today_cba + timedelta(days=3),
                "event_type": WeatherEvent.RAIN,
                "probability": Decimal("85.00"),
                "source": "mock_seeder",
                "issued_at": now_utc - timedelta(hours=2),
            },
            # Caso 4: Pronóstico de ayer (fecha pasada -> ignorado por estar fuera de ventana)
            {
                "field_id": FIELD_CORDOBA_ID,
                "forecast_date": today_cba - timedelta(days=1),
                "event_type": WeatherEvent.HAIL,
                "probability": Decimal("90.00"),
                "source": "mock_seeder",
                "issued_at": now_utc - timedelta(hours=2),
            },
            # Caso 5: Stale (issued_at emitido hace 26 horas -> no dispara ni re-arma)
            {
                "field_id": FIELD_CORDOBA_ID,
                "forecast_date": today_cba + timedelta(days=4),
                "event_type": WeatherEvent.STRONG_WIND,
                "probability": Decimal("95.00"),
                "source": "mock_seeder",
                "issued_at": now_utc - timedelta(hours=26),
            },
            # Caso 6: Pronóstico en Tokyo para ventana local
            {
                "field_id": FIELD_TOKYO_ID,
                "forecast_date": today_tokyo + timedelta(days=1),
                "event_type": WeatherEvent.HEAT_WAVE,
                "probability": Decimal("75.00"),
                "source": "mock_seeder",
                "issued_at": now_utc - timedelta(hours=1),
            },
        ]

        # Pronósticos para los 7 días completos
        for day in range(1, 8):
            forecasts_data.append(
                {
                    "field_id": FIELD_CORDOBA_ID,
                    "forecast_date": today_cba + timedelta(days=day),
                    "event_type": WeatherEvent.HAIL,
                    "probability": Decimal(f"{20 + day * 5}.00"),
                    "source": "mock_seeder",
                    "issued_at": now_utc - timedelta(hours=1),
                }
            )

        upserted_count = 0
        for f in forecasts_data:
            stmt = pg_insert(WeatherForecast).values(**f)
            upsert_stmt = stmt.on_conflict_do_update(
                index_elements=["field_id", "forecast_date", "event_type"],
                set_={
                    "probability": stmt.excluded.probability,
                    "issued_at": stmt.excluded.issued_at,
                    "source": stmt.excluded.source,
                    "ingested_at": func.now(),
                },
                where=stmt.excluded.issued_at > WeatherForecast.issued_at,
            )
            res = await session.execute(upsert_stmt)
            rowcount = getattr(res, "rowcount", 0)
            if rowcount and rowcount > 0:
                upserted_count += 1

        await session.commit()
        logger.info(
            "seed_completed_successfully",
            users=len(users_data),
            fields=len(fields_data),
            alerts=len(alerts_data),
            forecasts_upserted=upserted_count,
        )

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(seed())
