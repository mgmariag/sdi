from __future__ import annotations

import random
from datetime import time as time_of_day
from decimal import Decimal
from typing import Any

from digital_twin.domain.plants import (
    PLANT_TYPE_HERBS,
    PLANT_TYPE_ORNAMENTALS,
    PLANT_TYPE_SUCCULENTS,
    PLANT_TYPE_VEGETABLES,
    WATER_NEED_HIGH,
    WATER_NEED_LOW,
    WATER_NEED_MEDIUM,
)
from digital_twin.domain.pots import (
    POT_SIZE_HUGE,
    POT_SIZE_LARGE,
    POT_SIZE_MEDIUM,
    POT_SIZE_SMALL,
    RAIN_EXPOSURE_COVERED,
    RAIN_EXPOSURE_FULLY_EXPOSED,
    RAIN_EXPOSURE_PARTIALLY_EXPOSED,
    SMALL_POT_7CM,
    SMALL_POT_15CM,
    SMALL_POT_30CM,
    SUN_EXPOSURE_FULL,
    SUN_EXPOSURE_PARTIAL,
    SUN_EXPOSURE_REFLECTED_HEAT,
    SUN_EXPOSURE_SHADE,
    WIND_EXPOSURE_GUSTY,
    WIND_EXPOSURE_MODERATE,
    WIND_EXPOSURE_SHELTERED,
)
from digital_twin.infrastructure.database.schema.constants import (
    DEFAULT_POT_COUNT,
    DEFAULT_SEED,
)


def seed_reference_data(conn) -> None:
    plant_types = [
        (
            PLANT_TYPE_VEGETABLES,
            "Vegetables",
            WATER_NEED_HIGH,
            32,
            55,
            78,
            15,
            True,
            True,
            "Consistent moisture; likely candidate for second evening watering in heatwaves.",
        ),
        (
            PLANT_TYPE_HERBS,
            "Herbs",
            WATER_NEED_MEDIUM,
            28,
            50,
            74,
            15,
            True,
            True,
            "Most culinary herbs prefer morning watering and can need evening checks in hot wind.",
        ),
        (
            PLANT_TYPE_ORNAMENTALS,
            "Ornamentals",
            WATER_NEED_MEDIUM,
            24,
            35,
            72,
            15,
            False,
            False,
            "Container ornamentals usually tolerate one morning watering unless exposed.",
        ),
        (
            PLANT_TYPE_SUCCULENTS,
            "Succulents",
            WATER_NEED_LOW,
            12,
            25,
            45,
            15,
            False,
            False,
            "Drought tolerant; water less frequently and avoid prolonged wet soil.",
        ),
    ]
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

    size_profiles = [
        (POT_SIZE_HUGE, "Huge planter", None, 70, 90, 30, 0.75, 1.35),
        (POT_SIZE_LARGE, "Large pot", None, 45, 45, 24, 0.9, 1.18),
        (POT_SIZE_MEDIUM, "Medium pot", None, 30, 20, 18, 1.0, 1.0),
        (f"{POT_SIZE_SMALL}_{SMALL_POT_7CM}", "Small pot 7 cm", SMALL_POT_7CM, 7, 0.4, 4, 1.9, 0.45),
        (f"{POT_SIZE_SMALL}_{SMALL_POT_15CM}", "Small pot 15 cm", SMALL_POT_15CM, 15, 2.2, 8, 1.55, 0.62),
        (f"{POT_SIZE_SMALL}_{SMALL_POT_30CM}", "Small pot 30 cm", SMALL_POT_30CM, 30, 12, 14, 1.18, 0.88),
    ]
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
        [
            (POT_SIZE_HUGE, 0.12),
            (POT_SIZE_LARGE, 0.22),
            (POT_SIZE_MEDIUM, 0.26),
            (POT_SIZE_SMALL, 0.40),
        ],
    )
    small_subtype = None
    profile_code = size_class
    if size_class == POT_SIZE_SMALL:
        small_subtype = _weighted_choice(
            rng,
            [(SMALL_POT_7CM, 0.25), (SMALL_POT_15CM, 0.35), (SMALL_POT_30CM, 0.40)],
        )
        profile_code = f"small_{small_subtype}"

    plant_type_code = _weighted_choice(
        rng,
        [
            (PLANT_TYPE_VEGETABLES, 0.24),
            (PLANT_TYPE_HERBS, 0.24),
            (PLANT_TYPE_ORNAMENTALS, 0.42),
            (PLANT_TYPE_SUCCULENTS, 0.10),
        ],
    )
    plant_type = plant_types[plant_type_code]
    profile = profiles[profile_code]

    sun_exposure = _weighted_choice(
        rng,
        [
            (SUN_EXPOSURE_FULL, 0.34),
            (SUN_EXPOSURE_PARTIAL, 0.32),
            (SUN_EXPOSURE_REFLECTED_HEAT, 0.20),
            (SUN_EXPOSURE_SHADE, 0.14),
        ],
    )
    wind_exposure = _weighted_choice(
        rng,
        [
            (WIND_EXPOSURE_MODERATE, 0.46),
            (WIND_EXPOSURE_SHELTERED, 0.32),
            (WIND_EXPOSURE_GUSTY, 0.22),
        ],
    )
    drip_flow = _adjust_flow(profile["base_drip_flow_ml_min"], plant_type_code, sun_exposure, wind_exposure, rng)
    cycle_soak = size_class in {POT_SIZE_HUGE, POT_SIZE_LARGE} or (
        sun_exposure == SUN_EXPOSURE_REFLECTED_HEAT and wind_exposure == WIND_EXPOSURE_GUSTY
    )
    if plant_type_code == PLANT_TYPE_SUCCULENTS:
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
        "rain_exposure": _rain_exposure_for_zone(balcony_zone),
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
        "soil_profile": _soil_profile(plant_type_code),
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

