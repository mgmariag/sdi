from __future__ import annotations

import random
from datetime import time as time_of_day
from decimal import Decimal
from typing import Any

from digital_twin.domain.plant import PlantCatalog, PlantType
from digital_twin.domain.pot import (
    PotExposureRules,
    PotSizeCatalog,
    PotSizeClass,
    PotSizeProfile,
)

DEFAULT_POT_COUNT = 200
DEFAULT_SEED = 2026

DEFAULT_PLANT_CATALOG = PlantCatalog(
    (
        PlantType(
            code="vegetables",
            label="Vegetables",
            water_need_level="high",
            moisture_min_pct=32,
            moisture_target_pct=55,
            moisture_max_pct=78,
            winter_moisture_target_pct=15,
            heat_sensitive=True,
            allows_second_watering=True,
            notes="Consistent moisture; likely candidate for second evening watering in heatwaves.",
            soil_profile="moisture_retentive_container_mix",
            flow_adjustment=Decimal("0.12"),
            sample_weight=0.24,
        ),
        PlantType(
            code="herbs",
            label="Herbs",
            water_need_level="medium",
            moisture_min_pct=28,
            moisture_target_pct=50,
            moisture_max_pct=74,
            winter_moisture_target_pct=15,
            heat_sensitive=True,
            allows_second_watering=True,
            notes="Most culinary herbs prefer morning watering and can need evening checks in hot wind.",
            soil_profile="free_draining_organic_mix",
            sample_weight=0.24,
        ),
        PlantType(
            code="ornamentals",
            label="Ornamentals",
            water_need_level="medium",
            moisture_min_pct=24,
            moisture_target_pct=35,
            moisture_max_pct=72,
            winter_moisture_target_pct=15,
            heat_sensitive=False,
            allows_second_watering=False,
            notes="Container ornamentals usually tolerate one morning watering unless exposed.",
            soil_profile="balanced_potting_mix",
            sample_weight=0.42,
        ),
        PlantType(
            code="succulents",
            label="Succulents",
            water_need_level="low",
            moisture_min_pct=12,
            moisture_target_pct=25,
            moisture_max_pct=45,
            winter_moisture_target_pct=15,
            heat_sensitive=False,
            allows_second_watering=False,
            notes="Drought tolerant; water less frequently and avoid prolonged wet soil.",
            soil_profile="gritty_fast_draining_mix",
            flow_adjustment=Decimal("-0.25"),
            sample_weight=0.10,
        ),
    )
)

DEFAULT_POT_SIZE_CATALOG = PotSizeCatalog(
    size_classes=(
        PotSizeClass("huge", sample_weight=0.12, cycle_soak=True),
        PotSizeClass("large", sample_weight=0.22, cycle_soak=True),
        PotSizeClass("medium", sample_weight=0.26),
        PotSizeClass("small", sample_weight=0.40),
    ),
    profiles=(
        PotSizeProfile("huge", "Huge planter", "huge", None, 70, 90, 30, 0.75, 1.35),
        PotSizeProfile("large", "Large pot", "large", None, 45, 45, 24, 0.9, 1.18),
        PotSizeProfile("medium", "Medium pot", "medium", None, 30, 20, 18, 1.0, 1.0),
        PotSizeProfile("small_7cm", "Small pot 7 cm", "small", "7cm", 7, 0.4, 4, 1.9, 0.45, 0.25),
        PotSizeProfile("small_15cm", "Small pot 15 cm", "small", "15cm", 15, 2.2, 8, 1.55, 0.62, 0.35),
        PotSizeProfile("small_30cm", "Small pot 30 cm", "small", "30cm", 30, 12, 14, 1.18, 0.88, 0.40),
    ),
)

DEFAULT_POT_EXPOSURE_RULES = PotExposureRules()


