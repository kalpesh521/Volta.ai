"""
All domain enums for the onboarding module.
Values mirror the exact UI labels so JSON round-trips are lossless.
"""
import enum


# ── Step 1: Solar Panel ────────────────────────────────────────────────────────

class PanelType(str, enum.Enum):
    MONOCRYSTALLINE = "Monocrystalline"   # 18-22% efficient
    POLYCRYSTALLINE = "Polycrystalline"   # 15-17% efficient
    THIN_FILM       = "Thin-film"         # 10-13% efficient
    BIFACIAL        = "Bifacial"          # 20-25% efficient


class SystemType(str, enum.Enum):
    ON_GRID  = "On-grid"   # grid only, no battery  → battery step skipped
    OFF_GRID = "Off-grid"  # battery only, no grid  → grid step skipped
    HYBRID   = "Hybrid"    # both battery & grid     → all steps shown


# ── Step 2: Inverter ──────────────────────────────────────────────────────────

class InverterBrand(str, enum.Enum):
    FRONIUS    = "Fronius"
    SOLAR_EDGE = "SolarEdge"
    GROWATT    = "Growatt"
    HUAWEI     = "Huawei FusionSolar"
    OTHER      = "Other"


# ── Step 3: Battery (Off-grid / Hybrid) ──────────────────────────────────────

class BackupHours(int, enum.Enum):
    TWO   = 2
    FOUR  = 4
    EIGHT = 8


# ── Step 4: Grid (On-grid / Hybrid) ──────────────────────────────────────────

class MeterType(str, enum.Enum):
    NET_METERING   = "Net metering"
    GROSS_METERING = "Gross metering"
    NO_EXPORT      = "No export"


class TariffType(str, enum.Enum):
    FLAT_RATE   = "Flat rate"
    TIME_OF_USE = "Time-of-use"
    SLAB_BASED  = "Slab-based"


# ── Step 5: Appliances ────────────────────────────────────────────────────────

class ApplianceKey(str, enum.Enum):
    """Fixed catalog of automatable appliances. Keys match the UI catalog."""
    WASHING_MACHINE  = "wash"
    WATER_HEATER     = "heater"
    EV_CHARGER       = "ev"
    POOL_PUMP        = "pump"
    AIR_CONDITIONER  = "ac"
    REFRIGERATOR     = "fridge"


# ── Onboarding progress tracking ─────────────────────────────────────────────

class OnboardingStep(str, enum.Enum):
    """Tracks the last completed step for resume/progress display."""
    SYSTEM     = "system"
    BATTERY    = "battery"
    GRID       = "grid"
    APPLIANCES = "appliances"
    COMPLETE   = "complete"