def _rain_exposure_for_zone(zone: str) -> str:
    if zone == "north_shelter":
        return RAIN_EXPOSURE_COVERED
    if zone in {"west_wall", "east_corner"}:
        return RAIN_EXPOSURE_PARTIALLY_EXPOSED
    if zone in {"south_rail", "hanging_row"}:
        return RAIN_EXPOSURE_FULLY_EXPOSED
    return RAIN_EXPOSURE_PARTIALLY_EXPOSED

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
    multiplier = Decimal("1.0")
    if plant_type_code == PLANT_TYPE_VEGETABLES:
        multiplier += Decimal("0.12")
    elif plant_type_code == PLANT_TYPE_SUCCULENTS:
        multiplier -= Decimal("0.25")

    if sun_exposure == SUN_EXPOSURE_REFLECTED_HEAT:
        multiplier += Decimal("0.12")
    elif sun_exposure == SUN_EXPOSURE_SHADE:
        multiplier -= Decimal("0.08")

    if wind_exposure == WIND_EXPOSURE_GUSTY:
        multiplier += Decimal("0.10")
    elif wind_exposure == WIND_EXPOSURE_SHELTERED:
        multiplier -= Decimal("0.05")

    jitter = Decimal(str(round(rng.uniform(-0.08, 0.08), 3)))
    flow = Decimal(base_flow) * (multiplier + jitter)
    return flow.quantize(Decimal("0.01"))

def _soil_profile(plant_type_code: str) -> str:
    return {
        PLANT_TYPE_VEGETABLES: "moisture_retentive_container_mix",
        PLANT_TYPE_HERBS: "free_draining_organic_mix",
        PLANT_TYPE_ORNAMENTALS: "balanced_potting_mix",
        PLANT_TYPE_SUCCULENTS: "gritty_fast_draining_mix",
    }[plant_type_code]

def _pot_label(index: int, size_class: str, small_subtype: str | None, plant_type_code: str) -> str:
    size = f"{size_class} {small_subtype}" if small_subtype else size_class
    plant = plant_type_code.replace("_", " ")
    return f"{size.title()} {plant.title()} Pot {index:03d}"