def seed_reference_data(conn) -> None:
    plant_types = DEFAULT_PLANT_CATALOG.reference_rows()
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO plant_types (
                code, label, water_need_level, moisture_min_pct, moisture_target_pct,
                moisture_max_pct, winter_moisture_target_pct, heat_sensitive,
                allows_second_watering, notes
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (code) DO UPDATE SET
                label = EXCLUDED.label,
                water_need_level = EXCLUDED.water_need_level,
                moisture_min_pct = EXCLUDED.moisture_min_pct,
                moisture_target_pct = EXCLUDED.moisture_target_pct,
                moisture_max_pct = EXCLUDED.moisture_max_pct,
                winter_moisture_target_pct = EXCLUDED.winter_moisture_target_pct,
                heat_sensitive = EXCLUDED.heat_sensitive,
                allows_second_watering = EXCLUDED.allows_second_watering,
                notes = EXCLUDED.notes
            """,
            plant_types,
        )

    size_profiles = DEFAULT_POT_SIZE_CATALOG.reference_rows()
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO pot_size_profiles (
                code, label, small_subtype, diameter_cm, volume_l,
                base_drip_flow_ml_min, evaporation_factor, retention_factor
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (code) DO UPDATE SET
                label = EXCLUDED.label,
                small_subtype = EXCLUDED.small_subtype,
                diameter_cm = EXCLUDED.diameter_cm,
                volume_l = EXCLUDED.volume_l,
                base_drip_flow_ml_min = EXCLUDED.base_drip_flow_ml_min,
                evaporation_factor = EXCLUDED.evaporation_factor,
                retention_factor = EXCLUDED.retention_factor
            """,
            size_profiles,
        )

def seed_pots(conn, target_count: int = DEFAULT_POT_COUNT, seed: int = DEFAULT_SEED) -> int:
    existing_count = conn.execute("SELECT count(*) FROM pots").fetchone()[0]
    if existing_count >= target_count:
        return 0

    rng = random.Random(seed)
    profiles = _load_size_profiles(conn)
    plant_types = _load_plant_types(conn)
    generated = [_generate_pot(i, rng, profiles, plant_types) for i in range(1, target_count + 1)]

    inserted = 0
    for pot in generated:
        result = conn.execute(
            """
            INSERT INTO pots (
                pot_code, label, size_class, small_subtype, plant_type_code,
                balcony_zone, rain_exposure, sun_exposure, wind_exposure,
                container_material, soil_profile, drip_flow_ml_min,
                cycle_soak_enabled, morning_window_start, morning_window_end,
                evening_window_start, evening_window_end, moisture_min_pct,
                moisture_target_pct, moisture_max_pct, winter_moisture_target_pct
            )
            VALUES (
                %(pot_code)s, %(label)s, %(size_class)s, %(small_subtype)s,
                %(plant_type_code)s, %(balcony_zone)s, %(rain_exposure)s,
                %(sun_exposure)s, %(wind_exposure)s,
                %(container_material)s, %(soil_profile)s, %(drip_flow_ml_min)s,
                %(cycle_soak_enabled)s, %(morning_window_start)s, %(morning_window_end)s,
                %(evening_window_start)s, %(evening_window_end)s, %(moisture_min_pct)s,
                %(moisture_target_pct)s, %(moisture_max_pct)s, %(winter_moisture_target_pct)s
            )
            ON CONFLICT (pot_code) DO NOTHING
            RETURNING id
            """,
            pot,
        ).fetchone()
        if result:
            inserted += 1

    return inserted

def sync_generated_pot_flow_rates(conn, target_count: int = DEFAULT_POT_COUNT, seed: int = DEFAULT_SEED) -> int:
    """Refresh generated demo-pot emitter rates after profile changes.

    Only deterministic POT-### seed records are touched. This keeps existing
    demo data realistic without overwriting unrelated/custom pot rows.
    """
    rng = random.Random(seed)
    profiles = _load_size_profiles(conn)
    plant_types = _load_plant_types(conn)
    generated = [_generate_pot(i, rng, profiles, plant_types) for i in range(1, target_count + 1)]

    updated = 0
    for pot in generated:
        result = conn.execute(
            """
            UPDATE pots
            SET drip_flow_ml_min = %(drip_flow_ml_min)s
            WHERE pot_code = %(pot_code)s
              AND pot_code ~ '^POT-[0-9]{3}$'
              AND drip_flow_ml_min IS DISTINCT FROM %(drip_flow_ml_min)s
            RETURNING id
            """,
            pot,
        ).fetchone()
        if result:
            updated += 1
    return updated

def _load_size_profiles(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT code, small_subtype, base_drip_flow_ml_min
        FROM pot_size_profiles
        """
    ).fetchall()
    return {row[0]: {"small_subtype": row[1], "base_drip_flow_ml_min": row[2]} for row in rows}

def _load_plant_types(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            code, moisture_min_pct, moisture_target_pct, moisture_max_pct,
            winter_moisture_target_pct, allows_second_watering
        FROM plant_types
        """
    ).fetchall()
    return {
        row[0]: {
            "moisture_min_pct": row[1],
            "moisture_target_pct": row[2],
            "moisture_max_pct": row[3],
            "winter_moisture_target_pct": row[4],
            "allows_second_watering": row[5],
        }
        for row in rows
    }

def _generate_pot(index: int, rng: random.Random, profiles: dict[str, dict[str, Any]], plant_types: dict[str, dict[str, Any]]) -> dict[str, Any]:
    size_class = _weighted_choice(
        rng,
        DEFAULT_POT_SIZE_CATALOG.weighted_size_distribution(),
    )
    small_subtype = None
    profile_code = size_class
    if DEFAULT_POT_SIZE_CATALOG.is_small(size_class):
        small_subtype = _weighted_choice(
            rng,
            DEFAULT_POT_SIZE_CATALOG.weighted_small_subtype_distribution(),
        )
        profile_code = DEFAULT_POT_SIZE_CATALOG.profile_code(size_class, small_subtype)

    plant_type_code = _weighted_choice(
        rng,
        DEFAULT_PLANT_CATALOG.weighted_distribution(),
    )
    plant_type = plant_types[plant_type_code]
    profile = profiles[profile_code]

    sun_exposure = _weighted_choice(
        rng,
        DEFAULT_POT_EXPOSURE_RULES.weighted_sun_distribution(),
    )
    wind_exposure = _weighted_choice(
        rng,
        DEFAULT_POT_EXPOSURE_RULES.weighted_wind_distribution(),
    )
    drip_flow = _adjust_flow(profile["base_drip_flow_ml_min"], plant_type_code, sun_exposure, wind_exposure, rng)
    cycle_soak = DEFAULT_POT_SIZE_CATALOG.uses_cycle_soak(size_class) or DEFAULT_POT_EXPOSURE_RULES.is_hot_gusty(
        sun_exposure,
        wind_exposure,
    )
    if DEFAULT_PLANT_CATALOG.has_low_water_need(plant_type_code):
        cycle_soak = False

    balcony_zone = _weighted_choice(
        rng,
        [
            ("south_rail", 0.30),
            ("west_wall", 0.22),
            ("east_corner", 0.18),
            ("north_shelter", 0.14),
            ("hanging_row", 0.16),
        ],
    )

    return {
        "pot_code": f"POT-{index:03d}",
        "label": _pot_label(index, size_class, small_subtype, plant_type_code),
        "size_class": size_class,
        "small_subtype": small_subtype,
        "plant_type_code": plant_type_code,
        "balcony_zone": balcony_zone,
        "rain_exposure": DEFAULT_POT_EXPOSURE_RULES.rain_exposure_for_zone(balcony_zone),
        "sun_exposure": sun_exposure,
        "wind_exposure": wind_exposure,
        "container_material": _weighted_choice(
            rng,
            [
                ("terracotta", 0.34),
                ("plastic", 0.30),
                ("ceramic", 0.22),
                ("fabric", 0.14),
            ],
        ),
        "soil_profile": DEFAULT_PLANT_CATALOG.soil_profile(plant_type_code),
        "drip_flow_ml_min": drip_flow,
        "cycle_soak_enabled": cycle_soak,
        "morning_window_start": time_of_day(6, 0),
        "morning_window_end": time_of_day(9, 0),
        "evening_window_start": time_of_day(17, 0),
        "evening_window_end": time_of_day(19, 0),
        "moisture_min_pct": plant_type["moisture_min_pct"],
        "moisture_target_pct": plant_type["moisture_target_pct"],
        "moisture_max_pct": plant_type["moisture_max_pct"],
        "winter_moisture_target_pct": plant_type["winter_moisture_target_pct"],
    }

def _weighted_choice(rng: random.Random, options: list[tuple[str, float]]) -> str:
    total = sum(weight for _, weight in options)
    marker = rng.uniform(0, total)
    cumulative = 0.0
    for value, weight in options:
        cumulative += weight
        if marker <= cumulative:
            return value
    return options[-1][0]

def _adjust_flow(base_flow: Decimal, plant_type_code: str, sun_exposure: str, wind_exposure: str, rng: random.Random) -> Decimal:
    multiplier = (
        Decimal("1.0")
        + DEFAULT_PLANT_CATALOG.flow_adjustment(plant_type_code)
        + DEFAULT_POT_EXPOSURE_RULES.flow_adjustment(sun_exposure, wind_exposure)
    )

    jitter = Decimal(str(round(rng.uniform(-0.08, 0.08), 3)))
    flow = Decimal(base_flow) * (multiplier + jitter)
    return flow.quantize(Decimal("0.01"))

def _pot_label(index: int, size_class: str, small_subtype: str | None, plant_type_code: str) -> str:
    size = f"{size_class} {small_subtype}" if small_subtype else size_class
    plant = DEFAULT_PLANT_CATALOG.label_for(plant_type_code)
    return f"{size.title()} {plant.title()} Pot {index:03d}"
